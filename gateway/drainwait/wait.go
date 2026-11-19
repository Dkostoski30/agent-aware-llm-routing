// Package drainwait holds a vLLM or simulated pod that is being removed until it has served the calls it holds (P4.3).
// It runs as the pod's preStop hook, so Kubernetes sends the server SIGTERM only once it returns: by then the picker
// has long dropped the pod from its pool (llm-d-router v0.11.0 does so at the pod's DeletionTimestamp), so no new call
// comes, and the calls already on it have ended.
package drainwait

import (
	"bufio"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Poll is how often the waiter reads the pod's /metrics: the resolution of the drain's end. One read costs
// milliseconds, so a finer poll would cost nothing but tell nothing either: a call lasts seconds (P2.9).
const Poll = time.Second

// The two counts vLLM v0.31.0 and llm-d-inference-sim v1.0.0 export, one series per model name (and engine).
const (
	runningMetric = "vllm:num_requests_running"
	waitingMetric = "vllm:num_requests_waiting"
)

// Outcome is how a drain ended.
type Outcome string

const (
	// Idle: the pod had no call for the whole quiet time.
	Idle Outcome = "idle"
	// Timeout: the timeout passed first, and the server's own shutdown deals with what is left.
	Timeout Outcome = "timeout"
)

// Result is a drain's outcome with the last read: its open calls, or its error when it failed.
type Result struct {
	Outcome Outcome
	Open    float64
	Err     error
}

// Clock is the time the waiter reads and sleeps on; tests give it one that moves only when it sleeps.
type Clock interface {
	Now() time.Time
	Sleep(time.Duration)
}

// Wait reads the pod's /metrics every poll until its running and waiting calls have been 0 for quiet, or timeout has
// passed since it began. A read that fails, or lacks a count, is busy: a pod too loaded to answer is not cut.
func Wait(read func() (string, error), clock Clock, timeout, quiet, poll time.Duration) Result {
	deadline := clock.Now().Add(timeout)
	var emptySince time.Time
	for {
		now := clock.Now()
		open, err := readOpen(read)
		if err == nil && open == 0 {
			if emptySince.IsZero() {
				emptySince = now
			}
			if now.Sub(emptySince) >= quiet {
				return Result{Outcome: Idle}
			}
		} else {
			emptySince = time.Time{}
		}
		if !now.Before(deadline) {
			return Result{Outcome: Timeout, Open: open, Err: err}
		}
		clock.Sleep(poll)
	}
}

func readOpen(read func() (string, error)) (float64, error) {
	text, err := read()
	if err != nil {
		return 0, err
	}
	return OpenCalls(text)
}

// OpenCalls is the sum of every running and waiting series in a Prometheus text exposition. Both counts must be there.
func OpenCalls(text string) (float64, error) {
	var total float64
	seen := map[string]bool{}
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, rest := splitName(line)
		if name != runningMetric && name != waitingMetric {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			return 0, fmt.Errorf("%s has no value: %q", name, line)
		}
		value, err := strconv.ParseFloat(fields[0], 64)
		if err != nil {
			return 0, fmt.Errorf("%s: %w", name, err)
		}
		total += value
		seen[name] = true
	}
	if err := scanner.Err(); err != nil {
		return 0, err
	}
	for _, name := range []string{runningMetric, waitingMetric} {
		if !seen[name] {
			return 0, fmt.Errorf("no %s in the pod's metrics", name)
		}
	}
	return total, nil
}

// splitName splits a sample line into its metric name and what follows its labels: the value and maybe a timestamp.
// Labels never closed leave the whole line after the name, which is no number.
func splitName(line string) (string, string) {
	if open := strings.IndexByte(line, '{'); open >= 0 {
		return line[:open], line[strings.LastIndexByte(line, '}')+1:]
	}
	name, rest, _ := strings.Cut(line, " ")
	return name, rest
}
