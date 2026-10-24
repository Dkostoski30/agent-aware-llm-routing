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

## Dev tools (installed by `infra/bootstrap.sh`)

| Tool | Version |
|---|---|
| git | |
| make | |
| jq | |
| Python | |
| uv | |
| Node.js | |
| Angular CLI | |
| Go | |
| golangci-lint | |
| pre-commit | |
| ruff | |
| mypy | |
| Helm | |
| k9s | |

## Container and cluster

| Component | Version / tag |
|---|---|
| Docker Engine | |
| NVIDIA Container Toolkit | |
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
