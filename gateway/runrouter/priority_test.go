package runrouter

import (
	"context"
	"math/rand/v2"
	"strconv"
	"testing"
	"time"

	fwkrh "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/requesthandling"
)

// The profiles' routing.priority_aging_seconds (P4.1).
const testAging = 30 * time.Second

func steps(step, expected string) map[string]string {
	return map[string]string{headerStep: step, headerExpectedSteps: expected}
}

func TestPriorityIsArrivalPlusTheAgingLeftByProgress(t *testing.T) {
	at := newFakeClock().at
	cases := []struct {
		name    string
		headers map[string]string
		aging   time.Duration
		// Milliseconds after the arrival.
		slack int64
	}{
		{"the first of ten steps", steps("1", "10"), testAging, 27000},
		{"halfway", steps("5", "10"), testAging, 15000},
		{"the last expected step", steps("10", "10"), testAging, 0},
		{"past the expected steps", steps("12", "10"), testAging, 0},
		{"a run of one step", steps("1", "1"), testAging, 0},
		{"a third done, rounded down", steps("1", "3"), testAging, 20000},
		{"a seventh done, rounded down", steps("1", "7"), testAging, 25714},
		{"no aging", steps("1", "10"), 0, 0},
		// The largest expected-steps a header can carry, where aging x steps left overflows 64 bits.
		{"expected steps at the top of 64 bits", steps("1", "18446744073709551615"), testAging, 29999},
		// A call without the run's progress has the most slack: it can be overtaken, never overtake.
		{"no headers", map[string]string{}, testAging, 30000},
		{"no step", map[string]string{headerExpectedSteps: "10"}, testAging, 30000},
		{"no expected steps", map[string]string{headerStep: "3"}, testAging, 30000},
		{"a step that is no number", steps("three", "10"), testAging, 30000},
		{"step zero", steps("0", "10"), testAging, 30000},
		{"a negative step", steps("-1", "10"), testAging, 30000},
		{"expected steps zero", steps("1", "0"), testAging, 30000},
		{"expected steps past 64 bits", steps("1", "18446744073709551616"), testAging, 30000},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got, want := priorityOf(c.headers, at, c.aging), at.UnixMilli()+c.slack; got != want {
				t.Fatalf("priority %d, want arrival %d + %d", got, at.UnixMilli(), c.slack)
			}
		})
	}
}

// The aging bound (card 57): a call is never overtaken by a call that arrives more than the aging time after it,
// whatever the two runs' progress. vLLM serves the lower priority first.
func TestNoCallIsOvertakenByOneArrivingMoreThanTheAgingLater(t *testing.T) {
	random := rand.New(rand.NewPCG(4, 1))
	start := newFakeClock().at
	header := func() map[string]string {
		if random.IntN(10) == 0 {
			return map[string]string{}
		}
		expected := 1 + random.IntN(40)
		return steps(strconv.Itoa(1+random.IntN(expected+5)), strconv.Itoa(expected))
	}
	for range 10000 {
		first := start.Add(time.Duration(random.Int64N(int64(time.Hour))))
		later := first.Add(testAging + time.Millisecond + time.Duration(random.Int64N(int64(time.Minute))))
		firstHeaders, laterHeaders := header(), header()
		if a, b := priorityOf(firstHeaders, first, testAging), priorityOf(laterHeaders, later, testAging); b <= a {
			t.Fatalf("%v at %s (priority %d) was overtaken by %v at %s (priority %d)", firstHeaders, first, a,
				laterHeaders, later, b)
		}
	}
}

// Within the aging time a run close to finishing goes first: what the rule is for.
func TestALastStepOvertakesAFirstStepThatArrivedWithinTheAging(t *testing.T) {
	at := newFakeClock().at
	early := priorityOf(steps("1", "10"), at, testAging)
	finisher := priorityOf(steps("10", "10"), at.Add(20*time.Second), testAging)
	if finisher >= early {
		t.Fatalf("the last step 20 s later got %d, the first step %d: want it lower, served first", finisher, early)
	}
}

func chatBody(fields map[string]any) *fwkrh.InferenceRequestBody {
	return &fwkrh.InferenceRequestBody{Payload: fwkrh.PayloadMap(fields), RawBody: []byte(`{"raw": true}`)}
}

