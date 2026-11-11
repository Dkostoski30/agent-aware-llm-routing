package runrouter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/go-logr/logr"
	"github.com/go-logr/logr/funcr"
	"github.com/prometheus/client_golang/prometheus"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/log"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"

	fwkdl "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/datalayer"
	fwkplugin "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/plugin"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

// The rules infra/routing-values.sh writes with every rule off.
const allOffRules = `"affinity": false, "overloadGuard": false, "parallelSpill": false, "priority": false, "warmup": false`

// The overload limits infra/routing-values.sh writes from every profile (testLimits).
const overloadJSON = `"overloadQueueThreshold": 1, "overloadSustainedSeconds": 10`

// parametersJSON is the plugin's config as infra/routing-values.sh writes it: the rules, the profile's
// routing.run_ttl_seconds and overload limits, and the end-of-run port.
func parametersJSON(rules string, port int) string {
	return fmt.Sprintf(`{%s, "runTTLSeconds": 300, %s, "endOfRunPort": %d}`, rules, overloadJSON, port)
}

// freePort is a port nothing listens on, for a factory that opens the end-of-run server. Each test takes its own, so
// a server another test is still closing never holds it.
func freePort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	return port
}

// testHandle gives the factory a context, the only part of plugin.Handle it uses. The embedded interface is nil, so a
// call to any other method fails the test with a panic.
type testHandle struct {
	fwkplugin.Handle
	ctx context.Context
}

func (h testHandle) Context() context.Context { return h.ctx }

// capture returns a context whose logger keeps every line at the picker's verbosity (--v=3 in
// infra/helm/router-values.yaml), and the lines kept.
func capture() (context.Context, *[]map[string]any) {
	lines := &[]map[string]any{}
	logger := funcr.NewJSON(func(obj string) {
		line := map[string]any{}
		if err := json.Unmarshal([]byte(obj), &line); err != nil {
			panic(err)
		}
		*lines = append(*lines, line)
	}, funcr.Options{Verbosity: 3})
	return log.IntoContext(context.Background(), logger), lines
}

// newRouter builds the plugin without the factory, so no port is opened, on a clock the test moves.
func newRouter(flags Flags, clock *fakeClock) *RunRouter {
	return newRunRouter(fwkplugin.TypedName{Type: Type, Name: "thesis"}, flags, testTTL, testLimits, clock.now,
		logr.Discard())
}

func endpoint(name string) scheduling.Endpoint {
	return scheduling.NewEndpoint(&fwkdl.EndpointMetadata{ID: k8stypes.NamespacedName{Namespace: "default", Name: name}},
		&fwkdl.Metrics{}, nil)
}

func endpoints(names []string) []scheduling.Endpoint {
	out := make([]scheduling.Endpoint, len(names))
	for i, name := range names {
		out[i] = endpoint(name)
	}
	return out
}

func names(endpoints []scheduling.Endpoint) []string {
	out := make([]string, len(endpoints))
	for i, e := range endpoints {
		out[i] = e.GetMetadata().ID.Name
	}
	return out
}

func request(headers map[string]string) *scheduling.InferenceRequest {
	return &scheduling.InferenceRequest{RequestID: "request-1", Headers: headers}
}

func resultOn(pod string) *scheduling.SchedulingResult {
	return &scheduling.SchedulingResult{
		PrimaryProfileName: "default",
		ProfileResults: map[string]*scheduling.ProfileRunResult{
			"default": {TargetEndpoints: []scheduling.Endpoint{endpoint(pod)}},
		},
	}
}

// endRun sends POST /runs/{id}/end to the plugin's handler, as the agent runtime and the replayer do.
func endRun(t *testing.T, router *RunRouter, runID string) {
	t.Helper()
	recorder := httptest.NewRecorder()
	router.handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/runs/"+runID+"/end", nil))
	if recorder.Code != http.StatusNoContent {
		t.Fatalf("end of %s: status %d, want 204", runID, recorder.Code)
	}
}

