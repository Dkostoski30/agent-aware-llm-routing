.PHONY: test lint docker-gpu k3s gpu-check vllm-run chat-check vllm-pair vllm-stop concurrency-check prefix-check vllm-deploy service-check gateway-check up down grafana smoke replay experiment

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

# Same long prompt twice against a running vLLM server: prefix-cache hits must rise (P0.5).
prefix-check:
	bash infra/checks/prefix_cache_check.sh $(PORT)

# Both pods load the model at once on one GPU and disk; 900 s covers that with a wide margin (P0.7).
VLLM_DEPLOY_TIMEOUT := 900s

# vLLM pods behind the Service vllm, values rendered from config/$(PROFILE).yaml (P0.7). k3s does not share
# Docker's image store, so k3s pulls the image itself once (~9 GB). Copying it from Docker (docker save | ctr import)
# crashed k3s: the 32 GB burst of writes starved k3s's SQLite datastore until a commit failed (experiments/log.md).
# Needs the model in ~/.cache/huggingface (infra/download-model.sh), the PodMonitor CRD (infra/install-monitoring.sh),
# both run by make up, and the vLLM containers stopped.
vllm-deploy:
	@if [ -z "$(PROFILE)" ]; then echo "usage: make vllm-deploy PROFILE=real|demo"; exit 2; fi
	@set -eu; \
	image=$$(yq -e .image infra/charts/vllm/values.yaml); \
	if ! sudo k3s crictl inspecti "$$image" >/dev/null 2>&1; then \
		echo "vllm-deploy: k3s pulls $$image (~9 GB), once per machine; no output until it is done" \
			"(progress: watch -n 10 sudo du -sh /var/lib/rancher/k3s/agent/containerd)"; \
		sudo k3s crictl pull "$$image"; \
	fi; \
	yq -e '{"replicas": .replicas, "model": .model, "pod": .pod, "monitoring": .monitoring}' config/$(PROFILE).yaml | \
		helm upgrade --install vllm infra/charts/vllm --values - \
			--set hfCacheHostPath=$$HOME/.cache/huggingface; \
	echo "vllm-deploy: waiting for the pods to load the model (about 2-3 min)"; \
	kubectl rollout status deployment/vllm --timeout=$(VLLM_DEPLOY_TIMEOUT); \
	kubectl get pods -l app.kubernetes.io/name=vllm -o wide

# chat_check.sh through the Service plus a count of which pod answered (P0.7).
service-check:
	bash infra/checks/service_check.sh

# Requests through the Gateway reach every vLLM pod, the access log carries the D1 headers and the Endpoint Picker
# logs its choices (P1.2).
gateway-check:
	bash infra/checks/gateway_check.sh

# The whole cluster from scripts (P1.0); PROFILE defaults to real. Docker vLLM containers are stopped first, since
# the GPU cannot hold them and the pods at once. Machine setup (make docker-gpu, infra/bootstrap.sh) comes before.
up:
	$(MAKE) vllm-stop
	bash infra/install-k3s.sh
	bash infra/install-monitoring.sh $(or $(PROFILE),real)
	bash infra/download-model.sh $(or $(PROFILE),real)
	$(MAKE) vllm-deploy PROFILE=$(or $(PROFILE),real)
	bash infra/install-gateway.sh

# Uninstalls what make up installs and stops k3s; keeps the k3s install, its images and the model cache (P1.0).
down:
	bash infra/down.sh

# Grafana on http://localhost:3000 until Ctrl+C, also from a Windows browser through WSL's localhost forwarding;
# anonymous users see the dashboards without a login (P1.1).
grafana:
	kubectl port-forward --namespace monitoring svc/monitoring-grafana 3000:80

smoke:
	@echo "not implemented yet: P1.5"; exit 1

replay:
	@echo "not implemented yet: P2.3"; exit 1

experiment:
	@echo "not implemented yet: P2.5"; exit 1
