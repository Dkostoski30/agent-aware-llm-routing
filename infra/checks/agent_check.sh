#!/usr/bin/env bash
# Checks the agent runtime in k3s (P1.3b): a real calculator task completes through the Gateway with the right answer,
# every LLM call of the run is in the Gateway's access log with the D1 headers, and the run's history is in Redis.
# Exit 1 if any of that is missing.
# The host is the k3s node, so the agent's ClusterIP is reachable from here (as in gateway_check.sh).
set -euo pipefail

AGENT_SERVICE=agent
AGENT_PORT=8080
GATEWAY=inference-gateway
# One operation: the 0.6B answers it 10/10 but cannot chain two ("7 times 8 plus 3" was 0/10 with every prompt and
# setting tried, P1.3b, experiments/log.md). The run is still a chain of two calls sharing one context.
TASK='What is 7 times 8?'
EXPECTED_ANSWER=56
# Fixed, so a rerun sends the same sampling seed with every call.
SEED=7
# A run makes at most agent.max_steps (10) calls; P0.4 measured p95 9.5 s per call with 4 in flight per pod, and this
# run is alone on the cluster. 300 s covers 10 such calls with a wide margin.
RUN_TIMEOUT_SECONDS=300
POLL_SECONDS=2
# One call to the agent's API, which answers from Redis without waiting for the model.
REQUEST_TIMEOUT_SECONDS=10

fail() {
  echo "agent-check: FAIL: $*" >&2
  exit 1
}

agent_ip=$(kubectl get service "$AGENT_SERVICE" -o jsonpath='{.spec.clusterIP}') || fail "no Service $AGENT_SERVICE"
agent=http://$agent_ip:$AGENT_PORT

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
run_id=$(jq -n --arg task "$TASK" --argjson seed "$SEED" '{task: $task, seed: $seed}' |
  curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -H 'Content-Type: application/json' -d @- \
    "$agent/runs" | jq -er .run_id) || fail "POST /runs to $agent failed"
echo "agent-check: run $run_id started: $TASK (seed $SEED)"

waited=0
while true; do
  run=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "$agent/runs/$run_id") ||
    fail "GET /runs/$run_id failed"
  status=$(jq -r .status <<<"$run")
  [[ $status == running ]] || break
  ((waited < RUN_TIMEOUT_SECONDS)) || fail "run $run_id still running after ${RUN_TIMEOUT_SECONDS}s"
  sleep "$POLL_SECONDS"
  waited=$((waited + POLL_SECONDS))
done

echo "== steps of $run_id"
jq -c '.steps[]' <<<"$run"
answer=$(jq -r '.answer // ""' <<<"$run")
echo "== status: $status, answer: $answer"
[[ $status == done ]] || fail "run ended $status: $(jq -r .error <<<"$run")"

# redis-cli without a terminal prints raw values, one per line.
redis() {
  kubectl exec deployment/redis -- redis-cli "$@"
}
key=thesis:run:$run_id
steps=$(redis HGET "$key" step)
[[ $steps =~ ^[1-9][0-9]*$ ]] || fail "no step count in $key"

echo "== Gateway access-log lines with run-id $run_id"
# Lines that are not JSON (the proxy's startup messages) are skipped.
lines=$(kubectl logs "deployment/$GATEWAY" --since-time="$since" |
  jq -c -R --arg id "$run_id" \
    'fromjson? | select(.["run-id"] == $id) | {step, "expected-steps": .["expected-steps"], endpoint}') ||
  fail "cannot read the access log of $GATEWAY"
echo "$lines"
seen=$(jq -s -c '[.[].step | tonumber] | sort' <<<"$lines")
want=$(jq -n -c --argjson n "$steps" '[range(1; $n + 1)]')
[[ $seen == "$want" ]] || fail "access-log steps $seen, expected $want (one line per call)"
jq -s -e 'all(.[]; .["expected-steps"] != null)' <<<"$lines" >/dev/null ||
  fail "an access-log line of the run has no expected-steps"

echo "== Redis: $key"
redis HGETALL "$key"
[[ $(redis HGET "$key" status) == done ]] || fail "$key does not say status done"
messages=$(redis GET "$key:messages" | jq -e length) || fail "no message list in $key:messages"
echo "$key:messages: $messages messages"

# Last, so a wrong answer from the model never hides whether the Gateway and Redis did their part.
[[ $answer == *"$EXPECTED_ANSWER"* ]] || fail "the answer does not contain $EXPECTED_ANSWER: $answer"
echo "agent-check: OK"
