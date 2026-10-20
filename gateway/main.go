package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	"kv-mesh/gateway/metrics"
	pb "kv-mesh/gateway/pb"
	"kv-mesh/gateway/router"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// decisionRecord is one row of the gateway's structured decision log. The
// experimental harness consumes this log to compute routing metrics against
// the live implementation.
type decisionRecord struct {
	Timestamp        string  `json:"ts"`
	RequestID        string  `json:"request_id"`
	PromptHash       uint64  `json:"prompt_hash"`
	WorkerID         string  `json:"worker_id"`
	AdvertisedHit    bool    `json:"advertised_hit"`
	MatchCount       int     `json:"match_count"`
	ActiveWorkers    int     `json:"active_workers"`
	Policy           string  `json:"policy"`
	RoutingLatencyUS float64 `json:"routing_latency_us"`
	StreamLatencyMS  float64 `json:"stream_latency_ms"`
	Error            string  `json:"error,omitempty"`
}

// decisionLogger writes decision records as JSON Lines.
type decisionLogger struct {
	mu     sync.Mutex
	f      *os.File
	w      io.Writer
	closer io.Closer
}

func newDecisionLogger(path string) (*decisionLogger, error) {
	if path == "" {
		return &decisionLogger{}, nil
	}
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	return &decisionLogger{f: f, w: f, closer: f}, nil
}

func (d *decisionLogger) log(rec decisionRecord) {
	if d == nil || d.f == nil {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	b, err := json.Marshal(rec)
	if err != nil {
		return
	}
	// Buffered write: the file is flushed and closed when the process exits.
	// An fsync per record would dominate the measured request path.
	_, _ = d.w.Write(append(b, '\n'))
}

func (d *decisionLogger) close() {
	if d != nil && d.closer != nil {
		_ = d.closer.Close()
	}
}

// connPool keeps one long-lived gRPC client connection per worker address.
// The original prototype opened a new connection per request, which dominated
// the measured routing-to-first-token overhead.
type connPool struct {
	mu    sync.Mutex
	conns map[string]*grpc.ClientConn
}

func newConnPool() *connPool { return &connPool{conns: make(map[string]*grpc.ClientConn)} }

func (p *connPool) get(ctx context.Context, address string) (*grpc.ClientConn, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if c, ok := p.conns[address]; ok {
		return c, nil
	}
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	p.conns[address] = conn
	return conn, nil
}

func (p *connPool) close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range p.conns {
		_ = c.Close()
	}
	p.conns = make(map[string]*grpc.ClientConn)
}

type gatewayServer struct {
	pb.UnimplementedGatewayNodeServer
	pb.UnimplementedWorkerNodeServer
	router  *router.Router
	metrics *metrics.Metrics
	logger  *decisionLogger
	pool    *connPool
}

func (s *gatewayServer) SendHeartbeat(ctx context.Context, req *pb.HeartbeatRequest) (*pb.HeartbeatAck, error) {
	accepted := s.router.UpdateWorkerState(req)
	if accepted {
		s.metrics.ObserveHeartbeat()
	} else {
		s.metrics.ObserveStaleHeartbeat()
	}
	return &pb.HeartbeatAck{Success: true, Accepted: accepted}, nil
}

