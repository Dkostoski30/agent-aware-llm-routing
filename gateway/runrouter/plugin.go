// Package runrouter is the thesis's routing plugin for llm-d-router v0.11.0's Endpoint Picker (P3.2). It is a filter:
// returning only the pinned pod is a hard rule, while a scorer's weight can be outvoted by the queue and KV scorers;
// returning every pod hands the choice back to the default scorers (docs/task-cards/37-P3.2-plugin-skeleton.md).
// Until P3.3 it changes nothing: the filter returns its input and PreRequest only logs the pick.
package runrouter

import (
	"context"
	"encoding/json"
	"fmt"

	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/llm-d/llm-d-router/pkg/common/observability/logging"
	fwkplugin "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/plugin"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/requestcontrol"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

const (
	// Type names the plugin in the picker's config (infra/routing-values.sh).
	Type = "thesis-run-router"
	// The headers every LLM call carries (schemas/, D1).
	headerRunID = "x-run-id"
	headerStep  = "x-step"
)

var (
	_ scheduling.Filter         = &RunRouter{}
	_ requestcontrol.PreRequest = &RunRouter{}
)

// Register adds the plugin to the picker's table; cmd/picker calls it before the runner reads its config. Beta, not
// Alpha: Alpha plugins need --allow-experimental-plugins, which would also let upstream's Alpha plugins load.
func Register() {
	fwkplugin.Register(Type, fwkplugin.StabilityBeta, Factory)
}

// RunRouter holds the routing flags; the rules that read them come in P3.3-P4.2.
type RunRouter struct {
	typedName fwkplugin.TypedName
	flags     Flags
}

// Factory builds the plugin from its config parameters, refusing any it cannot read (parseFlags).
func Factory(name string, parameters *json.Decoder, handle fwkplugin.Handle) (fwkplugin.Plugin, error) {
	flags, err := parseFlags(parameters)
	if err != nil {
		return nil, fmt.Errorf("plugin %s: %w", Type, err)
	}
	log.FromContext(handle.Context()).Info("Plugin loaded", "type", Type, "name", name,
		"affinity", flags.Affinity, "overloadGuard", flags.OverloadGuard, "parallelSpill", flags.ParallelSpill,
		"priority", flags.Priority, "warmup", flags.Warmup)
	return &RunRouter{typedName: fwkplugin.TypedName{Type: Type, Name: name}, flags: flags}, nil
}

// TypedName returns the plugin's type and the name the config gave it.
func (r *RunRouter) TypedName() fwkplugin.TypedName {
	return r.typedName
}

// Filter returns every candidate, in order: no rule is implemented yet.
func (r *RunRouter) Filter(_ context.Context, _ *scheduling.InferenceRequest, endpoints []scheduling.Endpoint) []scheduling.Endpoint {
	return endpoints
}

// PreRequest records nothing yet. It logs the picked pod of a call that belongs to a run, under run-id (D12), at the
// level of upstream's "Request handled" line (logging.VERBOSE), so the log shows the plugin is on the request path.
func (r *RunRouter) PreRequest(ctx context.Context, request *scheduling.InferenceRequest, result *scheduling.SchedulingResult) error {
	runID, ok := request.Headers[headerRunID]
	if !ok {
		return nil
	}
	primary, ok := result.ProfileResults[result.PrimaryProfileName]
	if !ok || primary == nil || len(primary.TargetEndpoints) == 0 {
		return nil
	}
	log.FromContext(ctx).V(logging.VERBOSE).Info("Run call picked", "run-id", runID,
		"step", request.Headers[headerStep], "pod", primary.TargetEndpoints[0].GetMetadata().ID.Name)
	return nil
}
