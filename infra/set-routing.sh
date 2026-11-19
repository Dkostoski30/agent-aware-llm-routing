#!/usr/bin/env bash
# Puts the Endpoint Picker on a routing variant (P2.5) and restarts it, which empties its prefix index even when the
# variant did not change. Waits until a call passes through the gateway.
# Usage: infra/set-routing.sh <profile> [file]. With no file: the picker as make up installs it for the profile, on the
# profile's routing section (infra/routing-values.sh). The file is either Helm values on top of
# infra/helm/router-values.yaml, as a P2.5 variant's, or routing rules under a top-level routing key laid over the
# profile's (P3.2), as routing: {plugin: true}. A file with routing and other keys is refused: rules go to
# routing-values.sh and not to helm, so the other keys would be dropped without a word.
# The picker's image is the build of gateway/ as it is now (infra/picker-image.sh), unless the values file names another,
# as infra/helm/router-upstream-values.yaml does. After a change to gateway/, make picker-image comes first: this script
# asks for no sudo, so it never builds, and a tag missing from k3s fails the rollout.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=infra/router-chart.sh
source "$REPO_ROOT/infra/router-chart.sh"

# The EPP's image is on the node by now, so 300 s is only a bound on a picker that cannot start, as its config refused
# (infra/install-gateway.sh uses the same for a first pull).
ROLLOUT_TIMEOUT=300s
# The gateway reaches a restarted EPP through its Service and, with failureMode FailClose, fails requests until then.
# 30 tries, 2 s apart: a minute for that reconnection, far more than a Service needs.
PROBE_TRIES=30
PROBE_PAUSE_SECONDS=2
# A first call on a new pod took up to 37 s (P2.2b, P2.4).
PROBE_TIMEOUT_SECONDS=120

profile=${1:?usage: infra/set-routing.sh <profile> [file]}
file=${2:-}
values=
rules=
if [[ -n $file ]]; then
  # Printed and compared, not yq -e: -e reports false as an error on stderr. As assignments, so a file yq cannot parse
  # stops the script. yq v4 cannot compare arrays with ==, so the keys are counted.
  has_routing=$(yq 'has("routing")' "$file")
  key_count=$(yq 'keys | length' "$file")
  if [[ $has_routing == true ]]; then
    if [[ $key_count != 1 ]]; then
      echo "set-routing: $file has routing and other keys; routing rules and Helm values go in separate files" >&2
      exit 1
    fi
    rules=$file
  else
    values=$file
  fi
fi

# A values file and not --set: --set overrides every values file, so the variant's own image would never apply.
# Captured first, since a failure inside <(...) would not stop the script.
picker_values=$(bash "$REPO_ROOT/infra/picker-image.sh" --values)
routing_values=$(bash "$REPO_ROOT/infra/routing-values.sh" "$profile" ${rules:+"$rules"})
# The local copy make up pulled (router_chart_path), so a switch needs no network.
chart=$(router_chart_path)
helm upgrade --install vllm-pool "$chart" \
  --namespace default \
  --values "$REPO_ROOT/infra/helm/router-values.yaml" \
  --values <(echo "$picker_values") \
  --values <(echo "$routing_values") \
  ${values:+--values "$values"} \
  --wait --timeout "$ROLLOUT_TIMEOUT"
kubectl rollout restart deployment/vllm-pool-epp
kubectl rollout status deployment/vllm-pool-epp --timeout="$ROLLOUT_TIMEOUT"

gateway_ip=$(kubectl get service inference-gateway -o jsonpath='{.spec.clusterIP}')
model=$(yq -e '.model.lora.name // .model.name' "$REPO_ROOT/config/$profile.yaml")
# Shorter than one prefix-cache block (at least 64 tokens in llm-d-router v0.11.0), so the picker's index learns nothing.
body=$(jq -n --arg model "$model" '{
  model: $model,
  messages: [{role: "user", content: "Say OK."}],
  max_tokens: 1,
  chat_template_kwargs: {enable_thinking: false}
}')
for ((try = 1; try <= PROBE_TRIES; try++)); do
  if curl -sS --fail --max-time "$PROBE_TIMEOUT_SECONDS" -o /dev/null \
    -H 'Content-Type: application/json' -H 'x-run-id: set-routing-probe' -H 'x-step: 1' -H 'x-expected-steps: 1' \
    -d "$body" "http://$gateway_ip/v1/chat/completions"; then
    echo "set-routing: ${file:-config/$profile.yaml} is serving, on try $try"
    exit 0
  fi
  sleep "$PROBE_PAUSE_SECONDS"
done
echo "set-routing: no call passed the gateway in $PROBE_TRIES tries" >&2
exit 1
