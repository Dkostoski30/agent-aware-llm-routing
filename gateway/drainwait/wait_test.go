package drainwait

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

// fakeClock moves only when the waiter sleeps, so a test knows the time of every read.
type fakeClock struct {
	now   time.Time
	slept []time.Duration
}

func (c *fakeClock) Now() time.Time { return c.now }

func (c *fakeClock) Sleep(d time.Duration) {
	c.slept = append(c.slept, d)
	c.now = c.now.Add(d)
}

// metrics is a pod's /metrics text with one series of each count, as vLLM v0.31.0 and llm-d-inference-sim v1.0.0
// write them.
func metrics(running, waiting int) string {
	return fmt.Sprintf(`# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3-0.6B-GPTQ-Int8"} %d.0
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3-0.6B-GPTQ-Int8"} %d.0
`, running, waiting)
}

// reads hands out one reply per read, the last one again once they run out, and counts the reads.
type reads struct {
	replies []reply
	count   int
}

type reply struct {
	text string
	err  error
}

func (r *reads) read() (string, error) {
	i := min(r.count, len(r.replies)-1)
	r.count++
	return r.replies[i].text, r.replies[i].err
}

func open(running, waiting int) reply { return reply{text: metrics(running, waiting)} }

var start = time.Date(2026, 11, 19, 18, 0, 0, 0, time.UTC)

// The profile's drain: drain.timeout_seconds 120, drain.quiet_seconds 5, and the waiter's poll.
const (
	testTimeout = 120 * time.Second
	testQuiet   = 5 * time.Second
)

func run(t *testing.T, replies ...reply) (Result, *fakeClock, *reads) {
	t.Helper()
	clock := &fakeClock{now: start}
	source := &reads{replies: replies}
	result := Wait(source.read, clock, testTimeout, testQuiet, Poll)
	return result, clock, source
}

func TestAnEmptyPodIsIdleOnceTheQuietTimeHasPassed(t *testing.T) {
	result, clock, _ := run(t, open(0, 0))
	if result.Outcome != Idle {
		t.Fatalf("outcome %s, want idle", result.Outcome)
	}
	if got := clock.now.Sub(start); got != testQuiet {
		t.Fatalf("idle after %s, want the quiet time %s", got, testQuiet)
	}
}

func TestABusyPodIsIdleTheQuietTimeAfterItsLastCall(t *testing.T) {
	// Busy for the first 3 reads (0, 1 and 2 s), empty from 3 s on: idle at 3 + 5 s.
	result, clock, _ := run(t, open(2, 1), open(1, 0), open(0, 1), open(0, 0))
	if result.Outcome != Idle {
		t.Fatalf("outcome %s, want idle", result.Outcome)
	}
	if got := clock.now.Sub(start); got != 3*time.Second+testQuiet {
		t.Fatalf("idle after %s, want 8s", got)
	}
}

func TestACallInsideTheQuietTimeStartsItAgain(t *testing.T) {
	// Empty at 0-3 s, a call at 4 s, empty from 5 s on: idle at 5 + 5 s.
	result, clock, _ := run(t, open(0, 0), open(0, 0), open(0, 0), open(0, 0), open(1, 0), open(0, 0))
	if result.Outcome != Idle {
		t.Fatalf("outcome %s, want idle", result.Outcome)
	}
	if got := clock.now.Sub(start); got != 10*time.Second {
		t.Fatalf("idle after %s, want 10s", got)
	}
}

func TestAPodThatNeverEmptiesEndsAtTheTimeout(t *testing.T) {
	result, clock, _ := run(t, open(1, 0))
	if result.Outcome != Timeout {
		t.Fatalf("outcome %s, want timeout", result.Outcome)
	}
	if got := clock.now.Sub(start); got != testTimeout {
		t.Fatalf("ended after %s, want the timeout %s", got, testTimeout)
	}
	if result.Open != 1 {
		t.Fatalf("open %v at the timeout, want the last read's 1", result.Open)
	}
}

