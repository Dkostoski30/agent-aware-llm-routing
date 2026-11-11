#!/usr/bin/env bash
# Prints the Helm values that put the Endpoint Picker on a profile's routing section (P3.2, D11), on top of
# infra/helm/router-values.yaml. infra/install-gateway.sh and infra/set-routing.sh pass them to helm.
# Usage: routing-values.sh <profile> [rules-file]
#   rules-file holds rules under a top-level routing key, laid over the profile's, as an experiment variant's
#   routing: {plugin: true, affinity: true}. A rule the profile does not have is refused, so a typo fails here, and so
#   is a rule on while plugin is off, and overload_guard on while affinity is off. run_ttl_seconds and the overload
#   limits are the profile's alone: variants differ only in rules.
# With routing.plugin false it prints {}: the chart's default-plugins.yaml stays. With it true, an EndpointPickerConfig
# named thesis-plugins.yaml: the default's plugins and weights, with thesis-run-router (gateway/runrouter) first, and
# the end-of-run port (P3.3) on the picker's container and Service, so the port is open exactly when the plugin runs.
set -euo pipefail

# shellcheck source=infra/profile.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/profile.sh"

profile=${1:?usage: infra/routing-values.sh <profile> [rules-file]}
rules_file=${2:-}

# The switch, the five rules, the pin TTL and the overload limits gateway/runrouter/flags.go requires, and no others,
# in every profile: a missing one would reach the plugin as null, which it refuses only when the picker starts, as a
# rollout timeout; an extra one, a typo, would let a variant's same typo through.
KEYS='["plugin", "affinity", "overload_guard", "parallel_spill", "priority", "warmup", "run_ttl_seconds",
  "overload_queue_threshold", "overload_sustained_seconds"]'
# The keys that are no rule but the profile's own numbers.
PROFILE_ONLY='["run_ttl_seconds", "overload_queue_threshold", "overload_sustained_seconds"]'
# Where the plugin serves POST /runs/{id}/end (D1, P3.3). Not a tunable: the picker's container already has 9002 (ext
# proc), 9003 (health) and 9090 (metrics), and 9010 is free next to them. infra/manifests/agent.yaml's PICKER_URL
# names the same port; the replayer finds it on the Service by its name.
END_OF_RUN_PORT=9010
END_OF_RUN_PORT_NAME=end-of-run

# map_or_refuse <json> <where>: a routing section must map rules to values; anything else has no rules to read.
map_or_refuse() {
  if [[ $(jq -r 'type' <<<"$1") != object ]]; then
    echo "routing-values: routing in $2 must be a map of rules, got $(jq -c . <<<"$1")" >&2
    exit 1
  fi
}

routing=$(profile_value "$profile" '.routing' | yq -o json)
map_or_refuse "$routing" "config/$profile.yaml"
missing=$(jq -rn --argjson keys "$KEYS" --argjson routing "$routing" '$keys - ($routing | keys) | map("routing." + .) | join(", ")')
if [[ -n $missing ]]; then
  echo "routing-values: config/$profile.yaml has no $missing" >&2
  exit 1
fi
extra=$(jq -rn --argjson keys "$KEYS" --argjson routing "$routing" '($routing | keys) - $keys | map("routing." + .) | join(", ")')
if [[ -n $extra ]]; then
  echo "routing-values: config/$profile.yaml has $extra, which no plugin reads" >&2
  exit 1
fi
if [[ -n $rules_file ]]; then
  rules=$(yq -o json '.routing' "$rules_file")
  map_or_refuse "$rules" "$rules_file"
  set_here=$(jq -r --argjson only "$PROFILE_ONLY" '[keys[] | select(IN($only[]))] | join(", ")' <<<"$rules")
  if [[ -n $set_here ]]; then
    echo "routing-values: $set_here in $rules_file: the profile's, a variant sets only rules" >&2
    exit 1
  fi
  unknown=$(jq -rn --argjson known "$routing" --argjson rules "$rules" '$rules | keys - ($known | keys) | join(", ")')
  if [[ -n $unknown ]]; then
    echo "routing-values: no rule $unknown in config/$profile.yaml" >&2
    exit 1
  fi
  routing=$(jq -n --argjson known "$routing" --argjson rules "$rules" '$known + $rules')
fi
if ! jq -e '.run_ttl_seconds | type == "number" and . == floor and . > 0' <<<"$routing" >/dev/null; then
  echo "routing-values: routing.run_ttl_seconds must be a positive whole number of seconds, got $(jq -c .run_ttl_seconds <<<"$routing")" >&2
  exit 1
