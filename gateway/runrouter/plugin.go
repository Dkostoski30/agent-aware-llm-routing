// Package runrouter is the thesis's routing plugin for llm-d-router v0.11.0's Endpoint Picker (P3.2). It is a filter:
// returning only the pinned pod is a hard rule, while a scorer's weight can be outvoted by the queue and KV scorers;
// returning every pod hands the choice back to the default scorers (docs/task-cards/37-P3.2-plugin-skeleton.md).
// With affinity on (P3.3), steps 2..n of a run go to the pod its last call went to; the first step is the default
// scorers' pick, by load and prefix. A run whose pod left the pool goes where the scorers send it and stays there
// (P3.4). With the overload guard on (P3.5), a call whose pinned pod is overloaded goes to another pod and the run
// stays pinned (spill); a run whose pod stays overloaded moves (remap). With parallel spill on (P3.6), the calls of
// one x-parallel-group stay on the run's pod while it has room for them, and the rest go to pods that have room. With
// least work first on (P3.7b), a run's first call goes to the pod whose pinned runs have the least work left, unless
// the pods are level within the slack; with least context first on instead, to the pod whose pinned runs' latest
// calls are the smallest in bytes.
package runrouter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"time"

	"github.com/go-logr/logr"
	"sigs.k8s.io/controller-runtime/pkg/log"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"

	"github.com/llm-d/llm-d-router/pkg/common/observability/logging"
	fwkdl "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/datalayer"
	fwkplugin "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/plugin"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/requestcontrol"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
)

const (
	// Type names the plugin in the picker's config (infra/routing-values.sh).
	Type = "thesis-run-router"
	// The headers every LLM call carries (schemas/, D1).
	headerRunID         = "x-run-id"
	headerStep          = "x-step"
	headerExpectedSteps = "x-expected-steps"
	headerParallelGroup = "x-parallel-group"
	// Why a call of a run went where it went, in PreRequest's log line and the decisions counter: no live pin, so the
	// default scorers chose; the run's pinned pod; the scorers chose because the pinned pod had left the pool; the
	// pinned pod was over the queue threshold, so this call went to another pod and the pin stayed; or it had been over
	// the threshold for the sustained time, so the pin moved (P3.5's card names these two); a call of a parallel group
	// stayed on the pinned pod, or found no room there and went to another pod with room, the pin staying (P3.6); a
	// run's first call went to the one pod with the least pinned work, the others being over the slack, or with the
	// smallest pinned context (P3.7b).
	reasonFirst         = "first"
	reasonPinned        = "pinned"
	reasonRemapped      = "remapped"
	reasonSpill         = "spill"
	reasonRemap         = "remap"
	reasonParallelStay  = "parallel-stay"
	reasonParallelSpill = "parallel-spill"
	reasonLeastWork     = "least-work"
	reasonLeastContext  = "least-context"
	// The end of a run (D1): the agent runtime and the replayer send it once a run stops.
	endOfRunPattern = "POST /runs/{id}/end"
	// How long the end-of-run server waits for a request's headers. The callers send a bodiless POST within the
	// cluster, so 10 s only stops a client that opened a connection and sent nothing from holding it forever.
	readHeaderTimeout = 10 * time.Second
)

var (
	_ scheduling.Filter                    = &RunRouter{}
	_ requestcontrol.PreRequest            = &RunRouter{}
	_ requestcontrol.ResponseBodyProcessor = &RunRouter{}
)

// moveKey holds the overload guard's verdict on the request, from Filter to PreRequest: the picker hands both the same
// request (llm-d-router v0.11.0's requestcontrol/director.go).
var moveKey = fwkplugin.NewDataKey("thesis-overload-move", Type)

// parallelKey holds the parallel rule's verdict on the request, from Filter to PreRequest and ResponseBody, which the
// picker hands the same request too.
var parallelKey = fwkplugin.NewDataKey("thesis-parallel-group", Type)

// placementKey holds a first-call rule's verdict on a run's first call, from Filter to PreRequest.
var placementKey = fwkplugin.NewDataKey("thesis-first-call", Type)

