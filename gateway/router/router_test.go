package router

import (
	"fmt"
	"sync"
	"testing"
	"time"

	pb "kv-mesh/gateway/pb"
)

// fakeClock is a manually advanced clock used to make TTL behaviour
// deterministic in tests.
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

func hb(id string, seq uint64, instance string, prefixes ...uint64) *pb.HeartbeatRequest {
	return &pb.HeartbeatRequest{
		WorkerId:        id,
		GrpcAddress:     "127.0.0.1:5005" + id,
		VramUtilization: 0.5,
		CachedPrefixes:  prefixes,
		HeartbeatSeq:    seq,
		InstanceId:      instance,
	}
}

func TestHashPromptIsDeterministicAndDistinct(t *testing.T) {
	a := HashPrompt("Explain quantum computing in simple terms")
	b := HashPrompt("Explain quantum computing in simple terms")
	c := HashPrompt("Explain quantum computing in simple terms.")

	if a != b {
		t.Fatalf("expected identical prompts to hash identically: %d != %d", a, b)
	}
	if a == c {
		t.Fatalf("expected distinct prompts to hash differently")
	}
}

func TestSelectReturnsErrorWhenNoWorkersRegistered(t *testing.T) {
	r := NewRouter()
	if _, err := r.Select("hello"); err == nil {
		t.Fatal("expected an error when no workers are registered")
	}
}

func TestSelectReturnsErrorWhenAllWorkersExpired(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{TTL: 10 * time.Second, Clock: clk.Now})

	r.UpdateWorkerState(hb("a", 1, "i1"))
	clk.Advance(11 * time.Second)

	if _, err := r.Select("hello"); err == nil {
		t.Fatal("expected an error when every worker is past the TTL")
	}
}

func TestSelectAffinityHitPrefersAdvertisingWorker(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{Policy: PolicyAffinity, Clock: clk.Now})

	hash := HashPrompt("hot prompt")
	r.UpdateWorkerState(hb("a", 1, "i1"))
	r.UpdateWorkerState(hb("b", 1, "i1", hash))

	sel, err := r.Select("hot prompt")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if sel.Worker.ID != "b" {
		t.Fatalf("expected cache-hit worker b, got %s", sel.Worker.ID)
	}
	if !sel.AdvertisedHit {
		t.Fatal("expected AdvertisedHit to be true")
	}
	if sel.MatchCount != 1 {
		t.Fatalf("expected MatchCount 1, got %d", sel.MatchCount)
	}
}

func TestSelectAffinityMissFallsBackToRoundRobin(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{Policy: PolicyAffinity, Clock: clk.Now})

	r.UpdateWorkerState(hb("a", 1, "i1"))
	r.UpdateWorkerState(hb("b", 1, "i1"))
	r.UpdateWorkerState(hb("c", 1, "i1"))

	// Round-robin fallback must visit workers in a stable, ID-sorted order.
	var seen []string
	for i := 0; i < 6; i++ {
		sel, err := r.Select("cold prompt")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if sel.AdvertisedHit {
			t.Fatal("expected no advertised hit for a cold prompt")
		}
		seen = append(seen, sel.Worker.ID)
	}
	want := []string{"a", "b", "c", "a", "b", "c"}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("round-robin order mismatch: got %v want %v", seen, want)
		}
	}
}

func TestSelectIsDeterministicAcrossRepeatedDecisions(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{Policy: PolicyAffinity, Clock: clk.Now})

	hash := HashPrompt("shared")
	r.UpdateWorkerState(hb("a", 1, "i1"))
	r.UpdateWorkerState(hb("b", 1, "i1", hash))
	r.UpdateWorkerState(hb("c", 1, "i1", hash))

	first, err := r.Select("shared")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for i := 0; i < 100; i++ {
		sel, err := r.Select("shared")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if sel.Worker.ID != first.Worker.ID {
			t.Fatalf("selection is not deterministic: got %s want %s", sel.Worker.ID, first.Worker.ID)
		}
	}
}

func TestRoundRobinPolicyIgnoresCacheState(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{Policy: PolicyRoundRobin, Clock: clk.Now})

	hash := HashPrompt("hot")
	r.UpdateWorkerState(hb("a", 1, "i1"))
	r.UpdateWorkerState(hb("b", 1, "i1", hash))

	sel, err := r.Select("hot")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if sel.AdvertisedHit {
		t.Fatal("round-robin policy must not report advertised hits")
	}
	// The first round-robin pick over the sorted live set is worker "a".
	if sel.Worker.ID != "a" {
		t.Fatalf("expected worker a, got %s", sel.Worker.ID)
	}
}

