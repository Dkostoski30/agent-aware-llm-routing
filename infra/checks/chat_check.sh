#!/usr/bin/env bash
# Checks that a vLLM server answers a chat request and returns a tool call (P0.3). Also used against the
# Kubernetes Service in P0.7, so it takes a base URL instead of only a port.
# Usage: chat_check.sh [BASE_URL]   (default http://localhost:8000). Exit 1 if either check fails.
set -euo pipefail

BASE_URL=${1:-http://localhost:8000}

# One non-streaming answer from the 0.6B takes a few seconds; 120 s also covers a busy 4B on this GPU.
REQUEST_TIMEOUT_SECONDS=120

fail() {
  echo "chat-check: FAIL: $*" >&2
  exit 1
}

# post <path> <json body>: prints the response body; fails on a connection error or an HTTP error status.
post() {
  curl -sS --fail-with-body --max-time "$REQUEST_TIMEOUT_SECONDS" \
    -H 'Content-Type: application/json' -d "$2" "$BASE_URL$1"
}

if ! models=$(curl -sS --fail-with-body --max-time "$REQUEST_TIMEOUT_SECONDS" "$BASE_URL/v1/models"); then
  fail "no server at $BASE_URL (is make vllm-run running?)"
fi
model=$(jq -er '.data[0].id' <<<"$models") || fail "no model listed at $BASE_URL/v1/models"
echo "chat-check: model $model at $BASE_URL"

# Thinking mode is off for every call in the thesis (CLAUDE.md); temperature 0 makes the check repeatable.
chat_body=$(jq -n --arg model "$model" '{
  model: $model,
  messages: [{role: "user", content: "In one sentence, what is Kubernetes?"}],
  temperature: 0,
  chat_template_kwargs: {enable_thinking: false}
}')
response=$(post /v1/chat/completions "$chat_body") || fail "chat request: $response"
answer=$(jq -er '.choices[0].message.content | select(length > 0)' <<<"$response") ||
  fail "chat request returned no text: $response"
echo "== 1. chat answer"
echo "$answer"

# The tool argument is an enum, as every agent tool argument is where possible (D10).
tool_body=$(jq -n --arg model "$model" '{
  model: $model,
  messages: [{role: "user", content: "What is the weather in Ohrid right now?"}],
  tools: [{
    type: "function",
    function: {
      name: "get_weather",
      description: "Current weather for a city.",
      parameters: {
        type: "object",
        properties: {city: {type: "string", enum: ["Skopje", "Ohrid"]}},
        required: ["city"]
      }
    }
  }],
  tool_choice: "auto",
  temperature: 0,
  chat_template_kwargs: {enable_thinking: false}
}')
response=$(post /v1/chat/completions "$tool_body") || fail "tool request: $response"
tool_calls=$(jq -e '.choices[0].message.tool_calls | select(length > 0)' <<<"$response") ||
  fail "tool request returned no tool_calls: $response"
jq -e '.[0].function.name == "get_weather"' <<<"$tool_calls" >/dev/null ||
  fail "tool call is not get_weather: $tool_calls"
echo "== 2. tool_calls"
echo "$tool_calls"
echo "chat-check: OK"
