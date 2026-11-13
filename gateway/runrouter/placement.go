package runrouter

import "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"

// firstCallRule is how a first-call rule weighs the runs pinned to a pod (P3.7b): least work first by each run's
// remaining work, least context first by its latest call's body size.
type firstCallRule struct {
	// The reason PreRequest logs when the rule left one pod, and the name the log line gives the pods' loads.
	reason  string
	measure string
	// A pod whose load is at most this over the lightest pod's is level with it.
	slack int
	weigh func(progress) int
}

// leastWorkRule weighs a pinned run by its remaining work, max(expected - step, 1) as of its latest call (D2's count).
func leastWorkRule(slack int) firstCallRule {
	return firstCallRule{reason: reasonLeastWork, measure: "work", slack: slack,
		weigh: func(of progress) int { return of.work }}
}

// leastContextRule weighs a pinned run by its latest call's body size in bytes: the context its next call brings to
// its pod's KV cache, which on a real pod decides when a call may start (P3.7b, experiments/log.md). No slack: sizes in
// bytes are almost never equal, so the rule leaves one pod nearly always and pins the run in Filter, and runs that
// arrive together do not pile onto one pod.
func leastContextRule() firstCallRule {
	return firstCallRule{reason: reasonLeastContext, measure: "context", slack: 0,
		weigh: func(of progress) int { return of.size }}
}

// placement is a first-call rule's verdict on a run's first call, from Filter to PreRequest, which the picker hands
// the same request.
type placement struct {
	// The rule's reason and measure (firstCallRule).
	reason  string
	measure string
	// Each candidate's load before this run: the rule's weight of each live run pinned to it, added up.
	load map[string]int
	// The pod the run was pinned to in Filter, when it was the only candidate within the slack of the lightest; ""
	// when several were and the scorers chose among them.
	pod string
	// The candidates within the slack of the lightest, in their order: the only pods the call may go to.
	to []scheduling.Endpoint
}

// place judges a run's first call by rule. It returns false and changes nothing when the run has a live pin: only a
// run's first call is placed this way, and a run whose pod left the pool goes to the scorers as before (P3.4).
// Otherwise it offers the candidates whose load is at most the rule's slack over the lightest's. When that is one pod,
// the run is pinned there now with its own progress, under the lock the count was taken under, so a first call right
// after counts this run even before PreRequest has run for it: a burst sends 5 runs at once
// (replay.patterns.burst-fan-out). When several pods are level, the scorers choose and the pin is set in PreRequest,
// so first calls that arrive together then see the same loads, as they would on the default picker. Pins on pods
// that are no candidate are not counted: those pods are not offered.
func (p *pins) place(runID string, endpoints []scheduling.Endpoint, rule firstCallRule, own progress) (placement, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	if entry, ok := p.byRun[runID]; ok && p.live(entry.used, now) {
		return placement{}, false
	}
	verdict := placement{reason: rule.reason, measure: rule.measure, load: make(map[string]int, len(endpoints))}
	for _, endpoint := range endpoints {
		verdict.load[endpoint.GetMetadata().ID.Name] = 0
	}
	for _, entry := range p.byRun {
		if _, candidate := verdict.load[entry.pod]; candidate && p.live(entry.used, now) {
			verdict.load[entry.pod] += rule.weigh(entry.progress)
		}
	}
	lightest := 0
	for i, endpoint := range endpoints {
		if load := verdict.load[endpoint.GetMetadata().ID.Name]; i == 0 || load < lightest {
			lightest = load
		}
	}
	for _, endpoint := range endpoints {
		// The difference, not lightest + slack, which a slack near the largest int would wrap.
		if verdict.load[endpoint.GetMetadata().ID.Name]-lightest <= rule.slack {
			verdict.to = append(verdict.to, endpoint)
		}
	}
	if len(verdict.to) == 1 {
		verdict.pod = verdict.to[0].GetMetadata().ID.Name
		p.byRun[runID] = pin{pod: verdict.pod, used: now, progress: own}
	}
	return verdict, true
}