fi
if ! jq -e '.overload_queue_threshold | type == "number" and . == floor and . >= 0' <<<"$routing" >/dev/null; then
  echo "routing-values: routing.overload_queue_threshold must be a whole number of calls, 0 or more, got $(jq -c .overload_queue_threshold <<<"$routing")" >&2
  exit 1
fi
if ! jq -e '.overload_sustained_seconds | type == "number" and . == floor and . > 0' <<<"$routing" >/dev/null; then
  echo "routing-values: routing.overload_sustained_seconds must be a positive whole number of seconds, got $(jq -c .overload_sustained_seconds <<<"$routing")" >&2
  exit 1
fi
# floor, so a number written 300.0 reaches the plugin as 300: jq prints a number as written, and the plugin's decoder
# refuses 300.0 for an int.
ttl=$(jq '.run_ttl_seconds | floor' <<<"$routing")
queue_threshold=$(jq '.overload_queue_threshold | floor' <<<"$routing")
sustained=$(jq '.overload_sustained_seconds | floor' <<<"$routing")
routing=$(jq --argjson only "$PROFILE_ONLY" 'with_entries(select(.key | IN($only[]) | not))' <<<"$routing")
if ! jq -e 'all(.[]; type == "boolean")' <<<"$routing" >/dev/null; then
  echo "routing-values: every rule must be true or false, got $(jq -c . <<<"$routing")" >&2
  exit 1
fi

if [[ $(jq -r '.plugin' <<<"$routing") != true ]]; then
  # A rule on with the plugin off would do nothing, and the variant would read as the rule's result.
  on=$(jq -r 'del(.plugin) | to_entries | map(select(.value) | .key) | join(", ")' <<<"$routing")
  if [[ -n $on ]]; then
    echo "routing-values: $on on, but plugin is off" >&2
    exit 1
  fi
  echo '{}'
  exit 0
fi
# The guard moves pinned runs only (P3.5): with affinity off it would do nothing, and the variant would read as its
# result.
if [[ $(jq -r '.overload_guard and (.affinity | not)' <<<"$routing") == true ]]; then
  echo "routing-values: overload_guard on, but affinity is off" >&2
  exit 1
fi

# The plugin's parameters, by the names gateway/runrouter/flags.go reads. plugin is the switch above, not a rule.
parameters=$(jq -c --argjson ttl "$ttl" --argjson threshold "$queue_threshold" --argjson sustained "$sustained" \
  --argjson port "$END_OF_RUN_PORT" '{
  affinity: .affinity,
  overloadGuard: .overload_guard,
  parallelSpill: .parallel_spill,
  priority: .priority,
  warmup: .warmup,
  runTTLSeconds: $ttl,
  overloadQueueThreshold: $threshold,
  overloadSustainedSeconds: $sustained,
  endOfRunPort: $port
}' <<<"$routing")

# The chart's default-plugins.yaml (llm-d-router-gateway v0.11.0, read from the picker's ConfigMap on 2026-11-10), with
# thesis-run-router added. It is a filter, so it takes no weight; with every rule off it returns every pod and the
# default's scorers decide, as on default-plugins.yaml. No picker is named, so it is max-score-picker, as there.
config=$(
  cat <<EOF
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: thesis-run-router
  parameters: $parameters
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
- type: metrics-data-source
  parameters:
    scheme: "http"
    path: "/metrics"
    insecureSkipVerify: true
- type: core-metrics-extractor
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: thesis-run-router
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
  - pluginRef: prefix-cache-scorer
    weight: 3
EOF
)
CONFIG=$config PORT=$END_OF_RUN_PORT PORT_NAME=$END_OF_RUN_PORT_NAME yq -n '
  .router.epp.pluginsConfigFile = "thesis-plugins.yaml" |
  .router.epp.pluginsCustomConfig["thesis-plugins.yaml"] = strenv(CONFIG) |
  .router.epp.pluginsCustomConfig["thesis-plugins.yaml"] style="literal" |
  .router.epp.extraContainerPorts = [{"name": strenv(PORT_NAME), "containerPort": env(PORT), "protocol": "TCP"}] |
  .router.extraServicePorts = [{"name": strenv(PORT_NAME), "port": env(PORT), "targetPort": env(PORT), "protocol": "TCP"}]'
