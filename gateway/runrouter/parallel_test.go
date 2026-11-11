package runrouter

import (
	"strings"
	"testing"

	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

// The threshold the tests run with: routing.parallel_queue_threshold of every profile.
const testParallelThreshold = 2

// groupCall is one call of a parallel group through the parallel rule, or the end of one sent before.
type groupCall struct {
	run   string
	group string
	// The candidates and their queues; pinned names the run's pinned pod among them.
	pods   []queued
	pinned string
	// The end of the i-th call of the sequence (0-based) instead of a call; end twice ends it twice.
	end  bool
	ends int
	// The verdict the call must get: its reason, the pods it may go to, and the in-flight count it saw.
	reason   string
	to       []string
	inFlight int
}

func TestParallelCheck(t *testing.T) {
	calm := []queued{{"pod-a", 0}, {"pod-b", 0}}
	busy := []queued{{"pod-a", 2}, {"pod-b", 0}}
	cases := []struct {
		name      string
		threshold int
		calls     []groupCall
	}{
		{"a group stays while its pod has room", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"},
				inFlight: 1},
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"},
				inFlight: 2},
		}},
		{"the call that finds the pod full spills", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelSpill,
				to: []string{"pod-b"}, inFlight: 1},
		}},
		{"a spilled call is not counted on the pinned pod", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelSpill,
				to: []string{"pod-b"}, inFlight: 1},
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelSpill,
				to: []string{"pod-b"}, inFlight: 1},
		}},
		{"another group of the run counts apart", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-3", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
		}},
		{"another run counts apart", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-2", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
		}},
		{"a group whose first call spills spills its second too: a spill is counted nowhere", testParallelThreshold,
			[]groupCall{
				{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 3}, {"pod-b", 0}}, pinned: "pod-a",
					reason: reasonParallelSpill, to: []string{"pod-b"}},
				{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 3}, {"pod-b", 0}}, pinned: "pod-a",
					reason: reasonParallelSpill, to: []string{"pod-b"}},
			}},
		{"a call's end frees its room", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{end: true, ends: 0},
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
		}},
		{"a call ended twice frees its room once", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"},
				inFlight: 1},
			{end: true, ends: 0},
			{end: true, ends: 0},
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"},
				inFlight: 1},
		}},
		{"the count is per pinned pod", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: busy, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 0}, {"pod-b", 2}}, pinned: "pod-b",
				reason: reasonParallelStay, to: []string{"pod-b"}},
		}},
		{"no other pod with room stays", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 3}, {"pod-b", 3}}, pinned: "pod-a",
				reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 3}, {"pod-b", 3}}, pinned: "pod-a",
				reason: reasonParallelStay, to: []string{"pod-a"}, inFlight: 1},
		}},
		{"only pods with room are offered, in order", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 3}, {"pod-b", 5}, {"pod-c", 2}, {"pod-d", 0}},
				pinned: "pod-a", reason: reasonParallelSpill, to: []string{"pod-c", "pod-d"}},
		}},
		{"a pool of one pod stays", testParallelThreshold, []groupCall{
			{run: "run-1", group: "step-2", pods: []queued{{"pod-a", 5}}, pinned: "pod-a", reason: reasonParallelStay,
				to: []string{"pod-a"}},
		}},
		{"a threshold of 0 spills the second call of an idle pod", 0, []groupCall{
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelStay, to: []string{"pod-a"}},
			{run: "run-1", group: "step-2", pods: calm, pinned: "pod-a", reason: reasonParallelSpill,
				to: []string{"pod-b"}, inFlight: 1},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rule := newParallel(tc.threshold)
			verdicts := make([]parallelVerdict, len(tc.calls))
			for i, c := range tc.calls {
				if c.end {
					rule.release(verdicts[c.ends].held)
					continue
				}
				candidates := endpointsWithQueues(c.pods)
				var pinned scheduling.Endpoint
				for _, candidate := range candidates {
					if candidate.GetMetadata().ID.Name == c.pinned {
						pinned = candidate
					}
				}
				got := rule.check(c.run, c.group, pinned, candidates)
				verdicts[i] = got
				if got.reason != c.reason || got.inFlight != c.inFlight || got.group != c.group ||
					got.from != c.pinned || strings.Join(names(got.to), ",") != strings.Join(c.to, ",") {
					t.Fatalf("call %d: got %s from %s to %v with %d in flight, want %s from %s to %v with %d", i,
						got.reason, got.from, names(got.to), got.inFlight, c.reason, c.pinned, c.to, c.inFlight)
				}
				for _, p := range c.pods {
					if p.pod == c.pinned && got.queue != p.queue {
						t.Fatalf("call %d: queue %d, want the pinned pod's %d", i, got.queue, p.queue)
					}
				}
				if (got.held != nil) != (got.reason == reasonParallelStay) {
					t.Fatalf("call %d: %s held a count: %v, want one on a stay only", i, got.reason, got.held != nil)
				}
			}
		})
	}
}

func TestParallelCheckOffersThePinnedEndpointPassedIn(t *testing.T) {
	candidates := endpointsWithQueues([]queued{{"pod-a", 0}, {"pod-b", 0}})
	got := newParallel(testParallelThreshold).check("run-1", "step-2", candidates[1], candidates)
	if len(got.to) != 1 || got.to[0] != candidates[1] {
		t.Fatalf("got %v, want the pod-b endpoint passed in", names(got.to))
	}
}

func TestParallelReleaseOfEveryCallLeavesNoCount(t *testing.T) {
	rule := newParallel(testParallelThreshold)
	candidates := endpointsWithQueues([]queued{{"pod-a", 0}, {"pod-b", 0}})
	first := rule.check("run-1", "step-2", candidates[0], candidates)
	second := rule.check("run-1", "step-2", candidates[0], candidates)
	rule.release(first.held)
	rule.release(second.held)
	if len(rule.counts) != 0 {
		t.Fatalf("counts %v left after every call ended, want none: a finished group must not pile up", rule.counts)
	}
}
