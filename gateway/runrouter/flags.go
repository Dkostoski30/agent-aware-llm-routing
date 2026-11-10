package runrouter

import (
	"encoding/json"
	"errors"
	"fmt"
)

// Flags turns each routing rule on or off (D11). The values come from the profile's routing section, which
// infra/routing-values.sh writes into the picker's config; none has a default here, so a rule is never on or off by
// accident of a missing key.
type Flags struct {
	// Keeps steps 2..n of a run on the pod that has its context cached (P3.3).
	Affinity bool
	// Moves a run off its pod only under sustained overload (P3.5).
	OverloadGuard bool
	// Lets calls of one parallel group spill to other pods under overload (P3.6).
	ParallelSpill bool
	// Serves runs close to finishing first (P4.1).
	Priority bool
	// Sends new runs to the newest pod right after scale-up (P4.2).
	Warmup bool
}

// parameters is the JSON the picker's config holds. Pointers, so a missing key and null are told apart from false.
type parameters struct {
	Affinity      *bool `json:"affinity"`
	OverloadGuard *bool `json:"overloadGuard"`
	ParallelSpill *bool `json:"parallelSpill"`
	Priority      *bool `json:"priority"`
	Warmup        *bool `json:"warmup"`
}

// parseFlags reads every flag from the decoder the framework hands the factory. It refuses unknown keys (the decoder
// is strict), missing or null ones, and no parameters at all.
func parseFlags(decoder *json.Decoder) (Flags, error) {
	if decoder == nil {
		return Flags{}, errors.New("no parameters: every routing flag must be set")
	}
	var raw parameters
	if err := decoder.Decode(&raw); err != nil {
		return Flags{}, fmt.Errorf("parameters: %w", err)
	}
	fields := []struct {
		key   string
		value *bool
	}{
		{"affinity", raw.Affinity},
		{"overloadGuard", raw.OverloadGuard},
		{"parallelSpill", raw.ParallelSpill},
		{"priority", raw.Priority},
		{"warmup", raw.Warmup},
	}
	for _, field := range fields {
		if field.value == nil {
			return Flags{}, fmt.Errorf("parameters: %s is missing or null", field.key)
		}
	}
	return Flags{
		Affinity:      *raw.Affinity,
		OverloadGuard: *raw.OverloadGuard,
		ParallelSpill: *raw.ParallelSpill,
		Priority:      *raw.Priority,
		Warmup:        *raw.Warmup,
	}, nil
}