func TestFilterWithEveryRuleOffReturnsItsInputInOrder(t *testing.T) {
	run := map[string]string{headerRunID: "run-1", headerStep: "2"}
	cases := []struct {
		name      string
		headers   map[string]string
		endpoints []string
	}{
		{"no endpoints", run, []string{}},
		{"one endpoint", run, []string{"pod-a"}},
		{"three endpoints keep their order", run, []string{"pod-c", "pod-a", "pod-b"}},
		{"a request with no x-run-id is left alone", map[string]string{}, []string{"pod-b", "pod-a"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			input := endpoints(tc.endpoints)
			got := newRouter(Flags{}, newFakeClock()).Filter(context.Background(), request(tc.headers), input)
			if strings.Join(names(got), ",") != strings.Join(tc.endpoints, ",") {
				t.Fatalf("got %v, want %v", names(got), tc.endpoints)
			}
			for i := range got {
				if got[i] != input[i] {
					t.Fatalf("endpoint %d is not the one passed in", i)
				}
			}
		})
	}
}

// event is one call through the picker, or the end of a run, in a sequence the affinity rule sees.
type event struct {
	// The fake clock moves this far first.
	advance time.Duration
	// "" sends a call with no x-run-id.
	runID string
	// POST /runs/{runID}/end instead of a call.
	end bool
	// The candidates the filter gets.
	pods []string
	// What the filter must return.
	want []string
	// The scorers' choice among want, which PreRequest gets.
	pick string
	// The reason PreRequest must log; "" means no line.
	reason string
	// The pod a remapped run was pinned to, which the line must name; "" means the line has no from.
	from string
}

func TestAffinityPinsLaterStepsToTheFirstStepsPod(t *testing.T) {
	both := []string{"pod-a", "pod-b"}
	cases := []struct {
		name   string
		events []event
	}{
		{"the first step is unpinned", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
		}},
		{"later steps are pinned", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{runID: "run-1", pods: both, want: []string{"pod-b"}, pick: "pod-b", reason: reasonPinned},
			{runID: "run-1", pods: both, want: []string{"pod-b"}, pick: "pod-b", reason: reasonPinned},
		}},
		{"the TTL's expiry unpins", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{advance: testTTL + time.Second, runID: "run-1", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
			{runID: "run-1", pods: both, want: []string{"pod-a"}, pick: "pod-a", reason: reasonPinned},
		}},
		{"each call renews the TTL", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{advance: testTTL - time.Second, runID: "run-1", pods: both, want: []string{"pod-b"}, pick: "pod-b",
				reason: reasonPinned},
			{advance: testTTL - time.Second, runID: "run-1", pods: both, want: []string{"pod-b"}, pick: "pod-b",
				reason: reasonPinned},
		}},
		{"the end of the run removes the pin", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{runID: "run-1", end: true},
			{runID: "run-1", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
		}},
		{"another run is untouched", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{runID: "run-2", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
			{runID: "run-2", end: true},
			{runID: "run-1", pods: both, want: []string{"pod-b"}, pick: "pod-b", reason: reasonPinned},
		}},
		{"a request with no x-run-id passes through and pins nothing", []event{
			{pods: both, want: both, pick: "pod-a"},
			{pods: both, want: both, pick: "pod-b"},
		}},
		{"a pin on a pod that is no candidate passes every candidate and moves the pin", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{runID: "run-1", pods: []string{"pod-a", "pod-c"}, want: []string{"pod-a", "pod-c"}, pick: "pod-c",
				reason: reasonRemapped, from: "pod-b"},
			{runID: "run-1", pods: []string{"pod-a", "pod-c"}, want: []string{"pod-c"}, pick: "pod-c",
				reason: reasonPinned},
		}},
		{"a remapped run stays on its new pod when the old one is back", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
			{runID: "run-1", pods: []string{"pod-b"}, want: []string{"pod-b"}, pick: "pod-b", reason: reasonRemapped,
				from: "pod-a"},
			{runID: "run-1", pods: both, want: []string{"pod-b"}, pick: "pod-b", reason: reasonPinned},
		}},
		{"an expired pin on a pod that left is a first pick", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
			{advance: testTTL + time.Second, runID: "run-1", pods: []string{"pod-b"}, want: []string{"pod-b"},
				pick: "pod-b", reason: reasonFirst},
		}},
		{"an ended run whose pod left is a first pick", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
			{runID: "run-1", end: true},
			{runID: "run-1", pods: []string{"pod-b"}, want: []string{"pod-b"}, pick: "pod-b", reason: reasonFirst},
		}},
		{"a run pinned to the pod that stays is untouched by another's remap", []event{
			{runID: "run-1", pods: both, want: both, pick: "pod-a", reason: reasonFirst},
			{runID: "run-2", pods: both, want: both, pick: "pod-b", reason: reasonFirst},
			{runID: "run-1", pods: []string{"pod-b"}, want: []string{"pod-b"}, pick: "pod-b", reason: reasonRemapped,
				from: "pod-a"},
			{runID: "run-2", pods: []string{"pod-b"}, want: []string{"pod-b"}, pick: "pod-b", reason: reasonPinned},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			router := newRouter(Flags{Affinity: true}, clock)
			for i, e := range tc.events {
				clock.advance(e.advance)
				if e.end {
					endRun(t, router, e.runID)
					continue
				}
				headers := map[string]string{}
				if e.runID != "" {
					headers = map[string]string{headerRunID: e.runID, headerStep: fmt.Sprint(i + 1)}
				}
				call := request(headers)
				input := endpoints(e.pods)
				got := router.Filter(context.Background(), call, input)
				if strings.Join(names(got), ",") != strings.Join(e.want, ",") {
					t.Fatalf("event %d: filter returned %v, want %v", i, names(got), e.want)
				}
				ctx, lines := capture()
				if err := router.PreRequest(ctx, call, resultOn(e.pick)); err != nil {
					t.Fatalf("event %d: %v", i, err)
				}
				if e.reason == "" {
					if len(*lines) != 0 {
						t.Fatalf("event %d: got lines %v, want none", i, *lines)
					}
					continue
				}
				if len(*lines) != 1 {
					t.Fatalf("event %d: got %d lines, want 1: %v", i, len(*lines), *lines)
				}
				line := (*lines)[0]
				if line["run-id"] != e.runID || line["pod"] != e.pick || line["reason"] != e.reason {
					t.Fatalf("event %d: line %v, want run-id %s, pod %s, reason %s", i, line, e.runID, e.pick, e.reason)
				}
				from, hasFrom := line["from"]
				if (e.from == "" && hasFrom) || (e.from != "" && from != e.from) {
					t.Fatalf("event %d: line %v, want from %q", i, line, e.from)
				}
			}
		})
	}
}

