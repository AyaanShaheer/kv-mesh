import argparse
import concurrent.futures
import time
import threading
import uuid
import grpc
import hashlib

import kvmesh_pb2
import kvmesh_pb2_grpc
import kv_cpp  # <--- IMPORTING YOUR C++ MODULE

def hash_prompt(prompt_text):
    hex_str = hashlib.md5(prompt_text.encode('utf-8')).hexdigest()[:16]
    return int(hex_str, 16)

class WorkerNode(kvmesh_pb2_grpc.WorkerNodeServicer):
    def __init__(self, worker_id, gateway_address, local_port):
        self.worker_id = worker_id
        self.gateway_address = gateway_address
        self.local_port = local_port
        self.local_address = f"127.0.0.1:{self.local_port}"
        
        # VRAM Capacity Simulation: Max 2 KV cache blocks
        self.vram_cache = []  # Using a list for FIFO (First-In, First-Out) eviction
        self.vram_capacity = 2
        
        # Initialize C++ Memory Manager
        self.mem_manager = kv_cpp.KVMemoryManager()
        
        self.running = True
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    def _manage_memory(self, prompt_hash):
        """Ensures the prompt_hash is in VRAM, swapping with CPU if needed."""
        if prompt_hash in self.vram_cache:
            print(f"[{self.worker_id}] VRAM HIT for hash {prompt_hash}")
            return

        if self.mem_manager.is_offloaded(prompt_hash):
            print(f"[{self.worker_id}] VRAM MISS but CPU HIT. Pre-fetching over PCIe...")
            self.mem_manager.prefetch_cache(prompt_hash)
        else:
            print(f"[{self.worker_id}] Total MISS. Computing new KV cache...")

        # If VRAM is full, offload the oldest block to CPU
        if len(self.vram_cache) >= self.vram_capacity:
            evicted_hash = self.vram_cache.pop(0)
            print(f"[{self.worker_id}] VRAM FULL. Evicting hash {evicted_hash} to CPU.")
            
            # Simulate a 1MB tensor block being sent to C++
            fake_tensor_data = b"0" * (1024 * 1024) 
            self.mem_manager.offload_cache(evicted_hash, fake_tensor_data)
        
        self.vram_cache.append(prompt_hash)

    def GenerateStream(self, request, context):
        print(f"\n--- [{self.worker_id}] New Request: '{request.prompt}' ---")
        prompt_hash = hash_prompt(request.prompt)
        
        # Handle memory routing (Eviction / Pre-fetching)
        self._manage_memory(prompt_hash)
        
        words = ["This", " is", " a", " response", " from", f" worker-{self.worker_id}."]
        for i, word in enumerate(words):
            if not context.is_active():
                break
            time.sleep(0.1)
            yield kvmesh_pb2.GenerateResponse(text=word, is_finished=(i == len(words) - 1))
        print(f"[{self.worker_id}] Finished generation.\n")

    def _heartbeat_loop(self):
        channel = grpc.insecure_channel(self.gateway_address)
        stub = kvmesh_pb2_grpc.GatewayNodeStub(channel)
        
        while self.running:
            try:
                # Dynamically calculate VRAM load based on actual usage
                vram_util = len(self.vram_cache) / self.vram_capacity
                
                req = kvmesh_pb2.HeartbeatRequest(
                    worker_id=self.worker_id,
                    grpc_address=self.local_address,
                    vram_utilization=vram_util,
                    # We broadcast what is actively in VRAM to the gateway
                    cached_prefixes=self.vram_cache 
                )
                stub.SendHeartbeat(req)
            except grpc.RpcError:
                pass
            time.sleep(3)

def serve(port):
    worker_id = str(uuid.uuid4())[:8]
    gateway_address = "127.0.0.1:50051"
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=10))
    kvmesh_pb2_grpc.add_WorkerNodeServicer_to_server(WorkerNode(worker_id, gateway_address, port), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"Worker {worker_id} started on port {port}.")
    server.wait_for_termination()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=50052)
    args = parser.parse_args()
    serve(args.port)
