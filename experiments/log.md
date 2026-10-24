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
