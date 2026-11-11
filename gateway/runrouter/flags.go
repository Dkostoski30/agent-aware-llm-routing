package runrouter

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"
)

// The highest TCP port.
const maxPort = 65535

// The longest time a time.Duration holds in whole seconds; one more would wrap to a negative duration: every pin would
// be dead on arrival, or every overloaded pod sustained at once.
const maxDurationSeconds = int(math.MaxInt64 / int64(time.Second))

// Flags turns each routing rule on or off (D11). The values come from the profile's routing section, which
// infra/routing-values.sh writes into the picker's config; none has a default here, so a rule is never on or off by
// accident of a missing key.
type Flags struct {
	// Keeps steps 2..n of a run on the pod that has its context cached (P3.3).
	Affinity bool
	// Sends one call of a pinned run elsewhere when its pod is overloaded, and moves the run only when the overload is
	// sustained (P3.5).
	OverloadGuard bool
	// Keeps the calls of one parallel group on their run's pod while it has room for them, and sends the rest to pods
	// that have room (P3.6). Acts on pinned runs only, so it needs affinity.
	ParallelSpill bool
	// Serves runs close to finishing first (P4.1).
	Priority bool
	// Sends new runs to the newest pod right after scale-up (P4.2).
	Warmup bool
}

// Parameters are the plugin's whole config: the rules, and what the end of a run needs.
type Parameters struct {
	Flags
	// How long a run's pin outlives its last call when no end of run arrives (routing.run_ttl_seconds).
	RunTTL time.Duration
	// When the overload guard moves a pinned run's call (P3.5).
	Overload Limits
	// A pinned pod has room for a parallel group's call while its waiting queue plus the group's calls in flight on it
	// are at or under this (routing.parallel_queue_threshold, P3.6).
	ParallelQueueThreshold int
	// The port POST /runs/{id}/end is served on (infra/routing-values.sh, which opens it on the picker's Service).
	EndOfRunPort int
}

// Limits say when a pod is overloaded, for the overload guard (P3.5).
type Limits struct {
	// A pod with more calls waiting than this is over the threshold (routing.overload_queue_threshold).
	QueueThreshold int
	// How long a pod stays over the threshold before a run pinned to it moves (routing.overload_sustained_seconds).
	Sustained time.Duration
}

// raw is the JSON the picker's config holds. Pointers, so a missing key and null are told apart from false or 0.
type raw struct {
	Affinity      *bool `json:"affinity"`
	OverloadGuard *bool `json:"overloadGuard"`
	ParallelSpill *bool `json:"parallelSpill"`
	Priority      *bool `json:"priority"`
	Warmup        *bool `json:"warmup"`
	RunTTLSeconds *int  `json:"runTTLSeconds"`
	EndOfRunPort  *int  `json:"endOfRunPort"`
	// The overload limits.
	OverloadQueueThreshold   *int `json:"overloadQueueThreshold"`
	OverloadSustainedSeconds *int `json:"overloadSustainedSeconds"`
	// The parallel rule's threshold.
	ParallelQueueThreshold *int `json:"parallelQueueThreshold"`
}

// parseParameters reads every parameter from the decoder the framework hands the factory. It refuses unknown keys
// (the decoder is strict), missing or null ones, a TTL that is not a positive whole number of seconds, a port outside
// 1-65535, a queue threshold or a parallel threshold that is not a whole number of calls, 0 or more, a sustained time
// that is not a positive whole number of seconds, and no parameters at all.
func parseParameters(decoder *json.Decoder) (Parameters, error) {
	if decoder == nil {
		return Parameters{}, errors.New("no parameters: every routing flag must be set")
	}
	var in raw
	if err := decoder.Decode(&in); err != nil {
		return Parameters{}, fmt.Errorf("parameters: %w", err)
	}
	flags := []struct {
		key   string
		value *bool
	}{
		{"affinity", in.Affinity},
		{"overloadGuard", in.OverloadGuard},
		{"parallelSpill", in.ParallelSpill},
		{"priority", in.Priority},
		{"warmup", in.Warmup},
	}
	for _, flag := range flags {
		if flag.value == nil {
			return Parameters{}, fmt.Errorf("parameters: %s is missing or null", flag.key)
		}
	}
	if in.RunTTLSeconds == nil || *in.RunTTLSeconds <= 0 || *in.RunTTLSeconds > maxDurationSeconds {
		return Parameters{}, fmt.Errorf("parameters: runTTLSeconds must be a positive number of seconds, at most %d",
			maxDurationSeconds)
	}
	if in.EndOfRunPort == nil || *in.EndOfRunPort < 1 || *in.EndOfRunPort > maxPort {
		return Parameters{}, errors.New("parameters: endOfRunPort must be a port, 1-65535")
	}
	if in.OverloadQueueThreshold == nil || *in.OverloadQueueThreshold < 0 {
		return Parameters{}, errors.New("parameters: overloadQueueThreshold must be a number of calls, 0 or more")
	}
	if in.OverloadSustainedSeconds == nil || *in.OverloadSustainedSeconds <= 0 ||
		*in.OverloadSustainedSeconds > maxDurationSeconds {
		return Parameters{}, fmt.Errorf(
			"parameters: overloadSustainedSeconds must be a positive number of seconds, at most %d", maxDurationSeconds)
	}
	if in.ParallelQueueThreshold == nil || *in.ParallelQueueThreshold < 0 {
		return Parameters{}, errors.New("parameters: parallelQueueThreshold must be a number of calls, 0 or more")
	}
	return Parameters{
		Flags: Flags{
			Affinity:      *in.Affinity,
			OverloadGuard: *in.OverloadGuard,
			ParallelSpill: *in.ParallelSpill,
			Priority:      *in.Priority,
			Warmup:        *in.Warmup,
		},
		RunTTL: time.Duration(*in.RunTTLSeconds) * time.Second,
		Overload: Limits{
			QueueThreshold: *in.OverloadQueueThreshold,
			Sustained:      time.Duration(*in.OverloadSustainedSeconds) * time.Second,
		},
		ParallelQueueThreshold: *in.ParallelQueueThreshold,
		EndOfRunPort:           *in.EndOfRunPort,
	}, nil
}
