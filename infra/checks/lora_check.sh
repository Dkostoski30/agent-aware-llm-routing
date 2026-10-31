#!/usr/bin/env bash
# P1.7 step 1: vLLM serves a LoRA adapter on the real profile's GPTQ-Int8 model. Starts the real profile's vLLM
# container (as make vllm-run does, on its port) twice: without an adapter, then with the spike's adapter from
# make finetune-spike. Prints the GPU memory used each time, and sends "What is 7 times 8?" with the agent's
# calculator tool to the base model and to the adapter in both D10 modes. Every request must be answered, and in
# required mode both must call the calculator: vLLM parses that call with the hermes parser (experiments/log.md), so
# a parsed call also shows the format the model writes. An adapter answer that differs from the base model's shows
# that vLLM applies the adapter's weights. The spike's adapter was trained on repeated text, not on tool calls, so
# what it answers says nothing about training (P1.7 step 1).
# Run it with the cluster down (make down): the vLLM pods would hold the GPU. Exit 1 if any check fails.
# Usage: lora_check.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../local/vllm-common.sh"

# The profile whose model the adapter is served on (experiments/finetune-train.yaml, served_profile).
EXPERIMENT=$REPO_ROOT/experiments/finetune-train.yaml
PROFILE=$(yq -e .served_profile "$EXPERIMENT")
# The container name and port of make vllm-run, so make vllm-stop also removes a container this script left behind.
CONTAINER=vllm
HOST_PORT=8000
BASE_URL=http://localhost:$HOST_PORT
# The name requests use to reach the adapter; vLLM lists it in /v1/models next to the base model.
ADAPTER_NAME=thesis-spike
ADAPTER_MOUNT=/adapters/spike
# vLLM became healthy after 114-160 s in Docker (P0.3, P0.4); 600 s also covers a first start that sets up LoRA.
READY_TIMEOUT_SECONDS=600
# One non-streaming tool call from the 0.6B takes a few seconds (P0.3).
REQUEST_TIMEOUT_SECONDS=120

fail() {
  echo "lora-check: FAIL: $*" >&2
  exit 1
}

remove_container() {
  if [[ -n $(docker ps --all --quiet --filter "name=^$CONTAINER\$") ]]; then
    docker rm --force "$CONTAINER" >/dev/null
  fi
}
trap remove_container EXIT

gpu_used_mib() {
  nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
}

# start_vllm [docker flags...]: starts the container in the background and waits until /health answers. The
# container is started with --rm, so its log is copied to a file while it runs and shown if it stops.
start_vllm() {
  local log waited=0
  log=$(mktemp)
  vllm_docker_run "$PROFILE" "$CONTAINER" "$HOST_PORT" --detach "$@" >/dev/null
  docker logs --follow "$CONTAINER" >"$log" 2>&1 &
  until curl -sf --max-time 5 "$BASE_URL/health" >/dev/null; do
    if [[ -z $(docker ps --quiet --filter "name=^$CONTAINER\$") ]]; then
      tail -n 40 "$log" >&2
      fail "vLLM stopped before it was healthy (last log lines above)"
    fi
    if ((waited >= READY_TIMEOUT_SECONDS)); then
      tail -n 40 "$log" >&2
      fail "vLLM not healthy after ${READY_TIMEOUT_SECONDS}s (last log lines above)"
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "lora-check: vLLM healthy after ~${waited}s"
}

# answer <model> <tool_choice>: prints the model's text and tool calls for the request the agent would send: the
# real profile's system prompt, the calculator tool, and final_answer too in required mode (agent_runtime.tools, D10).
answer() {
  local body response
  body=$(jq -n --arg model "$1" --arg system "$system_prompt" --arg mode "$2" '
    def tool(name; description; parameters):
      {type: "function", function: {name: name, description: description, parameters: parameters}};
    {
      model: $model,
      messages: [{role: "system", content: $system}, {role: "user", content: "What is 7 times 8?"}],
      tools: ([tool("calculator"; "Applies one arithmetic operation to two numbers: a op b.";
          {type: "object",
           properties: {op: {type: "string", enum: ["add", "subtract", "multiply", "divide"]},
                        a: {type: "number"}, b: {type: "number"}},
           required: ["op", "a", "b"], additionalProperties: false})]
        + if $mode == "required" then
            [tool("final_answer"; "Ends the task with the answer for the user.";
              {type: "object", properties: {answer: {type: "string", minLength: 1}},
               required: ["answer"], additionalProperties: false})]
          else [] end),
      tool_choice: $mode,
      temperature: 0,
      chat_template_kwargs: {enable_thinking: false}
    }')
  response=$(curl -sS --fail-with-body --max-time "$REQUEST_TIMEOUT_SECONDS" \
    -H 'Content-Type: application/json' -d "$body" "$BASE_URL/v1/chat/completions") ||
    fail "$1, $2 request: $response"
  jq -ec '.choices[0].message |
    {content, tool_calls: [.tool_calls[]?.function | {name, arguments: (.arguments | fromjson)}]}' <<<"$response" ||
    fail "$1, $2 request returned no chat message: $response"
}

if systemctl is-active --quiet k3s; then
  fail "k3s is running and its vLLM pods hold the GPU; run make down first"
fi
adapter_dir=$(yq -e .output_dir "$EXPERIMENT")/spike-adapter
adapter_dir=${adapter_dir/#\~/$HOME}
[[ -f $adapter_dir/adapter_model.safetensors ]] || fail "no adapter in $adapter_dir; run make finetune-spike first"
rank=$(yq -e .lora.rank "$EXPERIMENT")
base_model=$(profile_value "$PROFILE" .model.name)
system_prompt=$(profile_value "$PROFILE" .agent.system_prompt)
echo "lora-check: GPU memory used before vLLM: $(gpu_used_mib) MiB"

echo "== 1. $PROFILE profile without an adapter"
start_vllm
without=$(gpu_used_mib)
echo "lora-check: GPU memory used: $without MiB"
remove_container

echo "== 2. $PROFILE profile with the adapter $adapter_dir (rank $rank)"
VLLM_EXTRA_ARGS=(--enable-lora --lora-modules "$ADAPTER_NAME=$ADAPTER_MOUNT" --max-lora-rank "$rank")
start_vllm --volume "$adapter_dir:$ADAPTER_MOUNT:ro"
with=$(gpu_used_mib)
echo "lora-check: GPU memory used: $with MiB, $((with - without)) MiB more than without the adapter"
curl -sS --max-time "$REQUEST_TIMEOUT_SECONDS" "$BASE_URL/v1/models" | jq -e --arg name "$ADAPTER_NAME" \
  'any(.data[]; .id == $name)' >/dev/null || fail "$ADAPTER_NAME is not in /v1/models"

for mode in auto required; do
  base_answer=$(answer "$base_model" "$mode")
  adapter_answer=$(answer "$ADAPTER_NAME" "$mode")
  echo "lora-check: $mode, base:    $base_answer"
  echo "lora-check: $mode, adapter: $adapter_answer"
  if [[ $base_answer == "$adapter_answer" ]]; then
    echo "lora-check: $mode: the same answer; at temperature 0 the adapter did not change this one"
  else
    echo "lora-check: $mode: the answers differ, so vLLM applied the adapter's weights"
  fi
  if [[ $mode == required ]]; then
    jq -e '.tool_calls[0].name == "calculator"' <<<"$base_answer" >/dev/null ||
      fail "required, base: no parsed calculator call"
    jq -e '.tool_calls[0].name == "calculator"' <<<"$adapter_answer" >/dev/null ||
      fail "required, adapter: no parsed calculator call"
  fi
done
echo "lora-check: OK"
