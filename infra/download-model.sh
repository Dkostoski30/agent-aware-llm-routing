#!/usr/bin/env bash
# Downloads the model of config/<PROFILE>.yaml at its pinned revision into ~/.cache/huggingface (P1.0), the cache the
# vLLM pods mount read-only with HF_HUB_OFFLINE=1. hf skips files already in the cache, so a second run only checks.
# Usage: download-model.sh PROFILE
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PROFILE   (real | demo)" >&2
  exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/local/vllm-common.sh"

name=$(profile_value "$1" .model.name)
revision=$(profile_value "$1" .model.revision)

# --cache-dir: the hub cache inside HF_CACHE, the same layout vLLM writes to and the pods read from (/hf/hub).
hf download "$name" --revision "$revision" --cache-dir "$HF_CACHE/hub"
