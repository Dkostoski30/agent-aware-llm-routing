#!/usr/bin/env bash
# Runs one vLLM container in the foreground on port 8000 with the model from config/<PROFILE>.yaml (P0.3).
# Ctrl+C stops it; --rm removes it. The first run downloads the model into ~/.cache/huggingface.
# Usage: vllm-run.sh PROFILE
set -euo pipefail

# Host port chat_check.sh and prefix_cache_check.sh use by default.
HOST_PORT=8000

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PROFILE   (real | demo)" >&2
  exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/vllm-common.sh"
vllm_docker_run "$1" vllm "$HOST_PORT"
