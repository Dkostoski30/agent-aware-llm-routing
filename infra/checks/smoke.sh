#!/usr/bin/env bash
# make smoke (P1.5): one calculator task through every layer (app backend -> agent -> Gateway -> Endpoint Picker ->
# vLLM, history in Redis, metrics in Prometheus) on the cluster as it is, any profile. Since P3.12b the task is a chat
# message: a one-off user registers, logs in, creates a chat and sends it, and the answer is read from the chat; that
# user stays in the app's database until make reset-app. Prints "ok <layer>: ..." per check and stops at the first
# failure with "FAIL <layer>: <expected>, <seen>" and exit 1. The run-id links the app, gateway and redis checks; the
# Endpoint Picker logs no run-id, so the picker check is linked to the run by its time window.
# The host is the k3s node, so pod IPs and ClusterIPs are reachable from here (as in agent_check.sh).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/agent_calls.sh"

# Every Deployment a run passes through; each must have all its desired pods Ready, and at least one.
DEPLOYMENTS=(vllm inference-gateway vllm-pool-epp redis agent app)
APP_SELECTOR=app.kubernetes.io/name=app
APP_PORT=8080
# The cookie a login sets (app_backend/app.py, SESSION_COOKIE).
SESSION_COOKIE=session
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

# random_hex <bytes>: that many random bytes as lowercase hex.
random_hex() {
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

# app_request <path> [curl args]...: one request to the app backend as the smoke user; prints the body.
app_request() {
  local path=$1
  shift
  curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" --cookie "$SESSION_COOKIE=$session" "$@" "$app$path"
}

# wait_reply <chat id> <assistant message id>: reads the chat until that message is no longer running, or
# RUN_TIMEOUT_SECONDS have passed, and prints the message; after the timeout it still says running. Each read lets the
# app finish the message from the agent runtime (P3.12a).
wait_reply() {
  local message waited=0
  while true; do
    message=$(app_request "/chats/$1" | jq -ec --argjson id "$2" '.messages[] | select(.id == $id)') || return 1
    if [[ $(jq -r .status <<<"$message") != running ]] || ((waited >= RUN_TIMEOUT_SECONDS)); then
      echo "$message"
      return 0
    fi
    sleep "$POLL_SECONDS"
    waited=$((waited + POLL_SECONDS))
  done
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

# The Ready app pod's own IP (the app runs one replica), not the Service: after a restart this node's Service rules
# led to the old pod for more than 10 s (the agent's, P1.6c, experiments/log.md).
app_ip=$(kubectl get pods -l "$APP_SELECTOR" -o json | jq -er '
  [.items[] | select(.metadata.deletionTimestamp == null)
    | select(any(.status.conditions[]?; .type == "Ready" and .status == "True")) | .status.podIP]
  | select(length == 1) | .[0]') ||
  fail app "one Ready app pod that is not being deleted expected, found none or several"
app=http://$app_ip:$APP_PORT

# A new user each time: register answers 409 for an email that exists. .invalid is a reserved domain (RFC 2606), so
# the address can never be someone's. 16 random bytes give a password above app.min_password_length in every profile.
email=smoke-$(random_hex 6)@smoke.invalid
credentials=$(jq -n --arg email "$email" --arg password "$(random_hex 16)" '{email: $email, password: $password}')
curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -H 'Content-Type: application/json' -d "$credentials" \
  -o /dev/null "$app/auth/register" || fail app "POST /auth/register of $email answered, it failed"
# The session cookie is Secure (app.cookie_secure), and curl neither keeps nor sends a Secure cookie over plain http to
# a pod IP, so the token is read from the login's Set-Cookie header and sent by hand (app_request).
session=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -H 'Content-Type: application/json' \
  -d "$credentials" -D - -o /dev/null "$app/auth/login" | tr -d '\r' |
  sed -n "s/^set-cookie: $SESSION_COOKIE=\([^;]*\);.*\$/\1/Ip") ||
  fail app "POST /auth/login of $email answered, it failed"
[[ -n $session ]] || fail app "a $SESSION_COOKIE cookie from POST /auth/login of $email expected, none set"
chat_id=$(app_request /chats -H 'Content-Type: application/json' -d '{"title": "make smoke"}' | jq -er .id) ||
  fail app "POST /chats answered with an id, it failed"

# The window the run's picks fall in, in whole seconds: picker timestamps are compared at that resolution, which can
# only widen the window by less than a second at its start.
from=$(date +%s)
since=$(date -u -d "@$from" +%Y-%m-%dT%H:%M:%SZ)
message=$(jq -n --arg task "$TASK" --argjson seed "$SEED" '{task: $task, seed: $seed}')
sent=$(app_request "/chats/$chat_id/messages" -H 'Content-Type: application/json' -d "$message") ||
  fail app "POST /chats/$chat_id/messages answered, it failed"
run_id=$(jq -er .run_id <<<"$sent") || fail app "a run_id from POST /chats/$chat_id/messages expected, got: $sent"
reply_id=$(jq -er .assistant_message_id <<<"$sent") ||
  fail app "an assistant_message_id from POST /chats/$chat_id/messages expected, got: $sent"
reply=$(wait_reply "$chat_id" "$reply_id") || fail app "GET /chats/$chat_id with message $reply_id answered, it failed"
to=$(date +%s)
status=$(jq -r .status <<<"$reply")
text=$(jq -r .text <<<"$reply")
[[ $(jq -r .run_id <<<"$reply") == "$run_id" ]] ||
  fail app "message $reply_id with run $run_id expected, got: $reply"
[[ $status != running ]] || fail app "run $run_id done within ${RUN_TIMEOUT_SECONDS}s expected, still running"
[[ $status == done ]] || fail app "run $run_id done expected, ended $status: $text"
[[ $text == *"$EXPECTED_ANSWER"* ]] || fail app "run $run_id answer with $EXPECTED_ANSWER expected, got: $text"
echo "ok app: chat $chat_id of $email, run $run_id ($TASK, seed $SEED) done in $((to - from)) s, answer: $text"

# The run's detail as the browser gets it: through the app, which passes on the agent runtime's answer (P3.12b).
run=$(app_request "/runs/$run_id") || fail app "GET /runs/$run_id through the app answered, it failed"
[[ $(jq -r .status <<<"$run") == done ]] ||
  fail app "GET /runs/$run_id through the app with status done expected, got: $run"
echo "ok app: GET /runs/$run_id through the app answers status done"

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
# Only Running pods that are not being deleted must have a sample. A pod the kubelet rejected at GPU admission after a
# reboot or make down + up stays Failed (STATUS Unknown in kubectl get pods), is never scraped, and its ReplicaSet does
# not delete it (P2.11). The pods check above already needs every desired pod of the Deployment Ready; pods in another
# phase are only named.
vllm_pods=$(kubectl get pods -l "$VLLM_SELECTOR" --field-selector=status.phase=Running -o json |
  jq -c '[.items[] | select(.metadata.deletionTimestamp == null) | .metadata.name] | sort') ||
  fail prometheus "the Running vllm pods listed, kubectl failed"
skipped=$(kubectl get pods -l "$VLLM_SELECTOR" --field-selector=status.phase!=Running -o json |
  jq -c '[.items[].metadata.name] | sort') ||
  fail prometheus "the vllm pods that are not Running listed, kubectl failed"
missing=$(jq -n -c --argjson want "$vllm_pods" --argjson seen "$seen_pods" '$want - $seen')
[[ $missing == '[]' ]] ||
  fail prometheus "a $VLLM_METRIC sample for every Running vllm pod $vllm_pods expected, none for $missing"
note=
[[ $skipped == '[]' ]] || note="; not Running, skipped: $skipped"
echo "ok prometheus: $VLLM_METRIC has a sample for $vllm_pods$note"
