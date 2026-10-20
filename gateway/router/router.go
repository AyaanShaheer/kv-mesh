package router

import (
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	pb "kv-mesh/gateway/pb"
)

// HashPrompt derives a 64-bit fingerprint from the raw prompt string.
//
// NOTE (manuscript §3.3, §7.3 "Cache-key validity"): this is *exact string*
// matching, not token-block prefix matching. Two prompts that differ only in
// whitespace or tokenization map to different fingerprints, and the key does
// not bind to a model revision or tokenizer revision. The value is therefore
// usable for measuring routing placement behaviour, but it is not a
// production-grade engine cache key.
func HashPrompt(prompt string) uint64 {
	hasher := md5.New()
	hasher.Write([]byte(prompt))
	hexStr := hex.EncodeToString(hasher.Sum(nil))[:16]
	val, _ := strconv.ParseUint(hexStr, 16, 64)
	return val
}

// Policy selects the worker that should serve a request.
type Policy string

const (
	// PolicyAffinity is the KV-Mesh default: prefer a live worker that
	// advertises the request fingerprint, fall back to round-robin.
	PolicyAffinity Policy = "affinity"
	// PolicyRoundRobin ignores cache state entirely (baseline).
	PolicyRoundRobin Policy = "roundrobin"
	// PolicyLeastLoaded ignores cache state and picks the worker reporting the
	// smallest queue depth (baseline).
	PolicyLeastLoaded Policy = "leastloaded"
)

// Valid reports whether p is a known policy.
func (p Policy) Valid() bool {
	switch p {
	case PolicyAffinity, PolicyRoundRobin, PolicyLeastLoaded:
		return true
	}
	return false
}

// Worker is the gateway's view of a single worker node.
type Worker struct {
	ID                string
	Address           string
	VRAMUtilization   float32
	LastSeen          time.Time
	CachedPrefixes    map[uint64]bool // prefixes resident in the fast tier
	OffloadedPrefixes map[uint64]bool // prefixes resident in the host tier
	QueueDepth        int32
	CacheBytes        uint64
	HeartbeatSeq      uint64
	InstanceID        string
}

// Options configures a Router.
type Options struct {
	// TTL is the liveness window; workers whose last heartbeat is older than
	// this are excluded from routing and eventually evicted from the registry.
	TTL time.Duration
	// Policy selects the routing strategy.
	Policy Policy
	// Clock is injectable so that unit tests can control time deterministically.
	Clock func() time.Time
}

const (
	// DefaultTTL is the heartbeat liveness window used by the prototype.
	DefaultTTL = 10 * time.Second
	// DefaultPolicy is the KV-Mesh cache-affinity policy.
	DefaultPolicy = PolicyAffinity
)

// Router maintains the mesh routing table: the set of live workers and the
// cache fingerprints each of them advertises.
//
// Concurrency note (manuscript §7.3, "selection fairness"): the previous
// implementation mutated the round-robin cursor while holding a *read* lock and
// iterated a Go map to pick a cache-hit worker, which made placement
// non-deterministic and racy under concurrent load. The cursor is now an
// atomic counter and workers are always visited in a stable, ID-sorted order.
type Router struct {
	mu      sync.RWMutex
	workers map[string]*Worker

	// rrCursor is the round-robin cursor. It is advanced atomically so that
	// concurrent requests never write to it under a read lock.
	rrCursor atomic.Uint64

	// Cached view of the live workers, kept in worker-ID order.
	//
	// The cache is rebuilt whenever the registry changes (tracked by
	// registryVersion) or when the oldest cached worker reaches its TTL.
	// Between those events a routing decision only has to scan the cached
	// slice, which removes the per-request sort and its allocations from the
	// hot path.
	//
	// cacheMu is always acquired *before* mu, never the other way round, so
	// the two locks cannot deadlock.
	cacheMu         sync.Mutex
	activeCache     []*Worker
	activeCacheOK   bool
	cacheVersion    uint64
	nextExpiry      time.Time
	registryVersion atomic.Uint64

	ttl    time.Duration
	policy Policy
	now    func() time.Time
}

