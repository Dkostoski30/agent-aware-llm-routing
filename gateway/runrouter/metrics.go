package runrouter

import (
	"sync"

	"github.com/prometheus/client_golang/prometheus"
)

// decisions counts the calls of a run the picker placed, by PreRequest's reason (P3.5, P3.6, P3.7b), with affinity on: off, a
// call has no reason. It is on controller-runtime's registry, which llm-d-router v0.11.0's runner serves on the
// picker's metrics port (9090) beside its own metrics.
var decisions = prometheus.NewCounterVec(prometheus.CounterOpts{
	Name: "thesis_picker_decisions_total",
	Help: "Calls of a run placed by the thesis-run-router, by decision: first, pinned, remapped (its pod left), " +
		"spill (one call off an overloaded pod), remap (the run moved off a pod overloaded too long), parallel-stay " +
		"(a parallel group's call on its run's pod), parallel-spill (one off it, which had no room), least-work (a " +
		"run's first call on the one pod with the least pinned work left) and least-context (on the one pod with the " +
		"smallest pinned context).",
}, []string{"decision"})

// registerDecisions guards the counter's one registration: the registry refuses a second, and Register may run more
// than once in one process, as the tests run it.
var registerDecisions sync.Once
