package runrouter

import (
	"strings"
	"sync"
	"testing"
	"time"
)

// The slack the tests run with: routing.least_work_slack of every profile.
const testLeastWorkSlack = 2

// pinned is a live run pinned to a pod with its remaining work, set up before a placement.
type pinned struct {
	run  string
	pod  string
	work int
}

func TestPlaceNarrowsAFirstCallToTheLightestPod(t *testing.T) {
	cases := []struct {
		name  string
		pins  []pinned
		pods  []string
		slack int
		// The candidates offered, and the pod the run is pinned to now ("" when the scorers choose).
		want     []string
		reserved string
		work     map[string]int
	}{
		{"no pins: level, the scorers choose", nil, []string{"pod-a", "pod-b"}, 2,
			[]string{"pod-a", "pod-b"}, "", map[string]int{"pod-a": 0, "pod-b": 0}},
		{"a gap over the slack: the lighter pod", []pinned{{"run-x", "pod-a", 3}}, []string{"pod-a", "pod-b"}, 2,
			[]string{"pod-b"}, "pod-b", map[string]int{"pod-a": 3, "pod-b": 0}},
		{"a gap of exactly the slack: level", []pinned{{"run-x", "pod-a", 2}}, []string{"pod-a", "pod-b"}, 2,
			[]string{"pod-a", "pod-b"}, "", map[string]int{"pod-a": 2, "pod-b": 0}},
		{"slack 0: any gap decides", []pinned{{"run-x", "pod-b", 1}}, []string{"pod-a", "pod-b"}, 0,
			[]string{"pod-a"}, "pod-a", map[string]int{"pod-a": 0, "pod-b": 1}},
		{"slack 0: a tie is level", []pinned{{"run-x", "pod-a", 1}, {"run-y", "pod-b", 1}},
			[]string{"pod-a", "pod-b"}, 0, []string{"pod-a", "pod-b"}, "", map[string]int{"pod-a": 1, "pod-b": 1}},
		{"a pod's runs add up", []pinned{{"run-x", "pod-a", 3}, {"run-y", "pod-a", 4}, {"run-z", "pod-b", 6}},
			[]string{"pod-a", "pod-b"}, 0, []string{"pod-b"}, "pod-b", map[string]int{"pod-a": 7, "pod-b": 6}},
		{"three pods: those within the slack, in order", []pinned{{"run-x", "pod-b", 1}, {"run-y", "pod-c", 9}},
			[]string{"pod-a", "pod-b", "pod-c"}, 2, []string{"pod-a", "pod-b"}, "",
			map[string]int{"pod-a": 0, "pod-b": 1, "pod-c": 9}},
		{"runs on a pod that is no candidate are not counted", []pinned{{"run-x", "pod-a", 3}, {"run-y", "pod-c", 10}},
			[]string{"pod-a", "pod-b"}, 2, []string{"pod-b"}, "pod-b", map[string]int{"pod-a": 3, "pod-b": 0}},
		{"a lone candidate gets the run", []pinned{{"run-x", "pod-a", 30}}, []string{"pod-a"}, 2,
			[]string{"pod-a"}, "pod-a", map[string]int{"pod-a": 30}},
		{"no candidates: nothing offered, nothing pinned", nil, []string{}, 2, nil, "", map[string]int{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := newPins(testTTL, newFakeClock().now)
			for _, p := range tc.pins {
				store.record(p.run, p.pod, progress{work: p.work})
			}
			verdict, first := store.place("run-1", endpoints(tc.pods), leastWorkRule(tc.slack), progress{work: 9})
			if !first {
				t.Fatal("a run with no pin was not judged")
			}
			if strings.Join(names(verdict.to), ",") != strings.Join(tc.want, ",") {
				t.Fatalf("offered %v, want %v", names(verdict.to), tc.want)
			}
			if verdict.pod != tc.reserved || store.pod("run-1") != tc.reserved {
				t.Fatalf("verdict pod %q, run pinned to %q, want %q for both", verdict.pod, store.pod("run-1"),
					tc.reserved)
			}
			if tc.reserved != "" && store.byRun["run-1"].work != 9 {
				t.Fatalf("the reserved pin holds work %d, want the run's 9", store.byRun["run-1"].work)
			}
			if len(verdict.load) != len(tc.work) {
				t.Fatalf("work %v, want %v", verdict.load, tc.work)
			}
			for pod, want := range tc.work {
				if verdict.load[pod] != want {
					t.Fatalf("work %v, want %v", verdict.load, tc.work)
				}
			}
		})
	}
}

func TestPlaceLeavesARunWithALivePinAlone(t *testing.T) {
	store := newPins(testTTL, newFakeClock().now)
	store.record("run-1", "pod-c", progress{work: 4})
	if _, first := store.place("run-1", endpoints([]string{"pod-a", "pod-b"}), leastWorkRule(0),
		progress{work: 9}); first {
		t.Fatal("judged a run that has a live pin: only a run's first call is placed by work")
	}
	if got := store.byRun["run-1"]; got.pod != "pod-c" || got.work != 4 {
		t.Fatalf("pin %+v, want pod-c with work 4 untouched", got)
	}
}