func TestAffinityFilterReturnsThePinnedEndpointPassedIn(t *testing.T) {
	router := newRouter(Flags{Affinity: true}, newFakeClock())
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1"})
	if err := router.PreRequest(context.Background(), call, resultOn("pod-b")); err != nil {
		t.Fatal(err)
	}
	input := endpoints([]string{"pod-a", "pod-b"})
	got := router.Filter(context.Background(), call, input)
	if len(got) != 1 || got[0] != input[1] {
		t.Fatalf("got %v, want the pod-b endpoint passed in", names(got))
	}
}

func TestAffinityOffKeepsNoPin(t *testing.T) {
	router := newRouter(Flags{}, newFakeClock())
	both := []string{"pod-a", "pod-b"}
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1"})
	for i := range 2 {
		got := router.Filter(context.Background(), call, endpoints(both))
		if strings.Join(names(got), ",") != strings.Join(both, ",") {
			t.Fatalf("call %d: got %v, want %v", i, names(got), both)
		}
		ctx, lines := capture()
		if err := router.PreRequest(ctx, call, resultOn("pod-b")); err != nil {
			t.Fatal(err)
		}
		if _, ok := (*lines)[0]["reason"]; ok {
			t.Fatalf("call %d: line %v has a reason with affinity off", i, (*lines)[0])
		}
	}
	if len(router.pins.byRun) != 0 {
		t.Fatalf("pins kept with affinity off: %v", router.pins.byRun)
	}
}

func TestEndOfRunAnswers204WithOrWithoutAPin(t *testing.T) {
	router := newRouter(Flags{Affinity: true}, newFakeClock())
	endRun(t, router, "never-seen")
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1"})
	if err := router.PreRequest(context.Background(), call, resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	endRun(t, router, "run-1")
	endRun(t, router, "run-1")
}

func TestEndOfRunTakesOnlyPostOnItsPath(t *testing.T) {
	cases := []struct {
		name   string
		method string
		path   string
		want   int
	}{
		{"GET on the path", http.MethodGet, "/runs/run-1/end", http.StatusMethodNotAllowed},
		{"another path", http.MethodPost, "/runs/run-1", http.StatusNotFound},
		// ServeMux cleans the double slash and redirects to /runs/end, which matches nothing.
		{"no run id", http.MethodPost, "/runs//end", http.StatusTemporaryRedirect},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router := newRouter(Flags{Affinity: true}, newFakeClock())
			recorder := httptest.NewRecorder()
			router.handler().ServeHTTP(recorder, httptest.NewRequest(tc.method, tc.path, nil))
			if recorder.Code != tc.want {
				t.Fatalf("status %d, want %d", recorder.Code, tc.want)
			}
		})
	}
}