// Register adds the plugin to the picker's table, and its decisions counter to the registry the picker serves;
// cmd/picker calls it before the runner reads its config. Beta, not Alpha: Alpha plugins need
// --allow-experimental-plugins, which would also let upstream's Alpha plugins load.
func Register() {
	fwkplugin.Register(Type, fwkplugin.StabilityBeta, Factory)
	registerDecisions.Do(func() { ctrlmetrics.Registry.MustRegister(decisions) })
}

// RunRouter holds the routing flags, the runs' pins, the pods' overload timers, the parallel groups' calls in flight
// and the first-call rule that is on, if any.
type RunRouter struct {
	typedName fwkplugin.TypedName
	flags     Flags
	pins      *pins
	overload  *overload
	parallel  *parallel
	// nil when neither first-call rule is on; parseParameters refuses both.
	firstCall *firstCallRule
	// The factory's logger, for the end-of-run server, whose requests carry none.
	logger logr.Logger
}

func newRunRouter(typedName fwkplugin.TypedName, flags Flags, ttl time.Duration, limits Limits,
	parallelThreshold, leastWorkSlack int, now func() time.Time, logger logr.Logger) *RunRouter {
	router := &RunRouter{typedName: typedName, flags: flags, pins: newPins(ttl, now), overload: newOverload(limits, now),
		parallel: newParallel(parallelThreshold), logger: logger}
	switch {
	case flags.LeastWork:
		rule := leastWorkRule(leastWorkSlack)
		router.firstCall = &rule
	case flags.LeastContext:
		rule := leastContextRule()
		router.firstCall = &rule
	}
	return router
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
		params.ParallelQueueThreshold, params.LeastWorkSlack, time.Now, logger)
	go router.serve(ctx, listener)
	flags := params.Flags
	logger.Info("Plugin loaded", "type", Type, "name", name,
		"affinity", flags.Affinity, "overloadGuard", flags.OverloadGuard, "parallelSpill", flags.ParallelSpill,
		"leastWorkFirst", flags.LeastWork, "leastContextFirst", flags.LeastContext, "priority", flags.Priority,
		"warmup", flags.Warmup,
		"runTTLSeconds", int(params.RunTTL/time.Second), "endOfRunPort", params.EndOfRunPort,
		"overloadQueueThreshold", params.Overload.QueueThreshold,
		"overloadSustainedSeconds", int(params.Overload.Sustained/time.Second),
		"parallelQueueThreshold", params.ParallelQueueThreshold, "leastWorkSlack", params.LeastWorkSlack)
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
// check), with its verdict left on the request for PreRequest. With parallel spill on, a call of a parallel group that
// the guard left on its pinned pod gets the pods the parallel rule offers (parallel's check), with its verdict left on
// the request for PreRequest and ResponseBody (decided 2026-11-11: the guard first). A group whose run has no live pin
// gets no rule: the scorers place each of its calls, and calls that arrive together may land on two pods. With a
// first-call rule on (least work or least context) as well as affinity, a call with no pinned candidate whose run has
// no live pin, the run's first call, gets the pods the rule offers (pins' place), with its verdict left on the request
// for PreRequest. The guard never judges such a call, as it has no pin.
func (r *RunRouter) Filter(_ context.Context, request *scheduling.InferenceRequest, endpoints []scheduling.Endpoint) []scheduling.Endpoint {
	runID := request.Headers[headerRunID]
	pinned := r.pinnedCandidate(runID, endpoints)
	if r.flags.OverloadGuard {
		if verdict := r.overload.check(pinned, endpoints); verdict != nil {
			request.PutAttribute(moveKey, *verdict)
			return verdict.to
		}
	}
	if pinned == nil {
		if r.flags.Affinity && r.firstCall != nil && runID != "" {
			if verdict, first := r.pins.place(runID, endpoints, *r.firstCall, progressOf(request)); first {
				request.PutAttribute(placementKey, verdict)
				return verdict.to
			}
		}
		return endpoints
	}
	if group := request.Headers[headerParallelGroup]; r.flags.ParallelSpill && group != "" {
		verdict := r.parallel.check(runID, group, pinned, endpoints)
		request.PutAttribute(parallelKey, verdict)
		return verdict.to
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
// and the agent runtime sends one call at a time. A call the parallel rule judged has its verdict's reason, group,
// queue and the group's calls in flight on the pinned pod before it: a stay renews the pin on the pod chosen, which is
// the pinned pod; a spill names the pinned pod as from and renews its pin, as the guard's spill does. A first call a
// first-call rule judged has every candidate's load in the line, under the rule's measure (work or context), and the
// rule's reason when it left one pod, first when the scorers chose among level pods; the run is pinned to the pod
// chosen. Every pin written or renewed holds the run's progress as of this call.
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
		now := progressOf(request)
		reason := ""
		if verdict, moved := scheduling.ReadRequestAttribute[move](request, moveKey); moved {
			reason = verdict.reason
			fields = append(fields, "from", verdict.from, "queue", verdict.queue)
			if reason == reasonRemap {
				r.pins.record(runID, target, now)
				fields = append(fields, "overloadedSeconds", verdict.overloaded.Seconds())
			} else {
				r.pins.renew(runID, now)
			}
		} else if verdict, judged := scheduling.ReadRequestAttribute[parallelVerdict](request, parallelKey); judged {
			reason = verdict.reason
			fields = append(fields, "group", verdict.group, "queue", verdict.queue, "inFlight", verdict.inFlight)
			if reason == reasonParallelSpill {
				fields = append(fields, "from", verdict.from)
				r.pins.renew(runID, now)
			} else {
				r.pins.record(runID, target, now)
			}
		} else if verdict, placed := scheduling.ReadRequestAttribute[placement](request, placementKey); placed {
			reason = reasonFirst
			if verdict.pod != "" {
				reason = verdict.reason
			}
			fields = append(fields, verdict.measure, verdict.load)
			r.pins.record(runID, target, now)
		} else {
			switch before := r.pins.record(runID, target, now); before {
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

// ResponseBody gives back, at the end of a call's stream, the count the parallel rule held for it on its pinned pod.
// llm-d-router v0.11.0 runs it with EndOfStream once a stream ends, and also when a call that got a pod fails, is
// cancelled or its client leaves (handlers/server.go), so a failed call does not hold its count; earlier chunks are
// ignored. Being a ResponseBody plugin makes the picker run its body plugins for every chunk of every call, which it
// skips when none is registered (requestcontrol/director.go): the cost of the count, a queued call per chunk that
// returns at once. A nil request has no verdict to read.
func (r *RunRouter) ResponseBody(_ context.Context, request *scheduling.InferenceRequest,
	response *requestcontrol.Response, _ *fwkdl.EndpointMetadata) {
	if request == nil || !response.EndOfStream {
		return
	}
	if verdict, judged := scheduling.ReadRequestAttribute[parallelVerdict](request, parallelKey); judged &&
		verdict.held != nil {
		r.parallel.release(verdict.held)
	}
}

// progressOf is what a pin keeps of this call: its run's remaining work and the call's body size in bytes, which
// llm-d-router v0.11.0's director fills in from the request (requestcontrol/director.go); a size it does not have is 0.
func progressOf(request *scheduling.InferenceRequest) progress {
	return progress{work: remainingWork(request.Headers), size: request.RequestSizeBytes}
}

// remainingWork is a run's work left as of this call, as the run tracker counts each active run: max(expected - step,
// 1) (D2). A missing x-step or x-expected-steps, or one that is no whole number, counts 1, the floor: every LLM call
// carries both (D1), and a run with a call in flight has at least that call's work left.
func remainingWork(headers map[string]string) int {
	step, stepErr := strconv.Atoi(headers[headerStep])
	expected, expectedErr := strconv.Atoi(headers[headerExpectedSteps])
	if stepErr != nil || expectedErr != nil {
		return 1
	}
	return max(expected-step, 1)
}
