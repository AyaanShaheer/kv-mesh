#include <pybind11/pybind11.h>
#include <unordered_map>
#include <vector>
#include <string>
#include <iostream>

namespace py = pybind11;

class KVMemoryManager {
private:
    // Maps a prefix hash to a raw byte vector simulating CPU RAM storage
    std::unordered_map<uint64_t, std::vector<char>> cpu_memory_pool;

public:
    KVMemoryManager() {
        std::cout << "[C++ Backend] Heterogeneous Memory Pool Initialized." << std::endl;
    }

    // Simulates a PCIe transfer from GPU VRAM to Host CPU RAM
    void offload_cache(uint64_t prefix_hash, const py::bytes& data) {
        std::string str_data = static_cast<std::string>(data);
        cpu_memory_pool[prefix_hash] = std::vector<char>(str_data.begin(), str_data.end());
        std::cout << "[C++ Backend] Offloaded " << str_data.size() << " bytes for hash " << prefix_hash << " to CPU RAM." << std::endl;
    }

    // Simulates a PCIe transfer from Host CPU RAM back to GPU VRAM
    py::bytes prefetch_cache(uint64_t prefix_hash) {
        if (cpu_memory_pool.find(prefix_hash) != cpu_memory_pool.end()) {
            std::string str_data(cpu_memory_pool[prefix_hash].begin(), cpu_memory_pool[prefix_hash].end());
            std::cout << "[C++ Backend] Pre-fetched hash " << prefix_hash << " back to GPU." << std::endl;
            
            // In a real system, we'd free the CPU RAM here once it's safely on the GPU.
            cpu_memory_pool.erase(prefix_hash); 
            
            return py::bytes(str_data);
        }
        return py::bytes("");
    }

    bool is_offloaded(uint64_t prefix_hash) {
        return cpu_memory_pool.find(prefix_hash) != cpu_memory_pool.end();
    }

    int get_pool_size() {
        return cpu_memory_pool.size();
    }
};

// Bindings to expose the C++ class to Python
PYBIND11_MODULE(kv_cpp, m) {
    m.doc() = "C++ Heterogeneous Memory Manager for KV-Mesh";
    
    py::class_<KVMemoryManager>(m, "KVMemoryManager")
        .def(py::init<>())
        .def("offload_cache", &KVMemoryManager::offload_cache)
        .def("prefetch_cache", &KVMemoryManager::prefetch_cache)
        .def("is_offloaded", &KVMemoryManager::is_offloaded)
        .def("get_pool_size", &KVMemoryManager::get_pool_size);
}
