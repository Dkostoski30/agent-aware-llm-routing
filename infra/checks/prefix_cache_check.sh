#!/usr/bin/env bash
# Shows that vLLM reuses a cached prefix and that the reuse appears in /metrics (P0.5).
# Sends the same long prompt twice and prints every prefix_cache metric before and after, plus the time to first
# token of both requests. Exit 1 unless the second request added more prefix-cache hits than the first
# (the first can hit a few shared chat-template blocks; the second should hit almost the whole prompt).
# Usage: prefix_cache_check.sh [PORT]   (default 8000, the vllm-run port)
set -euo pipefail

PORT=${1:-8000}
BASE_URL=http://localhost:$PORT

# 90 lines of about 22 tokens each (measured in P0.5) give the ~2000-token prompt the card asks for: long enough
# that prefill dominates the request time, short enough to fit max_model_len of every profile.
PROMPT_LINES=90
# Covers prefilling ~2000 tokens on a cold 4B; the 0.6B needs well under a second.
REQUEST_TIMEOUT_SECONDS=60

fail() {
  echo "prefix-check: FAIL: $*" >&2
  exit 1
}

# prefix_metrics: prints every sample of a metric named *prefix_cache*, without HELP/TYPE comments and without the
# *_created series (the counter's creation timestamp, not a count).
prefix_metrics() {
  local metrics
  metrics=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "$BASE_URL/metrics") || return 1
  awk '!/^#/ {name = $1; sub(/\{.*/, "", name); if (name ~ /prefix_cache/ && name !~ /_created$/) print}' <<<"$metrics"
}

# hits <metrics lines>: sums the local prefix-cache hit counters (one per engine); external_* counts a KV connector's
# cache, which is not used here.
hits() {
  awk '$1 ~ /^vllm:prefix_cache_hits/ {sum += $NF} END {printf "%.0f\n", sum}' <<<"$1"
}

# ttft <json body>: with max_tokens 1 the whole request is prefill plus one token, so its total time is the TTFT.
ttft() {
  curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" -o /dev/null -w '%{time_total}' \
    -H 'Content-Type: application/json' -d "$1" "$BASE_URL/v1/chat/completions"
}

model=$(curl -sS --fail --max-time "$REQUEST_TIMEOUT_SECONDS" "$BASE_URL/v1/models" | jq -er '.data[0].id') ||
  fail "no server at $BASE_URL (is make vllm-run running?)"

# The run marker comes first, so a rerun of this check starts from an uncached prompt; the rest is fixed text.
prompt="Run $(date +%s%N). Read the delivery terms below, then answer with one word."$'\n'
for ((i = 1; i <= PROMPT_LINES; i++)); do
  prompt+="Clause $i: the supplier delivers batch $i within $((i % 30 + 1)) days of the written order."$'\n'
done
body=$(jq -n --arg model "$model" --arg prompt "$prompt" '{
  model: $model,
  messages: [{role: "user", content: $prompt}],
  max_tokens: 1,
  temperature: 0,
  chat_template_kwargs: {enable_thinking: false}
}')

before=$(prefix_metrics) || fail "cannot read $BASE_URL/metrics"
[[ -n $before ]] || fail "no prefix_cache metrics at $BASE_URL/metrics"
first=$(ttft "$body") || fail "first request failed"
middle=$(prefix_metrics) || fail "cannot read $BASE_URL/metrics"
second=$(ttft "$body") || fail "second request failed"
after=$(prefix_metrics) || fail "cannot read $BASE_URL/metrics"

echo "== prefix_cache metrics before"
echo "$before"
echo "== prefix_cache metrics after"
echo "$after"
echo "== requests (model $model)"
echo "TTFT first:  ${first}s"
echo "TTFT second: ${second}s"

first_hits=$(($(hits "$middle") - $(hits "$before")))
second_hits=$(($(hits "$after") - $(hits "$middle")))
echo "prefix-cache hits added: first request $first_hits, second request $second_hits"
if ((second_hits <= first_hits)); then
  fail "the second request did not hit the prefix cache"
fi
if awk -v a="$first" -v b="$second" 'BEGIN {exit !(b >= a)}'; then
  echo "prefix-check: WARNING: second TTFT is not lower than the first"
fi
echo "prefix-check: OK"
