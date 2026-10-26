# Experiment and verification log

Newest entry first. Every task's done-when output goes here, plus anything surprising.

## Template

```
### YYYY-MM-DD · <task ID> · <green | red>
Command: <what you ran>
Result: <key lines of output, numbers>
Notes: <surprises, decisions, what changed>
```

---

### 2026-10-26 · P1.1 · green
Command: `make up` · `kubectl get pods -n monitoring` · `make service-check` · `make grafana`, dashboard `thesis-vllm`
Result:
- kube-prometheus-stack 92.3.0 installed by `make up` before the vLLM release; 4 pods Running in `monitoring`
  (grafana 3/3, operator, kube-state-metrics, prometheus 2/2).
- `make service-check` OK (12 / 10 requests per pod); the "vLLM pods" dashboard shows both pods in the queue,
  KV-cache and prefix-cache hit rate panels (user checked in the browser).
Notes:
- Operator and kube-state-metrics restarted once at 20:53:30 UTC, while both vLLM pods were loading the model:
  kube-state-metrics logged `Failed to contact API server for /livez`, failed its liveness probe and was
  restarted; the operator exited cleanly on SIGTERM. The k3s API server stopped answering for a moment under the
  model load. Not a failure of this task; watch for it in later `make up` runs (same family as the P0.7
  datastore slowness).

### 2026-10-26 · P1.0 · green
Command: `make down` · `systemctl is-active k3s; nvidia-smi` · `time make up` · `make service-check`
Result:
- `make down`: releases vllm, monitoring, nvidia-device-plugin uninstalled, both namespaces deleted, k3s stopped
  by `k3s-killall.sh`; `inactive`, `nvidia-smi` lists no process (335 MiB used by the desktop).
- `make up` from a stopped k3s: exit 0, **8 min 2 s** wall time. Device plugin back with
  `allocatable nvidia.com/gpu: 2`; model already in the cache (hf only checked 10 files); both vLLM pods
  `1/1 Running` after the rollout (4 min 29 s from install to both Ready).
- `make service-check` OK through the Service, 12 / 10 requests per pod.
Notes:
- The 8 min include the first pull of the monitoring images (P1.1, same run); the vLLM image and the model came
  from k3s's containerd and the HF cache, as `make down` keeps them.
- In k3s both pods became Ready after 4.5 min, slower than the 2-3 min in Docker (P0.3, P0.4): they load at the
  same time from one disk and one GPU.
- The second `make down` (idempotency) was not part of this run.

### 2026-10-26 · P0.7 · green · Gate 0 green
Command: `make k3s` · `make vllm-deploy PROFILE=real` · `kubectl get pods` · `make service-check`
Result:
- `make k3s`: device plugin with time-slicing, node reports `allocatable nvidia.com/gpu: 2`.
- `kubectl get pods`: `vllm-5fff45f674-5qvc7` and `vllm-5fff45f674-csj8f`, both `1/1 Running`, 0 restarts.
- `make service-check` through ClusterIP `10.43.129.213:8000`: chat answer and tool call
  `get_weather({"city": "Ohrid"})` OK; chat requests per pod **9 and 13** (2 from chat-check + 20 spread requests).
- Gate 0: two vLLM pods on the GPU answer through a Service (this entry), prefix caching visible (P0.5).
Notes:
- Getting the image into k3s, attempt 1 (`docker save | k3s ctr images import -`): after 23 min **k3s crashed**
  (`level=fatal msg="Transaction commit failed"`, then systemd restarted it). The 32 GB of image writes starved
  k3s's SQLite datastore on the same disk: 102 "Slow SQL" warnings during the import vs 13 in the two hours
  before; no journal gap long enough for a laptop sleep before the crash. Replaced (user decision) by
  `sudo k3s crictl pull`, k3s pulling the image itself; it was already complete when the deploy ran.
- First rollout: both pods in CrashLoopBackOff, `ValueError: VLLM_PORT 'tcp://10.43.129.213:8000' appears to be
  a URI`. Kubernetes injects `<SERVICE>_PORT` variables for every Service, and for the Service `vllm` that is
  `VLLM_PORT`, which vLLM reads as its own setting. Fixed with `enableServiceLinks: false` in the pod spec.
