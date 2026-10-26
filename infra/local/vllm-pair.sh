#!/usr/bin/env bash
# Starts two vLLM containers from config/real.yaml on ports 8001 and 8002 and waits until both answer /health (P0.4).
# They start one after the other: vLLM sizes its KV cache from the GPU memory free at startup, so two replicas
# profiling at the same time would each see the other's memory half-allocated.
# Containers run in the background; make vllm-stop removes them. Rerunning replaces them.
set -euo pipefail

PROFILE=real
CONTAINERS=(vllm-1 vllm-2)
PORTS=(8001 8002)

# Loading the 0.6B from the local cache takes about a minute; a first run also downloads it. 600 s covers both.
HEALTH_TIMEOUT_SECONDS=600

source "$(dirname "${BASH_SOURCE[0]}")/vllm-common.sh"

# wait_healthy <container> <port>: returns once /health answers; exits 1 if the container stops or time runs out.
wait_healthy() {
  local name=$1 port=$2 waited=0
  until curl -fsS --max-time 2 "http://localhost:$port/health" >/dev/null 2>&1; do
    if [[ $(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null) != true ]]; then
      echo "vllm-pair: $name stopped before it was healthy (container removed, see the log above)" >&2
      exit 1
    fi
    if ((waited >= HEALTH_TIMEOUT_SECONDS)); then
      echo "vllm-pair: $name not healthy after ${HEALTH_TIMEOUT_SECONDS}s; last log lines:" >&2
      docker logs --tail 30 "$name" >&2
      exit 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "vllm-pair: $name healthy on port $port after ${waited}s"
}

existing=$(docker ps --all --quiet --filter "name=^vllm-[12]$")
if [[ -n $existing ]]; then
  docker rm --force $existing >/dev/null
fi
for i in "${!CONTAINERS[@]}"; do
  vllm_docker_run "$PROFILE" "${CONTAINERS[$i]}" "${PORTS[$i]}" --detach >/dev/null
  # Follow the log while waiting, so a crash (e.g. out of GPU memory) shows its cause; stopped once healthy.
  docker logs --follow "${CONTAINERS[$i]}" &
  log_pid=$!
  wait_healthy "${CONTAINERS[$i]}" "${PORTS[$i]}"
  kill "$log_pid"
done
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
