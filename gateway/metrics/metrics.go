// Package metrics collects lightweight, lock-protected counters and latency
// samples for the KV-Mesh gateway. The gateway exposes them as JSON so the
// experimental harness can measure routing overhead and routing behaviour
// against the live implementation rather than only against a simulator.
package metrics

import (
	"encoding/json"
	"net/http"
	"sort"
	"sync"
	"sync/atomic"
)

// Metrics is a concurrent-safe metrics collector.
type Metrics struct {
	requestsTotal  atomic.Int64
	affinityHits   atomic.Int64
	affinityMisses atomic.Int64
	routingErrors  atomic.Int64
	workerErrors   atomic.Int64
	staleRejected  atomic.Int64
	heartbeats     atomic.Int64

	mu        sync.Mutex
	latency   []float64 // routing decision latency in microseconds
	maxSample int
}

// New returns a Metrics collector that retains up to maxSamples routing
// latency samples.
func New(maxSamples int) *Metrics {
	if maxSamples <= 0 {
		maxSamples = 100000
	}
	return &Metrics{maxSample: maxSamples}
}

// ObserveRequest records a routing decision.
func (m *Metrics) ObserveRequest(hit bool, routingLatencyUS float64) {
	m.requestsTotal.Add(1)
	if hit {
		m.affinityHits.Add(1)
	} else {
		m.affinityMisses.Add(1)
	}
	m.mu.Lock()
	if len(m.latency) < m.maxSample {
		m.latency = append(m.latency, routingLatencyUS)
	}
	m.mu.Unlock()
}

// ObserveRoutingError records a failed routing decision.
func (m *Metrics) ObserveRoutingError() { m.routingErrors.Add(1) }

// ObserveWorkerError records a failure while proxying to or streaming from a
// worker.
func (m *Metrics) ObserveWorkerError() { m.workerErrors.Add(1) }

// ObserveStaleHeartbeat records a heartbeat rejected as stale or out of order.
func (m *Metrics) ObserveStaleHeartbeat() { m.staleRejected.Add(1) }

// ObserveHeartbeat records an accepted heartbeat.
func (m *Metrics) ObserveHeartbeat() { m.heartbeats.Add(1) }

// Snapshot is a JSON-serializable view of the collected metrics.
type Snapshot struct {
	RequestsTotal      int64        `json:"requests_total"`
	AffinityHits       int64        `json:"affinity_hits"`
	AffinityMisses     int64        `json:"affinity_misses"`
	RoutingErrors      int64        `json:"routing_errors"`
	WorkerErrors       int64        `json:"worker_errors"`
	StaleHeartbeats    int64        `json:"stale_heartbeats_rejected"`
	HeartbeatsAccepted int64        `json:"heartbeats_accepted"`
	RoutingLatencyUS   LatencyStats `json:"routing_latency_us"`
}

// LatencyStats summarises a latency distribution.
type LatencyStats struct {
	Count int64   `json:"count"`
	Mean  float64 `json:"mean"`
	P50   float64 `json:"p50"`
	P95   float64 `json:"p95"`
	P99   float64 `json:"p99"`
	Max   float64 `json:"max"`
}

// Snapshot returns the current metric values.
func (m *Metrics) Snapshot() Snapshot {
	m.mu.Lock()
	samples := make([]float64, len(m.latency))
	copy(samples, m.latency)
	m.mu.Unlock()

	sort.Float64s(samples)

	stats := LatencyStats{Count: int64(len(samples))}
	if len(samples) > 0 {
		var sum float64
		for _, s := range samples {
			sum += s
		}
		stats.Mean = sum / float64(len(samples))
		stats.P50 = percentile(samples, 0.50)
		stats.P95 = percentile(samples, 0.95)
		stats.P99 = percentile(samples, 0.99)
		stats.Max = samples[len(samples)-1]
	}

	return Snapshot{
		RequestsTotal:      m.requestsTotal.Load(),
		AffinityHits:       m.affinityHits.Load(),
		AffinityMisses:     m.affinityMisses.Load(),
		RoutingErrors:      m.routingErrors.Load(),
		WorkerErrors:       m.workerErrors.Load(),
		StaleHeartbeats:    m.staleRejected.Load(),
		HeartbeatsAccepted: m.heartbeats.Load(),
		RoutingLatencyUS:   stats,
	}
}

// percentile returns the p-th percentile of a sorted slice using the
// nearest-rank method.
func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	idx := int(p * float64(len(sorted)))
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	if idx < 0 {
		idx = 0
	}
	return sorted[idx]
}

// Handler returns an http.Handler that serves the metrics snapshot as JSON.
func (m *Metrics) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		_ = enc.Encode(m.Snapshot())
	})
}