func (s *gatewayServer) GenerateStream(req *pb.GenerateRequest, stream pb.WorkerNode_GenerateStreamServer) error {
	rec := decisionRecord{
		Timestamp:  time.Now().UTC().Format(time.RFC3339Nano),
		RequestID:  req.RequestId,
		PromptHash: router.HashPrompt(req.Prompt),
		Policy:     string(s.router.Policy()),
	}

	start := time.Now()
	sel, err := s.router.Select(req.Prompt)
	rec.RoutingLatencyUS = float64(time.Since(start).Microseconds())
	if err != nil {
		rec.Error = err.Error()
		rec.StreamLatencyMS = float64(time.Since(start).Milliseconds())
		s.metrics.ObserveRoutingError()
		s.logger.log(rec)
		return fmt.Errorf("failed to route request: %v", err)
	}

	worker := sel.Worker
	rec.WorkerID = worker.ID
	rec.AdvertisedHit = sel.AdvertisedHit
	rec.MatchCount = sel.MatchCount
	rec.ActiveWorkers = sel.ActiveWorkers

	log.Printf("[Gateway] Routing request '%s' to worker %s at %s (advertised_hit=%v, active=%d)",
		req.RequestId, worker.ID, worker.Address, sel.AdvertisedHit, sel.ActiveWorkers)

	// The worker identity and the router's belief are attached to every
	// response chunk so that a client (or the experimental harness) can
	// correlate the advertised hit with the worker's ground truth.
	streamStart := time.Now()
	conn, err := s.pool.get(stream.Context(), worker.Address)
	if err != nil {
		rec.Error = err.Error()
		s.metrics.ObserveWorkerError()
		s.logger.log(rec)
		return fmt.Errorf("failed to connect to worker: %v", err)
	}

	client := pb.NewWorkerNodeClient(conn)
	workerStream, err := client.GenerateStream(stream.Context(), req)
	if err != nil {
		rec.Error = err.Error()
		rec.StreamLatencyMS = float64(time.Since(streamStart).Milliseconds())
		s.metrics.ObserveWorkerError()
		s.logger.log(rec)
		return fmt.Errorf("worker stream error: %v", err)
	}

	for {
		resp, err := workerStream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			rec.Error = err.Error()
			rec.StreamLatencyMS = float64(time.Since(streamStart).Milliseconds())
			s.metrics.ObserveWorkerError()
			s.logger.log(rec)
			return err
		}
		resp.WorkerId = worker.ID
		resp.AdvertisedHit = sel.AdvertisedHit
		if err := stream.Send(resp); err != nil {
			rec.Error = err.Error()
			s.metrics.ObserveWorkerError()
			s.logger.log(rec)
			return err
		}
	}

	rec.StreamLatencyMS = float64(time.Since(streamStart).Milliseconds())
	s.metrics.ObserveRequest(sel.AdvertisedHit, rec.RoutingLatencyUS)
	s.logger.log(rec)
	return nil
}

func main() {
	port := flag.String("port", ":50051", "gRPC listen port for the gateway")
	policy := flag.String("policy", string(router.DefaultPolicy),
		"routing policy: affinity | roundrobin | leastloaded")
	ttl := flag.Duration("ttl", router.DefaultTTL, "worker liveness window (heartbeat TTL)")
	metricsPort := flag.String("metrics-port", ":50061", "HTTP port for the /metrics JSON endpoint")
	decisionLog := flag.String("decision-log", "", "path to a JSON Lines routing decision log (empty disables)")
	flag.Parse()

	pol := router.Policy(*policy)
	if !pol.Valid() {
		log.Fatalf("unknown policy %q (expected affinity, roundrobin or leastloaded)", *policy)
	}

	rt := router.NewRouterWithOptions(router.Options{TTL: *ttl, Policy: pol})
	mx := metrics.New(200000)

	logger, err := newDecisionLogger(*decisionLog)
	if err != nil {
		log.Fatalf("failed to open decision log: %v", err)
	}
	defer logger.close()

	pool := newConnPool()
	defer pool.close()

	srv := &gatewayServer{router: rt, metrics: mx, logger: logger, pool: pool}

	lis, err := net.Listen("tcp", *port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	s := grpc.NewServer()
	pb.RegisterGatewayNodeServer(s, srv)
	pb.RegisterWorkerNodeServer(s, srv)

	// Periodically reap workers whose heartbeat has aged past the TTL so the
	// registry does not grow without bound.
	go func() {
		ticker := time.NewTicker(*ttl / 2)
		defer ticker.Stop()
		for range ticker.C {
			if expired := rt.ExpireWorkers(); len(expired) > 0 {
				log.Printf("[Gateway] Expired %d stale worker(s): %v", len(expired), expired)
			}
		}
	}()

	// Metrics endpoint.
	mux := http.NewServeMux()
	mux.Handle("/metrics", mx.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintln(w, "ok")
	})
	go func() {
		if err := http.ListenAndServe(*metricsPort, mux); err != nil {
			log.Printf("[Gateway] metrics server stopped: %v", err)
		}
	}()

	log.Printf("KV-Mesh Gateway running on port %s (policy=%s, ttl=%s, metrics=%s)", *port, pol, *ttl, *metricsPort)
	if err := s.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}
