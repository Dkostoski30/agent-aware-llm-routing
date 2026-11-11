package runrouter

import (
	"sync"
	"time"
)

// pin is the pod a run's calls go to, and when a call of the run last went there.
type pin struct {
	pod  string
	used time.Time
}

// pins maps each active run to its pod (P3.3). They live in memory in the one picker pod: a picker restart forgets
// every pin, and runs fall back to load and prefix, a stated limitation (docs/task-cards/38-P3.3-task-affinity.md).
type pins struct {
	mu sync.Mutex
	// How long a pin outlives the run's last call, for a run whose end of run never arrives.
	ttl   time.Duration
	now   func() time.Time
	byRun map[string]pin
}

func newPins(ttl time.Duration, now func() time.Time) *pins {
	return &pins{ttl: ttl, now: now, byRun: map[string]pin{}}
}

// live reports whether a pin last used at used still holds at now.
func (p *pins) live(used, now time.Time) bool {
	return now.Sub(used) <= p.ttl
}

// pod returns the pod the run is pinned to, or "" when it has no pin or its pin outlived the TTL.
func (p *pins) pod(runID string) string {
	p.mu.Lock()
	defer p.mu.Unlock()
	entry, ok := p.byRun[runID]
	if !ok || !p.live(entry.used, p.now()) {
		return ""
	}
	return entry.pod
}

// record pins the run to pod as of now and returns the pod it was pinned to before, "" if it had no live pin. It
// also drops every pin that outlived the TTL, so the runs whose end never arrived do not pile up: one pass over the
// active runs, a few dozen, per call.
func (p *pins) record(runID, pod string) string {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	before := ""
	if entry, ok := p.byRun[runID]; ok && p.live(entry.used, now) {
		before = entry.pod
	}
	for id, entry := range p.byRun {
		if !p.live(entry.used, now) {
			delete(p.byRun, id)
		}
	}
	p.byRun[runID] = pin{pod: pod, used: now}
	return before
}

// end removes the run's pin and reports whether it had a live one.
func (p *pins) end(runID string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	entry, ok := p.byRun[runID]
	delete(p.byRun, runID)
	return ok && p.live(entry.used, p.now())
}
