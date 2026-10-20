"""Discrete-event simulator for the KV-Mesh control plane.

The simulator reproduces the behaviour of the Go gateway (``gateway/router/router.go``)
and the Python worker (``worker/main.py``) so that routing behaviour can be
measured at a scale and under fault conditions that are impractical with a
live cluster.

Fidelity is maintained by mirroring the implementation's rules exactly:

* A worker is *active* when its last received heartbeat is younger than the
  TTL window.
* Active workers are always visited in worker-ID order, which is what makes
  the round-robin fallback reproducible.
* The affinity policy selects, among the active workers advertising the
  request fingerprint, the one with the smallest
  ``(queue_depth, vram_utilization, worker_id)`` tuple.
* The round-robin cursor is a single counter shared by all requests and is
  advanced only when a request misses the cache.
* A heartbeat replaces the worker's advertised state wholesale; it is delayed,
  jittered or dropped according to the fault configuration.

The simulator adds two things the live prototype does not have: a queueing
model (so that the least-loaded baseline is meaningful) and ground truth
about where each entry actually lives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .fingerprint import hash_prompt

# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

AFFINITY = "affinity"
ROUND_ROBIN = "roundrobin"
LEAST_LOADED = "leastloaded"
ORACLE = "oracle"

POLICIES = (AFFINITY, ROUND_ROBIN, LEAST_LOADED, ORACLE)

# Fixed simulated KV block size, in bytes. The live prototype uses 1 MiB
# synthetic payloads; the same figure is used here so that "bytes offloaded"
# is comparable between the simulator and the live system.
BLOCK_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Worker model
# ---------------------------------------------------------------------------


@dataclass
class SimWorker:
    """A worker process with a simulated fast tier and host tier.

    The fast tier models GPU-resident KV blocks and is bounded by ``capacity``
    slots. The host tier models the C++ byte pool. A host tier of ``0``
    entries is unbounded, which reproduces the original prototype's behaviour;
    a positive bound applies FIFO eviction, which is what makes the host tier
    a *resource* that the routing policy can run out of.
    """

    worker_id: str
    capacity: int
    host_capacity: int = 0
    instance_id: str = ""
    started_at: float = 0.0
    alive: bool = True

    # Simulated fast tier: FIFO list of fingerprints.
    fast_tier: List[int] = field(default_factory=list)
    # Simulated host tier: insertion-ordered fingerprints plus a membership set.
    host_order: List[int] = field(default_factory=list)
    host_set: Set[int] = field(default_factory=set)

    # Queueing model. The worker is modelled as a FIFO single server with a
    # constant service time; ``busy_until`` is the instant at which the
    # worker becomes free again.
    busy_until: float = 0.0
    service_time: float = 0.05

    # Counters.
    requests: int = 0
    vram_hits: int = 0
    host_hits: int = 0
    misses: int = 0
    evictions: int = 0
    host_evictions: int = 0
    bytes_offloaded: int = 0

    def queue_depth(self, now: float) -> int:
        """Number of requests assigned to this worker still in service.

        For a FIFO server with a constant service time this is the number of
        service intervals that still lie between ``now`` and ``busy_until``.
        """
        if self.busy_until <= now:
            return 0
        return int(math.ceil((self.busy_until - now) / self.service_time))

    def contains(self, fingerprint: int) -> bool:
        return fingerprint in self.fast_tier or fingerprint in self.host_set

    def _host_insert(self, fingerprint: int) -> None:
        if fingerprint in self.host_set:
            return
        self.host_order.append(fingerprint)
        self.host_set.add(fingerprint)
        if self.host_capacity > 0:
            while len(self.host_order) > self.host_capacity:
                victim = self.host_order.pop(0)
                self.host_set.discard(victim)
                self.host_evictions += 1

    def execute(self, fingerprint: int, now: float, service_time: float) -> str:
        """Serve a request and return the ground-truth cache status.

        Statuses are ``vram_hit``, ``host_hit`` or ``miss``.
        """
        if fingerprint in self.fast_tier:
            status = "vram_hit"
            self.vram_hits += 1
        elif fingerprint in self.host_set:
            status = "host_hit"
            self.host_hits += 1
            # Prefetch: the entry moves back into the fast tier.
            self.host_set.discard(fingerprint)
            try:
                self.host_order.remove(fingerprint)
            except ValueError:
                pass
        else:
            status = "miss"
            self.misses += 1

        if fingerprint not in self.fast_tier:
            if len(self.fast_tier) >= self.capacity:
                evicted = self.fast_tier.pop(0)
                self._host_insert(evicted)
                self.evictions += 1
                self.bytes_offloaded += BLOCK_BYTES
            self.fast_tier.append(fingerprint)

        self.requests += 1
        self.busy_until = max(now, self.busy_until) + service_time
        return status


# ---------------------------------------------------------------------------
# Gateway-side registry
# ---------------------------------------------------------------------------


@dataclass
class RegistryEntry:
    """The gateway's (possibly stale) view of a worker."""

    advertised_fast: Set[int] = field(default_factory=set)
    advertised_host: Set[int] = field(default_factory=set)
    last_seen: float = 0.0
    seq: int = 0
    instance_id: str = ""
    vram_util: float = 0.0
    queue_depth: int = 0