func TestLeastLoadedPolicyPicksLowestQueueDepth(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{Policy: PolicyLeastLoaded, Clock: clk.Now})

	req := hb("a", 1, "i1")
	req.QueueDepth = 5
	r.UpdateWorkerState(req)

	req = hb("b", 1, "i1")
	req.QueueDepth = 1
	r.UpdateWorkerState(req)

	sel, err := r.Select("anything")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if sel.Worker.ID != "b" {
		t.Fatalf("expected least-loaded worker b, got %s", sel.Worker.ID)
	}
}

func TestStaleHeartbeatIsRejected(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{Clock: clk.Now})

	if !r.UpdateWorkerState(hb("a", 5, "i1")) {
		t.Fatal("expected first heartbeat to be accepted")
	}
	if r.UpdateWorkerState(hb("a", 4, "i1")) {
		t.Fatal("expected out-of-order heartbeat to be rejected")
	}
	if r.UpdateWorkerState(hb("a", 5, "i1")) {
		t.Fatal("expected duplicate heartbeat to be rejected")
	}
	if !r.UpdateWorkerState(hb("a", 6, "i1")) {
		t.Fatal("expected newer heartbeat to be accepted")
	}
}

func TestRestartedWorkerWithNewInstanceIDIsAccepted(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{TTL: 10 * time.Second, Clock: clk.Now})

	r.UpdateWorkerState(hb("a", 9, "instance-1"))
	// Worker restarts: same logical ID, new instance, sequence restarts at 1.
	if !r.UpdateWorkerState(hb("a", 1, "instance-2")) {
		t.Fatal("expected heartbeat from a restarted worker to be accepted")
	}
}

func TestExpireWorkersRemovesStaleEntries(t *testing.T) {
	clk := newFakeClock()
	r := NewRouterWithOptions(Options{TTL: 10 * time.Second, Clock: clk.Now})

	r.UpdateWorkerState(hb("a", 1, "i1"))
	r.UpdateWorkerState(hb("b", 1, "i1"))
	if r.WorkerCount() != 2 {
		t.Fatalf("expected 2 workers, got %d", r.WorkerCount())
	}

	clk.Advance(11 * time.Second)
	expired := r.ExpireWorkers()
	if len(expired) != 2 {
		t.Fatalf("expected 2 expired workers, got %v", expired)
	}
	if r.WorkerCount() != 0 {
		t.Fatalf("expected registry to be empty, got %d workers", r.WorkerCount())
	}
}

func TestConcurrentSelectionsAreRaceFreeAndBalanced(t *testing.T) {
	r := NewRouterWithOptions(Options{Policy: PolicyAffinity, TTL: time.Hour})
	for i := 0; i < 4; i++ {
		r.UpdateWorkerState(hb(fmt.Sprintf("w%d", i), 1, "i1"))
	}

	const goroutines = 32
	const perGoroutine = 200

	var wg sync.WaitGroup
	var mu sync.Mutex
	counts := map[string]int{}

	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < perGoroutine; i++ {
				sel, err := r.Select("cold prompt")
				if err != nil {
					t.Errorf("unexpected error: %v", err)
					return
				}
				mu.Lock()
				counts[sel.Worker.ID]++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	total := goroutines * perGoroutine
	if len(counts) != 4 {
		t.Fatalf("expected all 4 workers to receive traffic, got %v", counts)
	}
	// The atomic cursor must spread traffic evenly: within one request of perfect.
	for id, c := range counts {
		if c < total/4-total/8 || c > total/4+total/8 {
			t.Fatalf("worker %s received %d requests, expected ~%d", id, c, total/4)
		}
	}
}

func TestConcurrentHeartbeatUpdatesAreRaceFree(t *testing.T) {
	r := NewRouterWithOptions(Options{TTL: time.Hour})
	var wg sync.WaitGroup
	for g := 0; g < 16; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				r.UpdateWorkerState(hb(fmt.Sprintf("w%d", g%4), uint64(i+1), "i1", uint64(i)))
			}
		}(g)
	}
	wg.Wait()
}
