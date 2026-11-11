package runrouter

import (
	"strings"
	"testing"
	"time"

	k8stypes "k8s.io/apimachinery/pkg/types"

	fwkdl "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/datalayer"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

// The limits the tests run with: routing.overload_queue_threshold and routing.overload_sustained_seconds of every
// profile.
var testLimits = Limits{QueueThreshold: 1, Sustained: 10 * time.Second}

// queued is a candidate and its waiting queue, as the picker last scraped it.
type queued struct {
	pod   string
	queue int
}

func endpointsWithQueues(pods []queued) []scheduling.Endpoint {
	out := make([]scheduling.Endpoint, len(pods))
	for i, p := range pods {
		out[i] = scheduling.NewEndpoint(
			&fwkdl.EndpointMetadata{ID: k8stypes.NamespacedName{Namespace: "default", Name: p.pod}},
			&fwkdl.Metrics{WaitingQueueSize: p.queue}, nil)
	}
	return out
}

// look is one call through the filter, as the overload guard sees it.
type look struct {
	// The fake clock moves this far first.
	advance time.Duration
	pods    []queued
	// The call's pinned pod; "" is a call with no pinned candidate, which only looks at the queues.
	pinned string
	// "" means the call stays; otherwise reasonSpill or reasonRemap.
	reason string
	// The pods the call may go to on a spill or a remap.
	to []string
	// How long the pinned pod has been over the threshold, on a spill or a remap.
	overloaded time.Duration
}

func TestOverloadCheck(t *testing.T) {
	over := []queued{{"pod-a", 2}, {"pod-b", 0}}
	calm := []queued{{"pod-a", 0}, {"pod-b", 0}}
	cases := []struct {
		name  string
		looks []look
	}{
		{"a queue under the threshold stays", []look{
			{pods: []queued{{"pod-a", 0}, {"pod-b", 0}}, pinned: "pod-a"},
		}},
		{"a queue at the threshold stays", []look{
			{pods: []queued{{"pod-a", 1}, {"pod-b", 0}}, pinned: "pod-a"},
		}},
		{"a queue over the threshold spills to the other pod", []look{
			{pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
		}},
		{"a spike spills one call and the next stays", []look{
			{pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
			{advance: time.Second, pods: calm, pinned: "pod-a"},
		}},
		{"sustained overload remaps once, then spills while the timer restarts", []look{
			{pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
			{advance: 5 * time.Second, pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"},
				overloaded: 5 * time.Second},
			{advance: 5 * time.Second, pods: over, pinned: "pod-a", reason: reasonRemap, to: []string{"pod-b"},
				overloaded: 10 * time.Second},
			{advance: time.Second, pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"},
				overloaded: time.Second},
			{advance: 9 * time.Second, pods: over, pinned: "pod-a", reason: reasonRemap, to: []string{"pod-b"},
				overloaded: 10 * time.Second},
		}},
		{"a dip resets the timer", []look{
			{pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
			{advance: 6 * time.Second, pods: calm, pinned: "pod-a"},
			{advance: time.Second, pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
			{advance: 6 * time.Second, pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"},
				overloaded: 6 * time.Second},
		}},
		{"a dip seen by a call with no pin resets the timer too", []look{
			{pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
			{advance: 6 * time.Second, pods: calm},
			{advance: 6 * time.Second, pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
		}},
		{"overload seen by a call with no pin starts the timer", []look{
			{pods: over},
			{advance: 10 * time.Second, pods: over, pinned: "pod-a", reason: reasonRemap, to: []string{"pod-b"},
				overloaded: 10 * time.Second},
		}},
		{"every pod over the threshold stays, and the timer runs on", []look{
			{pods: []queued{{"pod-a", 2}, {"pod-b", 3}}, pinned: "pod-a"},
			{advance: 10 * time.Second, pods: over, pinned: "pod-a", reason: reasonRemap, to: []string{"pod-b"},
				overloaded: 10 * time.Second},
		}},
		{"only pods at or under the threshold are offered", []look{
			{pods: []queued{{"pod-a", 2}, {"pod-b", 5}, {"pod-c", 1}, {"pod-d", 0}}, pinned: "pod-a",
				reason: reasonSpill, to: []string{"pod-c", "pod-d"}},
		}},
		{"a pool of one pod stays", []look{
			{pods: []queued{{"pod-a", 5}}, pinned: "pod-a"},
		}},
		{"a pod that left the pool loses its timer", []look{
			{pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
			{advance: 6 * time.Second, pods: []queued{{"pod-b", 0}}},
			{advance: 6 * time.Second, pods: over, pinned: "pod-a", reason: reasonSpill, to: []string{"pod-b"}},
		}},
		{"each pod has its own timer", []look{
			{pods: []queued{{"pod-a", 2}, {"pod-b", 0}, {"pod-c", 0}}, pinned: "pod-a", reason: reasonSpill,
				to: []string{"pod-b", "pod-c"}},
			{advance: 6 * time.Second, pods: []queued{{"pod-a", 2}, {"pod-b", 2}, {"pod-c", 0}}, pinned: "pod-b",
				reason: reasonSpill, to: []string{"pod-c"}},
			{advance: 4 * time.Second, pods: []queued{{"pod-a", 2}, {"pod-b", 2}, {"pod-c", 0}}, pinned: "pod-b",
				reason: reasonSpill, to: []string{"pod-c"}, overloaded: 4 * time.Second},
			{pods: []queued{{"pod-a", 2}, {"pod-b", 2}, {"pod-c", 0}}, pinned: "pod-a", reason: reasonRemap,
				to: []string{"pod-c"}, overloaded: 10 * time.Second},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			guard := newOverload(testLimits, clock.now)
			for i, l := range tc.looks {
				clock.advance(l.advance)
				candidates := endpointsWithQueues(l.pods)
				var pinned scheduling.Endpoint
				for _, candidate := range candidates {
					if candidate.GetMetadata().ID.Name == l.pinned {
						pinned = candidate
					}
				}
				got := guard.check(pinned, candidates)
				if l.reason == "" {
					if got != nil {
						t.Fatalf("look %d: got %s to %v, want the call to stay", i, got.reason, names(got.to))
					}
					continue
				}
				if got == nil {
					t.Fatalf("look %d: the call stayed, want %s", i, l.reason)
				}
				if got.reason != l.reason || got.from != l.pinned || got.overloaded != l.overloaded ||
					strings.Join(names(got.to), ",") != strings.Join(l.to, ",") {
					t.Fatalf("look %d: got %s from %s to %v after %s, want %s from %s to %v after %s", i,
						got.reason, got.from, names(got.to), got.overloaded, l.reason, l.pinned, l.to, l.overloaded)
				}
				for _, p := range l.pods {
					if p.pod == l.pinned && got.queue != p.queue {
						t.Fatalf("look %d: queue %d, want the pinned pod's %d", i, got.queue, p.queue)
					}
				}
			}
		})
	}
}

func TestOverloadThresholdZeroSpillsOnOneWaitingCall(t *testing.T) {
	guard := newOverload(Limits{QueueThreshold: 0, Sustained: testLimits.Sustained}, newFakeClock().now)
	candidates := endpointsWithQueues([]queued{{"pod-a", 1}, {"pod-b", 0}})
	got := guard.check(candidates[0], candidates)
	if got == nil || got.reason != reasonSpill {
		t.Fatalf("got %v, want a spill: one waiting call is over a threshold of 0", got)
	}
}

func TestOverloadReadsAPodWithNoMetricsAsNotOverloaded(t *testing.T) {
	guard := newOverload(testLimits, newFakeClock().now)
	candidates := []scheduling.Endpoint{
		scheduling.NewEndpoint(&fwkdl.EndpointMetadata{ID: k8stypes.NamespacedName{Name: "pod-a"}}, nil, nil),
		scheduling.NewEndpoint(&fwkdl.EndpointMetadata{ID: k8stypes.NamespacedName{Name: "pod-b"}}, nil, nil),
	}
	if got := guard.check(candidates[0], candidates); got != nil {
		t.Fatalf("got %s, want the call to stay on a pod whose queue the picker has not read", got.reason)
	}
}
