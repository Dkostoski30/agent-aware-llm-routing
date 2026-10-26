# Sourced by vllm-run.sh (P0.3) and vllm-pair.sh (P0.4), so every local vLLM container starts the same way.
# Every model setting comes from config/<profile>.yaml (D11); only the image pin lives here.

# Newest vLLM release at least two weeks old on 2026-10-26 (versions.md). The tag is for reading, the digest pins it.
VLLM_IMAGE=vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3

# Port the OpenAI-compatible server listens on inside the container (vLLM's default).
VLLM_CONTAINER_PORT=8000

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
HF_CACHE=$HOME/.cache/huggingface

# profile_value <profile> <yq path>: prints one value from the profile; fails if the profile or the key is missing.
profile_value() {
  local config=$REPO_ROOT/config/$1.yaml
  if [[ ! -f $config ]]; then
    echo "vllm: no profile $config" >&2
    return 1
  fi
  if ! yq -e "$2" "$config"; then
    echo "vllm: $2 missing in $config" >&2
    return 1
  fi
}

# vllm_serve_args <profile>: fills the array VLLM_ARGS with the vLLM server flags for the profile.
vllm_serve_args() {
  local name revision gpu_util kv_bytes max_len parser
  name=$(profile_value "$1" .model.name)
  revision=$(profile_value "$1" .model.revision)
  gpu_util=$(profile_value "$1" .model.gpu_memory_utilization)
  kv_bytes=$(profile_value "$1" .model.kv_cache_memory_bytes)
  max_len=$(profile_value "$1" .model.max_model_len)
  parser=$(profile_value "$1" .model.tool_call_parser)
  # --tokenizer-revision: without it vLLM loads the tokenizer (and chat template) from main, not from the pinned commit.
  # --enforce-eager: CUDA graphs cost extra VRAM and startup time; on a 4GB GPU shared by two replicas both matter.
  VLLM_ARGS=(
    --model "$name"
    --revision "$revision"
    --tokenizer-revision "$revision"
    --gpu-memory-utilization "$gpu_util"
    --kv-cache-memory "$kv_bytes"
    --max-model-len "$max_len"
    --tool-call-parser "$parser"
    --enable-auto-tool-choice
    --enable-prefix-caching
    --enforce-eager
  )
}

# vllm_docker_run <profile> <container name> <host port> [docker flags...]: starts one vLLM container.
# It runs as the calling user, so files it downloads into the HF cache stay owned by that user; HOME points
# inside the container because the image's own caches (torch, triton) expect a writable home.
# --ipc=host: vLLM's API server and engine run as separate processes and exchange tensors through shared memory;
# Docker's default /dev/shm (64 MB) is too small for that.
vllm_docker_run() {
  local profile=$1 name=$2 port=$3
  shift 3
  vllm_serve_args "$profile"
  mkdir -p "$HF_CACHE"
  docker run --rm --name "$name" "$@" \
    --gpus all \
    --ipc=host \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp/home \
    --env HF_HOME=/hf \
    --volume "$HF_CACHE:/hf" \
    --publish "$port:$VLLM_CONTAINER_PORT" \
    "$VLLM_IMAGE" "${VLLM_ARGS[@]}"
}
