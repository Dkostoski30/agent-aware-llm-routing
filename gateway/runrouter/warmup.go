package runrouter

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/go-logr/logr"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	fwkdl "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/datalayer"
	fwkplugin "github.com/llm-d/llm-d-router/pkg/epp/framework/interface/plugin"
	"github.com/llm-d/llm-d-router/pkg/epp/framework/interface/scheduling"
	sourcenotifications "github.com/llm-d/llm-d-router/pkg/epp/framework/plugins/datalayer/source/notifications"
)

// podGVK is the kind the plugin watches for Ready times: core/v1 Pod.
var podGVK = schema.GroupVersionKind{Group: "", Version: "v1", Kind: "Pod"}

// readiness keeps when each Ready pod turned Ready (P4.2): its Ready condition's lastTransitionTime, from the picker's
// Pod watch. Keyed by pod name. A pod that is not Ready, deleted, or not yet seen has no entry and counts as old.
type readiness struct {
	mu     sync.Mutex
	since  map[string]time.Time
	logger logr.Logger
}

func newReadiness(logger logr.Logger) *readiness {
	return &readiness{since: map[string]time.Time{}, logger: logger}
}

// learn keeps pod's Ready time, and logs it when it is one the plugin did not hold: the report reads Ready times from
// these lines (make warmup-report), and a status update that leaves Ready as it was writes none.
func (r *readiness) learn(pod string, at time.Time) {
	r.mu.Lock()
	before, known := r.since[pod]
	r.since[pod] = at
	r.mu.Unlock()
	if !known || !before.Equal(at) {
		r.logger.Info("Pod ready", "pod", pod, "readyAt", at.UTC().Format(time.RFC3339))
	}
}

func (r *readiness) forget(pod string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.since, pod)
}

// readyAt returns when pod turned Ready, and false when no Ready time is held for it.
func (r *readiness) readyAt(pod string) (time.Time, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	at, known := r.since[pod]
	return at, known
}

// podWatch is the plugin's Pod extractor. llm-d-router v0.11.0's datalayer hands it every add, update and delete of a
// pod in the pool's namespace (datalayer/k8s_bind.go, through the manager's cache, which the picker's Role already
// lets list and watch pods), as upstream's topology extractor gets them.
type podWatch struct {
	typedName fwkplugin.TypedName
	ready     *readiness
}

func (w *podWatch) TypedName() fwkplugin.TypedName { return w.typedName }

func (w *podWatch) GVK() schema.GroupVersionKind { return podGVK }

// Extract keeps the pod's Ready time while its Ready condition is True, and drops it on a delete or any other status.
// A Ready condition whose time it cannot read drops the pod too and is returned as an error, which the datalayer
// counts and logs: such a pod counts as old.
func (w *podWatch) Extract(_ context.Context, event fwkdl.NotificationEvent) error {
	pod := event.Object.GetName()
	if event.Type == fwkdl.EventDelete {
		w.ready.forget(pod)
		return nil
	}
	at, ready, err := readyTime(event.Object)
	if err != nil || !ready {
		w.ready.forget(pod)
		return err
	}
	w.ready.learn(pod, at)
	return nil
}

// readyTime reads the pod's Ready condition: whether it is True, and since when. The API server writes
// lastTransitionTime in RFC 3339, to the second.
func readyTime(pod *unstructured.Unstructured) (time.Time, bool, error) {
	conditions, _, err := unstructured.NestedSlice(pod.Object, "status", "conditions")
	if err != nil {
		return time.Time{}, false, fmt.Errorf("pod %s: conditions: %w", pod.GetName(), err)
	}
	for _, entry := range conditions {
		condition, ok := entry.(map[string]any)
		if !ok || condition["type"] != "Ready" {
			continue
		}
		if condition["status"] != "True" {
			return time.Time{}, false, nil
		}
		text, _ := condition["lastTransitionTime"].(string)
		at, err := time.Parse(time.RFC3339, text)
		if err != nil {
			return time.Time{}, false, fmt.Errorf("pod %s: Ready since %q: %w", pod.GetName(), text, err)
		}
		return at, true, nil
	}
	return time.Time{}, false, nil
}

// RegisterDependencies asks the picker's datalayer for the Pod watch, creating a k8s-notification-source for Pods
// when the config has none, as upstream's topology extractor does. It runs whatever the flags: the Ready times are
// logged for a variant with warm-up off too, which the report compares against.
func (r *RunRouter) RegisterDependencies(registrar fwkdl.Registrar) error {
	return registrar.Register(fwkdl.PendingRegistration{
		Owner:      r.typedName,
		SourceType: sourcenotifications.NotificationSourceType,
		Extractor:  r.podWatch(),
		DefaultSource: sourcenotifications.NewK8sNotificationSource(sourcenotifications.NotificationSourceType,
			r.typedName.Name+"/pod", podGVK),
	})
}

// podWatch is the router's Pod extractor, named after the router.
func (r *RunRouter) podWatch() *podWatch {
	return &podWatch{typedName: fwkplugin.TypedName{Type: Type + "-pods", Name: r.typedName.Name + "/pods"},
		ready: r.ready}
}

// arrival is warm-up's verdict on a run's first call, from Filter to PreRequest, which the picker hands the same
// request.
type arrival struct {
	// reasonWarmup when some new pod had room, reasonWarmupFull when none had.
	reason string
	// Each new candidate's seconds since it turned Ready, and its waiting queue, by endpoint name.
	age   map[string]float64
	queue map[string]int
	// The new candidates with room, in their order: the only pods the call may go to. Empty when none had room.
	to []scheduling.Endpoint
}

// warmupKey holds warm-up's verdict on the request.
var warmupKey = fwkplugin.NewDataKey("thesis-warmup", Type)

// judge looks at the candidates of a run's first call. A candidate is new when it turned Ready at most limits.Window
// before now, and has room when its waiting queue is at most limits.QueueThreshold: the picker's last read of the
// pod's metrics, so first calls that arrive within one read see the same queue. It returns false, and the call is
// left alone, when no candidate is new or every one is (right after make up, all pods are new and none is favored).
// Otherwise the verdict offers the new pods with room, or none when no new pod had room.
func (r *readiness) judge(endpoints []scheduling.Endpoint, limits WarmupLimits, now time.Time) (arrival, bool) {
	verdict := arrival{age: map[string]float64{}, queue: map[string]int{}}
	old := false
	for _, endpoint := range endpoints {
		metadata := endpoint.GetMetadata()
		at, known := r.readyAt(metadata.Name)
		if !known || now.Sub(at) > limits.Window {
			old = true
			continue
		}
		verdict.age[metadata.ID.Name] = now.Sub(at).Seconds()
		verdict.queue[metadata.ID.Name] = queue(endpoint)
		if queue(endpoint) <= limits.QueueThreshold {
			verdict.to = append(verdict.to, endpoint)
		}
	}
	if !old || len(verdict.age) == 0 {
		return arrival{}, false
	}
	verdict.reason = reasonWarmup
	if len(verdict.to) == 0 {
		verdict.reason = reasonWarmupFull
	}
	return verdict, true
}
