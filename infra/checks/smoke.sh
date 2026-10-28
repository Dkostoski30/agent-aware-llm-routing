#!/usr/bin/env bash
# make smoke (P1.5): one calculator task through every layer (agent -> Gateway -> Endpoint Picker -> vLLM, history in
# Redis, metrics in Prometheus) on the cluster as it is, any profile. Prints "ok <layer>: ..." per check and stops at
# the first failure with "FAIL <layer>: <expected>, <seen>" and exit 1. The run-id links the agent, gateway and redis
# checks; the Endpoint Picker logs no run-id, so the picker check is linked to the run by its time window.
# The host is the k3s node, so pod IPs and ClusterIPs are reachable from here (as in agent_check.sh).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/agent_calls.sh"

# Every Deployment a run passes through; each must have all its desired pods Ready, and at least one.
DEPLOYMENTS=(vllm inference-gateway vllm-pool-epp redis agent)
AGENT_SELECTOR=app.kubernetes.io/name=agent
VLLM_SELECTOR=app.kubernetes.io/name=vllm
EPP=vllm-pool-epp
PROMETHEUS_SERVICE=monitoring-kube-prometheus-prometheus
PROMETHEUS_NAMESPACE=monitoring
PROMETHEUS_PORT=9090
# A gauge every vLLM pod exports from startup; the PodMonitor labels each series with its pod (P1.1).
VLLM_METRIC=vllm:num_requests_running
# One instant query over a few series answers in milliseconds; 10 s means Prometheus is not answering.
PROMETHEUS_TIMEOUT_SECONDS=10

# fail <layer> <message>
fail() {
  echo "FAIL $1: $2" >&2
  exit 1
}

summary=
for deployment in "${DEPLOYMENTS[@]}"; do
  counts=$(kubectl get deployment "$deployment" -o jsonpath='{.spec.replicas} {.status.readyReplicas}') ||
    fail pods "a Deployment $deployment expected, kubectl found none"
  read -r desired ready <<<"$counts"
  # The status leaves readyReplicas out when no pod is Ready.
  ready=${ready:-0}
  # At least one: a Deployment scaled to 0 has all of its 0 pods Ready, but nothing can serve the run.
  ((desired >= 1 && ready == desired)) ||
    fail pods "$deployment: all of at least 1 pod Ready expected, $ready of $desired Ready"
  summary+=" $deployment $ready/$desired"
done
echo "ok pods:$summary"

