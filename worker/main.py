"""KV-Mesh Python worker node.

Serves the ``WorkerNode`` gRPC service, models a two-tier cache lifecycle
(fast tier + host-memory byte pool) and reports its state to the Go gateway
through periodic heartbeats.

IMPORTANT (manuscript §4, "Implementation Status and Assumptions"):
the generation path below is *simulated*. The worker emits a fixed synthetic
token sequence, and the "KV cache" is a list of fingerprints with a synthetic
byte payload standing in for a GPU-resident tensor. No model is loaded, no CUDA
memory is allocated and no PCIe transfer is performed. The code exists to
exercise and measure the *control plane* (routing, liveness, tiered-state
lifecycle), not to demonstrate inference acceleration.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import signal
import threading
import time
import uuid

import grpc

import kvmesh_pb2
import kvmesh_pb2_grpc
import kv_cpp  # <--- the native C++ host-memory pool


def hash_prompt(prompt_text):
    """64-bit fingerprint of the raw prompt string (truncated MD5).

    Exact-string matching only: see manuscript §3.3 for why this is not a
    production-grade engine cache key.
    """
    hex_str = hashlib.md5(prompt_text.encode("utf-8")).hexdigest()[:16]
    return int(hex_str, 16)


class DecisionLog:
    """Append-only JSON Lines log of per-request ground truth."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._fh = None
        if path:
            self._fh = open(path, "a", buffering=1)

    def write(self, record):
        if self._fh is None:
            return
        with self._lock:
            self._fh.write(json.dumps(record) + "\n")

    def close(self):
        if self._fh is not None:
            with self._lock:
                self._fh.close()
            self._fh = None


class WorkerNode(kvmesh_pb2_grpc.WorkerNodeServicer):
    def __init__(
        self,
        worker_id,
        gateway_address,
        local_port,
        capacity=2,
        heartbeat_interval=3.0,
        decision_log_path=None,
        token_delay=0.1,
        advertise_host_tier=True,
        host_capacity=0,
        quiet=False,
    ):
        self.worker_id = worker_id
        self.gateway_address = gateway_address
        self.local_port = local_port
        self.local_address = f"127.0.0.1:{self.local_port}"
        self.instance_id = uuid.uuid4().hex

        # VRAM Capacity Simulation: a small number of KV cache blocks. This is
        # a *simulated* fast tier, not GPU memory.
        self.vram_cache = []  # FIFO (First-In, First-Out) eviction
        self.vram_capacity = capacity

        self.quiet = quiet

        # Initialize C++ Memory Manager. A host_capacity of 0 means the pool
        # is unbounded, which is the original prototype's behaviour.
        self.mem_manager = kv_cpp.KVMemoryManager(host_capacity)
        if quiet:
            self.mem_manager.set_verbose(False)

        self.heartbeat_interval = heartbeat_interval
        self.advertise_host_tier = advertise_host_tier
        self.token_delay = token_delay
        self.decision_log = DecisionLog(decision_log_path)

        self._lock = threading.Lock()      # guards vram_cache
        self._inflight = 0                 # simulated queue depth
        self._heartbeat_seq = 0

        self.running = True
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    # ------------------------------------------------------------------
    # Tiered memory lifecycle
    # ------------------------------------------------------------------
    def _manage_memory(self, prompt_hash):
        """Ensure prompt_hash is in the fast tier, swapping with the host tier.

        Returns the ground-truth cache status observed at execution time:
        "vram_hit", "host_hit" or "miss".
        """
        with self._lock:
            if prompt_hash in self.vram_cache:
                if not self.quiet:
                    print(f"[{self.worker_id}] VRAM HIT for hash {prompt_hash}")
                return "vram_hit"

            if self.mem_manager.is_offloaded(prompt_hash):
                if not self.quiet:
                    print(f"[{self.worker_id}] VRAM MISS but CPU HIT. Pre-fetching over PCIe...")
                self.mem_manager.prefetch_cache(prompt_hash)
                status = "host_hit"
            else:
                if not self.quiet:
                    print(f"[{self.worker_id}] Total MISS. Computing new KV cache...")
                status = "miss"

            # If VRAM is full, offload the oldest block to CPU
            if len(self.vram_cache) >= self.vram_capacity:
                evicted_hash = self.vram_cache.pop(0)
                if not self.quiet:
                    print(f"[{self.worker_id}] VRAM FULL. Evicting hash {evicted_hash} to CPU.")

                # Simulate a 1MB tensor block being sent to C++
                fake_tensor_data = b"0" * (1024 * 1024)
                self.mem_manager.offload_cache(evicted_hash, fake_tensor_data)

            self.vram_cache.append(prompt_hash)
            return status

    # ------------------------------------------------------------------
    # Request path
    # ------------------------------------------------------------------
    def GenerateStream(self, request, context):
        if not self.quiet:
            print(f"\n--- [{self.worker_id}] New Request: '{request.prompt}' ---")
        prompt_hash = hash_prompt(request.prompt)
        started = time.time()

        with self._lock:
            self._inflight += 1
        try:
            cache_status = self._manage_memory(prompt_hash)
            true_hit = cache_status in ("vram_hit", "host_hit")

            words = ["This", " is", " a", " response", " from", f" worker-{self.worker_id}."]
            for i, word in enumerate(words):
                if not context.is_active():
                    break
                time.sleep(self.token_delay)
                yield kvmesh_pb2.GenerateResponse(
                    text=word,
                    is_finished=(i == len(words) - 1),
                    worker_id=self.worker_id,
                    cache_status=cache_status,
                )
        finally:
            with self._lock:
                self._inflight -= 1

        self.decision_log.write(
            {
                "ts": time.time(),
                "request_id": request.request_id,
                "worker_id": self.worker_id,
                "instance_id": self.instance_id,
                "prompt_hash": prompt_hash,
                "cache_status": cache_status,
                "true_hit": true_hit,
                "latency_ms": (time.time() - started) * 1000.0,
            }
        )
        if not self.quiet:
            print(f"[{self.worker_id}] Finished generation.\n")

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    def _heartbeat_loop(self):
        channel = grpc.insecure_channel(self.gateway_address)
        stub = kvmesh_pb2_grpc.GatewayNodeStub(channel)

        while self.running:
            try:
                with self._lock:
                    vram_util = len(self.vram_cache) / float(self.vram_capacity)
                    cached = list(self.vram_cache)
                    queue_depth = self._inflight
                self._heartbeat_seq += 1

                offloaded = []
                if self.advertise_host_tier:
                    try:
                        offloaded = list(self.mem_manager.get_offloaded_hashes())
                    except AttributeError:
                        offloaded = []

                req = kvmesh_pb2.HeartbeatRequest(
                    worker_id=self.worker_id,
                    grpc_address=self.local_address,
                    vram_utilization=vram_util,
                    # We broadcast what is actively in VRAM to the gateway
                    cached_prefixes=cached,
                    heartbeat_seq=self._heartbeat_seq,
                    instance_id=self.instance_id,
                    offloaded_prefixes=offloaded,
                    queue_depth=queue_depth,
                    cache_bytes=len(cached) * (1024 * 1024),
                )
                ack = stub.SendHeartbeat(req)
                if not ack.accepted:
                    print(f"[{self.worker_id}] Heartbeat rejected as stale (seq={self._heartbeat_seq}).")
            except grpc.RpcError:
                pass
            time.sleep(self.heartbeat_interval)

    def stop(self):
        self.running = False
        self.decision_log.close()