func TestPriorityOnWritesTheBodysPriorityOnEveryCall(t *testing.T) {
	cases := []struct {
		name    string
		headers map[string]string
		slack   int64
	}{
		{"a run's call", map[string]string{headerRunID: "run-1", headerStep: "2", headerExpectedSteps: "4"}, 15000},
		// A call with no priority would be 0 to vLLM, ahead of every call the rule numbered.
		{"a call outside any run", map[string]string{}, 30000},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			clock := newFakeClock()
			router := newRouter(Flags{Priority: true}, clock)
			call := request(c.headers)
			call.Body = chatBody(map[string]any{"model": "m", "priority": float64(-5)})
			if err := router.PreRequest(context.Background(), call, resultOn("pod-a")); err != nil {
				t.Fatal(err)
			}
			payload := call.Body.Payload.(fwkrh.PayloadMap)
			if got, want := payload["priority"], clock.at.UnixMilli()+c.slack; got != want {
				t.Fatalf("priority %v (%T), want %d", got, got, want)
			}
			if payload["model"] != "m" {
				t.Fatalf("model %v, want m untouched", payload["model"])
			}
			if !call.Body.Mutated {
				t.Fatal("the body is not marked mutated, so the picker would forward the original bytes")
			}
		})
	}
}

func TestPriorityOffLeavesTheBodyAlone(t *testing.T) {
	router := newRouter(Flags{Affinity: true}, newFakeClock())
	call := request(map[string]string{headerRunID: "run-1", headerStep: "2", headerExpectedSteps: "4"})
	call.Body = chatBody(map[string]any{"model": "m"})
	if err := router.PreRequest(context.Background(), call, resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	if _, written := call.Body.Payload.(fwkrh.PayloadMap)["priority"]; written || call.Body.Mutated {
		t.Fatalf("payload %v, mutated %v: want it untouched", call.Body.Payload, call.Body.Mutated)
	}
}

func TestACallWhoseBodyCannotTakeAPrioritySaysSo(t *testing.T) {
	cases := []struct {
		name string
		body *fwkrh.InferenceRequestBody
	}{
		{"no body", nil},
		{"a raw body", &fwkrh.InferenceRequestBody{Payload: fwkrh.RawPayload(`{}`)}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ctx, lines := capture()
			router := newRouter(Flags{Priority: true}, newFakeClock())
			call := request(map[string]string{headerRunID: "run-1"})
			call.Body = c.body
			if err := router.PreRequest(ctx, call, resultOn("pod-a")); err != nil {
				t.Fatal(err)
			}
			found := false
			for _, line := range *lines {
				if line["msg"] == "Priority not written" && line["request-id"] == "request-1" {
					found = true
				}
			}
			if !found {
				t.Fatalf("lines %v, want a 'Priority not written' line for request-1", *lines)
			}
			if c.body != nil && c.body.Mutated {
				t.Fatal("a raw body was marked mutated")
			}
		})
	}
}

func TestTheRunsPickLineCarriesThePriority(t *testing.T) {
	ctx, lines := capture()
	clock := newFakeClock()
	router := newRouter(Flags{Priority: true}, clock)
	call := request(map[string]string{headerRunID: "run-1", headerStep: "4", headerExpectedSteps: "4"})
	call.Body = chatBody(map[string]any{"model": "m"})
	if err := router.PreRequest(ctx, call, resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	for _, line := range *lines {
		if line["msg"] == "Run call picked" {
			if got, want := line["priority"], float64(clock.at.UnixMilli()); got != want {
				t.Fatalf("priority %v in the line, want %v", got, want)
			}
			return
		}
	}
	t.Fatalf("lines %v, want a 'Run call picked' line", *lines)
}

func TestPriorityOffPutsNoPriorityInThePickLine(t *testing.T) {
	ctx, lines := capture()
	router := newRouter(Flags{Affinity: true}, newFakeClock())
	call := request(map[string]string{headerRunID: "run-1"})
	call.Body = chatBody(map[string]any{"model": "m"})
	if err := router.PreRequest(ctx, call, resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	for _, line := range *lines {
		if _, has := line["priority"]; has {
			t.Fatalf("line %v has a priority with the rule off", line)
		}
	}
}