func TestEndOfRunLogsTheRunUnderRunID(t *testing.T) {
	lines := []map[string]any{}
	logger := funcr.NewJSON(func(obj string) {
		line := map[string]any{}
		if err := json.Unmarshal([]byte(obj), &line); err != nil {
			panic(err)
		}
		lines = append(lines, line)
	}, funcr.Options{Verbosity: 3})
	router := newRunRouter(fwkplugin.TypedName{Type: Type, Name: "thesis"}, Flags{Affinity: true}, testTTL,
		testLimits, newFakeClock().now, logger)
	if err := router.PreRequest(context.Background(), request(map[string]string{headerRunID: "run-1"}),
		resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	endRun(t, router, "run-1")
	if len(lines) != 1 || lines[0]["run-id"] != "run-1" || lines[0]["pinned"] != true {
		t.Fatalf("lines %v, want one with run-id run-1 and pinned true", lines)
	}
}

func TestFactoryReadsEveryParameter(t *testing.T) {
	port := freePort(t)
	raw := fmt.Sprintf(`{"affinity": true, "overloadGuard": false, "parallelSpill": true, "priority": false,
		"warmup": true, "runTTLSeconds": 120, "overloadQueueThreshold": 3, "overloadSustainedSeconds": 20,
		"endOfRunPort": %d}`, port)
	built, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(raw)), testHandle{ctx: t.Context()})
	if err != nil {
		t.Fatal(err)
	}
	router := built.(*RunRouter)
	want := Flags{Affinity: true, OverloadGuard: false, ParallelSpill: true, Priority: false, Warmup: true}
	if router.flags != want {
		t.Fatalf("got %+v, want %+v", router.flags, want)
	}
	if router.pins.ttl != 120*time.Second {
		t.Fatalf("TTL %s, want 120s", router.pins.ttl)
	}
	if want := (Limits{QueueThreshold: 3, Sustained: 20 * time.Second}); router.overload.limits != want {
		t.Fatalf("overload limits %+v, want %+v", router.overload.limits, want)
	}
}

func TestFactoryServesTheEndOfRunOnItsPort(t *testing.T) {
	port := freePort(t)
	raw := parametersJSON(allOffRules, port)
	if _, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(raw)), testHandle{ctx: t.Context()}); err != nil {
		t.Fatal(err)
	}
	response, err := http.Post(fmt.Sprintf("http://127.0.0.1:%d/runs/run-1/end", port), "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := response.Body.Close(); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("status %d, want 204", response.StatusCode)
	}
}

// closeWait bounds how long the server may take to close after its context ends: it closes from its own goroutine.
const closeWait = 5 * time.Second

func TestTheEndOfRunServerClosesWithTheHandlesContext(t *testing.T) {
	port := freePort(t)
	ctx, cancel := context.WithCancel(t.Context())
	raw := parametersJSON(allOffRules, port)
	if _, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(raw)), testHandle{ctx: ctx}); err != nil {
		t.Fatal(err)
	}
	address := fmt.Sprintf("127.0.0.1:%d", port)
	connection, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatalf("server not up before the context ended: %v", err)
	}
	if err := connection.Close(); err != nil {
		t.Fatal(err)
	}
	cancel()
	deadline := time.Now().Add(closeWait)
	for {
		connection, err := net.Dial("tcp", address)
		if err != nil {
			return
		}
		if err := connection.Close(); err != nil {
			t.Fatal(err)
		}
		if time.Now().After(deadline) {
			t.Fatalf("port %d still open %s after the context ended", port, closeWait)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestFactoryRefusesAPortInUse(t *testing.T) {
	taken, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := taken.Close(); err != nil {
			t.Fatal(err)
		}
	}()
	raw := parametersJSON(allOffRules, taken.Addr().(*net.TCPAddr).Port)
	_, err = Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(raw)), testHandle{ctx: t.Context()})
	if err == nil || !strings.Contains(err.Error(), "end-of-run") {
		t.Fatalf("error %v, want one naming the end-of-run port", err)
	}
}

