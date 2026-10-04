# Contributing to KV-Mesh

Thank you for your interest in contributing to **KV-Mesh**! 🚀

KV-Mesh is a distributed LLM inference mesh designed for prefix-cache-aware request routing and heterogeneous tiered KV-cache memory offloading across GPU VRAM and Host CPU RAM.

Contributions of all kinds are welcome: feature additions, performance optimizations, bug fixes, documentation improvements, and architectural extensions.

---

## 🧭 Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free environment for everyone. Please treat all contributors with respect and professionalism.

---

## 🛠️ Tech Stack Overview

KV-Mesh spans a polyglot architecture:
- **Gateway & Router**: Written in **Go 1.22+** for ultra-low latency, concurrent connection handling, and gRPC stream proxying.
- **Worker Servicer**: Written in **Python 3.11+** integrating gRPC, simulated inference engines, and memory lifecycle loops.
- **Heterogeneous Memory Manager**: Written in **C++ (C++17/20)** via **pybind11** for native CPU RAM tensor pool management and PCIe offloading.
- **Interface Definition**: Protocol Buffers (**Protobuf v3**) and **gRPC**.

---

## 🚀 Setting Up Your Development Environment

### 1. Prerequisites
- **Git**
- **Go 1.22+**
- **Python 3.10+** (Python 3.11 or 3.12 recommended)
- **C++ Compiler** with C++17/20 support (`g++`, `clang++`, or MSVC)
- **Python Development Headers** (`python3-dev` on Linux/WSL)
- **Protocol Buffer Compiler** (`protoc`) with Go and Python plugins

---

### 2. Clone and Setup

```bash
git clone https://github.com/AyaanShaheer/kv-mesh.git
cd kv-mesh
```

#### Python Environment & Native C++ Extension
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # Linux / macOS / WSL
# or: .venv\Scripts\activate    # Windows PowerShell

# Install dependencies
pip install -r requirements.txt

# Compile C++ memory manager extension
cd worker
python setup.py build_ext --inplace
cd ..
```

#### Go Dependencies
```bash
cd gateway
go mod download
go mod tidy
cd ..
```

---

### 3. Protocol Buffers Workflow

If you modify `proto/kvmesh.proto`, recompile both Python and Go stubs:

**Python Stubs:**
```bash
python -m grpc_tools.protoc -Iproto --python_out=worker --grpc_python_out=worker proto/kvmesh.proto
```

**Go Stubs:**
```bash
protoc --proto_path=proto \
       --go_out=gateway/pb --go_opt=paths=source_relative \
       --go-grpc_out=gateway/pb --go-grpc_opt=paths=source_relative \
       proto/kvmesh.proto
```

---

## 🧪 Testing Locally

To test end-to-end functionality locally, run the Gateway, at least two Worker nodes, and the test Client:

### Terminal 1: Start Go Gateway
```bash
cd gateway
go run main.go
```

### Terminal 2: Start Worker 1
```bash
cd worker
python main.py --port 50052
```

### Terminal 3: Start Worker 2
```bash
cd worker
python main.py --port 50053
```

### Terminal 4: Run Inference Client
```bash
cd worker
# 1. First prompt (Cache Miss on all nodes -> computes and caches)
python client.py "Explain the theory of relativity"

# 2. Repeated prompt (Cache HIT -> Gateway immediately routes to warm worker)
python client.py "Explain the theory of relativity"

# 3. Third prompt (Test VRAM capacity and FIFO eviction to C++ Host RAM)
python client.py "Write a merge sort algorithm in C++"
```

Verify the console logs:
- **Gateway**: Look for `[Gateway] Routing request ...` and `[Router] KV Cache HIT!`.
- **Worker**: Look for `VRAM HIT`, `VRAM FULL. Evicting hash ... to CPU`, and `Pre-fetching over PCIe`.

---

## 🎨 Diagram & Architectural Updates

If you alter system architecture or request lifecycles, keep the diagrams in `.archify/` updated using **Archify**:

```bash
# Validate architecture candidate
node .agents/skills/archify/bin/archify.mjs validate architecture .archify/architecture-kvmesh-hld-*/candidate.json --json

# Deliver rendered HTML
node .agents/skills/archify/bin/archify.mjs deliver architecture .archify/architecture-kvmesh-hld-*/candidate.json .archify/architecture-kvmesh-hld-*/kvmesh-architecture.html --quality showcase --json
```

---

## 📐 Development Guidelines & Conventions

### Go Code Style
- Follow standard Go formatting conventions (`gofmt`, `go vet`).
- Ensure thread safety for shared router state (`sync.RWMutex`).
- Use context propagation and handle gRPC stream cancellation cleanly.

### Python Code Style
- Adhere to PEP 8 conventions.
- Keep daemon threads responsive to server shutdown signals.
- Gracefully handle worker disconnections and reconnection retries.

### C++ Code Style
- Modern C++ (RAII, smart pointers, standard library containers).
- Never leak raw pointers through pybind11 wrappers.
- Keep tensor byte conversions zero-copy or minimal-overhead.

---

## 🔀 Git & Commit Conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` A new feature or capability
- `fix:` A bug fix
- `perf:` Performance improvements
- `refactor:` Code refactoring without behavior change
- `docs:` Documentation updates
- `test:` Adding or updating tests
- `chore:` Build scripts, dependencies, or tool configurations

### Branch Naming
- `feature/<feature-name>`
- `bugfix/<issue-name>`
- `perf/<optimization-name>`

---

## 📬 Submitting a Pull Request (PR)

1. **Fork the repository** and create your branch from `main`.
2. Ensure your changes compile without warnings or linting errors.
3. Test with the multi-worker cluster setup.
4. Push your branch to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```
5. Open a Pull Request against the `main` branch.
6. Provide a clear PR description explaining:
   - What problem this PR solves.
   - The approach taken and components modified.
   - Log output or testing proof.

Thank you for helping build **KV-Mesh**! 🚀
