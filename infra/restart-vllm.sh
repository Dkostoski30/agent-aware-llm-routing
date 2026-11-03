#!/usr/bin/env bash
# Restarts the vLLM pods, which empties their prefix caches, so that an experiment's variants all start from the same
# state (P2.5). The Deployment's strategy is Recreate, so the old pods free the GPU before the new ones start.
set -euo pipefail

# Both pods load the model at once on one GPU and disk; 900 s covers that with a wide margin (as VLLM_DEPLOY_TIMEOUT in
# the Makefile, P0.7).
ROLLOUT_TIMEOUT=900s

kubectl rollout restart deployment/vllm
kubectl rollout status deployment/vllm --timeout="$ROLLOUT_TIMEOUT"
