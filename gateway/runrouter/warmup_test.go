package runrouter

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/log"

	fwkdl "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/datalayer"
	fwkplugin "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/plugin"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
	sourcenotifications "github.com/llm-d/llm-d-router/pkg/epp/framework/plugins/datalayer/source/notifications"
)

// The warm-up limits the tests run with: routing.warmup_seconds and routing.warmup_queue_threshold of every profile.
var testWarmup = WarmupLimits{Window: 120 * time.Second, QueueThreshold: 1}

// podEvent is the Pod watch's event for a pod with the given status conditions.
func podEvent(kind fwkdl.EventType, pod string, conditions ...any) fwkdl.NotificationEvent {
	object := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "Pod",
		"metadata":   map[string]any{"name": pod, "namespace": "default"},
		"status":     map[string]any{"conditions": conditions},
	}}
	return fwkdl.NotificationEvent{Type: kind, Object: object}
}

// condition is a pod status condition as the API server writes it, its time in RFC 3339 to the second.
func condition(kind, status string, at time.Time) map[string]any {
	return map[string]any{"type": kind, "status": status, "lastTransitionTime": at.UTC().Format(time.RFC3339)}
}

// podOn is a candidate as llm-d-router v0.11.0's datastore builds it: endpoint <pod>-rank-0, the pod's name, and the
// waiting queue the picker last read from its metrics.
func podOn(pod string, waiting int) scheduling.Endpoint {
	return scheduling.NewEndpoint(&fwkdl.EndpointMetadata{
		ID:   k8stypes.NamespacedName{Namespace: "default", Name: pod + "-rank-0"},
		Name: pod,
	}, &fwkdl.Metrics{WaitingQueueSize: waiting}, nil)
}

// watch hands the router's Pod extractor each event, failing the test on an error.
func watch(t *testing.T, router *RunRouter, events ...fwkdl.NotificationEvent) {
	t.Helper()
	for _, event := range events {
		if err := router.podWatch().Extract(context.Background(), event); err != nil {
			t.Fatal(err)
		}
	}
}

func TestPodWatchKeepsTheReadyTimeOfEachReadyPod(t *testing.T) {
	at := time.Date(2026, 11, 19, 14, 0, 0, 0, time.UTC)
	later := at.Add(90 * time.Second)
	add, del := fwkdl.EventAddOrUpdate, fwkdl.EventDelete
	cases := []struct {
		name   string
		events []fwkdl.NotificationEvent
		want   time.Time
		known  bool
	}{
		{"Ready", []fwkdl.NotificationEvent{podEvent(add, "pod-a", condition("Ready", "True", at))}, at, true},
		{"Ready among other conditions", []fwkdl.NotificationEvent{podEvent(add, "pod-a",
			condition("PodScheduled", "True", at.Add(-time.Minute)), condition("Ready", "True", at))}, at, true},
		{"Ready, then not Ready", []fwkdl.NotificationEvent{podEvent(add, "pod-a", condition("Ready", "True", at)),
			podEvent(add, "pod-a", condition("Ready", "False", later))}, time.Time{}, false},
		{"Ready, then Unknown: its node stopped reporting", []fwkdl.NotificationEvent{
			podEvent(add, "pod-a", condition("Ready", "True", at)),
			podEvent(add, "pod-a", condition("Ready", "Unknown", later))}, time.Time{}, false},
		{"Ready, then deleted", []fwkdl.NotificationEvent{podEvent(add, "pod-a", condition("Ready", "True", at)),
			podEvent(del, "pod-a")}, time.Time{}, false},
		{"Ready twice: the readiness flapped", []fwkdl.NotificationEvent{
			podEvent(add, "pod-a", condition("Ready", "True", at)),
			podEvent(add, "pod-a", condition("Ready", "True", later))}, later, true},
		{"no Ready condition yet", []fwkdl.NotificationEvent{podEvent(add, "pod-a",
			condition("PodScheduled", "True", at))}, time.Time{}, false},
		{"no conditions at all", []fwkdl.NotificationEvent{podEvent(add, "pod-a")}, time.Time{}, false},
		{"another pod's", []fwkdl.NotificationEvent{podEvent(add, "pod-b", condition("Ready", "True", at))},
			time.Time{}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router := newRouter(Flags{}, newFakeClock())
			watch(t, router, tc.events...)
			got, known := router.ready.readyAt("pod-a")
			if known != tc.known || !got.Equal(tc.want) {
				t.Fatalf("Ready time %s (known %v), want %s (known %v)", got, known, tc.want, tc.known)
			}
		})
	}
}

