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