- `make vllm-deploy` waits with `kubectl rollout status` (prints each pod as it becomes Ready) instead of a silent
  `helm --wait`; the Deployment uses `Recreate`, since a rolling update's third pod could never get a GPU slot.
- Pods mount `~/.cache/huggingface` read-only with `HF_HUB_OFFLINE=1`; the model must be downloaded by
  `make vllm-run` first.
- `service_check.sh` calls the ClusterIP from the host (the host is the node) instead of `kubectl port-forward`,
  which tunnels to one pod (user decision). Readiness delay and the KV size come from `config/` (user decision).

### 2026-10-26 · P0.5 · green
Command: `make vllm-run PROFILE=real` · `make prefix-check`
Result:
- Metric names in vLLM 0.31.0: `vllm:prefix_cache_queries_total`, `vllm:prefix_cache_hits_total` (both in tokens,
  label `engine="0"`), plus `vllm:external_prefix_cache_{queries,hits}_total` (KV connector, 0 here).
- ~1980-token prompt sent twice: hits added by the first request **0**, by the second **1968** (all but the last
  partial 16-token block).
- **TTFT first 0.524 s, second 0.081 s** (6.5× lower). TTFT = total time of a `max_tokens: 1` request.
Notes:
- First run used 150 prompt lines (~3385 tokens, hits 3376, TTFT 0.758 s -> 0.089 s); the script's per-line
  estimate was wrong (22 tokens per line, not 14), so it now uses 90 lines for the card's ~2000 tokens.
- The run marker at the start of the prompt makes every run start uncached, so the check can be rerun.
- The check fails unless the second request adds more hits than the first, not just "hits went up": the first
  request could hit shared chat-template blocks.

### 2026-10-26 · P0.4 · green (attempt 2)
Command: `make vllm-pair` · `make concurrency-check ARGS="--ports 8001 8002 --concurrency 4 --minutes 10"`
Result:
- port 8001: 344 requests, **0 errors**, p50 6.35 s, p95 9.55 s; port 8002: 335 requests, **0 errors**, p50 6.38 s,
  p95 9.43 s (4 in flight per port, max_tokens 128).
- Peak GPU memory 3468 / 4096 MiB (whole GPU, 558 MiB of it the desktop). Peak RAM vllm-1 2.79 GiB, vllm-2 2.92 GiB:
  under the card's ~3 GB, so no `--swap-space` change.