func TestPodWatchRefusesAReadyConditionWithNoTimeItCanRead(t *testing.T) {
	cases := map[string]map[string]any{
		"no time":         {"type": "Ready", "status": "True"},
		"a time it can't": {"type": "Ready", "status": "True", "lastTransitionTime": "yesterday"},
	}
	for name, ready := range cases {
		t.Run(name, func(t *testing.T) {
			router := newRouter(Flags{}, newFakeClock())
			watch(t, router, podEvent(fwkdl.EventAddOrUpdate, "pod-a",
				condition("Ready", "True", time.Date(2026, 11, 19, 14, 0, 0, 0, time.UTC))))
			err := router.podWatch().Extract(context.Background(), podEvent(fwkdl.EventAddOrUpdate, "pod-a", ready))
			if err == nil || !strings.Contains(err.Error(), "pod-a") {
				t.Fatalf("error %v, want one naming pod-a", err)
			}
			if at, known := router.ready.readyAt("pod-a"); known {
				t.Fatalf("pod-a kept Ready time %s, want none: a pod with no Ready time counts as old", at)
			}
		})
	}
}

func TestPodWatchLogsEachReadyTimeOnce(t *testing.T) {
	ctx, lines := capture()
	router := newRunRouter(fwkplugin.TypedName{Type: Type, Name: "thesis"}, Flags{}, testTTL, testLimits,
		testParallelThreshold, testLeastWorkSlack, testAging, testWarmup, newFakeClock().now, log.FromContext(ctx))
	at := time.Date(2026, 11, 19, 14, 0, 0, 0, time.UTC)
	later := at.Add(90 * time.Second)
	watch(t, router,
		podEvent(fwkdl.EventAddOrUpdate, "pod-a", condition("Ready", "True", at)),
		// A status update that leaves Ready as it was: the pod's IP, a container's restart count.
		podEvent(fwkdl.EventAddOrUpdate, "pod-a", condition("Ready", "True", at)),
		podEvent(fwkdl.EventAddOrUpdate, "pod-a", condition("Ready", "False", later)),
		podEvent(fwkdl.EventAddOrUpdate, "pod-a", condition("Ready", "True", later)),
	)
	if len(*lines) != 2 {
		t.Fatalf("got %d lines, want 2: %v", len(*lines), *lines)
	}
	for i, want := range []time.Time{at, later} {
		line := (*lines)[i]
		if line["msg"] != "Pod ready" || line["pod"] != "pod-a" || line["readyAt"] != want.Format(time.RFC3339) {
			t.Fatalf("line %d %v, want Pod ready for pod-a at %s", i, line, want.Format(time.RFC3339))
		}
	}
}

// registrar keeps what a plugin registers, as the picker's datalayer runtime does before Configure.
type registrar struct {
	got []fwkdl.PendingRegistration
}

func (r *registrar) Register(pending fwkdl.PendingRegistration) error {
	r.got = append(r.got, pending)
	return nil
}

func TestThePluginWatchesPodsThroughThePickersNotificationSource(t *testing.T) {
	router := newRouter(Flags{}, newFakeClock())
	var got registrar
	if err := router.RegisterDependencies(&got); err != nil {
		t.Fatal(err)
	}
	if len(got.got) != 1 {
		t.Fatalf("got %d registrations, want 1: %+v", len(got.got), got.got)
	}
	pending := got.got[0]
	if pending.Owner != router.TypedName() || pending.SourceType != sourcenotifications.NotificationSourceType {
		t.Fatalf("registration %+v, want the router's, on %s", pending, sourcenotifications.NotificationSourceType)
	}
	extractor, ok := pending.Extractor.(fwkdl.NotificationExtractor)
	if !ok || extractor.GVK() != podGVK {
		t.Fatalf("extractor %+v, want a Pod notification extractor", pending.Extractor)
	}
	source, ok := pending.DefaultSource.(fwkdl.NotificationSource)
	if !ok || source.GVK() != podGVK {
		t.Fatalf("default source %+v, want a Pod notification source", pending.DefaultSource)
	}
}

