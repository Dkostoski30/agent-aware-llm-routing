package runrouter

import (
	"sync"

	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

// groupKey names the calls of one parallel group of a run that went to one pod (P3.6).
type groupKey struct {
	run   string
	group string
	pod   string
}

// reservation is one call the parallel rule counted on its pinned pod. The call's end gives it back once, however
// often the end is seen.
type reservation struct {
	key  groupKey
	once sync.Once
}

// parallelVerdict is the parallel rule's decision on one call of a group whose run has a live pin (P3.6), passed from
// Filter to PreRequest and ResponseBody on the request.
type parallelVerdict struct {
	// reasonParallelStay or reasonParallelSpill.
	reason string
	// The call's x-parallel-group.
	group string
	// The pinned pod, its waiting queue, and the group's calls already sent to it and not ended, before this one.
	from     string
	queue    int
	inFlight int
	// The pods the call may go to.
	to []scheduling.Endpoint
	// The count this call holds on the pinned pod, on a stay; nil on a spill.
	held *reservation
}

// parallel counts, per run's group and pod, the group's calls the filter sent to that pod that have not ended (P3.6).
// The calls of one group reach the picker at almost the same moment, faster than the 50 ms metrics refresh shows
// them in the pod's queue, so the rule counts its own. Upstream's RunningRequestsSize is not used: it is read with
// the queue, and lags the same.
type parallel struct {
	mu sync.Mutex
	// A pinned pod has room for a group's call while its queue plus the group's calls in flight on it are at or under
	// this (routing.parallel_queue_threshold).
	threshold int
	counts    map[groupKey]int
}

func newParallel(threshold int) *parallel {
	return &parallel{threshold: threshold, counts: map[groupKey]int{}}
}

// check judges one call of a group of runID whose run is pinned to pinned, one of endpoints. Room on pinned: the call
// stays and is counted there. No room: the call may go only to the other pods whose queue is at or under the
// threshold; with none, it stays and is counted, since moving would lose the cache and gain no room. A spilled call
// is counted nowhere, since the filter does not know which pod the scorers pick, so the group's next call sees the
// pinned pod as the spilled one did: when a group's first call finds no room, its second spills too (P3.6's
// burst-fan-out replay: 4 groups of 2), and the two spilled calls see the same queues on the other pods. The check
// and the count are one step under one lock, so the calls of a group that arrive together each see the ones before.
// Counting here, not in PreRequest, leaves the count held by a call the scheduler drops after the filter. In
// llm-d-router v0.11.0, with this plugin the profile's only filter in its only profile (infra/routing-values.sh), a
// scheduling that gets at least one pod from the filter always reaches PreRequest (scheduling/scheduler_profile.go,
// the single profile handler's ProcessResults), and this filter never returns none; a filter added after it that
// returned none would leave the count held.
func (p *parallel) check(runID, group string, pinned scheduling.Endpoint,
	endpoints []scheduling.Endpoint) parallelVerdict {
	p.mu.Lock()
	defer p.mu.Unlock()
	from := pinned.GetMetadata().ID.Name
	key := groupKey{run: runID, group: group, pod: from}
	verdict := parallelVerdict{reason: reasonParallelStay, group: group, from: from, queue: queue(pinned),
		inFlight: p.counts[key]}
	if verdict.queue+verdict.inFlight > p.threshold {
		for _, endpoint := range endpoints {
			if endpoint.GetMetadata().ID.Name != from && queue(endpoint) <= p.threshold {
				verdict.to = append(verdict.to, endpoint)
			}
		}
		if len(verdict.to) > 0 {
			verdict.reason = reasonParallelSpill
			return verdict
		}
	}
	p.counts[key]++
	verdict.to = []scheduling.Endpoint{pinned}
	verdict.held = &reservation{key: key}
	return verdict
}

// release gives back the count a call held; a group whose calls have all ended leaves no entry.
func (p *parallel) release(held *reservation) {
	held.once.Do(func() {
		p.mu.Lock()
		defer p.mu.Unlock()
		p.counts[held.key]--
		if p.counts[held.key] <= 0 {
			delete(p.counts, held.key)
		}
	})
}