func TestFactoryRefusesParametersItCannotTrust(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		// A part of the error, so the refusal names what was wrong.
		says string
	}{
		{"no parameters", ``, "parameters"},
		{"an unknown rule", parametersJSON(allOffRules+`, "afinity": true`, 9010), "afinity"},
		{"a missing rule", `{"affinity": false, "overloadGuard": false, "parallelSpill": false, "priority": false,
			"runTTLSeconds": 300, "endOfRunPort": 9010}`, "warmup"},
		{"a rule that is not a bool", parametersJSON(`"affinity": "yes", "overloadGuard": false, "parallelSpill": false,
			"priority": false, "warmup": false`, 9010), "affinity"},
		{"null for a rule", parametersJSON(`"affinity": null, "overloadGuard": false, "parallelSpill": false,
			"priority": false, "warmup": false`, 9010), "affinity"},
		{"no TTL", fmt.Sprintf(`{%s, "endOfRunPort": 9010}`, allOffRules), "runTTLSeconds"},
		{"a TTL of zero", fmt.Sprintf(`{%s, "runTTLSeconds": 0, "endOfRunPort": 9010}`, allOffRules), "runTTLSeconds"},
		{"a negative TTL", fmt.Sprintf(`{%s, "runTTLSeconds": -1, "endOfRunPort": 9010}`, allOffRules),
			"runTTLSeconds"},
		{"a TTL that is not whole seconds", fmt.Sprintf(`{%s, "runTTLSeconds": 1.5, "endOfRunPort": 9010}`,
			allOffRules), "runTTLSeconds"},
		// One second more than a time.Duration holds, which would wrap to a negative TTL.
		{"a TTL too long for a duration", fmt.Sprintf(`{%s, "runTTLSeconds": 9223372037, "endOfRunPort": 9010}`,
			allOffRules), "runTTLSeconds"},
		{"no port", fmt.Sprintf(`{%s, "runTTLSeconds": 300}`, allOffRules), "endOfRunPort"},
		{"port zero", parametersJSON(allOffRules, 0), "endOfRunPort"},
		{"a port past 65535", parametersJSON(allOffRules, 65536), "endOfRunPort"},
		{"no queue threshold", overloadParameters(`"overloadSustainedSeconds": 10`), "overloadQueueThreshold"},
		{"a negative queue threshold", overloadParameters(`"overloadQueueThreshold": -1, "overloadSustainedSeconds": 10`),
			"overloadQueueThreshold"},
		{"a queue threshold that is not a whole number",
			overloadParameters(`"overloadQueueThreshold": 1.5, "overloadSustainedSeconds": 10`), "overloadQueueThreshold"},
		{"no sustained time", overloadParameters(`"overloadQueueThreshold": 1`), "overloadSustainedSeconds"},
		{"a sustained time of zero", overloadParameters(`"overloadQueueThreshold": 1, "overloadSustainedSeconds": 0`),
			"overloadSustainedSeconds"},
		{"a negative sustained time",
			overloadParameters(`"overloadQueueThreshold": 1, "overloadSustainedSeconds": -1`), "overloadSustainedSeconds"},
		{"a sustained time that is not whole seconds",
			overloadParameters(`"overloadQueueThreshold": 1, "overloadSustainedSeconds": 2.5`), "overloadSustainedSeconds"},
		// One second more than a time.Duration holds.
		{"a sustained time too long for a duration",
			overloadParameters(`"overloadQueueThreshold": 1, "overloadSustainedSeconds": 9223372037`),
			"overloadSustainedSeconds"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(tc.raw)), testHandle{ctx: t.Context()})
			if err == nil {
				t.Fatalf("factory accepted %s", tc.raw)
			}
			if !strings.Contains(err.Error(), tc.says) {
				t.Fatalf("error %q does not name %q", err, tc.says)
			}
		})
	}
}

// overloadParameters is a config with every other parameter valid and the given overload limits.
func overloadParameters(limits string) string {
	return fmt.Sprintf(`{%s, "runTTLSeconds": 300, %s, "endOfRunPort": 9010}`, allOffRules, limits)
}