// warmupRouter is a router with warm-up on and these pods' Ready times, each so long before the clock's now.
func warmupRouter(t *testing.T, flags Flags, clock *fakeClock, readyAgo map[string]time.Duration) *RunRouter {
	t.Helper()
	router := newRouter(flags, clock)
	for pod, ago := range readyAgo {
		watch(t, router, podEvent(fwkdl.EventAddOrUpdate, pod, condition("Ready", "True", clock.now().Add(-ago))))
	}
	return router
}

// The pods of the warm-up tests: old, Ready an hour ago; new and newer, within the window; edge, Ready exactly the
// window ago; stale, a second past it. A pod with no Ready time known is unknown.
var readyAgo = map[string]time.Duration{
	"old":   time.Hour,
	"new":   30 * time.Second,
	"newer": 10 * time.Second,
	"edge":  testWarmup.Window,
	"stale": testWarmup.Window + time.Second,
}

func TestWarmupSendsARunsFirstCallToTheNewPodsWithRoom(t *testing.T) {
	first := map[string]string{headerRunID: "run-1", headerStep: "1", headerExpectedSteps: "10"}
	cases := []struct {
		name    string
		flags   Flags
		headers map[string]string
		pods    []scheduling.Endpoint
		want    []string
		// The verdict's reason; "" when the rule left the call alone.
		reason string
	}{
		{"one new pod with room", Flags{Warmup: true}, first, []scheduling.Endpoint{podOn("old", 0), podOn("new", 0)},
			[]string{"new-rank-0"}, reasonWarmup},
		{"a new pod at the threshold has room", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("old", 0), podOn("new", 1)}, []string{"new-rank-0"}, reasonWarmup},
		{"two new pods with room, in order", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("newer", 0), podOn("old", 0), podOn("new", 0)},
			[]string{"newer-rank-0", "new-rank-0"}, reasonWarmup},
		{"two new pods, one with room", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("old", 0), podOn("new", 2), podOn("newer", 0)}, []string{"newer-rank-0"},
			reasonWarmup},
		{"Ready exactly the window ago is new", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("old", 0), podOn("edge", 0)}, []string{"edge-rank-0"}, reasonWarmup},
		{"a pod with no Ready time counts as old", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("unknown", 0), podOn("new", 0)}, []string{"new-rank-0"}, reasonWarmup},
		{"the new pod over the threshold", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("old", 0), podOn("new", 2)}, []string{"old-rank-0", "new-rank-0"},
			reasonWarmupFull},
		{"no new pod", Flags{Warmup: true}, first, []scheduling.Endpoint{podOn("old", 0), podOn("stale", 0)},
			[]string{"old-rank-0", "stale-rank-0"}, ""},
		{"every pod new, as right after make up", Flags{Warmup: true}, first,
			[]scheduling.Endpoint{podOn("new", 0), podOn("newer", 0)}, []string{"new-rank-0", "newer-rank-0"}, ""},
		{"warm-up off", Flags{}, first, []scheduling.Endpoint{podOn("old", 0), podOn("new", 0)},
			[]string{"old-rank-0", "new-rank-0"}, ""},
		{"a later step", Flags{Warmup: true}, map[string]string{headerRunID: "run-1", headerStep: "2"},
			[]scheduling.Endpoint{podOn("old", 0), podOn("new", 0)}, []string{"old-rank-0", "new-rank-0"}, ""},
		{"no step", Flags{Warmup: true}, map[string]string{headerRunID: "run-1"},
			[]scheduling.Endpoint{podOn("old", 0), podOn("new", 0)}, []string{"old-rank-0", "new-rank-0"}, ""},
		{"no run", Flags{Warmup: true}, map[string]string{headerStep: "1"},
			[]scheduling.Endpoint{podOn("old", 0), podOn("new", 0)}, []string{"old-rank-0", "new-rank-0"}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router := warmupRouter(t, tc.flags, newFakeClock(), readyAgo)
			call := request(tc.headers)
			got := router.Filter(context.Background(), call, tc.pods)
			if strings.Join(names(got), ",") != strings.Join(tc.want, ",") {
				t.Fatalf("filter returned %v, want %v", names(got), tc.want)
			}
			verdict, judged := scheduling.ReadRequestAttribute[arrival](call, warmupKey)
			if judged != (tc.reason != "") || verdict.reason != tc.reason {
				t.Fatalf("verdict %+v (judged %v), want reason %q", verdict, judged, tc.reason)
			}
		})
	}
}

