// Package runrouter is the thesis's routing plugin for llm-d-router v0.11.0's Endpoint Picker (P3.2). It is a filter:
// returning only the pinned pod is a hard rule, while a scorer's weight can be outvoted by the queue and KV scorers;
// returning every pod hands the choice back to the default scorers (docs/task-cards/37-P3.2-plugin-skeleton.md).
// With affinity on (P3.3), steps 2..n of a run go to the pod its last call went to; the first step is the default
// scorers' pick, by load and prefix. A run whose pod left the pool goes where the scorers send it and stays there
// (P3.4). With the overload guard on (P3.5), a call whose pinned pod is overloaded goes to another pod and the run
// stays pinned (spill); a run whose pod stays overloaded moves (remap).
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
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"

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
	// Why a call of a run went where it went, in PreRequest's log line and the decisions counter: no live pin, so the
	// default scorers chose; the run's pinned pod; the scorers chose because the pinned pod had left the pool; the
	// pinned pod was over the queue threshold, so this call went to another pod and the pin stayed; or it had been over
	// the threshold for the sustained time, so the pin moved (P3.5's card names the last two).
	reasonFirst    = "first"
	reasonPinned   = "pinned"
	reasonRemapped = "remapped"
	reasonSpill    = "spill"
	reasonRemap    = "remap"
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

// moveKey holds the overload guard's verdict on the request, from Filter to PreRequest: the picker hands both the same
// request (llm-d-router v0.11.0's requestcontrol/director.go).
var moveKey = fwkplugin.NewDataKey("thesis-overload-move", Type)

// Register adds the plugin to the picker's table, and its decisions counter to the registry the picker serves;
// cmd/picker calls it before the runner reads its config. Beta, not Alpha: Alpha plugins need
// --allow-experimental-plugins, which would also let upstream's Alpha plugins load.
func Register() {
	fwkplugin.Register(Type, fwkplugin.StabilityBeta, Factory)
	registerDecisions.Do(func() { ctrlmetrics.Registry.MustRegister(decisions) })
}

// RunRouter holds the routing flags, the runs' pins and the pods' overload timers.
type RunRouter struct {
	typedName fwkplugin.TypedName
	flags     Flags
	pins      *pins
	overload  *overload
	// The factory's logger, for the end-of-run server, whose requests carry none.
	logger logr.Logger
}

func newRunRouter(typedName fwkplugin.TypedName, flags Flags, ttl time.Duration, limits Limits, now func() time.Time,
	logger logr.Logger) *RunRouter {
	return &RunRouter{typedName: typedName, flags: flags, pins: newPins(ttl, now), overload: newOverload(limits, now),
		logger: logger}
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
	router := newRunRouter(fwkplugin.TypedName{Type: Type, Name: name}, params.Flags, params.RunTTL, params.Overload,
		time.Now, logger)
	go router.serve(ctx, listener)
	flags := params.Flags
	logger.Info("Plugin loaded", "type", Type, "name", name,
		"affinity", flags.Affinity, "overloadGuard", flags.OverloadGuard, "parallelSpill", flags.ParallelSpill,
		"priority", flags.Priority, "warmup", flags.Warmup,
		"runTTLSeconds", int(params.RunTTL/time.Second), "endOfRunPort", params.EndOfRunPort,
		"overloadQueueThreshold", params.Overload.QueueThreshold,
		"overloadSustainedSeconds", int(params.Overload.Sustained/time.Second))
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
// PreRequest moves the pin to the pod chosen instead. With the overload guard on, every call has the guard look at
// every candidate's queue, and a call the guard moves off its pinned pod gets the pods it offers instead (overload's
// check), with its verdict left on the request for PreRequest.
func (r *RunRouter) Filter(_ context.Context, request *scheduling.InferenceRequest, endpoints []scheduling.Endpoint) []scheduling.Endpoint {
	pinned := r.pinnedCandidate(request.Headers[headerRunID], endpoints)
	if r.flags.OverloadGuard {
		if verdict := r.overload.check(pinned, endpoints); verdict != nil {
			request.PutAttribute(moveKey, *verdict)
			return verdict.to
		}
	}
	if pinned == nil {
		return endpoints
	}
	return []scheduling.Endpoint{pinned}
}

// pinnedCandidate returns the candidate the run is pinned to, or nil: affinity off, no run, no live pin, or a pin on a
// pod that is no longer a candidate. Pods are told apart by endpoint name, which the picker makes <pod>-rank-<n>
// (llm-d-router v0.11.0's datastore) and which is unique: an InferencePool selects pods in its own namespace only.
func (r *RunRouter) pinnedCandidate(runID string, endpoints []scheduling.Endpoint) scheduling.Endpoint {
	if !r.flags.Affinity || runID == "" {
		return nil
	}
	pod := r.pins.pod(runID)
	if pod == "" {
		return nil
	}
	for _, endpoint := range endpoints {
		if endpoint.GetMetadata().ID.Name == pod {
			return endpoint
		}
	}
	return nil
}

// PreRequest pins the run to the chosen pod and renews its TTL when affinity is on, and logs the pick of a call that
// belongs to a run, under run-id (D12), at the level of upstream's "Request handled" line (logging.VERBOSE). With
// affinity on the line has a reason, which the decisions counter counts. A call the overload guard moved has the
// guard's: spill keeps the pin on the overloaded pod and renews its TTL; remap moves it to the chosen pod; both name
// the overloaded pod as from and its queue, and a remap how long since the pod's timer started (its first look over
// the threshold, or its last remap). Any other call: pinned when the run's live pin was this pod; remapped, with the
// old pod as from, when it was another, since Filter lets only a live pin's pod through unless that pod left the pool;
// first when the run had no live pin. The pin is read and written under one lock, so a call of the same run in between
// cannot make the reason wrong. Two calls of one run that pass the filter together with no pin to hold them (no live
// pin yet, or their pin's pod gone) may be sent to two pods, and the second then reads as remapped from the first's,
// though no pod left. Neither caller sends that today: the replayer fans out steps 2..n only, which a live pin holds,
// and the agent runtime sends one call at a time. P3.6 decides where a parallel group goes.
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
		reason := ""
		if verdict, moved := scheduling.ReadRequestAttribute[move](request, moveKey); moved {
			reason = verdict.reason
			fields = append(fields, "from", verdict.from, "queue", verdict.queue)
			if reason == reasonRemap {
				r.pins.record(runID, target)
				fields = append(fields, "overloadedSeconds", verdict.overloaded.Seconds())
			} else {
				r.pins.renew(runID)
			}
		} else {
			switch before := r.pins.record(runID, target); before {
			case "":
				reason = reasonFirst
			case target:
				reason = reasonPinned
			default:
				reason = reasonRemapped
				fields = append(fields, "from", before)
			}
		}
		fields = append(fields, "reason", reason)
		decisions.WithLabelValues(reason).Inc()
	}
	log.FromContext(ctx).V(logging.VERBOSE).Info("Run call picked", fields...)
	return nil
}
