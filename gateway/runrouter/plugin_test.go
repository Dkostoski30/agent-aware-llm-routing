package runrouter

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/go-logr/logr/funcr"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/log"

	fwkdl "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/datalayer"
	fwkplugin "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/plugin"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

// The parameters infra/routing-values.sh writes with every rule off.
const allOff = `{"affinity": false, "overloadGuard": false, "parallelSpill": false, "priority": false, "warmup": false}`

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

func newPlugin(t *testing.T, raw string) *RunRouter {
	t.Helper()
	built, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(raw)), testHandle{ctx: context.Background()})
	if err != nil {
		t.Fatalf("factory refused %s: %v", raw, err)
	}
	return built.(*RunRouter)
}

func endpoint(name string) scheduling.Endpoint {
	return scheduling.NewEndpoint(&fwkdl.EndpointMetadata{ID: k8stypes.NamespacedName{Name: name}}, &fwkdl.Metrics{}, nil)
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
			input := make([]scheduling.Endpoint, len(tc.endpoints))
			for i, name := range tc.endpoints {
				input[i] = endpoint(name)
			}
			got := newPlugin(t, allOff).Filter(context.Background(), request(tc.headers), input)
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

func TestFactoryReadsEveryFlag(t *testing.T) {
	raw := `{"affinity": true, "overloadGuard": false, "parallelSpill": true, "priority": false, "warmup": true}`
	got := newPlugin(t, raw).flags
	want := Flags{Affinity: true, OverloadGuard: false, ParallelSpill: true, Priority: false, Warmup: true}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
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
		{"an unknown rule", `{"affinity": false, "overloadGuard": false, "parallelSpill": false, "priority": false,
			"warmup": false, "afinity": true}`, "afinity"},
		{"a missing rule", `{"affinity": false, "overloadGuard": false, "parallelSpill": false, "priority": false}`,
			"warmup"},
		{"a rule that is not a bool", `{"affinity": "yes", "overloadGuard": false, "parallelSpill": false,
			"priority": false, "warmup": false}`, "affinity"},
		{"null for a rule", `{"affinity": null, "overloadGuard": false, "parallelSpill": false, "priority": false,
			"warmup": false}`, "affinity"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(tc.raw)), testHandle{ctx: context.Background()})
			if err == nil {
				t.Fatalf("factory accepted %s", tc.raw)
			}
			if !strings.Contains(err.Error(), tc.says) {
				t.Fatalf("error %q does not name %q", err, tc.says)
			}
		})
	}
}

func TestFactoryLogsItsFlagsOnce(t *testing.T) {
	ctx, lines := capture()
	if _, err := Factory("thesis", fwkplugin.StrictDecoder(json.RawMessage(allOff)), testHandle{ctx: ctx}); err != nil {
		t.Fatal(err)
	}
	if len(*lines) != 1 {
		t.Fatalf("got %d lines, want 1: %v", len(*lines), *lines)
	}
	line := (*lines)[0]
	if line["type"] != Type || line["affinity"] != false || line["warmup"] != false {
		t.Fatalf("line %v lacks the type or the flags", line)
	}
}

func TestPreRequestLogsThePickUnderRunID(t *testing.T) {
	ctx, lines := capture()
	headers := map[string]string{headerRunID: "run-1", headerStep: "2"}
	if err := newPlugin(t, allOff).PreRequest(ctx, request(headers), resultOn("pod-a")); err != nil {
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
	ctx, lines := capture()
	if err := newPlugin(t, allOff).PreRequest(ctx, request(map[string]string{}), resultOn("pod-a")); err != nil {
		t.Fatal(err)
	}
	if len(*lines) != 0 {
		t.Fatalf("got lines %v, want none", *lines)
	}
}

func TestPreRequestLogsNothingForAResultWithNoTarget(t *testing.T) {
	ctx, lines := capture()
	headers := map[string]string{headerRunID: "run-1", headerStep: "1"}
	empty := &scheduling.SchedulingResult{PrimaryProfileName: "default", ProfileResults: map[string]*scheduling.ProfileRunResult{}}
	if err := newPlugin(t, allOff).PreRequest(ctx, request(headers), empty); err != nil {
		t.Fatal(err)
	}
	if len(*lines) != 0 {
		t.Fatalf("got lines %v, want none", *lines)
	}
}

func TestPreRequestLogsNothingBelowThePickersVerbosity(t *testing.T) {
	lines := 0
	quiet := funcr.New(func(_, _ string) { lines++ }, funcr.Options{Verbosity: 2})
	ctx := log.IntoContext(context.Background(), quiet)
	headers := map[string]string{headerRunID: "run-1", headerStep: "1"}
	if err := newPlugin(t, allOff).PreRequest(ctx, request(headers), resultOn("pod-a")); err != nil {
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