func TestPlaceCountsNoExpiredPin(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-x", "pod-a", progress{work: 30})
	clock.advance(testTTL + time.Second)
	store.record("run-y", "pod-b", progress{work: 1})
	verdict, first := store.place("run-1", endpoints([]string{"pod-a", "pod-b"}), leastWorkRule(0), progress{work: 9})
	if !first || verdict.pod != "pod-a" {
		t.Fatalf("verdict %+v, want run-1 on pod-a: run-x's pin there expired", verdict)
	}
}

func TestPlaceTreatsARunWhosePinExpiredAsFirst(t *testing.T) {
	clock := newFakeClock()
	store := newPins(testTTL, clock.now)
	store.record("run-1", "pod-a", progress{work: 30})
	clock.advance(testTTL + time.Second)
	verdict, first := store.place("run-1", endpoints([]string{"pod-a", "pod-b"}), leastWorkRule(2), progress{work: 9})
	if !first || verdict.load["pod-a"] != 0 {
		t.Fatalf("verdict %+v, first %v: an expired pin is no pin and counts no work", verdict, first)
	}
}

// Five first calls at once, as a burst sends them: each counts the ones placed before it, since the pin is reserved
// under the lock that counts. Placed one at a time with pod-a at 10 and slack 0, runs of work 4 go b, b, b (b at 12
// over a's 10), a, b: 4 on pod-b and 1 on pod-a whatever the order, as the runs are alike. Without the reservation
// every one would see pod-b at 0 and all 5 would go there.
func TestPlaceCountsTheFirstCallsPlacedJustBefore(t *testing.T) {
	store := newPins(testTTL, newFakeClock().now)
	store.record("run-x", "pod-a", progress{work: 10})
	pods := endpoints([]string{"pod-a", "pod-b"})
	runs := []string{"run-1", "run-2", "run-3", "run-4", "run-5"}
	var wg sync.WaitGroup
	for _, run := range runs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			store.place(run, pods, leastWorkRule(0), progress{work: 4})
		}()
	}
	wg.Wait()
	on := map[string]int{}
	for _, run := range runs {
		on[store.pod(run)]++
	}
	if on["pod-a"] != 1 || on["pod-b"] != 4 {
		t.Fatalf("runs per pod %v, want 1 on pod-a and 4 on pod-b", on)
	}
}

func TestPlaceByContextWeighsEachPinnedRunsLatestCallSize(t *testing.T) {
	store := newPins(testTTL, newFakeClock().now)
	// Many steps left but a short context on pod-a; few steps left and a long context on pod-b.
	store.record("run-x", "pod-a", progress{work: 20, size: 3000})
	store.record("run-y", "pod-b", progress{work: 2, size: 9000})
	store.record("run-z", "pod-b", progress{work: 1, size: 500})
	verdict, first := store.place("run-1", endpoints([]string{"pod-a", "pod-b"}), leastContextRule(),
		progress{work: 9, size: 1000})
	if !first || verdict.pod != "pod-a" || verdict.load["pod-a"] != 3000 || verdict.load["pod-b"] != 9500 {
		t.Fatalf("verdict %+v, want run-1 on pod-a, the smaller context, with loads 3000 and 9500", verdict)
	}
	if verdict.reason != reasonLeastContext || verdict.measure != "context" {
		t.Fatalf("verdict names %s and %s, want %s and context", verdict.reason, verdict.measure, reasonLeastContext)
	}
	if got := store.byRun["run-1"].progress; got != (progress{work: 9, size: 1000}) {
		t.Fatalf("the reserved pin holds %+v, want the run's own work 9 and size 1000", got)
	}
}

func TestPlaceByContextHasNoSlack(t *testing.T) {
	store := newPins(testTTL, newFakeClock().now)
	store.record("run-x", "pod-a", progress{size: 2001})
	store.record("run-y", "pod-b", progress{size: 2000})
	verdict, _ := store.place("run-1", endpoints([]string{"pod-a", "pod-b"}), leastContextRule(), progress{size: 1})
	if verdict.pod != "pod-b" {
		t.Fatalf("verdict %+v, want pod-b: one byte decides", verdict)
	}
	tied := newPins(testTTL, newFakeClock().now)
	verdict, _ = tied.place("run-1", endpoints([]string{"pod-a", "pod-b"}), leastContextRule(), progress{size: 1})
	if verdict.pod != "" || len(verdict.to) != 2 {
		t.Fatalf("verdict %+v, want both pods offered and none reserved: equal contexts are level", verdict)
	}
}

func TestPlaceByWorkNamesItsRule(t *testing.T) {
	store := newPins(testTTL, newFakeClock().now)
	verdict, _ := store.place("run-1", endpoints([]string{"pod-a"}), leastWorkRule(2), progress{work: 1})
	if verdict.reason != reasonLeastWork || verdict.measure != "work" {
		t.Fatalf("verdict names %s and %s, want %s and work", verdict.reason, verdict.measure, reasonLeastWork)
	}
}
