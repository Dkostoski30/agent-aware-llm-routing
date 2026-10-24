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
