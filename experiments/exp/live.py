"""Live-system experiment harness.

This module drives the *real* implementation: the Go gateway and the Python
workers, talking gRPC over loopback. It exists for two reasons.

1. It validates the simulator. The same trace is replayed through the live
   cluster and through the simulator, and the routing decisions are compared.
   Agreement between the two is the evidence that the simulator is a faithful
   model of the control plane.
2. It measures the routing overhead of the real gateway (the decision latency
   that the gateway itself records), which the simulator cannot measure
   because it is written in a different language.

The workers in this harness are the prototype's simulated workers: they
synthesise their responses and do not run a model. The harness therefore
measures *control-plane* behaviour, not inference acceleration.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_PY = os.path.join(REPO_ROOT, ".venv", "bin", "python")
VENV_PY_WIN = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")


def _python() -> str:
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


def free_port() -> int:
    """Return a currently free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class LiveConfig:
    """Configuration of a live-system run."""

    n_workers: int = 4
    policy: str = "affinity"
    ttl: float = 10.0
    heartbeat_interval: float = 1.0
    capacity: int = 8
    host_capacity: int = 64
    token_delay: float = 0.005
    n_requests: int = 2000
    request_rate: float = 200.0
    workload_kind: str = "zipf"
    n_keys: int = 256
    zipf_s: float = 1.1
    seed: int = 0
    out_dir: str = ""
    gateway_log: str = ""
    metrics_port: int = 0

    def key(self) -> str:
        return (
            f"live-{self.policy}-w{self.n_workers}-c{self.capacity}-"
            f"hb{self.heartbeat_interval:g}-ttl{self.ttl:g}-{self.workload_kind}-seed{self.seed}"
        )