func TestAPodThatEmptiesExactlyAtTheTimeoutIsIdle(t *testing.T) {
	// Busy until 115 s, empty from 115 s: the quiet time ends at 120 s, the timeout, and the drain did its job.
	replies := make([]reply, 0, 116)
	for range 115 {
		replies = append(replies, open(1, 0))
	}
	replies = append(replies, open(0, 0))
	result, clock, _ := run(t, replies...)
	if result.Outcome != Idle {
		t.Fatalf("outcome %s, want idle", result.Outcome)
	}
	if got := clock.now.Sub(start); got != testTimeout {
		t.Fatalf("idle after %s, want 120s", got)
	}
}

func TestAFailedReadCountsAsBusy(t *testing.T) {
	// Empty at 0-3 s, a failed read at 4 s, empty from 5 s on: a pod too loaded to answer is not cut.
	failed := reply{err: errors.New("context deadline exceeded")}
	result, clock, _ := run(t, open(0, 0), open(0, 0), open(0, 0), open(0, 0), failed, open(0, 0))
	if result.Outcome != Idle {
		t.Fatalf("outcome %s, want idle", result.Outcome)
	}
	if got := clock.now.Sub(start); got != 10*time.Second {
		t.Fatalf("idle after %s, want 10s", got)
	}
}

func TestAPodThatNeverAnswersEndsAtTheTimeoutWithTheError(t *testing.T) {
	result, _, _ := run(t, reply{err: errors.New("connection refused")})
	if result.Outcome != Timeout {
		t.Fatalf("outcome %s, want timeout", result.Outcome)
	}
	if result.Err == nil || !strings.Contains(result.Err.Error(), "connection refused") {
		t.Fatalf("error %v, want the last read's", result.Err)
	}
}

func TestTheWaiterPollsAtItsInterval(t *testing.T) {
	_, clock, source := run(t, open(0, 0))
	for _, d := range clock.slept {
		if d != Poll {
			t.Fatalf("slept %s, want the poll %s", d, Poll)
		}
	}
	// Reads at 0, 1, ..., 5 s.
	if source.count != 6 {
		t.Fatalf("%d reads, want 6", source.count)
	}
}

func TestOpenCallsSumsRunningAndWaitingOverEverySeries(t *testing.T) {
	text := `vllm:num_requests_running{model_name="base"} 1
vllm:num_requests_running{model_name="adapter"} 2
vllm:num_requests_waiting{model_name="base"} 0
vllm:num_requests_waiting{model_name="adapter"} 3
vllm:num_requests_running_total 50
vllm:num_preemptions_total{model_name="base"} 7
`
	got, err := OpenCalls(text)
	if err != nil {
		t.Fatal(err)
	}
	if got != 6 {
		t.Fatalf("open %v, want 1+2+0+3", got)
	}
}

func TestOpenCallsReadsASeriesWithoutLabelsAndATimestamp(t *testing.T) {
	text := "vllm:num_requests_running 2 1763575200000\nvllm:num_requests_waiting 1\n"
	got, err := OpenCalls(text)
	if err != nil {
		t.Fatal(err)
	}
	if got != 3 {
		t.Fatalf("open %v, want 3", got)
	}
}

func TestOpenCallsRefusesTextWithoutBothCounts(t *testing.T) {
	for name, text := range map[string]string{
		"no running": "vllm:num_requests_waiting{model_name=\"base\"} 0\n",
		"no waiting": "vllm:num_requests_running{model_name=\"base\"} 0\n",
		"neither":    "# HELP up\n",
	} {
		if _, err := OpenCalls(text); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}

func TestOpenCallsRefusesASampleWithoutANumber(t *testing.T) {
	for name, running := range map[string]string{
		"a word":              `vllm:num_requests_running{model_name="base"} many`,
		"no value":            `vllm:num_requests_running{model_name="base"}`,
		"no labels, no value": `vllm:num_requests_running`,
		"labels unclosed":     `vllm:num_requests_running{model_name="base" 2`,
	} {
		if _, err := OpenCalls(running + "\nvllm:num_requests_waiting 0\n"); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}

func TestAPodWithoutTheCountsIsBusy(t *testing.T) {
	// A /metrics without the counts (a server that has not registered them) cannot show the pod empty.
	result, _, _ := run(t, reply{text: "# HELP up\n"})
	if result.Outcome != Timeout {
		t.Fatalf("outcome %s, want timeout", result.Outcome)
	}
}
