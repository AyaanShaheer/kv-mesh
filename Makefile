# KV-Mesh — build, test, evaluate and publish.
#
# Every target is deterministic given the configuration files under
# experiments/configs/ and the seeds recorded in each run's summary.json.

PY        := .venv/bin/python
PIP       := .venv/bin/python -m pip
GO        := go
TECTONIC  := tectonic
RESULTS   := experiments/results
SWEEPS    := main staleness churn capacity saturation

.PHONY: all build gateway extension proto test test-go test-py experiments sweeps live \
        microbench report paper clean clean-results help

all: build test

help:
	@echo "build       build the C++ extension, the gateway binary and the protobuf stubs"
	@echo "test        run the Go unit tests (with the race detector) and the pytest suite"
	@echo "experiments run the full experimental programme (sweeps + live arm + microbenchmarks)"
	@echo "report      regenerate every table and figure from the archived results"
	@echo "paper       build paper/kv-mesh.pdf from the LaTeX source"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build: extension gateway proto

extension:
	cd worker && ../$(PY) setup.py build_ext --inplace

gateway:
	cd gateway && $(GO) build -o bin/kvmesh-gateway . && $(GO) build -o bin/hashprobe ./cmd/hashprobe

proto:
	PATH="$(PATH):$$(go env GOPATH)/bin" protoc --proto_path=proto \
	    --go_out=gateway/pb --go_opt=paths=source_relative \
	    --go-grpc_out=gateway/pb --go-grpc_opt=paths=source_relative \
	    proto/kvmesh.proto
	$(PY) -m grpc_tools.protoc -Iproto --python_out=worker --grpc_python_out=worker proto/kvmesh.proto

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test: test-go test-py

test-go:
	cd gateway && $(GO) vet ./... && $(GO) test ./... -race -count=1

test-py:
	$(PY) -m pytest experiments/tests/ -q

# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

experiments: sweeps live microbench

sweeps:
	@for c in $(SWEEPS); do \
	    echo "=== sweep: $$c ==="; \
	    $(PY) experiments/scripts/run_sweep.py \
	        --config experiments/configs/$$c.yaml \
	        --out $(RESULTS)/$$c --clean; \
	done

live: gateway extension
	$(PY) experiments/scripts/run_live.py --out $(RESULTS)/live --requests 1000 --rate 200

microbench: extension
	$(PY) experiments/scripts/bench_pool.py --out $(RESULTS)/microbench/pool_benchmark.json
	cd gateway && $(GO) test -run XXX -bench . -benchtime 300000x -benchmem ./router/ \
	    > ../$(RESULTS)/microbench/router_bench.txt

report:
	$(PY) experiments/scripts/make_report.py

# ---------------------------------------------------------------------------
# Paper
# ---------------------------------------------------------------------------

paper:
	cd paper && $(TECTONIC) -X compile kv-mesh.tex --outdir .

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean-results:
	rm -rf $(RESULTS)

clean:
	rm -rf gateway/bin worker/build worker/*.so
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