- Both replicas: KV cache 0.47 GiB = 4384 tokens each; healthy after 114 s and 120 s.
Notes:
- Attempt 1 (KV cache sized by `gpu_memory_utilization` 0.42): replica 1 fine (0.62 GiB KV), replica 2 computed
  **-0.79 GiB** and exited. Under WSL vLLM sees only device-wide memory (both replicas log "Initial free memory
  3.22 GiB"), so replica 2 counted replica 1's memory as its own. Fix, suggested by vLLM's own log: a fixed KV
  size, `kv_cache_memory_bytes` in `config/` (480 MiB real/sim, 800 MiB demo) passed as `--kv-cache-memory`.
- With it set, `gpu_memory_utilization` no longer sizes anything; vLLM only checks at startup that this share of
  VRAM is free (`vllm/v1/worker/utils.py`, `request_memory`).
- Replicas start one after the other (`vllm-pair.sh`), so they never profile memory at the same time.
- `concurrency_check.py` uses only the standard library (user decision, no httpx); summary math unit-tested.

### 2026-10-26 · P0.3 · green (demo model changed)
Command: `make vllm-run PROFILE=real` · `make chat-check` · `make vllm-run PROFILE=demo` · `make chat-check`
Result:
- 0.6B: answer "Kubernetes is a system for automating the deployment, scaling, and management of containerized
  applications."; tool call `get_weather({"city": "Ohrid"})`. VRAM 2271 MiB whole GPU, RAM 3.42 GiB.
- **4B AWQ does not fit.** util 0.85: refused at startup (only 3.22 of 4.0 GiB free next to the desktop and the
  CUDA context). util 0.78: weights 2.5 GiB, KV 0.18 GiB, max context **1312 tokens** (4096 needs 0.56 GiB).
- The D8 fallback `Qwen/Qwen3-1.7B` is FP16, 4.06 GB of weights: cannot fit either. Demo model is now
  **Qwen/Qwen3-1.7B-GPTQ-Int8** (user decision): weights 1.92 GiB, KV 0.86 GiB = 8016 tokens at util 0.78,
  chat + tool call OK, VRAM 3213 MiB, RAM 3.85 GiB. After the P0.4 change (fixed 800 MiB KV): 7312 tokens, OK.
- Startup (0.6B, model cached): ~160 s to healthy: 37 s Python imports, 32 s config resolution (HF hub lookup),
  38 s engine process start, 19 s model load (weights 2.7 s), 12 s KV/profiling. 115 s with `HF_HUB_OFFLINE=1`.
  This is the cold start the simulator has to model (D4).
Notes:
- vLLM v0.31.0 (2026-10-05), the newest release at least two weeks old; v0.32.0 was 8 days old. Pinned by digest.
- `max_model_len` 8192 did not fit the 0.6B at util 0.42 (needs 0.88 GiB KV, 0.61 GiB available, max 5728):
  real and sim now use 4096 (user decision).
- Model revisions pinned in `config/` as `model.revision` (user decision), passed as `--revision` and
  `--tokenizer-revision` (without the second, the tokenizer and chat template come from `main`).
- Containers run as the calling user with `HF_HOME=/hf`, so files downloaded into `~/.cache/huggingface` are not
  owned by root. Read-only mount + `HF_HUB_OFFLINE=1` (as the P0.7 pods use it) also tested: chat-check OK.
- Manual step, once per machine: `hf auth login` with a read token (huggingface.co/settings/tokens). Optional:
  without it downloads work, only with lower rate limits. The token lives in `~/.cache/huggingface/token`,
  outside the repo.
- `chat_check.sh` takes a base URL instead of a port (user decision), so P0.7 reuses it against the Service.
- vLLM warnings on this GPU, harmless: pinned memory not available under WSL; FlashInfer top-k sampling needs more
  than 16 SMs, so vLLM falls back.

### 2026-10-26 · P0.6 · green (ladder step 1)
Command: `make k3s && make gpu-check`
Result:
- `make k3s`: k3s v1.36.5+k3s1 installed, nvidia runtime found in the k3s containerd config, RuntimeClass
  `nvidia` applied, device plugin chart 0.20.1 deployed, `node reports allocatable nvidia.com/gpu: 1`.
- `make gpu-check`: plugin variant (`nvidia.com/gpu: 1` limit), pod Succeeded after 6 s (image already pulled):
  `Compute 8.6 CUDA device: [NVIDIA GeForce RTX 3050 Laptop GPU]`, 16384 bodies, 10 iterations in 16.144 ms,
  **3325.434 single-precision GFLOP/s** (P0.2 in Docker: 3321.221, same within noise (one run each)).
Notes:
- **Ladder step 1 worked**: k3s + NVIDIA Container Toolkit + device plugin. The plugin detects WSL2 and reports
  one device `all` backed by `/dev/dxg`, so the node shows exactly one GPU. The no-plugin variant (step 3)
  was not needed and was not run. P0.7 needs 2 GPU slots for 2 pods: time-slicing on top of this, or step 3.
- Proof of step 1 is the allocatable `nvidia.com/gpu: 1`, not nbody alone: the image sets
  `NVIDIA_VISIBLE_DEVICES=all` itself, so with `runtimeClassName: nvidia` it would get the GPU without the plugin.
- The chart's default affinity needs a PCI/NFD node label that never appears under WSL2 (no PCI GPU), so the
  values set `affinity: null`; without it the plugin pod is never scheduled.
- k3s already ships an identical `RuntimeClass nvidia` (packaged addon); ours is applied server-side so the
  cluster does not silently depend on that addon.
- The card's `command: ["nbody", ...]` failed (`exec: "nbody": executable file not found in $PATH`,
  exit 128): in Kubernetes `command` replaces the ENTRYPOINT (`/cuda-samples/sample`, a symlink to
  `/cuda-samples/nbody`), while in P0.2's `docker run` the words after the image were only arguments to it.
  Fixed to `command: ["/cuda-samples/nbody", "-gpu", "-benchmark"]`.
- Departures from the card: (1) the kubeconfig mode is set as `write-kubeconfig-mode: "0644"` in
  `/etc/rancher/k3s/config.yaml` instead of the card's `--write-kubeconfig-mode 644` install flag, so changing
  it does not need a reinstall; (2) Traefik is disabled, which the card did not ask for (P1.2 brings its own
  gateway); ServiceLB kept for LoadBalancer Services.
