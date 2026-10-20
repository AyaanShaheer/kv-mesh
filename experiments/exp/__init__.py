"""Experiment harness for the KV-Mesh manuscript.

The package is organised around three *arms*:

``exp.sim``
    A discrete-event simulator of the mesh control plane. It is a faithful
    Python port of the Go gateway's routing logic (see ``exp.sim.router``) and
    is used for the large factorial sweep.

``exp.live``
    A harness that drives the *real* Go gateway and *real* Python workers over
    gRPC, used to validate the simulator and to measure routing overhead in the
    actual implementation.

``exp.bench``
    Microbenchmarks for the routing decision (Go) and for the C++ host-memory
    byte pool (Python/pybind11).
"""

__all__ = ["fingerprint", "stats", "workload", "sim", "live", "bench", "analyze"]