func TestFactoryLogsItsParametersOnce(t *testing.T) {
	ctx, lines := capture()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	raw := parametersJSON(allOffRules, freePort(t))
	if _, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(raw)), testHandle{ctx: ctx}); err != nil {
		t.Fatal(err)
	}
	if len(*lines) != 1 {
		t.Fatalf("got %d lines, want 1: %v", len(*lines), *lines)
	}
	line := (*lines)[0]
	if line["type"] != Type || line["affinity"] != false || line["warmup"] != false ||
		line["runTTLSeconds"] != float64(300) || line["overloadQueueThreshold"] != float64(1) ||
		line["overloadSustainedSeconds"] != float64(10) {
		t.Fatalf("line %v lacks the type, the flags, the TTL or the overload limits", line)
	}
}

func TestPreRequestLogsThePickUnderRunID(t *testing.T) {
	ctx, lines := capture()
	headers := map[string]string{headerRunID: "run-1", headerStep: "2"}
	if err := newRouter(Flags{}, newFakeClock()).PreRequest(ctx, request(headers), resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	if len(*lines) != 1 {
		t.Fatalf("got %d lines, want 1: %v", len(*lines), *lines)
	}
	line := (*lines)[0]
	if line["run-id"] != "run-1" || line["step"] != "2" || line["pod"] != "pod-a" {
		t.Fatalf("line %v lacks run-id, step or pod", line)
	}
}

func TestPreRequestLogsNothingForARequestWithNoRunID(t *testing.T) {
	cases := []struct {
		name    string
		headers map[string]string
	}{
		{"no x-run-id", map[string]string{}},
		{"an empty x-run-id", map[string]string{headerRunID: ""}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ctx, lines := capture()
			router := newRouter(Flags{Affinity: true}, newFakeClock())
			if err := router.PreRequest(ctx, request(tc.headers), resultOn("pod-a")); err != nil {
				t.Fatal(err)
			}
			if len(*lines) != 0 || len(router.pins.byRun) != 0 {
				t.Fatalf("got lines %v and pins %v, want none", *lines, router.pins.byRun)
			}
		})
	}
}

func TestPreRequestLogsNothingForAResultWithNoTarget(t *testing.T) {
	ctx, lines := capture()
	headers := map[string]string{headerRunID: "run-1", headerStep: "1"}
	empty := &scheduling.SchedulingResult{PrimaryProfileName: "default", ProfileResults: map[string]*scheduling.ProfileRunResult{}}
	router := newRouter(Flags{Affinity: true}, newFakeClock())
	if err := router.PreRequest(ctx, request(headers), empty); err != nil {
		t.Fatal(err)
	}
	if len(*lines) != 0 || len(router.pins.byRun) != 0 {
		t.Fatalf("got lines %v and pins %v, want none", *lines, router.pins.byRun)
	}
}