func TestWarmupLeavesAFirstCallPinnedToALivePodWhereItIs(t *testing.T) {
	router := warmupRouter(t, Flags{Affinity: true, Warmup: true}, newFakeClock(), readyAgo)
	router.pins.record("run-1", "old-rank-0", progress{work: 9})
	// The run's first call again, as a retry would send it: its pin holds, and its context is cached there.
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1", headerExpectedSteps: "10"})
	got := router.Filter(context.Background(), call, []scheduling.Endpoint{podOn("old", 0), podOn("new", 0)})
	if strings.Join(names(got), ",") != "old-rank-0" {
		t.Fatalf("filter returned %v, want the pinned old-rank-0", names(got))
	}
}

func TestWarmupDecidesFirstCallsBeforeLeastWorkWhilePodsAreNew(t *testing.T) {
	clock := newFakeClock()
	router := warmupRouter(t, Flags{Affinity: true, LeastWork: true, Warmup: true}, clock, readyAgo)
	router.pins.record("run-busy", "new-rank-0", progress{work: 9})
	pods := []scheduling.Endpoint{podOn("old", 0), podOn("new", 0)}
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1", headerExpectedSteps: "10"})
	if got := names(router.Filter(context.Background(), call, pods)); strings.Join(got, ",") != "new-rank-0" {
		t.Fatalf("filter returned %v while new was new, want new-rank-0 whatever its work", got)
	}
	if _, placed := scheduling.ReadRequestAttribute[placement](call, placementKey); placed {
		t.Fatal("least work judged a call warm-up had decided")
	}
	// The new pod's window ends: least work places the next run's first call, off the pod with the work.
	clock.advance(testWarmup.Window)
	call = request(map[string]string{headerRunID: "run-2", headerStep: "1", headerExpectedSteps: "10"})
	if got := names(router.Filter(context.Background(), call, pods)); strings.Join(got, ",") != "old-rank-0" {
		t.Fatalf("filter returned %v after the window, want least work's old-rank-0", got)
	}
	if verdict, judged := scheduling.ReadRequestAttribute[arrival](call, warmupKey); judged {
		t.Fatalf("warm-up judged a call with no pod new: %+v", verdict)
	}
}

func TestWarmupDecidesAFirstCallWithNoRoomAndLeastWorkStaysOut(t *testing.T) {
	router := warmupRouter(t, Flags{Affinity: true, LeastWork: true, Warmup: true}, newFakeClock(), readyAgo)
	router.pins.record("run-busy", "old-rank-0", progress{work: 9})
	call := request(map[string]string{headerRunID: "run-1", headerStep: "1", headerExpectedSteps: "10"})
	got := router.Filter(context.Background(), call, []scheduling.Endpoint{podOn("old", 0), podOn("new", 2)})
	if strings.Join(names(got), ",") != "old-rank-0,new-rank-0" {
		t.Fatalf("filter returned %v, want every candidate: no new pod had room", names(got))
	}
	if _, placed := scheduling.ReadRequestAttribute[placement](call, placementKey); placed {
		t.Fatal("least work judged a call warm-up had decided")
	}
}