- k3s pinned to the `stable` channel (v1.36.5), not the newest release (v1.37.1).
- The first pull of the nbody image into k3s's containerd took 1m13s (122 MB): k3s does not share Docker's
  image store.

### 2026-10-25 · P0.2 · green
Command: `make docker-gpu` (twice, `wsl.exe --shutdown` in between) · `docker run --rm --gpus all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark`
Result:
- nbody: `Compute 8.6 CUDA device: [NVIDIA GeForce RTX 3050 Laptop GPU]`, 16384 bodies, 10 iterations in 16.165 ms,
  **3321.221 single-precision GFLOP/s**.
- Image digest: `nvcr.io/nvidia/k8s/cuda-sample@sha256:59261e419d6d48a772aad5bb213f9f1588fcdb042b115ceb7166c89a51f03363`
  (the `nbody` tag is not pinned; the digest records what ran).
- Second `make docker-gpu`: no apt install, no group change, every package at its pin (docker-ce 29.9.2,
  containerd 2.4.2, buildx 0.38.1, Toolkit 1.20.1), runtimes `io.containerd.runc.v2 nvidia runc`.
  Docker was not restarted: started 23:32:28 at WSL boot, `daemon.json` rewritten at 23:37:12 with
  identical content.
- `command -v docker` -> `/usr/bin/docker`; the user is in group `docker` after the WSL restart.
Notes:
- Pins are the newest versions available on 2026-10-25 (user decision), not the two-week rule used for
  the dev tools. No `apt-mark hold` (user decision); rerunning the script moves packages back to their pins.
- Only Engine + buildx; compose and rootless-extras left out (Kubernetes, not Compose).
- Neither Docker's nor NVIDIA's install page lists its key fingerprint. The script pins the fingerprints
  of the keys served over HTTPS on 2026-10-25 (Docker 9DC8...CD88, NVIDIA C95B...ECB0) and stops if they change.
- systemd was already on (`/etc/wsl.conf`), so that step of the script did nothing here.
- Docker Desktop stays installed on Windows with autostart off; its `docker-desktop` distro shares the
  WSL2 VM's memory. The script warns if it is running.
- `wsl --shutdown` only exists on Windows; inside Ubuntu it is `wsl.exe --shutdown`. The script's
  messages say so after the first run hit this.
- `nvidia-ctk` rewrites `daemon.json` on every run and prints INFO lines, and gpg printed keybox messages
  from its temporary home: both made quiet after the second run (`--quiet`). Output-only change, not re-run.

### 2026-10-25 · P0.0 · green
Command: `bash infra/bootstrap.sh` (twice) · `make lint` · `make test`
Result:
- Second bootstrap run installed nothing, printed every version, exit 0: git 2.43.0, make 4.3, jq 1.7,
  yq 4.54.1, Python 3.12.3, uv 0.13.0, Node.js 24.21.0 (npm 11.19.0, bundled), Angular CLI 22.2.2,
  Go 1.27.2, golangci-lint 2.14.0, Helm 4.3.0, k9s 0.51.0, hf 2.2.0. Full list in `versions.md`.
- `make lint`: all hooks pass; golangci-lint, eslint and tsc skipped (no `gateway/` or `ui/` yet), exit 0.
- `make test`: 57 passed (schema tests: valid examples accepted, invalid rejected, one rule per case), exit 0.
Notes:
- Headers keep the `x-` prefix (`x-run-id`, `x-step`, `x-expected-steps`, `x-parallel-group`), as in the
  card. CLAUDE.md still says `run-id`, `step`, ...; it gets aligned in a separate commit.
- `x-step` and `x-expected-steps` are validated as digit strings (`^[1-9][0-9]*$`), not integers as the
  card says: HTTP header values are always strings on the wire. Readers parse them into integers.
