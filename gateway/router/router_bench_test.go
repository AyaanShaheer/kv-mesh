package router

import (
	"fmt"
	"testing"
	"time"
)

// buildRouterWithWorkers returns a router with n live workers, each
// advertising the fingerprint of "hot prompt" (so that the affinity policy
// finds a cache hit) plus n distinct cold prompts (so that the miss path can
// also be measured).
func buildRouterWithWorkers(n int, policy Policy, advertise bool) (*Router, uint64) {
	r := NewRouterWithOptions(Options{TTL: time.Hour, Policy: policy})

	hot := HashPrompt("hot prompt")
	for i := 0; i < n; i++ {
		prefixes := []uint64{}
		if advertise {
			prefixes = append(prefixes, hot)
		}
		r.UpdateWorkerState(hb(fmt.Sprintf("w%03d", i), 1, "bench", prefixes...))
	}
	return r, hot
}

// BenchmarkSelectAffinityHit measures the cost of a routing decision that
// finds a cache hit. The scenario mirrors the paper's RQ3: routing overhead
// as the worker count grows.
func BenchmarkSelectAffinityHit(b *testing.B) {
	for _, n := range []int{1, 2, 4, 8, 16, 32, 64, 128} {
		b.Run(fmt.Sprintf("workers=%d", n), func(b *testing.B) {
			r, _ := buildRouterWithWorkers(n, PolicyAffinity, true)
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := r.Select("hot prompt"); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

// BenchmarkSelectAffinityMiss measures the cost of a routing decision that
// falls through to round-robin, which additionally advances the shared cursor.
func BenchmarkSelectAffinityMiss(b *testing.B) {
	for _, n := range []int{1, 2, 4, 8, 16, 32, 64, 128} {
		b.Run(fmt.Sprintf("workers=%d", n), func(b *testing.B) {
			r, _ := buildRouterWithWorkers(n, PolicyAffinity, false)
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := r.Select("cold prompt"); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

// BenchmarkSelectRoundRobin measures the cache-agnostic baseline.
func BenchmarkSelectRoundRobin(b *testing.B) {
	for _, n := range []int{1, 2, 4, 8, 16, 32, 64, 128} {
		b.Run(fmt.Sprintf("workers=%d", n), func(b *testing.B) {
			r, _ := buildRouterWithWorkers(n, PolicyRoundRobin, false)
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := r.Select("cold prompt"); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

// BenchmarkSelectLeastLoaded measures the load-aware baseline.
func BenchmarkSelectLeastLoaded(b *testing.B) {
	for _, n := range []int{1, 2, 4, 8, 16, 32, 64, 128} {
		b.Run(fmt.Sprintf("workers=%d", n), func(b *testing.B) {
			r, _ := buildRouterWithWorkers(n, PolicyLeastLoaded, false)
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := r.Select("cold prompt"); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}

// BenchmarkHashPrompt measures the fingerprint computation on its own.
func BenchmarkHashPrompt(b *testing.B) {
	prompt := "SYSTEM CONTEXT v1 :: session=k00042 :: request payload follows <<k00042>>"
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = HashPrompt(prompt)
	}
}

// BenchmarkUpdateWorkerState measures the cost of ingesting a heartbeat,
// which is the write path of the control plane.
func BenchmarkUpdateWorkerState(b *testing.B) {
	for _, n := range []int{1, 8, 64} {
		b.Run(fmt.Sprintf("workers=%d", n), func(b *testing.B) {
			r := NewRouterWithOptions(Options{TTL: time.Hour})
			for i := 0; i < n; i++ {
				r.UpdateWorkerState(hb(fmt.Sprintf("w%03d", i), 1, "bench"))
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				r.UpdateWorkerState(hb(fmt.Sprintf("w%03d", i%maxInt(n, 1)), uint64(i+2), "bench"))
			}
		})
	}
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
