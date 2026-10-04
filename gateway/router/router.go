package router

import (
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"strconv"
	"sync"
	"time"

	pb "kv-mesh/gateway/pb"
)

func HashPrompt(prompt string) uint64 {
	hasher := md5.New()
	hasher.Write([]byte(prompt))
	hexStr := hex.EncodeToString(hasher.Sum(nil))[:16]
	val, _ := strconv.ParseUint(hexStr, 16, 64)
	return val
}

type Worker struct {
	ID              string
	Address         string
	VRAMUtilization float32
	LastSeen        time.Time
	CachedPrefixes  map[uint64]bool // Tracks what is in this worker's KV Cache
}

type Router struct {
	mu      sync.RWMutex
	workers map[string]*Worker
	next    int
}

func NewRouter() *Router {
	return &Router{workers: make(map[string]*Worker)}
}

func (r *Router) UpdateWorkerState(req *pb.HeartbeatRequest) {
	r.mu.Lock()
	defer r.mu.Unlock()

	prefixes := make(map[uint64]bool)
	for _, p := range req.CachedPrefixes {
		prefixes[p] = true
	}

	r.workers[req.WorkerId] = &Worker{
		ID:              req.WorkerId,
		Address:         req.GrpcAddress,
		VRAMUtilization: req.VramUtilization,
		LastSeen:        time.Now(),
		CachedPrefixes:  prefixes,
	}
}

// GetBestWorker replaces GetNextWorker. It tries to find a Cache Hit first.
func (r *Router) GetBestWorker(prompt string) (*Worker, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	if len(r.workers) == 0 {
		return nil, fmt.Errorf("no workers available")
	}

	var activeWorkers []*Worker
	for _, w := range r.workers {
		if time.Since(w.LastSeen) < 10*time.Second {
			activeWorkers = append(activeWorkers, w)
		}
	}

	if len(activeWorkers) == 0 {
		return nil, fmt.Errorf("no active workers")
	}

	promptHash := HashPrompt(prompt)

	// 1. PREFIX MATCHING: Check if any worker already has this in their KV Cache
	for _, w := range activeWorkers {
		if w.CachedPrefixes[promptHash] {
			fmt.Printf("[Router] KV Cache HIT! Routing hash %d to %s\n", promptHash, w.ID)
			return w, nil
		}
	}

	// 2. CACHE MISS: Fall back to Round-Robin
	fmt.Printf("[Router] KV Cache MISS for hash %d. Falling back to Round-Robin.\n", promptHash)
	r.next = (r.next + 1) % len(activeWorkers)
	return activeWorkers[r.next], nil
}
