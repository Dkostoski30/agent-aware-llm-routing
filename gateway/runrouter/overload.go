package runrouter

import (
	"sync"
	"time"

	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

// move is the overload guard's verdict on a call whose pinned pod is over the queue threshold while another pod is
// not (P3.5): a spill sends this one call elsewhere and keeps the pin; a remap moves the pin as well.
type move struct {
	// reasonSpill or reasonRemap.
	reason string
	// The pinned pod and its waiting queue.
	from  string
	queue int
	// How long since from's timer started: since a look first found it over the threshold, or since its last remap
	// restarted the timer.
	overloaded time.Duration
	// The candidates at or under the threshold: the only pods the call may go to.
	to []scheduling.Endpoint
}

// overload keeps, for each pod, since when every look at its queue found it over the threshold (P3.5). A look is one
// call's pass through the filter, which sees every candidate's queue as the picker last read it from the pod's
// metrics (every 50 ms, llm-d-router v0.11.0's --refresh-metrics-interval). A dip between two calls goes unseen: the
// timer is only as fine as the time between calls.
type overload struct {
	mu     sync.Mutex
	limits Limits
	now    func() time.Time
	// The pods over the threshold at their last look, and when a look first found them over.
	overSince map[string]time.Time
}

func newOverload(limits Limits, now func() time.Time) *overload {
	return &overload{limits: limits, now: now, overSince: map[string]time.Time{}}
}

// queue is the number of calls waiting on the endpoint: vLLM's vllm:num_requests_waiting, which on a real pod also
// holds the calls waiting for KV blocks. The picker gives every endpoint metrics, empty until its first read; nil is
// read as empty too, so the guard never moves a run on a number it does not have.
func queue(endpoint scheduling.Endpoint) int {
	metrics := endpoint.GetMetrics()
	if metrics == nil {
		return 0
	}
	return metrics.WaitingQueueSize
}

func (o *overload) over(endpoint scheduling.Endpoint) bool {
	return queue(endpoint) > o.limits.QueueThreshold
}

// check looks at every candidate's queue, then judges a call to pinned, which is nil for a call with no pinned
// candidate. It returns nil when the call stays where affinity sends it: no pin; the pinned pod at or under the
// threshold; or every other pod over it, where moving would lose the cache and gain no room. Otherwise a spill, or a
// remap once the pinned pod has been over the threshold for limits.Sustained. A remap restarts the pod's timer, so the
// runs pinned there leave one per window while spills carry the load, and they do not all land on one other pod at
// once. One lock over the look and the verdict, so two calls cannot both take one window's remap.
func (o *overload) check(pinned scheduling.Endpoint, endpoints []scheduling.Endpoint) *move {
	o.mu.Lock()
	defer o.mu.Unlock()
	now := o.now()
	o.look(endpoints, now)
	if pinned == nil || !o.over(pinned) {
		return nil
	}
	from := pinned.GetMetadata().ID.Name
	var to []scheduling.Endpoint
	for _, endpoint := range endpoints {
		if endpoint.GetMetadata().ID.Name != from && !o.over(endpoint) {
			to = append(to, endpoint)
		}
	}
	if len(to) == 0 {
		return nil
	}
	verdict := &move{reason: reasonSpill, from: from, queue: queue(pinned), overloaded: now.Sub(o.overSince[from]),
		to: to}
	if verdict.overloaded >= o.limits.Sustained {
		verdict.reason = reasonRemap
		o.overSince[from] = now
	}
	return verdict
}

// look starts the timer of every candidate over the threshold that has none, and drops the timer of every pod at or
// under it, or no longer a candidate. The filter is handed every pod of the pool (llm-d-router v0.11.0's
// requestcontrol/candidates.go: no caller sends a subset in the request's metadata, and thesis-plugins.yaml has no
// screener), so a pod missing from it has left (P3.4).
func (o *overload) look(endpoints []scheduling.Endpoint, now time.Time) {
	over := map[string]bool{}
	for _, endpoint := range endpoints {
		if !o.over(endpoint) {
			continue
		}
		name := endpoint.GetMetadata().ID.Name
		over[name] = true
		if _, ok := o.overSince[name]; !ok {
			o.overSince[name] = now
		}
	}
	for name := range o.overSince {
		if !over[name] {
			delete(o.overSince, name)
		}
	}
}
