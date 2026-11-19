// Package main is the drain waiter of P4.3, shipped in the picker's image so a vLLM or simulated pod can run it
// (docs/task-cards/59-P4.3-drain.md):
//
//	drainwait install PATH   copies this binary to PATH: the pod's init container, whose image has no cp
//	drainwait wait --metrics URL --timeout D --quiet D
//	                         the server container's preStop hook: returns once the pod's calls have ended, or at the
//	                         timeout (gateway/drainwait)
package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/Dkostoski30/agent-aware-llm-routing/gateway/drainwait"
)

// readTimeout bounds one /metrics read. A read slower than this counts as busy, as a failed one does, so it only keeps
// one stuck read from holding the waiter past its own timeout. 5 s: a pod's /metrics answers in milliseconds even
// under load; Prometheus reads it every 5 s (monitoring.scrape_interval_seconds).
const readTimeout = 5 * time.Second

func main() {
	os.Exit(run(os.Args[1:], os.Stderr))
}

// run returns the exit code: 0 once installed or once the drain ended, whichever way; 2 on a wrong command line or a
// failed install. A hook's exit code does not change the pod's termination, which goes on either way.
func run(args []string, stderr io.Writer) int {
	if len(args) == 0 {
		say(stderr, "usage: drainwait install PATH | drainwait wait --metrics URL --timeout D --quiet D")
		return 2
	}
	switch args[0] {
	case "install":
		if len(args) != 2 {
			say(stderr, "usage: drainwait install PATH")
			return 2
		}
		if err := install(args[1]); err != nil {
			say(stderr, err.Error())
			return 2
		}
		return 0
	case "wait":
		return wait(args[1:], stderr)
	default:
		say(stderr, fmt.Sprintf("drainwait: no command %q", args[0]))
		return 2
	}
}

func install(path string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	binary, err := os.ReadFile(self)
	if err != nil {
		return err
	}
	// Executable by the server container's user, which is not this one's in the vLLM pod (root there).
	return os.WriteFile(path, binary, 0o755)
}

func wait(args []string, stderr io.Writer) int {
	flags := flag.NewFlagSet("wait", flag.ContinueOnError)
	flags.SetOutput(stderr)
	metrics := flags.String("metrics", "", "the pod's own /metrics URL")
	timeout := flags.Duration("timeout", 0, "drain.timeout_seconds")
	quiet := flags.Duration("quiet", -1, "drain.quiet_seconds")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if *metrics == "" || *timeout <= 0 || *quiet < 0 || flags.NArg() != 0 {
		say(stderr, "usage: drainwait wait --metrics URL --timeout D --quiet D (timeout above 0, quiet 0 or more)")
		return 2
	}
	client := &http.Client{Timeout: readTimeout}
	result := drainwait.Wait(func() (string, error) { return get(client, *metrics) }, systemClock{}, *timeout, *quiet,
		drainwait.Poll)
	// Kubernetes shows a hook's output only when the hook fails; this line is for a run by hand.
	say(stderr, fmt.Sprintf("drainwait: %s, open %v, error %v", result.Outcome, result.Open, result.Err))
	return 0
}

func get(client *http.Client, url string) (string, error) {
	response, err := client.Get(url)
	if err != nil {
		return "", err
	}
	body, err := io.ReadAll(response.Body)
	if closeErr := response.Body.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return "", err
	}
	if response.StatusCode != http.StatusOK {
		return "", errors.New(response.Status)
	}
	return string(body), nil
}

// say writes one line for whoever runs the waiter. A failed write is not acted on: stderr is the only place left to
// report it, and the exit code already says how the command ended.
func say(stderr io.Writer, line string) {
	_, _ = fmt.Fprintln(stderr, line)
}

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

func (systemClock) Sleep(d time.Duration) { time.Sleep(d) }