// NewRouter returns a Router with the prototype defaults (10 s TTL, affinity
// policy, wall clock).
func NewRouter() *Router {
	return NewRouterWithOptions(Options{})
}

// NewRouterWithOptions returns a Router configured by opts. Zero-valued fields
// fall back to the prototype defaults.
func NewRouterWithOptions(opts Options) *Router {
	if opts.TTL <= 0 {
		opts.TTL = DefaultTTL
	}
	if !opts.Policy.Valid() {
		opts.Policy = DefaultPolicy
	}
	if opts.Clock == nil {
		opts.Clock = time.Now
	}
	return &Router{
		workers: make(map[string]*Worker),
		ttl:     opts.TTL,
		policy:  opts.Policy,
		now:     opts.Clock,
	}
}

// Policy returns the configured routing policy.
func (r *Router) Policy() Policy { return r.policy }

// TTL returns the configured liveness window.
func (r *Router) TTL() time.Duration { return r.ttl }

// UpdateWorkerState records a heartbeat. It returns false when the update was
// rejected because it was stale or out of order relative to the state already
// held for that worker.
//
// A restarted worker presents a new instance ID while keeping its logical
// worker ID; in that case the sequence expectation is reset and the update is
// accepted.
func (r *Router) UpdateWorkerState(req *pb.HeartbeatRequest) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	if existing, ok := r.workers[req.WorkerId]; ok && existing.InstanceID == req.InstanceId {
		if req.HeartbeatSeq <= existing.HeartbeatSeq {
			return false // stale or duplicated heartbeat
		}
	}

	prefixes := make(map[uint64]bool, len(req.CachedPrefixes))
	for _, p := range req.CachedPrefixes {
		prefixes[p] = true
	}
	offloaded := make(map[uint64]bool, len(req.OffloadedPrefixes))
	for _, p := range req.OffloadedPrefixes {
		offloaded[p] = true
	}

	r.workers[req.WorkerId] = &Worker{
		ID:                req.WorkerId,
		Address:           req.GrpcAddress,
		VRAMUtilization:   req.VramUtilization,
		LastSeen:          r.now(),
		CachedPrefixes:    prefixes,
		OffloadedPrefixes: offloaded,
		QueueDepth:        req.QueueDepth,
		CacheBytes:        req.CacheBytes,
		HeartbeatSeq:      req.HeartbeatSeq,
		InstanceID:        req.InstanceId,
	}
	r.registryVersion.Add(1)
	return true
}

// Selection describes the outcome of a routing decision.
type Selection struct {
	// Worker is the chosen worker.
	Worker *Worker
	// AdvertisedHit is true when the chosen worker advertised the request
	// fingerprint at decision time.
	AdvertisedHit bool
	// MatchCount is the number of active workers that advertised the
	// fingerprint.
	MatchCount int
	// ActiveWorkers is the size of the live worker set considered.
	ActiveWorkers int
	// Policy is the policy that produced the decision.
	Policy Policy
}

// ActiveWorkers returns the live workers, ordered by worker ID. The ordering is
// stable, which makes every routing decision reproducible.
func (r *Router) ActiveWorkers() []*Worker {
	return r.active()
}

// active returns the cached, ID-sorted live worker set.
func (r *Router) active() []*Worker {
	r.cacheMu.Lock()
	defer r.cacheMu.Unlock()

	now := r.now()
	version := r.registryVersion.Load()
	if r.activeCacheOK && r.cacheVersion == version && now.Before(r.nextExpiry) {
		return r.activeCache
	}

	// Rebuild. The registry version is read while the registry is locked so
	// that the cache is never labelled with a version older than the data it
	// was built from.
	r.mu.RLock()
	version = r.registryVersion.Load()
	active := make([]*Worker, 0, len(r.workers))
	nextExpiry := now.Add(r.ttl)
	for _, w := range r.workers {
		expiry := w.LastSeen.Add(r.ttl)
		if now.Before(expiry) {
			active = append(active, w)
			if expiry.Before(nextExpiry) {
				nextExpiry = expiry
			}
		}
	}
	r.mu.RUnlock()

	sort.Slice(active, func(i, j int) bool { return active[i].ID < active[j].ID })
	r.activeCache = active
	r.cacheVersion = version
	r.nextExpiry = nextExpiry
	r.activeCacheOK = true
	return active
}

