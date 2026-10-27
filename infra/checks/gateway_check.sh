#!/usr/bin/env bash
# Checks the inference gateway (P1.2): chat requests through the Gateway must reach every Ready vLLM pod in the
# InferencePool, the access log must carry the D1 headers, and the Endpoint Picker must log a choice for every request.
# Exit 1 if any of that is missing.
# The host is the k3s node, so the Gateway's ClusterIP is reachable from here (as in service_check.sh).
set -euo pipefail

GATEWAY=inference-gateway
EPP=vllm-pool-epp
VLLM_SERVICE=vllm
VLLM_SELECTOR=app.kubernetes.io/name=vllm
# Requests in flight at once. The default picker scores queue length, so concurrent requests are what make it use a
# second pod; sent one at a time, every request may find the same pod idle.
CONCURRENCY=4
# Five rounds of CONCURRENCY requests, so the queue scorer sees several moments with requests in flight on a pod.
# The pick is a score, not a coin toss, so no probability backs the number; the passing runs split 13/8 and 10/11
# (experiments/log.md, P1.2).
SPREAD_REQUESTS=20
# Covers one short answer from a busy 0.6B (as in service_check.sh).
REQUEST_TIMEOUT_SECONDS=60
RUN_ID=p12-check

fail() {
  echo "gateway-check: FAIL: $*" >&2
  exit 1
}

gateway_ip=$(kubectl get service "$GATEWAY" -o jsonpath='{.spec.clusterIP}') || fail "no Service $GATEWAY"
vllm_ip=$(kubectl get service "$VLLM_SERVICE" -o jsonpath='{.spec.clusterIP}') || fail "no Service $VLLM_SERVICE"
# The model id the pods serve, read from the vLLM Service directly, not through the picker under test.
model=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "http://$vllm_ip:8000/v1/models" | jq -er '.data[0].id')

# Pod IP -> pod name of every Ready vLLM pod, to name the endpoint of each access-log line.
declare -A pod_by_ip
while read -r name ip; do
  pod_by_ip[$ip]=$name
done < <(kubectl get pods -l "$VLLM_SELECTOR" \
  -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name} {.status.podIP}{"\n"}{end}')
((${#pod_by_ip[@]} > 0)) || fail "no Ready pods with label $VLLM_SELECTOR"
echo "gateway-check: Gateway $GATEWAY at http://$gateway_ip, Ready pods: ${pod_by_ip[*]}"

# chat [curl option]...: one short chat completion through the Gateway. The whole prompt is shorter than one
# prefix-cache block (at least 64 tokens in llm-d-router v0.11.0), so the prefix-cache scorer finds nothing to match
# and the queue and KV-cache scorers alone decide the pod.
chat() {
  jq -n --arg model "$model" '{
    model: $model,
    messages: [{role: "user", content: "Say OK."}],
    max_tokens: 1,
    chat_template_kwargs: {enable_thinking: false}
  }' | curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -o /dev/null \
    -H 'Content-Type: application/json' "$@" -d @- "http://$gateway_ip/v1/chat/completions"
}
export -f chat
export model gateway_ip REQUEST_TIMEOUT_SECONDS

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# One chat per line of seq; xargs keeps CONCURRENCY of them in flight.
seq "$SPREAD_REQUESTS" | xargs -P "$CONCURRENCY" -I {} bash -c chat ||
  fail "a request through the Gateway failed"
chat -H "x-run-id: $RUN_ID" -H 'x-step: 1' -H 'x-expected-steps: 1' ||
  fail "the request with the D1 headers failed"
sent=$((SPREAD_REQUESTS + 1))

# Lines that are not JSON (the proxy's startup messages) are skipped.
access=$(kubectl logs "deployment/$GATEWAY" --since-time="$since" |
  jq -c -R 'fromjson? | select(.["http.path"] == "/v1/chat/completions")') ||
  fail "cannot read the access log of $GATEWAY"

echo "== chat requests per pod since $since, from the Gateway's access log"
missing=0
for ip in "${!pod_by_ip[@]}"; do
  count=$(jq -s --arg ip "$ip" '[.[] | select((.endpoint // "") | startswith($ip + ":"))] | length' <<<"$access")
  echo "${pod_by_ip[$ip]}: $count"
  if ((count == 0)); then
    missing=$((missing + 1))
  fi
done
((missing == 0)) || fail "$missing Ready pod(s) in the pool got no request through the Gateway"

echo "== access-log line of the request with x-run-id $RUN_ID"
jq -e -c --arg id "$RUN_ID" \
  'select(.["run-id"] == $id and .step == "1" and .["expected-steps"] == "1")' <<<"$access" ||
  fail "no access-log line with run-id $RUN_ID, step 1 and expected-steps 1"

echo "== Endpoint Picker choices since $since"
# The EPP logs OpenTelemetry-style JSON: the message is in body (llm-d-router docs/otel-json-stdout.md).
handled=$(kubectl logs "deployment/$EPP" --since-time="$since" |
  jq -c -R 'fromjson? | select(.body == "Request handled") | {timestamp, endpoint}') ||
  fail "cannot read the logs of $EPP"
# grep -c exits 1 when it counts 0 lines; 0 is a valid count here and is judged below.
picks=$(grep -c . <<<"$handled" || true)
tail -n 3 <<<"$handled"
echo "$EPP logged $picks choices for $sent requests"
((picks >= sent)) || fail "the Endpoint Picker logged fewer choices than requests sent"
echo "gateway-check: OK"
