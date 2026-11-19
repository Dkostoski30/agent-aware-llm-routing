package runrouter

import (
	"math/bits"
	"strconv"
	"time"
)

// priorityOf is the vLLM priority of a call that arrives at now (P4.1, D3): the arrival in Unix milliseconds plus the
// part of the aging its run has left, aging x (expected - step) / expected, rounded down and 0 once the run reaches its
// expected steps. vLLM's priority scheduling serves the lowest first, ties by arrival (v0.31.0,
// v1/core/sched/request_queue.py), so a run close to finishing goes ahead of calls that arrived up to the aging before
// it. The aging is the bound: a call's slack is at most the aging, so a call that arrives more than the aging after it
// has a higher priority before its own slack is added, and never overtakes it (card 57, decided 2026-11-19). A call
// without a run's progress (a missing or non-numeric x-step or x-expected-steps, or zero expected steps) gets the whole
// aging, as does step 0 by the formula: it can be overtaken but never overtakes.
func priorityOf(headers map[string]string, now time.Time, aging time.Duration) int64 {
	return now.UnixMilli() + int64(slackMillis(headers, uint64(aging.Milliseconds())))
}

// slackMillis is aging x (expected - step) / expected in whole milliseconds, through a 128-bit product: expected-steps
// is a header, and any 64-bit count must not overflow it. Steps left are at most expected, so the quotient is at most the
// aging, and the product's high word stays below expected, which Div64 requires.
func slackMillis(headers map[string]string, aging uint64) uint64 {
	step, stepErr := strconv.ParseUint(headers[headerStep], 10, 64)
	expected, expectedErr := strconv.ParseUint(headers[headerExpectedSteps], 10, 64)
	if stepErr != nil || expectedErr != nil || expected == 0 {
		return aging
	}
	high, low := bits.Mul64(aging, expected-min(step, expected))
	slack, _ := bits.Div64(high, low, expected)
	return slack
}
