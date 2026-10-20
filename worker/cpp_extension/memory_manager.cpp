#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <algorithm>
#include <cstddef>
#include <deque>
#include <unordered_map>
#include <vector>
#include <string>
#include <iostream>

namespace py = pybind11;

// KVMemoryManager models the host-side (Tier 2) KV pool.
//
// IMPORTANT (manuscript §3.4, §4): this class does NOT perform any GPU<->CPU
// transfer. It stores byte payloads in host memory and hands them back. The
// transfer it emulates is a plain byte copy, so timings measured against this
// class are *host-memory copy* timings and must not be reported as PCIe or
// GPU-transfer latencies.
class KVMemoryManager {
private:
    // Maps a prefix hash to a raw byte vector simulating CPU RAM storage
    std::unordered_map<uint64_t, std::vector<char>> cpu_memory_pool;

    // Insertion order of the pool, used for FIFO eviction when the pool is
    // capacity-bounded. An unbounded pool (capacity == 0) never evicts.
    std::deque<uint64_t> insertion_order;

    // Maximum number of entries retained. 0 means unbounded.
    size_t capacity;

    // Counters exposed for the experimental instrumentation.
    uint64_t offload_count = 0;
    uint64_t prefetch_count = 0;
    uint64_t eviction_count = 0;
    uint64_t bytes_offloaded = 0;
    uint64_t bytes_prefetched = 0;

    // Console logging is enabled by default to preserve the prototype's
    // observable behaviour, but it is disabled during microbenchmarks so that
    // the measurement reflects the memory path rather than stream I/O.
    bool verbose = true;

public:
    // capacity == 0 retains every entry (the original prototype behaviour).
    explicit KVMemoryManager(size_t capacity = 0) : capacity(capacity) {
        if (verbose) {
            std::cout << "[C++ Backend] Heterogeneous Memory Pool Initialized (capacity="
                      << (capacity == 0 ? std::string("unbounded") : std::to_string(capacity))
                      << ")." << std::endl;
        }
    }

    // Stores a payload in the host pool, simulating a GPU->CPU offload.
    void offload_cache(uint64_t prefix_hash, const py::bytes& data) {
        std::string str_data = static_cast<std::string>(data);
        if (cpu_memory_pool.find(prefix_hash) == cpu_memory_pool.end()) {
            insertion_order.push_back(prefix_hash);
        }
        cpu_memory_pool[prefix_hash] = std::vector<char>(str_data.begin(), str_data.end());
        offload_count++;
        bytes_offloaded += str_data.size();
        if (verbose) {
            std::cout << "[C++ Backend] Offloaded " << str_data.size() << " bytes for hash " << prefix_hash << " to CPU RAM." << std::endl;
        }

        enforce_capacity();
    }

    // Retrieves a payload from the host pool, simulating a CPU->GPU prefetch.
    // The entry is removed from the pool, mirroring the prototype's semantics
    // in which a prefetched block is considered to have moved to the GPU.
    py::bytes prefetch_cache(uint64_t prefix_hash) {
        auto it = cpu_memory_pool.find(prefix_hash);
        if (it != cpu_memory_pool.end()) {
            std::string str_data(it->second.begin(), it->second.end());
            prefetch_count++;
            bytes_prefetched += str_data.size();
            if (verbose) {
                std::cout << "[C++ Backend] Pre-fetched hash " << prefix_hash << " back to GPU." << std::endl;
            }

            // In a real system, we'd free the CPU RAM here once it's safely on the GPU.
            cpu_memory_pool.erase(it);
            for (auto q = insertion_order.begin(); q != insertion_order.end(); ++q) {
                if (*q == prefix_hash) {
                    insertion_order.erase(q);
                    break;
                }
            }

            return py::bytes(str_data);
        }
        return py::bytes("");
    }

    bool is_offloaded(uint64_t prefix_hash) {
        return cpu_memory_pool.find(prefix_hash) != cpu_memory_pool.end();
    }

    // Returns the fingerprints currently resident in the host tier. The worker
    // uses this to advertise the host tier to the gateway, which allows the
    // experimental harness to reason about tier residency and not only about
    // fast-tier residency.
    std::vector<uint64_t> get_offloaded_hashes() {
        std::vector<uint64_t> keys;
        keys.reserve(cpu_memory_pool.size());
        for (const auto& entry : cpu_memory_pool) {
            keys.push_back(entry.first);
        }
        std::sort(keys.begin(), keys.end());
        return keys;
    }

    // Total number of bytes currently held in the host pool.
    size_t pool_bytes() {
        size_t total = 0;
        for (const auto& entry : cpu_memory_pool) {
            total += entry.second.size();
        }
        return total;
    }

    int get_pool_size() {
        return static_cast<int>(cpu_memory_pool.size());
    }

    // Evicts the oldest entries until the pool fits within its capacity.
    void enforce_capacity() {
        if (capacity == 0) {
            return;
        }
        while (cpu_memory_pool.size() > capacity && !insertion_order.empty()) {
            uint64_t victim = insertion_order.front();
            insertion_order.pop_front();
            auto it = cpu_memory_pool.find(victim);
            if (it != cpu_memory_pool.end()) {
                bytes_offloaded -= it->second.size();
                cpu_memory_pool.erase(it);
                eviction_count++;
            }
        }
    }

    void set_verbose(bool v) { verbose = v; }
    bool get_verbose() const { return verbose; }

    py::dict stats() {
        py::dict d;
        d["entries"] = static_cast<uint64_t>(cpu_memory_pool.size());
        d["capacity"] = static_cast<uint64_t>(capacity);
        d["bytes"] = static_cast<uint64_t>(pool_bytes());
        d["offloads"] = offload_count;
        d["prefetches"] = prefetch_count;
        d["evictions"] = eviction_count;
        d["bytes_offloaded"] = bytes_offloaded;
        d["bytes_prefetched"] = bytes_prefetched;
        return d;
    }
};

// Bindings to expose the C++ class to Python
PYBIND11_MODULE(kv_cpp, m) {
    m.doc() = "C++ Heterogeneous Memory Manager for KV-Mesh";

    py::class_<KVMemoryManager>(m, "KVMemoryManager")
        .def(py::init<size_t>(), py::arg("capacity") = 0)
        .def("offload_cache", &KVMemoryManager::offload_cache)
        .def("prefetch_cache", &KVMemoryManager::prefetch_cache)
        .def("is_offloaded", &KVMemoryManager::is_offloaded)
        .def("get_offloaded_hashes", &KVMemoryManager::get_offloaded_hashes)
        .def("pool_bytes", &KVMemoryManager::pool_bytes)
        .def("get_pool_size", &KVMemoryManager::get_pool_size)
        .def("enforce_capacity", &KVMemoryManager::enforce_capacity)
        .def("set_verbose", &KVMemoryManager::set_verbose)
        .def("get_verbose", &KVMemoryManager::get_verbose)
        .def("stats", &KVMemoryManager::stats);
}
