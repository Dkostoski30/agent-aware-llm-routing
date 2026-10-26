#!/usr/bin/env bash
# Checks the vLLM Service in k3s (P0.7): runs chat_check.sh through the Service, then sends more requests and counts
# from the pod logs which pod answered each one. Exit 1 if a check fails or a Ready pod answered nothing.
# The host is the k3s node, so the ClusterIP is reachable from here. kubectl port-forward is not used: it tunnels
# to one pod and would never show the Service spreading requests.
set -euo pipefail

SERVICE=vllm
SELECTOR=app.kubernetes.io/name=vllm
# kube-proxy picks a pod at random per connection; with 2 pods the chance that 20 requests all land on one is
# 2 × 0.5^20, about 1 in 500 000, so a pod without requests means it is not in the Service.
SPREAD_REQUESTS=20
# Covers one short answer from a busy 0.6B; chat_check.sh has its own timeout.
REQUEST_TIMEOUT_SECONDS=60

CHECKS_DIR=$(dirname "${BASH_SOURCE[0]}")

fail() {
  echo "service-check: FAIL: $*" >&2
  exit 1
}

ip=$(kubectl get service "$SERVICE" -o jsonpath='{.spec.clusterIP}') || fail "no Service $SERVICE"
base_url=http://$ip:8000
mapfile -t pods < <(kubectl get pods -l "$SELECTOR" \
  -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name}{"\n"}{end}')
((${#pods[@]} > 0)) || fail "no Ready pods with label $SELECTOR"
echo "service-check: Service $SERVICE at $base_url, Ready pods: ${pods[*]}"

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
bash "$CHECKS_DIR/chat_check.sh" "$base_url"

model=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "$base_url/v1/models" | jq -er '.data[0].id')
body=$(jq -n --arg model "$model" '{
  model: $model,
  messages: [{role: "user", content: "Say OK."}],
  max_tokens: 1,
  chat_template_kwargs: {enable_thinking: false}
}')
for ((i = 1; i <= SPREAD_REQUESTS; i++)); do
  curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -o /dev/null \
    -H 'Content-Type: application/json' -d "$body" "$base_url/v1/chat/completions" ||
    fail "request $i through the Service failed"
done

echo "== chat requests answered per pod since $since"
missing=0
for pod in "${pods[@]}"; do
  logs=$(kubectl logs "$pod" --since-time="$since") || fail "cannot read the logs of $pod"
  # vLLM's access log has one line per request, e.g. 10.42.0.1:51234 - "POST /v1/chat/completions HTTP/1.1" 200 OK
  count=$(awk '/"POST \/v1\/chat\/completions HTTP\/1.1" 200/ {n++} END {print n + 0}' <<<"$logs")
  echo "$pod: $count"
  if ((count == 0)); then
    missing=$((missing + 1))
  fi
done
((missing == 0)) || fail "$missing Ready pod(s) answered no request through the Service"
echo "service-check: OK"