func TestWarmupLogsItsVerdictPinsTheRunAndCountsIt(t *testing.T) {
	cases := []struct {
		name     string
		flags    Flags
		queue    int
		reason   string
		pinAfter string
	}{
		{"sent to a new pod, affinity on", Flags{Affinity: true, Warmup: true}, 0, reasonWarmup, "new-rank-0"},
		{"no new pod had room, affinity on", Flags{Affinity: true, Warmup: true}, 2, reasonWarmupFull, "old-rank-0"},
		{"sent to a new pod, affinity off", Flags{Warmup: true}, 0, reasonWarmup, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := decisionCount(t, tc.reason)
			router := warmupRouter(t, tc.flags, newFakeClock(), readyAgo)
			call := request(map[string]string{headerRunID: "run-1", headerStep: "1", headerExpectedSteps: "10"})
			chosen := router.Filter(context.Background(), call,
				[]scheduling.Endpoint{podOn("old", 0), podOn("new", tc.queue)})
			ctx, lines := capture()
			pick := chosen[len(chosen)-1]
			if tc.reason == reasonWarmupFull {
				pick = chosen[0]
			}
			result := &scheduling.SchedulingResult{PrimaryProfileName: "default",
				ProfileResults: map[string]*scheduling.ProfileRunResult{
					"default": {TargetEndpoints: []scheduling.Endpoint{pick}}}}
			if err := router.PreRequest(ctx, call, result); err != nil {
				t.Fatal(err)
			}
			if len(*lines) != 1 {
				t.Fatalf("got %d lines, want 1: %v", len(*lines), *lines)
			}
			line := (*lines)[0]
			wantAge := map[string]any{"new-rank-0": float64(30)}
			wantQueue := map[string]any{"new-rank-0": float64(tc.queue)}
			if line["reason"] != tc.reason || fmt.Sprint(line["newPodAge"]) != fmt.Sprint(wantAge) ||
				fmt.Sprint(line["newPodQueue"]) != fmt.Sprint(wantQueue) {
				t.Fatalf("line %v, want reason %s, newPodAge %v, newPodQueue %v", line, tc.reason, wantAge, wantQueue)
			}
			if got := router.pins.pod("run-1"); got != tc.pinAfter {
				t.Fatalf("run pinned to %q, want %q", got, tc.pinAfter)
			}
			if got := decisionCount(t, tc.reason) - before; got != 1 {
				t.Fatalf("%s counted %v times, want 1", tc.reason, got)
			}
		})
	}
}

func TestFactoryHandsTheRouterItsWarmupLimits(t *testing.T) {
	raw := fmt.Sprintf(`{%s, "runTTLSeconds": 300, %s, %s, %s, %s, "warmupSeconds": 90, "warmupQueueThreshold": 3,
		"endOfRunPort": %d}`, allOffRules, overloadJSON, parallelJSON, leastWorkJSON, agingJSON, freePort(t))
	built, err := Factory("thesis", fwkplugin.StrictDecoder([]byte(raw)), testHandle{ctx: t.Context()})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := built.(*RunRouter).warmup, (WarmupLimits{Window: 90 * time.Second, QueueThreshold: 3}); got != want {
		t.Fatalf("warm-up limits %+v, want %+v", got, want)
	}
}

// warmupParameters is every parameter but the warm-up limits, then limits, which is empty or starts with a comma.
func warmupParameters(limits string) string {
	return fmt.Sprintf(`{%s, "runTTLSeconds": 300, %s, %s, %s, %s, "endOfRunPort": 9010%s}`, allOffRules,
		overloadJSON, parallelJSON, leastWorkJSON, agingJSON, limits)
}

func TestFactoryRefusesWarmupLimitsItCannotTrust(t *testing.T) {
	threshold := `, "warmupQueueThreshold": 1`
	seconds := `, "warmupSeconds": 120`
	cases := []struct {
		name string
		raw  string
		says string
	}{
		{"no window", warmupParameters(threshold), "warmupSeconds"},
		{"null for the window", warmupParameters(`, "warmupSeconds": null` + threshold), "warmupSeconds"},
		{"a window of zero", warmupParameters(`, "warmupSeconds": 0` + threshold), "warmupSeconds"},
		{"a negative window", warmupParameters(`, "warmupSeconds": -1` + threshold), "warmupSeconds"},
		{"a window that is not whole seconds", warmupParameters(`, "warmupSeconds": 1.5` + threshold),
			"warmupSeconds"},
		// One second more than a time.Duration holds.
		{"a window too long for a duration", warmupParameters(`, "warmupSeconds": 9223372037` + threshold),
			"warmupSeconds"},
		{"no threshold", warmupParameters(seconds), "warmupQueueThreshold"},
		{"null for the threshold", warmupParameters(seconds + `, "warmupQueueThreshold": null`),
			"warmupQueueThreshold"},
		{"a negative threshold", warmupParameters(seconds + `, "warmupQueueThreshold": -1`), "warmupQueueThreshold"},
		{"a threshold that is not a whole number", warmupParameters(seconds + `, "warmupQueueThreshold": 0.5`),
			"warmupQueueThreshold"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Factory("thesis", fwkplugin.StrictDecoder([]byte(tc.raw)), testHandle{ctx: t.Context()})
			if err == nil {
				t.Fatalf("factory accepted %s", tc.raw)
			}
			if !strings.Contains(err.Error(), tc.says) {
				t.Fatalf("error %q does not name %q", err, tc.says)
			}
		})
	}
}