func TestPreRequestLogsNothingBelowThePickersVerbosity(t *testing.T) {
	lines := 0
	quiet := funcr.New(func(_, _ string) { lines++ }, funcr.Options{Verbosity: 2})
	ctx := log.IntoContext(context.Background(), quiet)
	headers := map[string]string{headerRunID: "run-1", headerStep: "1"}
	if err := newRouter(Flags{}, newFakeClock()).PreRequest(ctx, request(headers), resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	if lines != 0 {
		t.Fatalf("got %d lines at verbosity 2, want none", lines)
	}
}

func TestRegisterAddsThePluginAsBeta(t *testing.T) {
	Register()
	if _, ok := fwkplugin.Registry[Type]; !ok {
		t.Fatalf("%s is not registered", Type)
	}
	if got := fwkplugin.GetPluginStability(Type); got != fwkplugin.StabilityBeta {
		t.Fatalf("stability %s, want Beta: Alpha needs --allow-experimental-plugins", got)
	}
}

// guardEvent is one call of run-1 through the filter and PreRequest with the overload guard on.
type guardEvent struct {
	// The fake clock moves this far first.
	advance time.Duration
	pods    []queued
	// What the filter must return.
	want []string
	// The scorers' choice among want, which PreRequest gets.
	pick string
	// The reason PreRequest must log, and the pod the line must name as from; "" means none.
	reason string
	from   string
	// The pinned pod's queue on a spill or a remap.
	queue int
	// How long the pinned pod was over the threshold, on a remap.
	overloaded time.Duration
	// The pod run-1 is pinned to after the call.
	pinAfter string
}

func TestOverloadGuardSpillsAndRemapsPinnedRuns(t *testing.T) {
	calm := []queued{{"pod-a", 0}, {"pod-b", 0}}
	over := []queued{{"pod-a", 2}, {"pod-b", 0}}
	first := guardEvent{pods: calm, want: []string{"pod-a", "pod-b"}, pick: "pod-a", reason: reasonFirst,
		pinAfter: "pod-a"}
	spill := guardEvent{pods: over, want: []string{"pod-b"}, pick: "pod-b", reason: reasonSpill, from: "pod-a",
		queue: 2, pinAfter: "pod-a"}
	cases := []struct {
		name   string
		flags  Flags
		events []guardEvent
	}{
		{"a spike spills one call, keeps the pin, and the next call returns", Flags{Affinity: true, OverloadGuard: true},
			[]guardEvent{
				first,
				spill,
				{advance: time.Second, pods: calm, want: []string{"pod-a"}, pick: "pod-a", reason: reasonPinned,
					pinAfter: "pod-a"},
			}},
		{"sustained overload moves the pin", Flags{Affinity: true, OverloadGuard: true}, []guardEvent{
			first,
			spill,
			{advance: testLimits.Sustained, pods: over, want: []string{"pod-b"}, pick: "pod-b", reason: reasonRemap,
				from: "pod-a", queue: 2, overloaded: testLimits.Sustained, pinAfter: "pod-b"},
			{advance: time.Second, pods: over, want: []string{"pod-b"}, pick: "pod-b", reason: reasonPinned,
				pinAfter: "pod-b"},
		}},
		{"every pod overloaded stays pinned", Flags{Affinity: true, OverloadGuard: true}, []guardEvent{
			first,
			{pods: []queued{{"pod-a", 2}, {"pod-b", 2}}, want: []string{"pod-a"}, pick: "pod-a", reason: reasonPinned,
				pinAfter: "pod-a"},
		}},
		{"a spill renews the pin's TTL", Flags{Affinity: true, OverloadGuard: true}, []guardEvent{
			first,
			{advance: testTTL - time.Second, pods: over, want: []string{"pod-b"}, pick: "pod-b", reason: reasonSpill,
				from: "pod-a", queue: 2, pinAfter: "pod-a"},
			{advance: testTTL - time.Second, pods: calm, want: []string{"pod-a"}, pick: "pod-a", reason: reasonPinned,
				pinAfter: "pod-a"},
		}},
		{"the guard off never spills", Flags{Affinity: true}, []guardEvent{
			first,
			{pods: []queued{{"pod-a", 5}, {"pod-b", 0}}, want: []string{"pod-a"}, pick: "pod-a", reason: reasonPinned,
				pinAfter: "pod-a"},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			router := newRouter(tc.flags, clock)
			for i, e := range tc.events {
				clock.advance(e.advance)
				call := request(map[string]string{headerRunID: "run-1", headerStep: fmt.Sprint(i + 1)})
				got := router.Filter(context.Background(), call, endpointsWithQueues(e.pods))
				if strings.Join(names(got), ",") != strings.Join(e.want, ",") {
					t.Fatalf("event %d: filter returned %v, want %v", i, names(got), e.want)
				}
				ctx, lines := capture()
				if err := router.PreRequest(ctx, call, resultOn(e.pick)); err != nil {
					t.Fatalf("event %d: %v", i, err)
				}
				if len(*lines) != 1 {
					t.Fatalf("event %d: got %d lines, want 1: %v", i, len(*lines), *lines)
				}
				line := (*lines)[0]
				if line["run-id"] != "run-1" || line["pod"] != e.pick || line["reason"] != e.reason {
					t.Fatalf("event %d: line %v, want pod %s, reason %s", i, line, e.pick, e.reason)
				}
				from, hasFrom := line["from"]
				if (e.from == "" && hasFrom) || (e.from != "" && from != e.from) {
					t.Fatalf("event %d: line %v, want from %q", i, line, e.from)
				}
				moved := e.reason == reasonSpill || e.reason == reasonRemap
				if queue, ok := line["queue"]; ok != moved || (moved && queue != float64(e.queue)) {
					t.Fatalf("event %d: line %v, want queue %d on a spill or a remap only", i, line, e.queue)
				}
				overloaded, ok := line["overloadedSeconds"]
				if ok != (e.reason == reasonRemap) || (ok && overloaded != e.overloaded.Seconds()) {
					t.Fatalf("event %d: line %v, want overloadedSeconds %v on a remap only", i, line,
						e.overloaded.Seconds())
				}
				if pod := router.pins.pod("run-1"); pod != e.pinAfter {
					t.Fatalf("event %d: run pinned to %q, want %q", i, pod, e.pinAfter)
				}
			}
		})
	}
}

func TestOverloadGuardWithoutAffinityPassesEveryCandidate(t *testing.T) {
	router := newRouter(Flags{OverloadGuard: true}, newFakeClock())
	call := request(map[string]string{headerRunID: "run-1", headerStep: "2"})
	got := router.Filter(context.Background(), call, endpointsWithQueues([]queued{{"pod-a", 5}, {"pod-b", 0}}))
	if strings.Join(names(got), ",") != "pod-a,pod-b" {
		t.Fatalf("got %v, want every candidate: with no pin there is nothing to spill", names(got))
	}
}

// decisionCount reads thesis_picker_decisions_total{decision} from the registry the picker serves on its metrics port.
func decisionCount(t *testing.T, decision string) float64 {
	t.Helper()
	Register()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, family := range families {
		if family.GetName() != "thesis_picker_decisions_total" {
			continue
		}
		for _, metric := range family.GetMetric() {
			for _, label := range metric.GetLabel() {
				if label.GetName() == "decision" && label.GetValue() == decision {
					return metric.GetCounter().GetValue()
				}
			}
		}
	}
	return 0
}

func TestEveryDecisionOnACallOfARunIsCounted(t *testing.T) {
	reasons := []string{reasonFirst, reasonPinned, reasonRemapped, reasonSpill, reasonRemap}
	before := map[string]float64{}
	for _, reason := range reasons {
		before[reason] = decisionCount(t, reason)
	}
	clock := newFakeClock()
	router := newRouter(Flags{Affinity: true, OverloadGuard: true}, clock)
	steps := []struct {
		advance time.Duration
		pods    []queued
		pick    string
	}{
		{0, []queued{{"pod-a", 0}, {"pod-b", 0}}, "pod-a"},
		{0, []queued{{"pod-a", 0}, {"pod-b", 0}}, "pod-a"},
		{0, []queued{{"pod-a", 2}, {"pod-b", 0}}, "pod-b"},
		{testLimits.Sustained, []queued{{"pod-a", 2}, {"pod-b", 0}}, "pod-b"},
		{0, []queued{{"pod-a", 0}, {"pod-c", 0}}, "pod-c"},
	}
	for i, step := range steps {
		clock.advance(step.advance)
		call := request(map[string]string{headerRunID: "run-1", headerStep: fmt.Sprint(i + 1)})
		router.Filter(context.Background(), call, endpointsWithQueues(step.pods))
		if err := router.PreRequest(context.Background(), call, resultOn(step.pick)); err != nil {
			t.Fatal(err)
		}
	}
	for _, reason := range reasons {
		if got := decisionCount(t, reason) - before[reason]; got != 1 {
			t.Fatalf("%s counted %v times, want 1", reason, got)
		}
	}
}

func TestADecisionWithAffinityOffIsNotCounted(t *testing.T) {
	before := decisionCount(t, reasonFirst) + decisionCount(t, reasonPinned)
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1"})
	if err := newRouter(Flags{}, newFakeClock()).PreRequest(context.Background(), call, resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	if after := decisionCount(t, reasonFirst) + decisionCount(t, reasonPinned); after != before {
		t.Fatalf("counted %v with affinity off, want nothing", after-before)
	}
}

func TestRegisterPutsTheDecisionsCounterOnThePickersRegistry(t *testing.T) {
	Register()
	Register()
	err := ctrlmetrics.Registry.Register(decisions)
	if !errors.As(err, &prometheus.AlreadyRegisteredError{}) {
		t.Fatalf("registering the counter again returned %v, want AlreadyRegisteredError: Register put it there", err)
	}
}