class LiveCluster:
    """Starts and stops a live gateway plus a set of workers."""

    def __init__(self, config: LiveConfig):
        self.config = config
        self.gateway_proc: Optional[subprocess.Popen] = None
        self.worker_procs: List[subprocess.Popen] = []
        self.worker_ports: List[int] = []
        self.gateway_port = free_port()
        self.metrics_port = config.metrics_port or free_port()
        # Raw process output is written to a scratch directory: it is verbose
        # and is not an artifact of the experiment.
        self.scratch_dir = os.path.join(tempfile.gettempdir(), "kvmesh-live", config.key())
        os.makedirs(self.scratch_dir, exist_ok=True)
        # The decision logs are the experiment's raw artifacts and are kept.
        self.out_dir = config.out_dir or os.path.join("/tmp/opencode", "live")
        os.makedirs(self.out_dir, exist_ok=True)
        self.gateway_decision_log = os.path.join(self.out_dir, f"gateway-{config.key()}.jsonl")
        self.gateway_log = os.path.join(self.scratch_dir, f"gateway-{config.key()}.log")
        self.worker_logs: List[str] = []

    # -- lifecycle ---------------------------------------------------------

    def start_gateway(self) -> None:
        binary = os.path.join(REPO_ROOT, "gateway", "bin", "kvmesh-gateway")
        cmd = [
            binary,
            "-port", f":{self.gateway_port}",
            "-metrics-port", f":{self.metrics_port}",
            "-policy", self.config.policy,
            "-ttl", f"{self.config.ttl:g}s",
            "-decision-log", self.gateway_decision_log,
        ]
        if not os.path.exists(binary):
            raise RuntimeError(f"gateway binary not found at {binary}; run 'make gateway' first")
        self.gateway_proc = subprocess.Popen(
            cmd, stdout=open(self.gateway_log, "w"), stderr=subprocess.STDOUT
        )
        self._wait_for_port(self.gateway_port, timeout=30)

    def start_workers(self) -> None:
        for i in range(self.config.n_workers):
            port = free_port()
            self.worker_ports.append(port)
            log_path = os.path.join(self.scratch_dir, f"worker-{self.config.key()}-{i}.log")
            self.worker_logs.append(log_path)
            cmd = [
                _python(),
                os.path.join(REPO_ROOT, "worker", "main.py"),
                "--port", str(port),
                "--gateway", f"127.0.0.1:{self.gateway_port}",
                "--worker-id", f"w{i:03d}",
                "--capacity", str(self.config.capacity),
                "--host-capacity", str(self.config.host_capacity),
                "--heartbeat-interval", str(self.config.heartbeat_interval),
                "--token-delay", str(self.config.token_delay),
                "--decision-log", os.path.join(self.out_dir, f"worker-{self.config.key()}-{i}.jsonl"),
                "--quiet",
            ]
            env = dict(os.environ)
            env["PYTHONPATH"] = os.path.join(REPO_ROOT, "worker")
            self.worker_procs.append(
                subprocess.Popen(cmd, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, env=env)
            )
            self._wait_for_port(port, timeout=30)

    def stop(self) -> None:
        for proc in self.worker_procs:
            try:
                proc.send_signal(signal.SIGTERM)
            except Exception:
                proc.kill()
        for proc in self.worker_procs:
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if self.gateway_proc is not None:
            try:
                self.gateway_proc.send_signal(signal.SIGTERM)
                self.gateway_proc.wait(timeout=10)
            except Exception:
                self.gateway_proc.kill()

    @staticmethod
    def _wait_for_port(port: int, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError(f"port {port} did not open within {timeout}s")

    # -- measurement --------------------------------------------------------

    def metrics(self) -> Dict:
        import urllib.request

        url = f"http://127.0.0.1:{self.metrics_port}/metrics"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def gateway_decisions(self) -> List[Dict]:
        return _read_jsonl(self.gateway_decision_log)

    def worker_decisions(self) -> List[Dict]:
        records: List[Dict] = []
        for i in range(self.config.n_workers):
            path = os.path.join(self.out_dir, f"worker-{self.config.key()}-{i}.jsonl")
            records.extend(_read_jsonl(path))
        return records


def _read_jsonl(path: str) -> List[Dict]:
    """Read a JSON Lines file, transparently handling a gzip-compressed one."""
    if not os.path.exists(path):
        return []
    out = []
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Request driver
# ---------------------------------------------------------------------------


def drive_requests(
    gateway_port: int,
    n_requests: int,
    rate: float,
    prompt_for_key,
    timeout: float = 30.0,
) -> Dict:
    """Send ``n_requests`` streaming requests to the gateway.

    Returns a summary with the observed success/failure counts and the
    client-side latency distribution.
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "worker"))
    import grpc

    import kvmesh_pb2
    import kvmesh_pb2_grpc

    channel = grpc.insecure_channel(f"127.0.0.1:{gateway_port}")
    stub = kvmesh_pb2_grpc.WorkerNodeStub(channel)

    latencies: List[float] = []
    failures = 0
    advertised_hits = 0
    worker_ids: Dict[str, int] = {}
    import random

    rng = random.Random(0)
    interarrival = 1.0 / rate

    t_start = time.perf_counter()
    for i in range(n_requests):
        target = t_start + i * interarrival
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)

        key = prompt_for_key(i)
        req = kvmesh_pb2.GenerateRequest(
            prompt=key, max_tokens=8, temperature=0.0, request_id=f"r{i:06d}"
        )
        t0 = time.perf_counter()
        saw_advertised = False
        try:
            # The whole stream must be consumed. Breaking out of the iteration
            # early would cancel the server-side generation, which both
            # truncates the worker's ground-truth record and makes the
            # gateway log a spurious transport error.
            for resp in stub.GenerateStream(req, timeout=timeout):
                if resp.worker_id:
                    worker_ids[resp.worker_id] = worker_ids.get(resp.worker_id, 0) + 1
                if resp.advertised_hit and not saw_advertised:
                    saw_advertised = True
        except grpc.RpcError:
            failures += 1
        if saw_advertised:
            advertised_hits += 1
        latencies.append((time.perf_counter() - t0) * 1000.0)

    return {
        "requests_sent": n_requests,
        "failures": failures,
        "advertised_hits_reported": advertised_hits,
        "client_latency_ms_mean": (sum(latencies) / len(latencies)) if latencies else float("nan"),
        "client_latency_ms_p95": sorted(latencies)[int(0.95 * len(latencies))] if latencies else float("nan"),
        "worker_request_counts": worker_ids,
        "duration_s": time.perf_counter() - t_start,
    }
