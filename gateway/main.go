package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"

	pb "kv-mesh/gateway/pb"
	"kv-mesh/gateway/router"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type gatewayServer struct {
	pb.UnimplementedGatewayNodeServer
	pb.UnimplementedWorkerNodeServer
	router *router.Router
}

func (s *gatewayServer) SendHeartbeat(ctx context.Context, req *pb.HeartbeatRequest) (*pb.HeartbeatAck, error) {
	s.router.UpdateWorkerState(req)
	return &pb.HeartbeatAck{Success: true}, nil
}

func (s *gatewayServer) GenerateStream(req *pb.GenerateRequest, stream pb.WorkerNode_GenerateStreamServer) error {
	// PASS THE PROMPT TO THE ROUTER FOR HASHING
	worker, err := s.router.GetBestWorker(req.Prompt)
	if err != nil {
		return fmt.Errorf("failed to route request: %v", err)
	}

	log.Printf("[Gateway] Routing request '%s' to worker %s at %s", req.RequestId, worker.ID, worker.Address)

	conn, err := grpc.NewClient(worker.Address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return fmt.Errorf("failed to connect to worker: %v", err)
	}
	defer conn.Close()

	client := pb.NewWorkerNodeClient(conn)
	workerStream, err := client.GenerateStream(context.Background(), req)
	if err != nil {
		return fmt.Errorf("worker stream error: %v", err)
	}

	for {
		resp, err := workerStream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if err := stream.Send(resp); err != nil {
			return err
		}
	}
	return nil
}

func main() {
	port := ":50051"
	lis, err := net.Listen("tcp", port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	s := grpc.NewServer()
	srv := &gatewayServer{
		router: router.NewRouter(),
	}

	pb.RegisterGatewayNodeServer(s, srv)
	pb.RegisterWorkerNodeServer(s, srv)

	log.Printf("KV-Mesh Gateway running on port %s", port)
	if err := s.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}