- Header names are case-insensitive in HTTP (Go's `net/http` sends `X-Run-Id`), so readers lowercase
  them before validating; written into the schema description after review.
- `types-jsonschema` added beyond the card's tool list, approved: jsonschema ships no type information,
  so `mypy --strict` fails without it.
- `docs/task-cards/05-P0.3-vllm-single.md` is outside the card's files but in this commit: the
  trailing-whitespace hook strips one space from it, and without that `make lint` fails on a fresh clone.
- Fresh-eyes review done in a separate session. Fixed: case-insensitive header names (above) and a test
  docstring that claimed every example file is checked. Left as is: a test that only checks both example
  files exist (the accept/reject tests would fail anyway), and `pre-commit-hooks` pinned by tag `v6.0.0`,
  not by commit hash.
- Open for P1.3: agent calls are non-streaming (D6), so `ttft_ms` in the trace can't be measured by the
  client and has to come from vLLM or the gateway.

### 2026-10-25 · P0.0 prep · green
Command: `df -T ~/thesis` · `nvidia-smi` · `free -h` · `git ls-files --eol` · `ssh -T git@github.com` · `git ls-remote origin` · `curl -s -o /dev/null -w '%{http_code}' https://github.com/Dkostoski30/agent-aware-llm-routing`
Result:
- Repo at `~/thesis` on ext4 in `Ubuntu-24.04`. Line endings all LF, all files mode 100644.
- GPU still visible after the move: RTX 3050 Laptop, driver 621.01, 36 MiB / 4096 MiB used.
- `ssh -T`: "Hi Dkostoski30! You've successfully authenticated".
- `git ls-remote origin`: `refs/heads/master` = `2a781a7`, same as local. Anonymous HTTP returns 404, so the repo is private.
Notes:
- Repo created with `git clone /mnt/c/Users/kosto/Desktop/dipl/thesis ~/thesis`. A plain `cp -r`
  of the kit fails: git object directories are read-only, so `cp` recreates them without write
  permission before writing the objects into them.
- Remote is `git@github.com:Dkostoski30/agent-aware-llm-routing.git`. Auth is an ed25519 SSH key in
  `~/.ssh/id_ed25519`, added to GitHub by hand: one manual step per machine, kept out of the repo
  because it is a secret. No `gh` needed. The GitHub repo already exists, so P0.0 pushes to it
  instead of running `gh repo create`.
- Git identity set in the repo: `Daniel Kostoski <kostoskid66@gmail.com>`.
- Default WSL distro is now `Ubuntu-24.04` (corrects the P0.1 note below).
- `windows/.wslconfig` kept on purpose, although the P0.0 card says to copy the kit without
  `windows/`: it is the repo's only record of the locked memory/swap setting (rule 9).
- `.idea/` is tracked by the Initial commit; untracking it (`.gitignore` + `git rm -r --cached .idea`)
  is part of P0.0.
- `user__n_pass` (plaintext Linux login) exists only in the Windows copy, was never committed, and
  must stay out of the repo.

### 2026-10-24 · P0.1 · green
Command: `wsl -d Ubuntu-24.04 nvidia-smi` · `wsl --version` · `nvidia-smi` (Windows) · `free -h` and `ls -l /dev/dxg` inside WSL · `powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`
Result:
- WSL2 `nvidia-smi`: `NVIDIA GeForce RTX 3050 Laptop GPU`, 127MiB / 4096MiB used, 0% util, no processes. Exit 0.
- Windows `nvidia-smi`: same GPU, driver 621.01, 127MiB used by desktop processes (NVIDIA Overlay, shell, IDE CEF) before anything of ours runs.
- WSL 3.0.3.0, kernel 6.18.54.1-microsoft-standard-WSL2, Ubuntu 24.04.5 LTS, `/dev/dxg` present.
- `.wslconfig` applied: 9.7Gi RAM, 8.0Gi swap.
- No `nvidia*` or `cuda*` package installed inside Ubuntu (as required: the WSL libraries come from the Windows driver).
- Windows power plan Performance, sleep on AC never. C: 140.5 GB free.
Notes:
- Two Ubuntu 24.04 distros exist: `Ubuntu` (24.04.1, user `danielko`) and `Ubuntu-24.04` (24.04.5, user `kosto`). The default distro is `Ubuntu`, so a bare `wsl` lands in the wrong one. All thesis work uses `Ubuntu-24.04`.
- Docker Desktop is installed on Windows (a `docker-desktop` WSL distro exists) and `docker` inside `Ubuntu-24.04` resolves to the Windows shim with WSL integration off. P0.2 has to decide between Docker Desktop integration and a native Docker Engine in the distro.
- Armoury Crate power mode / GPU mode are not machine-checkable; confirmed only by the Windows power plan being Performance.
