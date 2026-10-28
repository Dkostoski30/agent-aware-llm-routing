#!/usr/bin/env bash
# Checks the agent runtime in k3s (P1.3b): a real calculator task completes through the Gateway with the right answer,
# every LLM call of the run is in the Gateway's access log with the D1 headers, and the run's history is in Redis.
# Exit 1 if any of that is missing.
# The host is the k3s node, so the agent's ClusterIP is reachable from here (as in gateway_check.sh).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/agent_calls.sh"

AGENT_SERVICE=agent

fail() {
  echo "agent-check: FAIL: $*" >&2
  exit 1
}

agent_ip=$(kubectl get service "$AGENT_SERVICE" -o jsonpath='{.spec.clusterIP}') || fail "no Service $AGENT_SERVICE"
agent=http://$agent_ip:$AGENT_PORT

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
run_id=$(start_run "$agent") || fail "POST /runs to $agent failed"
echo "agent-check: run $run_id started: $TASK (seed $SEED)"

run=$(wait_run "$agent" "$run_id") || fail "GET /runs/$run_id failed"
status=$(jq -r .status <<<"$run")
[[ $status != running ]] || fail "run $run_id still running after ${RUN_TIMEOUT_SECONDS}s"

echo "== steps of $run_id"
jq -c '.steps[]' <<<"$run"
answer=$(jq -r '.answer // ""' <<<"$run")
echo "== status: $status, answer: $answer"
[[ $status == done ]] || fail "run ended $status: $(jq -r .error <<<"$run")"

key=thesis:run:$run_id
steps=$(redis HGET "$key" step)
[[ $steps =~ ^[1-9][0-9]*$ ]] || fail "no step count in $key"

echo "== Gateway access-log lines with run-id $run_id"
lines=$(run_access_lines "$run_id" "$since") || fail "cannot read the access log of $GATEWAY"
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