def serve(args):
    worker_id = args.worker_id or str(uuid.uuid4())[:8]
    gateway_address = args.gateway
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=10))
    node = WorkerNode(
        worker_id,
        gateway_address,
        args.port,
        capacity=args.capacity,
        heartbeat_interval=args.heartbeat_interval,
        decision_log_path=args.decision_log,
        token_delay=args.token_delay,
        host_capacity=args.host_capacity,
        quiet=args.quiet,
    )
    kvmesh_pb2_grpc.add_WorkerNodeServicer_to_server(node, server)
    server.add_insecure_port(f"[::]:{args.port}")
    server.start()
    print(f"Worker {worker_id} (instance {node.instance_id[:8]}) started on port {args.port}.")
    print(f"  fast-tier slots={args.capacity} heartbeat={args.heartbeat_interval}s")

    def shutdown(signum, _frame):
        print(f"\n[{worker_id}] Signal {signum} received, shutting down.")
        node.stop()
        server.stop(1)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    server.wait_for_termination()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50052)
    parser.add_argument("--gateway", default="127.0.0.1:50051")
    parser.add_argument("--worker-id", default=None, help="stable logical worker id (default: random)")
    parser.add_argument("--capacity", type=int, default=2, help="simulated fast-tier slots")
    parser.add_argument("--heartbeat-interval", type=float, default=3.0, help="seconds between heartbeats")
    parser.add_argument("--decision-log", default=None, help="JSON Lines path for per-request ground truth")
    parser.add_argument("--token-delay", type=float, default=0.1, help="simulated per-token delay in seconds")
    parser.add_argument(
        "--host-capacity",
        type=int,
        default=0,
        help="capacity of the host-tier byte pool in entries (0 = unbounded)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-request console output (used by the experiment harness)",
    )
    args = parser.parse_args()
    serve(args)
