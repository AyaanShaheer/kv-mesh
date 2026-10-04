import grpc
import sys
import uuid
import kvmesh_pb2
import kvmesh_pb2_grpc

def run_prompt(prompt_text):
    print(f"Sending prompt: '{prompt_text}'")
    channel = grpc.insecure_channel('127.0.0.1:50051')
    stub = kvmesh_pb2_grpc.WorkerNodeStub(channel)
    request = kvmesh_pb2.GenerateRequest(
        prompt=prompt_text, max_tokens=50, temperature=0.7, request_id=str(uuid.uuid4())[:8]
    )
    try:
        response_stream = stub.GenerateStream(request)
        print("Response: ", end="")
        for response in response_stream:
            sys.stdout.write(response.text)
            sys.stdout.flush()
            if response.is_finished:
                break
        print("\n[Stream Complete]")
    except grpc.RpcError as e:
        print(f"\nRPC failed: {e.details()}")

if __name__ == '__main__':
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Default prompt"
    run_prompt(prompt)
