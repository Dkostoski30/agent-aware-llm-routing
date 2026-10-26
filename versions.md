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
| k3s | v1.36.5+k3s1 (the `stable` channel on 2026-10-26; `latest` was v1.37.1+k3s1). Pinned in `infra/install-k3s.sh` |
| kubectl | the symlink k3s creates (`/usr/local/bin/kubectl -> k3s`); same version as k3s |
| NVIDIA device plugin (Helm chart `nvidia-device-plugin`) | 0.20.1, image `nvcr.io/nvidia/k8s-device-plugin:v0.20.1` (newest; reports the WSL2 GPU as one device `all` on `/dev/dxg`). Pinned in `infra/install-k3s.sh` |
| vLLM image (`vllm/vllm-openai`) | v0.31.0, digest `sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3` (released 2026-10-05; the newest release at least two weeks old on 2026-10-26, v0.32.0 was 8 days old). Pinned in `infra/local/vllm-common.sh` and `infra/charts/vllm/values.yaml` |
| Gateway (Envoy Gateway or kgateway) | |
| Gateway API Inference Extension | |
| KEDA | |
| kube-prometheus-stack | |
| Redis | |
| llm-d-inference-sim | |

## Models

| Model | Hugging Face revision (commit) | Size on disk | Use |
|---|---|---|---|
| Qwen/Qwen3-0.6B-GPTQ-Int8 | `d3f20e7e71825fe57ad32705b396e9e156279107` | 0.71 GiB | Experiments, infrastructure demo |
| Qwen/Qwen3-1.7B-GPTQ-Int8 | `382fed3fa21d1a7e837b42a500564a0c4be4f060` | 2.07 GB | Practical demo (D8, after the 4B did not fit) |
| Qwen/Qwen3-4B-AWQ | `74d4bd2bd4bff9cafc9345221320bffb08b406a3` | 2.5 GiB in VRAM | Not used: does not fit (P0.3) |
| Qwen/Qwen3-1.7B | `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` | 4.06 GB | Not used: FP16 weights alone exceed what is free on the GPU |

Revisions are pinned in `config/<profile>.yaml` (`model.revision`) and passed to vLLM as `--revision` and
`--tokenizer-revision`.

## Measurements

| Task | Config | VRAM per pod | Host RAM per pod | Notes |
|---|---|---|---|---|
| P0.3 | 0.6B, 1 container, util 0.42, max-model-len 4096 | 2271 MiB whole GPU (558 MiB idle) | 3.42 GiB | Weights 0.74 GiB, KV 0.62 GiB = 5840 tokens. 8192 did not fit (needs 0.88 GiB KV) |
| P0.3 | 4B AWQ, 1 container, max-model-len 4096 | - | - | **Fits: no.** util 0.85 refused (3.22 GiB free); util 0.78: weights 2.5 GiB, KV 0.18 GiB = max 1312 tokens |
| P0.3 | 1.7B GPTQ-Int8, 1 container, util 0.78, max-model-len 4096 | 3213 MiB whole GPU | 3.85 GiB | Weights 1.92 GiB, KV 0.86 GiB = 8016 tokens |
| P0.4 | 0.6B, 2 containers, util 0.42, KV fixed at 480 MiB, max-model-len 4096 | 3468 MiB whole GPU (peak, both) | 2.79 / 2.92 GiB (peak) | 10 min, 4 in flight per port: 679 requests, 0 errors, p50 6.4 s, p95 9.5 s. KV 4384 tokens each |
