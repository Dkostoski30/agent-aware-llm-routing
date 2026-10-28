# Sourced by infra/checks/agent_check.sh (P1.3b) and infra/checks/smoke.sh (P1.5): the calculator task both send
# through the agent runtime, and the calls that start it, wait for it and read its traces. A function that fails
# returns non-zero and says nothing more, so each caller reports the failure in its own format.

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

# start_run <agent url>: starts the task and prints its run-id.
start_run() {
  jq -n --arg task "$TASK" --argjson seed "$SEED" '{task: $task, seed: $seed}' |
    curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -H 'Content-Type: application/json' -d @- \
      "$1/runs" | jq -er .run_id
}

# wait_run <agent url> <run-id>: polls the run until its status is no longer running, or RUN_TIMEOUT_SECONDS have
# passed, and prints its last state; after the timeout that state still says running.
wait_run() {
  local run waited=0
  while true; do
    run=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "$1/runs/$2") || return 1
    if [[ $(jq -r .status <<<"$run") != running ]] || ((waited >= RUN_TIMEOUT_SECONDS)); then
      echo "$run"
      return 0
    fi
    sleep "$POLL_SECONDS"
    waited=$((waited + POLL_SECONDS))
  done
}

# run_access_lines <run-id> <since>: the run's lines in the Gateway's access log since <since> (RFC 3339), one compact
# JSON object per line with step, expected-steps and endpoint. Lines that are not JSON (the proxy's startup messages)
# are skipped.
run_access_lines() {
  kubectl logs "deployment/$GATEWAY" --since-time="$2" |
    jq -c -R --arg id "$1" \
      'fromjson? | select(.["run-id"] == $id) | {step, "expected-steps": .["expected-steps"], endpoint}'
}

# redis <arg>...: one redis-cli command in the Redis pod; without a terminal redis-cli prints raw values, one per line.
redis() {
  kubectl exec deployment/redis -- redis-cli "$@"
}
