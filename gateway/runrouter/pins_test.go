package runrouter

import (
	"testing"
	"time"
)

// The TTL the tests run with: routing.run_ttl_seconds of every profile.
const testTTL = 300 * time.Second

// fakeClock is the time the pins read, moved by the test.
type fakeClock struct {
	at time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{at: time.Date(2026, 11, 10, 12, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) now() time.Time { return c.at }

func (c *fakeClock) advance(by time.Duration) { c.at = c.at.Add(by) }

func TestPinsRecordReturnsThePodPinnedBefore(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	if before := store.record("run-1", "pod-a", progress{work: 1}); before != "" {
		t.Fatalf("a first record returned %q, want no pod", before)
	}
	if before := store.record("run-1", "pod-b", progress{work: 1}); before != "pod-a" {
		t.Fatalf("got %q, want pod-a", before)
	}
	if got := store.pod("run-1"); got != "pod-b" {
		t.Fatalf("the run is pinned to %q, want the last recorded pod-b", got)
	}
}

func TestPinsHoldUpToTheTTLAndNotAfter(t *testing.T) {
	cases := []struct {
		name  string
		since time.Duration
		want  string
	}{
		{"just recorded", 0, "pod-a"},
		{"used exactly one TTL ago", testTTL, "pod-a"},
		{"used one TTL and a nanosecond ago", testTTL + time.Nanosecond, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			store := newPins(testTTL, clock.now)
			store.record("run-1", "pod-a", progress{work: 1})
			clock.advance(tc.since)
			if got := store.pod("run-1"); got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}

func TestPinsRecordAfterTheTTLReturnsNoPod(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-1", "pod-a", progress{work: 1})
	clock.advance(testTTL + time.Second)
	if before := store.record("run-1", "pod-b", progress{work: 1}); before != "" {
		t.Fatalf("got %q, want no pod: the pin had expired", before)
	}
}

func TestPinsRecordDropsEveryExpiredPin(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-1", "pod-a", progress{work: 1})
	store.record("run-2", "pod-b", progress{work: 1})
	clock.advance(testTTL + time.Second)
	store.record("run-3", "pod-a", progress{work: 1})
	if len(store.byRun) != 1 {
		t.Fatalf("%d pins kept, want only run-3's: %v", len(store.byRun), store.byRun)
	}
}

func TestPinsEndRemovesOnlyThatRun(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-1", "pod-a", progress{work: 1})
	store.record("run-2", "pod-b", progress{work: 1})
	if !store.end("run-1") {
		t.Fatal("end reported no pin for run-1")
	}
	if got := store.pod("run-1"); got != "" {
		t.Fatalf("run-1 still pinned to %q", got)
	}
	if got := store.pod("run-2"); got != "pod-b" {
		t.Fatalf("run-2 pinned to %q, want pod-b", got)
	}
}

func TestPinsEndOfARunWithNoLivePinReportsNone(t *testing.T) {
	cases := []struct {
		name   string
		record bool
		since  time.Duration
	}{
		{"never pinned", false, 0},
		{"expired", true, testTTL + time.Second},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			store := newPins(testTTL, clock.now)
			if tc.record {
				store.record("run-1", "pod-a", progress{work: 1})
			}
			clock.advance(tc.since)
			if store.end("run-1") {
				t.Fatal("end reported a live pin")
			}
		})
	}
}

func TestPinsRenewKeepsThePodAndRestartsTheTTL(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-1", "pod-a", progress{work: 1})
	clock.advance(testTTL - time.Second)
	store.renew("run-1", progress{work: 1})
	clock.advance(testTTL - time.Second)
	if got := store.pod("run-1"); got != "pod-a" {
		t.Fatalf("got %q, want pod-a: the renew restarted the TTL", got)
	}
}

func TestPinsRenewPinsNoRunWithoutALivePin(t *testing.T) {
	cases := []struct {
		name   string
		record bool
		since  time.Duration
	}{
		{"never pinned", false, 0},
		{"expired", true, testTTL + time.Second},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			store := newPins(testTTL, clock.now)
			if tc.record {
				store.record("run-1", "pod-a", progress{work: 1})
			}
			clock.advance(tc.since)
			store.renew("run-1", progress{work: 1})
			if got := store.pod("run-1"); got != "" {
				t.Fatalf("got %q, want no pin: renew must not revive one", got)
			}
		})
	}
}

func TestPinsRecordAndRenewKeepTheRunsLatestProgress(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-1", "pod-a", progress{work: 8, size: 800})
	if got := store.byRun["run-1"].progress; got != (progress{work: 8, size: 800}) {
		t.Fatalf("progress %+v after record, want work 8 and size 800", got)
	}
	store.renew("run-1", progress{work: 3, size: 1200})
	if got := store.byRun["run-1"]; got.progress != (progress{work: 3, size: 1200}) || got.pod != "pod-a" {
		t.Fatalf("pin %+v after renew, want pod-a with work 3 and size 1200: a spilled call moves its run on", got)
	}
}
