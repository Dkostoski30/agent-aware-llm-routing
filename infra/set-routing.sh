#!/usr/bin/env bash
# Puts the Endpoint Picker on a routing variant (P2.5) and restarts it, which empties its prefix index even when the
# variant did not change. Waits until a call passes through the gateway.
# Usage: infra/set-routing.sh <profile> [values-file]. With no values file: the default picker, as make up installs it.
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

profile=${1:?usage: infra/set-routing.sh <profile> [values-file]}
values=${2:-}

helm upgrade --install vllm-pool "$ROUTER_CHART" \
  --namespace default \
  --values "$REPO_ROOT/infra/helm/router-values.yaml" \
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
    echo "set-routing: ${values:-default} is serving, on try $try"
    exit 0
  fi
  sleep "$PROBE_PAUSE_SECONDS"
done
echo "set-routing: no call passed the gateway in $PROBE_TRIES tries" >&2
exit 1
