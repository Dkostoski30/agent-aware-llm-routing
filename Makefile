.PHONY: test lint docker-gpu k3s gpu-check vllm-run chat-check vllm-pair vllm-stop concurrency-check up down smoke replay experiment

# Python tests always exist (schemas/tests); Go tests run once the gateway module exists.
test:
	uv run --locked pytest
	if [ -f gateway/go.mod ]; then cd gateway && go test ./...; fi

lint:
	uv run --locked pre-commit run --all-files

# Docker Engine + NVIDIA Container Toolkit inside WSL2 (P0.2).
docker-gpu:
	bash infra/install-docker-gpu.sh

# k3s + nvidia RuntimeClass + NVIDIA device plugin (P0.6).
k3s:
	bash infra/install-k3s.sh

# The first run pulls the nbody image into k3s's containerd (separate from Docker's); 300 s covers that pull
# on a slow connection, a cached run finishes in seconds.
GPU_CHECK_TIMEOUT_SECONDS := 300

# Runs nbody in a pod: through the device plugin if the node advertises nvidia.com/gpu (ladder step 1),
# otherwise through the nvidia runtime alone (ladder step 3, PLAN.md section 4). Exits 0 only if the pod succeeded.
gpu-check:
	@set -eu; \
	gpus=$$(kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'); \
	if [ "$${gpus:-0}" -ge 1 ]; then manifest=infra/checks/gpu-check-plugin.yaml; \
	else manifest=infra/checks/gpu-check-noplugin.yaml; fi; \
	echo "gpu-check: allocatable nvidia.com/gpu=$${gpus:-0}, using $$manifest"; \
	kubectl delete pod gpu-test --ignore-not-found --wait; \
	kubectl apply -f "$$manifest"; \
	phase=Pending; waited=0; \
	while [ "$$phase" != Succeeded ] && [ "$$phase" != Failed ] && [ $$waited -lt $(GPU_CHECK_TIMEOUT_SECONDS) ]; do \
		sleep 2; waited=$$((waited + 2)); \
		phase=$$(kubectl get pod gpu-test -o jsonpath='{.status.phase}'); \
	done; \
	echo "gpu-check: pod phase $$phase after $${waited}s"; \
	if [ "$$phase" = Succeeded ] || [ "$$phase" = Failed ]; then kubectl logs pod/gpu-test; \
	else kubectl describe pod gpu-test; fi; \
	kubectl delete pod gpu-test --wait=false; \
	[ "$$phase" = Succeeded ]

# One vLLM container in the foreground on port 8000, model from config/$(PROFILE).yaml (P0.3).
vllm-run:
	@if [ -z "$(PROFILE)" ]; then echo "usage: make vllm-run PROFILE=real|demo"; exit 2; fi
	bash infra/local/vllm-run.sh $(PROFILE)

# Chat answer + tool call against a running vLLM server; BASE_URL defaults to the vllm-run port (P0.3).
chat-check:
	bash infra/checks/chat_check.sh $(BASE_URL)

# Two vLLM containers from config/real.yaml on ports 8001 and 8002, in the background (P0.4).
vllm-pair:
	bash infra/local/vllm-pair.sh

# Removes every local vLLM container (vllm-run, vllm-pair), e.g. before the GPU is handed to k3s (P0.7).
vllm-stop:
	@ids=$$(docker ps --all --quiet --filter 'name=^vllm(-[12])?$$'); \
	if [ -n "$$ids" ]; then docker rm --force $$ids; else echo "vllm-stop: no vLLM containers"; fi

# Load on both replicas, e.g. ARGS="--ports 8001 8002 --concurrency 4 --minutes 10" (P0.4).
concurrency-check:
	uv run --locked python infra/checks/concurrency_check.py $(ARGS)

up:
	@echo "not implemented yet: P1.0"; exit 1

down:
	@echo "not implemented yet: P1.0"; exit 1

smoke:
	@echo "not implemented yet: P1.5"; exit 1

replay:
	@echo "not implemented yet: P2.3"; exit 1

experiment:
	@echo "not implemented yet: P2.5"; exit 1