# The Ready agent pod's own IP (the agent runs one replica), not the Service: after an agent restart this node's
# Service rules led to the old pod for more than 10 s (P1.6c, experiments/log.md).
agent_ip=$(kubectl get pods -l "$AGENT_SELECTOR" -o json | jq -er '
  [.items[] | select(.metadata.deletionTimestamp == null)
    | select(any(.status.conditions[]?; .type == "Ready" and .status == "True")) | .status.podIP]
  | select(length == 1) | .[0]') ||
  fail agent "one Ready agent pod that is not being deleted expected, found none or several"
agent=http://$agent_ip:$AGENT_PORT

# The window the run's picks fall in, in whole seconds: picker timestamps are compared at that resolution, which can
# only widen the window by less than a second at its start.
from=$(date +%s)
since=$(date -u -d "@$from" +%Y-%m-%dT%H:%M:%SZ)
run_id=$(start_run "$agent") || fail agent "POST /runs to $agent answered, it failed"
run=$(wait_run "$agent" "$run_id") || fail agent "GET /runs/$run_id answered, it failed"
to=$(date +%s)
status=$(jq -er .status <<<"$run") ||
  fail agent "a run state with a status from GET /runs/$run_id expected, got: $run"
answer=$(jq -r '.answer // ""' <<<"$run")
[[ $status != running ]] || fail agent "run $run_id done within ${RUN_TIMEOUT_SECONDS}s expected, still running"
[[ $status == done ]] || fail agent "run $run_id done expected, ended $status: $(jq -r .error <<<"$run")"
[[ $answer == *"$EXPECTED_ANSWER"* ]] || fail agent "run $run_id answer with $EXPECTED_ANSWER expected, got: $answer"
echo "ok agent: run $run_id ($TASK, seed $SEED) done in $((to - from)) s, answer: $answer"

lines=$(run_access_lines "$run_id" "$since") || fail gateway "the access log of $GATEWAY readable, kubectl logs failed"
seen=$(jq -s -c '[.[].step | tonumber] | sort' <<<"$lines") ||
  fail gateway "a numeric step on every line of run $run_id expected, a line has none"
steps=$(jq -s length <<<"$lines")
want=$(jq -n -c --argjson n "$steps" '[range(1; $n + 1)]')
((steps >= 1)) && [[ $seen == "$want" ]] ||
  fail gateway "one line per step 1..n with run-id $run_id expected, saw steps $seen"
jq -s -e 'all(.[]; .["expected-steps"] != null)' <<<"$lines" >/dev/null ||
  fail gateway "expected-steps on every line of run $run_id expected, a line has none"
endpoints=$(jq -s -c '[.[].endpoint] | unique' <<<"$lines")
echo "ok gateway: $steps lines with run-id $run_id, steps $seen, endpoints $endpoints"

# One pick per LLM call; other traffic in the window can only add picks.
epp_log=$(kubectl logs "deployment/$EPP" --since-time="$since") ||
  fail picker "the logs of $EPP readable, kubectl logs failed"
picks=$(jq -R -n --argjson from "$from" --argjson to "$to" '[inputs | fromjson? | select(.body == "Request handled")
    | select((.timestamp | sub("\\.[0-9]+Z$"; "Z") | fromdate) as $t | $t >= $from and $t <= $to)] | length' \
  <<<"$epp_log") ||
  fail picker "an RFC 3339 UTC timestamp on every \"Request handled\" line of $EPP expected, jq could not read one"
((picks >= steps)) ||
  fail picker "at least $steps picks (one per step) in the run's window expected, saw $picks"
echo "ok picker: $picks picks by $EPP in the run's window, for $steps steps"

key=thesis:run:$run_id
redis_status=$(redis HGET "$key" status) || fail redis "$key readable, redis-cli failed"
redis_steps=$(redis HGET "$key" step) || fail redis "$key readable, redis-cli failed"
[[ $redis_status == done ]] || fail redis "$key status done expected, saw '$redis_status'"
[[ $redis_steps == "$steps" ]] || fail redis "$key step $steps (the Gateway's line count) expected, saw '$redis_steps'"
echo "ok redis: $key status done, step $redis_steps"

prometheus_ip=$(kubectl get service --namespace "$PROMETHEUS_NAMESPACE" "$PROMETHEUS_SERVICE" \
  -o jsonpath='{.spec.clusterIP}') || fail prometheus "a Service $PROMETHEUS_SERVICE expected, kubectl found none"
seen_pods=$(curl -sS --fail --max-time "$PROMETHEUS_TIMEOUT_SECONDS" --get --data-urlencode "query=$VLLM_METRIC" \
  "http://$prometheus_ip:$PROMETHEUS_PORT/api/v1/query" | jq -ec '[.data.result[].metric.pod] | unique') ||
  fail prometheus "an answer to the query $VLLM_METRIC expected, it failed"
vllm_pods=$(kubectl get pods -l "$VLLM_SELECTOR" -o json |
  jq -c '[.items[] | select(.metadata.deletionTimestamp == null) | .metadata.name] | sort') ||
  fail prometheus "the vllm pods listed, kubectl failed"
missing=$(jq -n -c --argjson want "$vllm_pods" --argjson seen "$seen_pods" '$want - $seen')
[[ $missing == '[]' ]] ||
  fail prometheus "a $VLLM_METRIC sample for every vllm pod $vllm_pods expected, none for $missing"
echo "ok prometheus: $VLLM_METRIC has a sample for $vllm_pods"
