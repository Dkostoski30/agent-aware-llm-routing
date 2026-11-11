// Package runrouter is the thesis's routing plugin for llm-d-router v0.11.0's Endpoint Picker (P3.2). It is a filter:
// returning only the pinned pod is a hard rule, while a scorer's weight can be outvoted by the queue and KV scorers;
// returning every pod hands the choice back to the default scorers (docs/task-cards/37-P3.2-plugin-skeleton.md).
// With affinity on (P3.3), steps 2..n of a run go to the pod its last call went to; the first step is the default
// scorers' pick, by load and prefix. A run whose pod left the pool goes where the scorers send it and stays there
// (P3.4).
package runrouter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"time"

	"github.com/go-logr/logr"
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
	// Why a call of a run went where it went, in PreRequest's log line: no live pin, so the default scorers chose;
	// the run's pinned pod; or the scorers chose because the pinned pod had left the pool.
	reasonFirst    = "first"
	reasonPinned   = "pinned"
	reasonRemapped = "remapped"
	// The end of a run (D1): the agent runtime and the replayer send it once a run stops.
	endOfRunPattern = "POST /runs/{id}/end"
	// How long the end-of-run server waits for a request's headers. The callers send a bodiless POST within the
	// cluster, so 10 s only stops a client that opened a connection and sent nothing from holding it forever.
	readHeaderTimeout = 10 * time.Second
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

// RunRouter holds the routing flags and the runs' pins.
type RunRouter struct {
	typedName fwkplugin.TypedName
	flags     Flags
	pins      *pins
	// The factory's logger, for the end-of-run server, whose requests carry none.
	logger logr.Logger
}

func newRunRouter(typedName fwkplugin.TypedName, flags Flags, ttl time.Duration, now func() time.Time,
	logger logr.Logger) *RunRouter {
	return &RunRouter{typedName: typedName, flags: flags, pins: newPins(ttl, now), logger: logger}
}

// Factory builds the plugin from its config parameters, refusing any it cannot read (parseParameters), and serves
// the end of a run on its port until the picker stops. The port is opened here, so a port the picker cannot have fails
// it at start instead of losing every end of run to the TTL.
func Factory(name string, parameters *json.Decoder, handle fwkplugin.Handle) (fwkplugin.Plugin, error) {
	params, err := parseParameters(parameters)
	if err != nil {
		return nil, fmt.Errorf("plugin %s: %w", Type, err)
	}
	ctx := handle.Context()
	logger := log.FromContext(ctx)
	listener, err := net.Listen("tcp", fmt.Sprintf(":%d", params.EndOfRunPort))
	if err != nil {
		return nil, fmt.Errorf("plugin %s: end-of-run port: %w", Type, err)
	}
	router := newRunRouter(fwkplugin.TypedName{Type: Type, Name: name}, params.Flags, params.RunTTL, time.Now, logger)
	go router.serve(ctx, listener)
	flags := params.Flags
	logger.Info("Plugin loaded", "type", Type, "name", name,
		"affinity", flags.Affinity, "overloadGuard", flags.OverloadGuard, "parallelSpill", flags.ParallelSpill,
		"priority", flags.Priority, "warmup", flags.Warmup,
		"runTTLSeconds", int(params.RunTTL/time.Second), "endOfRunPort", params.EndOfRunPort)
	return router, nil
}

// serve answers the end of runs on listener until ctx ends. Close, not Shutdown: the picker is stopping, and an end
// of run cut off here is left to the TTL, as a lost one is.
func (r *RunRouter) serve(ctx context.Context, listener net.Listener) {
	server := &http.Server{Handler: r.handler(), ReadHeaderTimeout: readHeaderTimeout}
	go func() {
		<-ctx.Done()
		if err := server.Close(); err != nil {
			r.logger.Error(err, "End-of-run server did not close")
		}
	}()
	if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
		r.logger.Error(err, "End-of-run server stopped")
	}
}

// handler serves POST /runs/{id}/end.
func (r *RunRouter) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(endOfRunPattern, r.endRun)
	return mux
}

// endRun removes the run's pin and answers 204 whether it had one or not: a run that never had a pin, or whose pin
// expired, has ended all the same.
func (r *RunRouter) endRun(w http.ResponseWriter, request *http.Request) {
	runID := request.PathValue("id")
	pinned := r.pins.end(runID)
	r.logger.V(logging.VERBOSE).Info("Run ended", "run-id", runID, "pinned", pinned)
	w.WriteHeader(http.StatusNoContent)
}

// TypedName returns the plugin's type and the name the config gave it.
func (r *RunRouter) TypedName() fwkplugin.TypedName {
	return r.typedName
}

// Filter returns only the run's pinned pod when affinity is on and that pod is a candidate; otherwise every candidate,
// in order, and the default scorers choose. A pin on a pod that is no longer a candidate passes every candidate, and
// PreRequest moves the pin to the pod chosen instead. Pods are told apart by endpoint name, which the picker makes
// <pod>-rank-<n> (llm-d-router v0.11.0's datastore) and which is unique: an InferencePool selects pods in its own
// namespace only.
func (r *RunRouter) Filter(_ context.Context, request *scheduling.InferenceRequest, endpoints []scheduling.Endpoint) []scheduling.Endpoint {
	runID := request.Headers[headerRunID]
	if !r.flags.Affinity || runID == "" {
		return endpoints
	}
	pod := r.pins.pod(runID)
	if pod == "" {
		return endpoints
	}
	for _, endpoint := range endpoints {
		if endpoint.GetMetadata().ID.Name == pod {
			return []scheduling.Endpoint{endpoint}
		}
	}
	return endpoints
}

// PreRequest pins the run to the chosen pod and renews its TTL when affinity is on, and logs the pick of a call that
// belongs to a run, under run-id (D12), at the level of upstream's "Request handled" line (logging.VERBOSE). With
// affinity on the line has a reason: pinned when the run's live pin was this pod; remapped, with the old pod as from,
// when it was another, since Filter lets only a live pin's pod through unless that pod left the pool; first when the
// run had no live pin. The pin is read and written under one lock, so a call of the same run in between cannot make
// the reason wrong. Two calls of one run that pass the filter together with no pin to hold them (no live pin yet, or
// their pin's pod gone) may be sent to two pods, and the second then reads as remapped from the first's, though no pod
// left. Neither caller sends that today: the replayer fans out steps 2..n only, which a live pin holds, and the agent
// runtime sends one call at a time. P3.6 decides where a parallel group goes.
func (r *RunRouter) PreRequest(ctx context.Context, request *scheduling.InferenceRequest, result *scheduling.SchedulingResult) error {
	runID := request.Headers[headerRunID]
	if runID == "" {
		return nil
	}
	primary, ok := result.ProfileResults[result.PrimaryProfileName]
	if !ok || primary == nil || len(primary.TargetEndpoints) == 0 {
		return nil
	}
	target := primary.TargetEndpoints[0].GetMetadata().ID.Name
	fields := []any{"run-id", runID, "step", request.Headers[headerStep], "pod", target}
	if r.flags.Affinity {
		switch before := r.pins.record(runID, target); before {
		case "":
			fields = append(fields, "reason", reasonFirst)
		case target:
			fields = append(fields, "reason", reasonPinned)
		default:
			fields = append(fields, "reason", reasonRemapped, "from", before)
		}
	}
	log.FromContext(ctx).V(logging.VERBOSE).Info("Run call picked", fields...)
	return nil
}