// ExpireWorkers removes workers whose last heartbeat is older than the TTL and
// returns the IDs of the removed workers. The gateway calls this periodically
// so the registry does not grow without bound.
func (r *Router) ExpireWorkers() []string {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := r.now()
	var expired []string
	for id, w := range r.workers {
		if now.Sub(w.LastSeen) >= r.ttl {
			delete(r.workers, id)
			expired = append(expired, id)
		}
	}
	sort.Strings(expired)
	if len(expired) > 0 {
		r.registryVersion.Add(1)
	}
	return expired
}

// GetBestWorker selects a worker for the given prompt according to the
// configured policy. It is retained for compatibility with the original
// prototype API; new code should use Select.
func (r *Router) GetBestWorker(prompt string) (*Worker, error) {
	sel, err := r.Select(prompt)
	if err != nil {
		return nil, err
	}
	return sel.Worker, nil
}

// Select performs a routing decision and reports the details of that decision.
func (r *Router) Select(prompt string) (*Selection, error) {
	active := r.active()

	if len(active) == 0 {
		return nil, fmt.Errorf("no active workers")
	}

	hash := HashPrompt(prompt)

	switch r.policy {
	case PolicyRoundRobin:
		// Deterministic rotation over the ID-sorted live worker set.
		idx := (r.rrCursor.Add(1) - 1) % uint64(len(active))
		return &Selection{Worker: active[idx], ActiveWorkers: len(active), Policy: r.policy}, nil

	case PolicyLeastLoaded:
		best := active[0]
		for _, w := range active[1:] {
			if lessLoaded(w, best) {
				best = w
			}
		}
		return &Selection{Worker: best, ActiveWorkers: len(active), Policy: r.policy}, nil

	default: // PolicyAffinity
		var best *Worker
		matches := 0
		for _, w := range active {
			if w.CachedPrefixes[hash] {
				matches++
				if best == nil || lessLoaded(w, best) {
					best = w
				}
			}
		}
		if best != nil {
			return &Selection{
				Worker:        best,
				AdvertisedHit: true,
				MatchCount:    matches,
				ActiveWorkers: len(active),
				Policy:        r.policy,
			}, nil
		}
		// Cache miss: fall back to round-robin over the same deterministic order.
		idx := (r.rrCursor.Add(1) - 1) % uint64(len(active))
		return &Selection{
			Worker:        active[idx],
			MatchCount:    0,
			ActiveWorkers: len(active),
			Policy:        r.policy,
		}, nil
	}
}

// lessLoaded orders two workers by queue depth, then advertised fast-tier
// occupancy, then worker ID. The final ID comparison makes the ordering total
// and therefore deterministic.
func lessLoaded(a, b *Worker) bool {
	if a.QueueDepth != b.QueueDepth {
		return a.QueueDepth < b.QueueDepth
	}
	if a.VRAMUtilization != b.VRAMUtilization {
		return a.VRAMUtilization < b.VRAMUtilization
	}
	return a.ID < b.ID
}

// WorkerCount returns the number of workers currently in the registry,
// including expired-but-not-yet-reaped entries.
func (r *Router) WorkerCount() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.workers)
}

// Snapshot returns a copy of the registry keyed by worker ID.
func (r *Router) Snapshot() map[string]Worker {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string]Worker, len(r.workers))
	for id, w := range r.workers {
		out[id] = *w
	}
	return out
}