@dataclass
class Decision:
    """The outcome of one routing decision."""

    request_id: int
    time: float
    prompt_key: str
    fingerprint: int
    selected_worker: Optional[str]
    advertised_hit: bool
    true_hit: bool
    cache_status: str
    active_workers: int
    match_count: int
    policy: str
    decision_us: float
    queueing_delay_ms: float
    service_ms: float
    failed: bool
    error: str = ""


class Simulator:
    """Event-driven simulation of the mesh."""

    def __init__(
        self,
        n_workers: int,
        capacity: int,
        heartbeat_interval: float,
        ttl: float,
        policy: str,
        service_time: float = 0.05,
        host_capacity: int = 0,
        heartbeat_delay: float = 0.0,
        heartbeat_jitter: float = 0.0,
        heartbeat_drop: float = 0.0,
        rng=None,
    ):
        import random

        self.policy = policy
        self.heartbeat_interval = heartbeat_interval
        self.ttl = ttl
        self.service_time = service_time
        self.host_capacity = host_capacity
        self.heartbeat_delay = heartbeat_delay
        self.heartbeat_jitter = heartbeat_jitter
        self.heartbeat_drop = heartbeat_drop
        self.rng = rng or random.Random(0)

        self.workers: Dict[str, SimWorker] = {}
        for i in range(n_workers):
            wid = f"w{i:03d}"
            self.workers[wid] = SimWorker(
                worker_id=wid,
                capacity=capacity,
                host_capacity=host_capacity,
                instance_id=f"{wid}-i0",
                service_time=service_time,
            )

        self.registry: Dict[str, RegistryEntry] = {}
        self._rr_cursor = 0
        self._next_heartbeat: Dict[str, float] = {
            wid: 0.0 for wid in self.workers
        }
        self._pending: List[Tuple[float, int, str, int]] = []
        self._seq: Dict[str, int] = {wid: 0 for wid in self.workers}

        self.decisions: List[Decision] = []
        self.stale_affinity = 0
        self.degraded_affinity = 0
        self.routing_failures = 0
        self.heartbeats_sent = 0
        self.heartbeats_dropped = 0
        self.heartbeats_delivered = 0
        self.heartbeats_rejected = 0

    # -- heartbeat machinery ------------------------------------------------

    def _schedule_heartbeat(self, wid: str, now: float) -> None:
        """Emit a heartbeat for ``wid`` and schedule the next one."""
        worker = self.workers[wid]
        if not worker.alive:
            return
        self._seq[wid] += 1
        seq = self._seq[wid]
        delay = self.heartbeat_delay
        if self.heartbeat_jitter:
            delay += self.rng.uniform(0.0, self.heartbeat_jitter)
        drop = self.rng.random() < self.heartbeat_drop
        self.heartbeats_sent += 1
        if drop:
            self.heartbeats_dropped += 1
        else:
            delivery = now + delay
            # Stable ordering by (delivery time, sequence).
            self._pending.append((delivery, seq, wid, 1))
        self._next_heartbeat[wid] = now + self.heartbeat_interval

    def _deliver_heartbeats(self, until: float) -> None:
        """Deliver all heartbeats whose delivery time is <= ``until``."""
        remaining: List[Tuple[float, int, str, int]] = []
        self._pending.sort()
        for item in self._pending:
            if item[0] <= until:
                _, seq, wid, _ = item
                self._apply_heartbeat(wid, seq, item[0])
            else:
                remaining.append(item)
        self._pending = remaining

    def _apply_heartbeat(self, wid: str, seq: int, now: float) -> None:
        worker = self.workers[wid]
        entry = self.registry.get(wid)
        if entry is not None and entry.instance_id == worker.instance_id and seq <= entry.seq:
            self.heartbeats_rejected += 1
            return
        self.registry[wid] = RegistryEntry(
            advertised_fast=set(worker.fast_tier),
            advertised_host=set(worker.host_set),
            last_seen=now,
            seq=seq,
            instance_id=worker.instance_id,
            vram_util=len(worker.fast_tier) / float(worker.capacity) if worker.capacity else 0.0,
            queue_depth=worker.queue_depth(now),
        )
        self.heartbeats_delivered += 1

    # -- churn --------------------------------------------------------------

    def stop_worker(self, wid: str, now: float) -> None:
        """Kill a worker: it stops heartbeating and its registry entry ages out."""
        worker = self.workers[wid]
        worker.alive = False
        self._pending = [p for p in self._pending if p[2] != wid]

    def restart_worker(self, wid: str, now: float) -> None:
        """Restart a worker with a new instance id, keeping its logical id.

        The fast tier is lost (a real restart loses in-process state) but the
        registry entry for the worker is retained until it ages out or is
        replaced, which is exactly the stale-registration behaviour the
        experiment is designed to expose.
        """
        worker = self.workers[wid]
        worker.alive = True
        worker.instance_id = f"{wid}-i{self._seq[wid] + 1}"
        worker.fast_tier = []
        worker.host_order = []
        worker.host_set = set()
        self._seq[wid] = 0
        self._next_heartbeat[wid] = now + self.heartbeat_interval

    # -- routing ------------------------------------------------------------

    def _active_workers(self, now: float) -> List[SimWorker]:
        """The workers the gateway believes are live.

        The gateway's knowledge comes from its registry, so a worker that has
        been killed remains eligible until its last heartbeat ages out of the
        TTL window. This is deliberately pessimistic: it is what makes a
        churn experiment produce the request failures that a real gateway
        would produce while a dead worker is still registered.
        """
        active = [
            self.workers[wid]
            for wid, entry in self.registry.items()
            if now - entry.last_seen < self.ttl
        ]
        active.sort(key=lambda w: w.worker_id)
        return active

    def _advertises(self, wid: str, fingerprint: int) -> bool:
        entry = self.registry.get(wid)
        return entry is not None and fingerprint in entry.advertised_fast

    def _choose(self, prompt: str, fingerprint: int, active: List[SimWorker], now: float):
        """Return (worker, advertised_hit, match_count)."""
        if self.policy == ROUND_ROBIN:
            idx = self._rr_cursor % len(active)
            self._rr_cursor += 1
            return active[idx], False, 0

        if self.policy == LEAST_LOADED:
            best = min(active, key=lambda w: (w.queue_depth(now), self._util(w), w.worker_id))
            return best, False, 0

        if self.policy == ORACLE:
            holders = [w for w in active if w.alive and w.contains(fingerprint)]
            if holders:
                best = min(holders, key=lambda w: (w.queue_depth(now), self._util(w), w.worker_id))
                return best, False, len(holders)
            idx = self._rr_cursor % len(active)
            self._rr_cursor += 1
            return active[idx], False, 0

        # AFFINITY
        best = None
        matches = 0
        for w in active:
            if self._advertises(w.worker_id, fingerprint):
                matches += 1
                key = (w.queue_depth(now), self._util(w), w.worker_id)
                if best is None or key < best[0]:
                    best = (key, w)
        if best is not None:
            return best[1], True, matches
        idx = self._rr_cursor % len(active)
        self._rr_cursor += 1
        return active[idx], False, 0

    def _util(self, worker: SimWorker) -> float:
        entry = self.registry.get(worker.worker_id)
        return entry.vram_util if entry else 0.0

    # -- main loop ----------------------------------------------------------

    def run(self, arrivals: Sequence[Tuple[float, str]], churn_events=None) -> List[Decision]:
        """Replay ``arrivals`` (a sequence of ``(time, prompt_key)`` pairs).

        ``churn_events`` is an optional sequence of ``(time, action, worker_id)``
        tuples, where ``action`` is ``"stop"`` or ``"restart"``. Events are
        applied in time order, interleaved with the request stream.
        """
        import time as _time

        churn_events = sorted(churn_events or [], key=lambda e: e[0])
        churn_idx = 0

        for now, key in arrivals:
            # Apply any churn events that came due before this request.
            while churn_idx < len(churn_events) and churn_events[churn_idx][0] <= now:
                _, action, wid = churn_events[churn_idx]
                if action == "stop":
                    self.stop_worker(wid, now)
                else:
                    self.restart_worker(wid, now)
                churn_idx += 1

            # Emit every heartbeat that came due before this request. A worker
            # that was silent for a long gap still emits the heartbeats it
            # "missed", which is how the gateway learns that the worker is
            # alive again.
            for wid in list(self._next_heartbeat):
                while self.workers[wid].alive and self._next_heartbeat[wid] <= now:
                    self._schedule_heartbeat(wid, self._next_heartbeat[wid])

            # Deliver the heartbeats that are due. Delivery happens after
            # emission so that a zero-latency heartbeat is visible to the
            # request that triggered it.
            self._deliver_heartbeats(now)

            prompt = f"SYSTEM CONTEXT v1 :: session={key} :: request payload follows <<{key}>>"
            fingerprint = hash_prompt(prompt)

            t0 = _time.perf_counter()
            active = self._active_workers(now)
            if not active:
                self.routing_failures += 1
                self.decisions.append(
                    Decision(
                        request_id=len(self.decisions),
                        time=now,
                        prompt_key=key,
                        fingerprint=fingerprint,
                        selected_worker=None,
                        advertised_hit=False,
                        true_hit=False,
                        cache_status="routing_failure",
                        active_workers=0,
                        match_count=0,
                        policy=self.policy,
                        decision_us=(_time.perf_counter() - t0) * 1e6,
                        queueing_delay_ms=0.0,
                        service_ms=0.0,
                        failed=True,
                        error="no active workers",
                    )
                )
                continue

            worker, advertised_hit, match_count = self._choose(prompt, fingerprint, active, now)
            decision_us = (_time.perf_counter() - t0) * 1e6

            # The gateway does not know that the worker has died until its
            # registration ages out, so a request routed to a stopped worker
            # fails, exactly as it would over a real transport.
            if not worker.alive:
                self.routing_failures += 1
                self.decisions.append(
                    Decision(
                        request_id=len(self.decisions),
                        time=now,
                        prompt_key=key,
                        fingerprint=fingerprint,
                        selected_worker=worker.worker_id,
                        advertised_hit=advertised_hit,
                        true_hit=False,
                        cache_status="unreachable",
                        active_workers=len(active),
                        match_count=match_count,
                        policy=self.policy,
                        decision_us=decision_us,
                        queueing_delay_ms=0.0,
                        service_ms=0.0,
                        failed=True,
                        error="worker unreachable",
                    )
                )
                continue

            # The worker may still be busy with earlier requests: the request
            # waits until the worker's current backlog has drained.
            busy = max(worker.busy_until, now)
            queueing_delay = busy - now
            status = worker.execute(fingerprint, busy, self.service_time)

            true_hit = status in ("vram_hit", "host_hit")
            if advertised_hit and not true_hit:
                # Strict staleness: the advertised entry was gone from both tiers.
                self.stale_affinity += 1
            elif advertised_hit and status == "host_hit":
                # Degraded locality: the entry survived but was evicted from the
                # fast tier, so the request pays a host-to-device transfer that
                # the router's advertisement implied it would not.
                self.degraded_affinity += 1

            self.decisions.append(
                Decision(
                    request_id=len(self.decisions),
                    time=now,
                    prompt_key=key,
                    fingerprint=fingerprint,
                    selected_worker=worker.worker_id,
                    advertised_hit=advertised_hit,
                    true_hit=true_hit,
                    cache_status=status,
                    active_workers=len(active),
                    match_count=match_count,
                    policy=self.policy,
                    decision_us=decision_us,
                    queueing_delay_ms=queueing_delay * 1000.0,
                    service_ms=self.service_time * 1000.0,
                    failed=False,
                )
            )

        # Apply any churn events that occur after the last request.
        while churn_idx < len(churn_events):
            _, action, wid = churn_events[churn_idx]
            if action == "stop":
                self.stop_worker(wid, churn_events[churn_idx][0])
            else:
                self.restart_worker(wid, churn_events[churn_idx][0])
            churn_idx += 1

        return self.decisions

    # -- reporting ----------------------------------------------------------

    def summary(self) -> Dict[str, float]:
        total = len(self.decisions)
        served = [d for d in self.decisions if not d.failed]
        advertised = sum(1 for d in served if d.advertised_hit)
        true_hits = sum(1 for d in served if d.true_hit)
        vram_hits = sum(1 for d in served if d.cache_status == "vram_hit")
        host_hits = sum(1 for d in served if d.cache_status == "host_hit")
        counts: Dict[str, int] = {}
        for d in served:
            counts[d.selected_worker] = counts.get(d.selected_worker, 0) + 1
        cv = _coefficient_of_variation(list(counts.values()))

        decision_times = [d.decision_us for d in served]
        queue_delays = sorted(d.queueing_delay_ms for d in served)
        active = [d.active_workers for d in served]

        return {
            "requests_total": total,
            "requests_served": len(served),
            "routing_failures": self.routing_failures,
            "advertised_hits": advertised,
            "true_hits": true_hits,
            "stale_affinity": self.stale_affinity,
            "degraded_affinity": self.degraded_affinity,
            "vram_hits": vram_hits,
            "host_hits": host_hits,
            "heartbeats_sent": self.heartbeats_sent,
            "heartbeats_delivered": self.heartbeats_delivered,
            "heartbeats_dropped": self.heartbeats_dropped,
            "heartbeats_rejected": self.heartbeats_rejected,
            "bytes_offloaded": sum(w.bytes_offloaded for w in self.workers.values()),
            "host_evictions": sum(w.host_evictions for w in self.workers.values()),
            "worker_imbalance_cv": cv,
            "mean_decision_us": (sum(decision_times) / len(decision_times)) if decision_times else float("nan"),
            "queueing_delay_p95_ms": _quantile(queue_delays, 0.95),
            "mean_active_workers": (sum(active) / len(active)) if active else float("nan"),
        }


def _coefficient_of_variation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile of an already-sorted sequence."""
    if not sorted_values:
        return float("nan")
    idx = int(q * len(sorted_values))
    if idx >= len(sorted_values):
        idx = len(sorted_values) - 1
    return float(sorted_values[idx])
