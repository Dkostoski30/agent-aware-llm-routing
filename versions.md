# versions.md

Every version used in the thesis. Fill in as you install. Never upgrade during the thesis without a reason written here.

## Machine (locked in P0.1)

| Item | Value |
|---|---|
| Laptop | ASUS ROG Strix G15 (2021) |
| CPU | Ryzen 7 |
| GPU | RTX 3050 Laptop, 4GB |
| RAM | 16GB |
| Windows build | Windows 11 Pro 10.0.26200.9711 |
| NVIDIA driver (Windows) | 621.01 (`DriverVersion` 32.0.16.2101), KMD 621.01, CUDA UMD 13.5 |
| Armoury Crate power mode | Performance (same for every experiment) |
| Armoury Crate GPU mode | Standard |
| Windows power plan | Performance (`27fa6203-3987-4dcc-918d-748559d549ec`), sleep on AC = never |
| `.wslconfig` | memory=10GB, swap=8GB (verified: 9.7Gi RAM, 8.0Gi swap inside WSL) |
| WSL version (`wsl --version`) | 3.0.3.0, kernel 6.18.54.1-microsoft-standard-WSL2, WSLg 1.0.79 |
| Ubuntu | 24.04.5 LTS (Noble Numbat) |
| WSL distro used for all work | `Ubuntu-24.04` (Linux user `kosto`) |
| GPU memory reported in WSL | 4096 MiB total, 127 MiB used at idle |
| Free space on C: | 140.5 GB (requirement: 60 GB) |
| Docker Desktop (Windows) | Installed, not used; "Start Docker Desktop when you sign in" off, so its `docker-desktop` distro does not take memory from the WSL2 VM (P0.2) |

## Dev tools (installed by `infra/bootstrap.sh`)

Pinned in `infra/bootstrap.sh` (downloads, checksum-verified) and `pyproject.toml` / `uv.lock` (Python dev tools).
Each pin is the newest release that was at least two weeks old on 2026-10-25.
apt packages come from Ubuntu 24.04 and are recorded here, not pinned: Ubuntu drops old versions from the archive after security updates.

| Tool | Version | Source |
|---|---|---|
| git | 1:2.43.0-1ubuntu7.4 | apt |
| make | 4.3-4.1build2 | apt |
| jq | 1.7.1-3ubuntu0.24.04.2 | apt |
| build-essential | 12.10ubuntu1 | apt |
| curl | 8.5.0-2ubuntu10.16 | apt |
| ca-certificates | 20260816ubuntu0.24.04.1 | apt |
| Python | 3.12.3 | Ubuntu 24.04 system Python |
| yq (mikefarah) | 4.54.1 | GitHub release |
| uv | 0.13.0 | GitHub release |
| nvm | 0.40.8 | git tag |
| Node.js | 24.21.0 (LTS "Krypton") | nvm |
| Angular CLI | 22.2.2 | npm |
| Go | 1.27.2 | go.dev |
| golangci-lint | 2.14.0 | GitHub release |
| Helm | 4.3.0 | get.helm.sh |
| k9s | 0.51.0 | GitHub release |
| hf CLI (huggingface_hub) | 2.2.0 | uv tool |
| pre-commit | 4.6.2 | uv.lock |
| pre-commit-hooks | v6.0.0 | `.pre-commit-config.yaml` |
| ruff | 0.17.0 | uv.lock |
| mypy | 2.4.0 | uv.lock |
| types-jsonschema | 4.26.0.20261006 | uv.lock |
| pytest | 9.1.1 | uv.lock |
| jsonschema | 4.26.0 | uv.lock |

## Container and cluster

Docker and the Toolkit are pinned in `infra/install-docker-gpu.sh` (apt, exact versions). Unlike the dev tools,
these are the newest versions available on 2026-10-25, without the two-week rule.

| Component | Version / tag |
|---|---|
| Docker Engine (docker-ce, docker-ce-cli) | 5:29.9.2-1~ubuntu.24.04~noble |
| containerd.io | 2.4.2-3~ubuntu.24.04~noble |
| docker-buildx-plugin | 0.38.1-1~ubuntu.24.04~noble |
| NVIDIA Container Toolkit (nvidia-container-toolkit, -base, libnvidia-container1, libnvidia-container-tools) | 1.20.1-1 |
| k3s | |
| NVIDIA device plugin (Helm chart) | |
| vLLM image (`vllm/vllm-openai`) | |
| Gateway (Envoy Gateway or kgateway) | |
| Gateway API Inference Extension | |
| KEDA | |
| kube-prometheus-stack | |
| Redis | |
| llm-d-inference-sim | |

## Models

| Model | Hugging Face revision (commit) | Size on disk | Use |
|---|---|---|---|
| Qwen/Qwen3-0.6B-GPTQ-Int8 | | | Experiments, infrastructure demo |
| Qwen/Qwen3-4B-AWQ | | | Practical demo, if it fits |
| Qwen/Qwen3-1.7B | | | Practical demo fallback |

## Measurements

| Task | Config | VRAM per pod | Host RAM per pod | Notes |
|---|---|---|---|---|
| P0.3 | 0.6B, 1 container | | | |
| P0.3 | 4B AWQ, 1 container, max-model-len 4096 | | | Fits: yes / no |
| P0.4 | 0.6B, 2 containers at 0.42 | | | |
