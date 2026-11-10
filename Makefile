.PHONY: test lint docker-gpu k3s gpu-check vllm-run chat-check vllm-pair vllm-stop concurrency-check prefix-check vllm-deploy sim-deploy service-check gateway-check picker-image agent-deploy agent-check agent-forward app-forward reset-app ui up down grafana smoke record convert replay replay-compare experiment experiment-graph experiment-compare calibrate go-no-go finetune-spike lora-check finetune-data heldout finetune

# Python tests always exist (schemas/tests); Go tests run once the gateway module exists, the training tests once the
# training environment is installed (uv sync --locked --project training, P1.7).
test:
	uv run --locked pytest
	if [ -f gateway/go.mod ]; then cd gateway && go test ./...; fi
	if [ -d training/.venv ]; then uv run --locked --project training pytest -c training/pyproject.toml training/tests; fi

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
# both run by make up, and the vLLM containers stopped. A model.lora is checked before anything is pulled or deployed:
# its name and path must end with the first 12 digits of its sha256, since an edit of the name alone once served the
# old weights under the new name (P2.1b, P2.11), and the weights at its path must have that sha256. Whether the image
# is there is read from the node's status, which names it by digest, without sudo: make calibrate redeploys hours into
# a run, after sudo has stopped remembering the password (P2.8). --force-conflicts: kubectl scale (make calibrate,
# make experiment) owns .spec.replicas afterwards, and helm 4's server-side apply refuses to set it back without it;
# with it, a deploy always gives the profile's replicas (P2.12).
vllm-deploy:
	@if [ -z "$(PROFILE)" ]; then echo "usage: make vllm-deploy PROFILE=real|demo"; exit 2; fi
	@set -eu; \
	lora_path=$$(yq '.model.lora.path // ""' config/$(PROFILE).yaml | sed "s|^~|$$HOME|"); \
	if [ -n "$$lora_path" ]; then \
		want=$$(yq -e .model.lora.sha256 config/$(PROFILE).yaml); \
		short=$$(printf %.12s "$$want"); \
		name=$$(yq -e .model.lora.name config/$(PROFILE).yaml); \
		case "$$name" in \
			*"$$short") ;; \
			*) echo "vllm-deploy: model.lora.name $$name does not end with $$short, the start of model.lora.sha256"; \
				exit 1;; \
		esac; \
		case "$${lora_path%/}" in \
			*"$$short") ;; \
			*) echo "vllm-deploy: model.lora.path $$lora_path does not end with $$short, the start of model.lora.sha256"; \
				exit 1;; \
		esac; \
		got=$$(sha256sum "$$lora_path/adapter_model.safetensors" | cut -d' ' -f1); \
		if [ "$$got" != "$$want" ]; then \
			echo "vllm-deploy: $$lora_path/adapter_model.safetensors has sha256 $$got, model.lora.sha256 is $$want"; \
			exit 1; \
		fi; \
	fi; \
	image=$$(yq -e .image infra/charts/vllm/values.yaml); \
	digest=$${image##*@}; \
	if ! kubectl get nodes -o jsonpath='{.items[*].status.images[*].names[*]}' | tr ' ' '\n' | grep -qF "@$$digest"; then \
		echo "vllm-deploy: k3s pulls $$image (~9 GB), once per machine; no output until it is done" \
			"(progress: watch -n 10 sudo du -sh /var/lib/rancher/k3s/agent/containerd)"; \
		sudo k3s crictl pull "$$image"; \
	fi; \
	yq -e '{"replicas": .replicas, "model": .model, "pod": .pod, "monitoring": .monitoring}' config/$(PROFILE).yaml | \
		helm upgrade --install vllm infra/charts/vllm --values - --force-conflicts \
			--set hfCacheHostPath=$$HOME/.cache/huggingface --set-string loraHostPath=$$lora_path; \
	echo "vllm-deploy: waiting for the pods to load the model (about 2-3 min)"; \
	kubectl rollout status deployment/vllm --timeout=$(VLLM_DEPLOY_TIMEOUT); \
	kubectl get pods -l app.kubernetes.io/name=vllm -o wide

# Simulated pods in place of the vLLM ones, values rendered from config/sim.yaml (P2.7). The release has the vllm chart's
# name, so make down removes either, and a cluster on another profile needs make down first: the two charts own the same
# objects. Each pod is Ready after simulator.startup_duration; VLLM_DEPLOY_TIMEOUT covers that and the first image pull.
# --force-conflicts as in vllm-deploy: make experiment scales sim pods too (P2.12).
sim-deploy:
	@set -eu; \
	yq -e '{"replicas": .replicas, "model": .model, "simulator": .simulator, "monitoring": .monitoring}' config/sim.yaml | \
		helm upgrade --install vllm infra/charts/sim --values - --force-conflicts; \
	echo "sim-deploy: waiting for the simulated cold start"; \
	kubectl rollout status deployment/vllm --timeout=$(VLLM_DEPLOY_TIMEOUT); \
	kubectl get pods -l app.kubernetes.io/name=vllm -o wide

# chat_check.sh through the Service plus a count of which pod answered (P0.7).
service-check:
	bash infra/checks/service_check.sh

# Requests through the Gateway reach every vLLM pod, the access log carries the D1 headers and the Endpoint Picker
# logs its choices (P1.2).
gateway-check:
	bash infra/checks/gateway_check.sh

# The Endpoint Picker's image built from gateway/ into k3s, tagged with a hash of gateway/; skipped if k3s has that tag
# (P3.1). Run it after a change to gateway/, then infra/set-routing.sh, which deploys the build but never makes it. The
# compile needs a few GB of the VM's memory, so not next to real vLLM pods: make up runs it before they start.
picker-image:
	bash infra/picker-image.sh

# Redis and the agent runtime, image built from the working tree, config from config/$(PROFILE).yaml (P1.3b).
agent-deploy:
	@if [ -z "$(PROFILE)" ]; then echo "usage: make agent-deploy PROFILE=real|sim|demo"; exit 2; fi
	bash infra/deploy-agent.sh $(PROFILE)

# A calculator task through the agent: right answer, every call in the Gateway's access log, history in Redis (P1.3b).
agent-check:
	bash infra/checks/agent_check.sh

# The agent's API on http://localhost:8080 until Ctrl+C; the UI's dev server proxies /api to it (P1.4).
agent-forward:
	kubectl port-forward svc/agent 8080:8080

# The app backend's API on http://localhost:8081 until Ctrl+C; 8081 since make agent-forward holds 8080 (P2.10b).
app-forward:
	kubectl port-forward svc/app 8081:8080

# The image is already in k3s and the claim already bound, so the restarted pod is Ready in seconds; 180 s is
# infra/deploy-app.sh's rollout timeout.
APP_RESTART_TIMEOUT := 180s

# Wipes the app backend's users, chats and attachments (P2.10b), the only command that does: removes the SQLite file
# (and a journal a crash may have left) on the claim app-data, then restarts the pod, which creates the schema in a new
# file. A request between the two finds no tables and fails; the restart fixes that too. make down keeps the file.
reset-app:
	kubectl exec deployment/app -- rm -f /data/app.db /data/app.db-journal
	kubectl rollout restart deployment/app
	kubectl rollout status deployment/app --timeout=$(APP_RESTART_TIMEOUT)

# The UI's dev server on http://localhost:4200 until Ctrl+C, also from a Windows browser through WSL's localhost
# forwarding; it proxies /api to make agent-forward (ui/proxy.conf.json). profile.json feeds the top bar's chip, so
# PROFILE should be the one make up deployed. The model is named without its Hugging Face org, as in the design (P1.4a).
ui:
	@if [ -z "$(PROFILE)" ]; then echo "usage: make ui PROFILE=real|sim|demo"; exit 2; fi
	cd ui && npm ci --no-audit --no-fund
	yq -e -o json '{"profile": "$(PROFILE)", "model": (.model.name | split("/") | .[-1])}' config/$(PROFILE).yaml \
		> ui/public/profile.json
	cd ui && npx --no-install ng serve

# The whole cluster from scripts (P1.0); PROFILE defaults to real. Docker vLLM containers are stopped first, since
# the GPU cannot hold them and the pods at once. Machine setup (make docker-gpu, infra/bootstrap.sh) comes before.
# PROFILE=sim deploys simulated pods and no model; it has no agent either, since nothing there can answer a tool call,
# so make smoke is not expected to pass on it (P2.7), and no app backend, which only serves the agent's clients
# (P2.10b). install-k3s.sh still sets up the GPU, which those pods don't use.
up:
	$(MAKE) vllm-stop
	bash infra/install-k3s.sh
	bash infra/picker-image.sh
	bash infra/install-monitoring.sh $(or $(PROFILE),real)
	if [ "$(PROFILE)" = sim ]; then \
		$(MAKE) sim-deploy; \
	else \
		bash infra/download-model.sh $(or $(PROFILE),real) && $(MAKE) vllm-deploy PROFILE=$(or $(PROFILE),real); \
	fi
	bash infra/install-gateway.sh
	if [ "$(PROFILE)" != sim ]; then bash infra/deploy-agent.sh $(or $(PROFILE),real); fi
	if [ "$(PROFILE)" != sim ]; then bash infra/deploy-app.sh $(or $(PROFILE),real); fi

# Uninstalls what make up installs and stops k3s; keeps the k3s install, its images, the model cache (P1.0) and the
# app's data (P2.10b).
down:
	bash infra/down.sh

# Grafana on http://localhost:3000 until Ctrl+C, also from a Windows browser through WSL's localhost forwarding;
# anonymous users see the dashboards without a login (P1.1).
grafana:
	kubectl port-forward --namespace monitoring svc/monitoring-grafana 3000:80

# 5 CSV questions x 2 seeds on the demo model in each D10 mode, scored, one CSV row per run in experiments/results/
# (P1.6b). Needs make up PROFILE=demo; restarts the agent once per mode and puts config/demo.yaml back at the end.
# EXPERIMENT picks another experiment file: experiments/real-go-no-go.yaml asks the same on the real profile (P1.7).
go-no-go:
	uv run --locked python experiments/go_no_go.py $(or $(EXPERIMENT),experiments/demo-go-no-go.yaml)

# LoRA training steps of the base model at each length in experiments/finetune-train.yaml: template check, peak VRAM,
# s/step; saves the spike's adapter (P1.7 step 1). Needs the cluster down (make down); the training environment is
# its own uv project, so the first run installs torch into training/.venv.
finetune-spike:
	uv run --locked --project training python training/spike.py experiments/finetune-train.yaml

# vLLM serves the spike's adapter on the real profile's GPTQ-Int8 model: VRAM with and without it, one tool call in
# each D10 mode (P1.7 step 1). Needs make finetune-spike and the cluster down.
lora-check:
	bash infra/checks/lora_check.sh

# Training examples and held-out questions from the agent's own tools, then their token lengths with the served chat
# template (P1.7 step 2). Writes under output_dir of experiments/finetune-data.yaml, outside git; the same file gives
# the same bytes. PYTHONPATH: the generator runs the agent's own loop and tools, and agent/ is not an installed
# package (agent/pyproject.toml); pytest finds it through pythonpath, a script through this.
finetune-data:
	PYTHONPATH=agent uv run --locked python experiments/finetune_data.py experiments/finetune-data.yaml
	uv run --locked --project training python training/render.py experiments/finetune-data.yaml

# The held-out set through the agent in each D10 mode, scored like the go/no-go, one CSV row per run in
# experiments/results/ (P1.7 step 2). Needs make finetune-data and make up (real); 336 runs, ~90 min (P2.1b).
# BASELINE=<csv> prints that earlier result next to this one, paired by question, with McNemar's p-value (P1.7 step 3).
heldout:
	PYTHONPATH=agent uv run --locked python experiments/heldout.py experiments/finetune-data.yaml \
		$(if $(BASELINE),--baseline $(BASELINE))

# The LoRA adapter from make finetune-data's examples and experiments/finetune-train.yaml (P1.7 step 3): ~2 h. Run it
# again after a crash or Ctrl+C and it resumes from the newest checkpoint. Needs the cluster down (make down).
finetune:
	uv run --locked --project training python training/train.py experiments/finetune-train.yaml

# One calculator task through every layer on the current cluster, any profile: pods Ready, agent answer, Gateway
# access log, Endpoint Picker picks, Redis history, Prometheus samples; stops at the first FAIL (P1.5).
smoke:
	@bash infra/checks/smoke.sh

# An experiment's questions through the agent, one run at a time, as one trace.v1 line per LLM call in
# experiments/results/traces/ (P2.2b). Needs make up with the experiment's profile; experiments/record-demo.yaml takes
# about 13 min. PYTHONPATH: loadgen/ is not an installed package (loadgen/pyproject.toml).
record:
	@if [ -z "$(EXPERIMENT)" ]; then echo "usage: make record EXPERIMENT=experiments/record-demo.yaml"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.record $(EXPERIMENT)

# A published dataset's agent runs as one trace.v1 line per LLM call in experiments/results/traces/, fitted into the
# model's context (P2.4). Downloads to ~/.cache/thesis/ once. Needs make up with the experiment's profile, since vLLM's
# /tokenize counts the tokens; nothing else should use the pods meanwhile. An existing trace is refused. It prints the
# values the profiles must hold, and a test fails until they do.
convert:
	@if [ -z "$(EXPERIMENT)" ]; then echo "usage: make convert EXPERIMENT=experiments/convert-apb.yaml"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.convert $(EXPERIMENT)

# A trace's LLM calls straight to the gateway, one trace.v1 line per call in experiments/results/replays/, with the
# runs arriving by a pattern in config/$(PROFILE).yaml, replay.patterns (P2.3). Needs make up with that profile. An
# existing output is refused; OUT= names another file. SEED= overrides replay.seed. Exits 1 if a call got no full reply.
replay:
	@if [ -z "$(TRACE)" ] || [ -z "$(PATTERN)" ]; then echo "usage: make replay TRACE=<trace.jsonl> PATTERN=<name> [PROFILE=real] [SEED=n] [OUT=file]"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.replay $(TRACE) $(PATTERN) \
		--profile $(or $(PROFILE),real) $(if $(SEED),--seed $(SEED)) $(if $(OUT),--out $(OUT))

# Two replays of one trace: each the pattern's plan at its seed, the same call sequence, tokens as in the trace, every gap
# within replay.gap_tolerance_ms of the recorded one (P2.3's done-when). Reads only files, so it needs no cluster.
replay-compare:
	@if [ -z "$(TRACE)" ] || [ -z "$(PATTERN)" ] || [ -z "$(A)" ] || [ -z "$(B)" ]; then echo "usage: make replay-compare TRACE=<trace.jsonl> PATTERN=<name> A=<replay> B=<replay> [PROFILE=real] [SEED=n]"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.compare $(TRACE) $(PATTERN) $(A) $(B) \
		--profile $(or $(PROFILE),real) $(if $(SEED),--seed $(SEED))

# An experiment file's variants, each from restarted pods on its routing, as one CSV, .meta.json and graph stamped with the
# file's hash (P2.5). Needs make up with the file's profile; experiments/baseline.yaml takes about 1.5 h, so keep Windows
# from sleeping, and the laptop idle: use during a variant shows in its speed. An earlier result of the same file is refused. The default picker is put back when it ends, or it says it could not.
experiment:
	@if [ -z "$(NAME)" ]; then echo "usage: make experiment NAME=baseline"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.experiment experiments/$(NAME).yaml

# An experiment's graph again, from its CSV alone: no cluster, no replay file (P2.5).
experiment-graph:
	@if [ -z "$(CSV)" ]; then echo "usage: make experiment-graph CSV=experiments/results/<name>-<hash>.csv"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.graph $(CSV)

# Sums, paired runs and slow first tokens across CSVs of one load, each with the same two variants, from the CSVs alone (P2.5).
# STALL_SECONDS is what counts as a slow first token, stated at each use because it only chooses what this report lists.
experiment-compare:
	@if [ -z "$(CSVS)" ] || [ -z "$(STALL_SECONDS)" ]; then echo "usage: make experiment-compare CSVS=\"<a.csv> <b.csv>\" STALL_SECONDS=5"; exit 2; fi
	PYTHONPATH=loadgen uv run --locked python -m load_generator.experiment_compare --stall-seconds $(STALL_SECONDS) $(CSVS)

# Real vLLM pods measured for the simulator, as sim/calibration-<hash>-*.csv and sim/cold-start-<hash>-*.csv (P2.8a).
# Needs make up (real, not sim): it deploys real and real-plain in turn, scales the pods and restarts them, then deploys
# real again. About 9-10 h, so it refuses to start unless Windows is on Performance with AC sleep and hibernate off. A
# rerun skips finished files and redoes the others from their start.
calibrate:
	PYTHONPATH=loadgen:sim uv run --locked python -m calibrator.calibrate experiments/calibrate.yaml
