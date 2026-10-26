# Development plan: LLM call routing and vLLM autoscaling on Kubernetes

Confirmed topic: **Рутирање на LLM повици и автоматско скалирање на vLLM инстанци во Kubernetes околина**
Defense: **by Fri 18.12.2026**. Start: **21.10.2026**. Full scope, nothing cut.
Setup: Windows + WSL2, RTX 3050 4GB, Ryzen 7, 16GB RAM, €0, code written with Claude Code.

---

## Key dates (back-scheduled from the faculty procedure)

| Date | Milestone |
|---|---|
| Sun 1.11 | Gate 1: walking skeleton · **Checkpoint 1: demo to mentor** |
| Sun 15.11 | **Checkpoint 2: chapters 1–3 to mentor** |
| **Sun 22.11** | **Feature freeze (Gate 4)**: everything built and working end to end |
| Sun 29.11 | Experiments done, all CSVs and graphs regenerate |
| **Mon 30.11** | **Checkpoint 3: full draft to mentor** |
| **Wed 9.12** | **Mentor uploads final text** (procedure step 4). 8.12 is a holiday. |
| 10–14.12 | Commission review, 3 working days (step 5) |
| 15–16.12 | Student services check (step 6) |
| **by Fri 18.12** | **Defense** (step 7) |

---

## 1. Rules (no AI slop gets through)

1. **One task = one atomic change.** Small enough for one Claude Code session and one commit (roughly under 200 changed lines). If it's bigger, split it.
2. **Every task has a "Done when" check** you run yourself: a command plus the expected output. A task is done when the check passes on your machine, not when Claude says it works.
3. **Spec before code.** Give Claude Code the task card (goal, files it may touch, interfaces, done-when). Start in plan mode, read the plan, then let it implement.
4. **Tests first for logic.** Plugin rules, metric math and the trace format get table-driven tests (Go `testing`, Python `pytest`) before or with the code. Tests must assert behavior, not just run.
5. **Tests are never edited to make them pass.** If a test is wrong, fix it in a separate, explicit commit.
6. **No new dependency without a reason.** Every version is pinned and recorded in `versions.md`.
7. **Two failed attempts = stop.** Don't patch on top of patches. Revert to the last green commit, rethink the task card, split it.
8. **Phase gates.** Each phase ends with its gate check green and a git tag (`gate-0`, `gate-1`, …). Never start a phase on a red gate.
9. **Fresh-eyes review.** After a task, a new Claude Code session reviews the diff against the task card (checklist in section 8) before you commit.
10. **Spikes before risky builds.** Unknown territory gets a timeboxed spike (read code, prove one thing works) before real implementation.
11. **Push to GitHub every day**, results included. The laptop is the only other copy.

---

## 2. Design decisions

| # | Decision | Why |
|---|---|---|
| D1 | Calls carry headers `x-run-id`, `x-step`, `x-expected-steps`, `x-parallel-group`; the runtime sends an end-of-run signal. `expected-steps` comes from recorded runs per task type; `max-steps` stays only as a safety cap. | Agents often finish long before `max-steps`; progress and remaining work based on it would be wrong. |
| D2 | A shared **run tracker** module (used by both the agent runtime and the replayer) keeps run state and exports remaining work Σ(expected-steps − step) over active runs as a Prometheus metric. | Autoscaling no longer depends on the plugin, so it's built in parallel with routing. |
| D3 | Progress priority: the plugin (or runtime) sets a per-request priority and vLLM's priority scheduling enforces it, if the P4.1 spike confirms it works through the gateway. | Simplest correct enforcement point; the queue lives in vLLM. |
| D4 | The simulator models cold start (measured model load time) and cached vs uncached prefill. | Without it, autoscaling and affinity results look unrealistically good. |
| D5 | Autoscaling strategies are validated on real vLLM at 1↔2 pods; the simulator extends them to larger scale. | CPU-based HPA on simulated pods is meaningless; real pods give a ground truth. |
| D6 | LLM calls from the agent are non-streaming; the UI streams steps over SSE. | Retry after a pod failure stays simple and correct. |
| D7 | Run history in Redis only. | Nothing needs a relational database. |
| D8 | Qwen3-0.6B-GPTQ-Int8 for all experiments and the infrastructure demo (needs 2 replicas). Practical demo on Qwen3-1.7B-GPTQ-Int8 as a single replica: Qwen3-4B-AWQ did not fit (P0.3, 1312-token context), the FP16 Qwen3-1.7B is 4 GB of weights. | Only the 0.6B fits twice in 4GB; the 4B is much better at multi-step tool use. |
| D10 | Agent tools are high-level and deterministic (Python computes, the model orchestrates). Tool arguments are enums from fixed lists where possible; tool calls use vLLM structured outputs. | Small models pick the right tool but get arguments wrong; this removes that failure mode. |
| D11 | One source of configuration: `config/` holds three profiles (`real`, `sim`, `demo`) with every tunable (model, replicas, thresholds, TTLs, KEDA targets, drain timeout). Each routing rule has a feature flag. Each experiment is a YAML file in `experiments/`; results are stamped with its hash. | Switching GPU/simulator/demo or swapping the model is one file; ablation needs flags; any result can be re-run from its file. |
| D12 | The headers (D1) and the trace format (D9) are versioned JSON schemas in `schemas/`; runtime, replayer and plugin run contract tests against them. Metrics start with `thesis_`; logs are JSON with `run-id` on every line. | Shared formats can't drift silently; one task can be followed across every component. |
| D9 | Traces record the time between calls (tool time) and the replayer reproduces it. | Otherwise replays put more load on the system than real agents. |

---

## 3. Repo layout (monorepo)

```
thesis/
├── CLAUDE.md            # context + rules for Claude Code
├── PLAN.md              # this file
├── versions.md          # every pinned version + machine setup
├── Makefile             # up, down, test, smoke, replay, experiment
├── config/              # profiles: real.yaml, sim.yaml, demo.yaml (D11)
├── schemas/             # versioned JSON schemas: headers, trace (D12)
├── infra/               # WSL2/k3s setup scripts, Helm values, manifests
├── gateway/             # Go: Endpoint Picker scheduling plugin
├── agent/               # Python/FastAPI: agent runtime, tools, run tracker
├── loadgen/             # Python: trace recorder, replayer, converters
├── sim/                 # simulator config + calibration data
├── ui/                  # Angular demo app
├── experiments/         # scripts, configs, results (CSV), graphs, log.md
└── docs/thesis/         # chapters
```

All scripts are idempotent: `make up` rebuilds the whole cluster from zero. WSL2 resets and laptop sleeps must never cost more than one command.

---

## 4. Brick walls and fallbacks

| Risk | Detected in | Fallback |
|---|---|---|
| GPU not visible to Kubernetes under WSL2 | P0.6 | Ladder below; 2-day rule |
| Two vLLM replicas don't fit in 4GB | P0.4 | One real replica + simulator for the rest |
| Gateway API Inference Extension changes its API | P1.2 | Pin a release tag, never upgrade during the thesis |
| Go plugin harder than expected | P3.1 spike | Fork an existing scorer plugin, change it instead of writing from zero |
| vLLM priority not reachable through the gateway | P4.1 spike | Implement ordering inside the plugin's queue handling |
| Simulator too inaccurate | P2.9 | Calibrate per concurrency level; report the error openly |
| Noisy experiment results | P5 | 3 repetitions, fixed seeds, discard warm-up |
| Weeks 9–22.11 overrun | Gate 3 | Shift writing of ch. 4 into the experiment week; never move the freeze date |

**GPU in Kubernetes under WSL2, fallback ladder (stop at the first that works):**
1. k3s inside WSL2 + NVIDIA Container Toolkit + current NVIDIA device plugin. Check: node reports allocatable `nvidia.com/gpu`.
2. minikube with the Docker driver inside WSL2 (`--gpus=all`).
3. No device plugin: pods use `runtimeClassName: nvidia` with `NVIDIA_VISIBLE_DEVICES=all` and no `nvidia.com/gpu` request; the NVIDIA runtime injects `/dev/dxg` and the WSL libraries. Multiple pods sharing one GPU is normal CUDA behavior, so this also replaces time-slicing on a single node.
4. Dual-boot Ubuntu 24.04.

---

## 5. Phases and atomic tasks

### Phase 0: Environment (21–25.10). Kill the biggest unknowns first.

Order: P0.1 → P0.0 → P0.2 → **P0.6** → P0.3 → P0.4 → P0.5 → P0.7. P0.1 comes first because the repo lives inside WSL. The GPU inside Kubernetes is tested before anything else because it's the only step that can block the whole plan. Each task has a ready card in `docs/task-cards/`.

**2-day rule:** if P0.6 isn't green by the end of day 2, stop debugging and dual-boot Ubuntu 24.04 (ladder step 4).

| ID | Task | Done when |
|---|---|---|
| P0.0 | Repo setup: git + GitHub remote, Makefile skeleton, `infra/bootstrap.sh` (dev tools, pinned), pre-commit hooks (ruff, mypy, golangci-lint, eslint, strict TypeScript), test runners; `config/` profiles (D11); `schemas/` for headers and traces (D12) | `pre-commit run --all-files` passes; `make test` runs; example header set and trace line validate against the schemas |
| P0.1 | Windows NVIDIA driver, WSL2 Ubuntu 24.04, `.wslconfig` (memory=10GB, swap=8GB); lock machine setup | `nvidia-smi` inside WSL2 shows the RTX 3050; power mode, GPU mode, `.wslconfig` and driver version written to `versions.md` |
| P0.2 | Docker engine + NVIDIA Container Toolkit in WSL2 | `docker run --rm --gpus all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark` runs on the GPU |
| P0.6 | Kubernetes with GPU (ladder, section 4) | A pod runs the same CUDA sample on the GPU |
| P0.3 | vLLM in Docker with Qwen3-0.6B-GPTQ-Int8, tool calling on; same check with Qwen3-4B-AWQ as a single replica (`--max-model-len 4096`) | Chat request returns text; a request with a tool definition returns `tool_calls` JSON; 4B fit (yes/no, VRAM) written to `versions.md` |
| P0.4 | Two vLLM containers at `--gpu-memory-utilization 0.42` each | Both answer concurrent requests for 10 minutes, no OOM; VRAM and host RAM per container saved to `versions.md` |
| P0.5 | Prefix cache visible | Same long prompt sent twice: the prefix-cache hit metric on `/metrics` increases |
| P0.7 | 2× vLLM as pods behind a Service (time-slicing config for 2 GPU slots, or ladder step 3) | Both pods Ready; `curl` through the Service answers |

**Gate 0:** two vLLM pods on the GPU answer through a Service, prefix caching visible.

### Phase 1: Walking skeleton (26.10–1.11)

| ID | Task | Done when |
|---|---|---|
| P1.0 | `make up` / `make down` reproduce Phase 0 from scripts | `make down && make up` brings both vLLM pods back to Ready with no manual step |
| P1.1 | Prometheus + Grafana scraping vLLM | Grafana panel shows queue length, KV-cache usage, prefix-cache hit rate per pod |
| P1.2 | Gateway (Envoy Gateway or kgateway) + Gateway API Inference Extension, default Endpoint Picker. Pin version. | Requests through the gateway reach both pods; picker logs show its choice |
| P1.3 | Agent runtime v0: loop + calculator tool, non-streaming calls (D6), headers (D1), run history in Redis (D7) | `pytest` with a mocked LLM passes; a real task completes through the gateway; headers visible in gateway logs; history readable from Redis |
| P1.4 | Angular shell: task input + live step list (SSE) | Typing a task in the UI shows each step as it happens |
| P1.5 | `make smoke`: UI-less end-to-end check | One command runs a task through every layer and exits 0 |
| P1.6 | Practical go/no-go: one demo task (CSV analysis) with high-level tools (D10) on the demo model, 10 runs | ≥9/10 correct results → go. Below that: narrow the tasks, or move the demo to a FINKI GPU server (ask at Checkpoint 1) |

**Gate 1:** `make smoke` green; P1.6 result known. **Checkpoint 1:** show the walking skeleton to the mentor; ask whether a FINKI GPU server is available for the demo.

### Phase 2: Workload and simulator (2–8.11)

| ID | Track | Task | Done when |
|---|---|---|---|
| P2.1 | Routing | Tools for 3 demo scenarios (D10): CSV analysis (`csv_summary`, `group_by`, `compare`), contracts (`read_document`, `extract_clauses`, `diff_clauses`), support (`get_order`, `get_policy`, `draft_reply`); enum arguments, structured outputs, parallel tool calls | Each tool has a unit test; the contracts task reads documents in parallel |
| P2.2 | Routing | Call recorder: every LLM call logged (run-id, step, prompt tokens, pod, TTFT, duration, gap since previous call) | A run produces one JSONL line per call with all fields |
| P2.3 | Routing | Trace format + replayer: arrival patterns (steady, burst), inter-call gaps (D9), fixed seed | Replaying 20 recorded runs reproduces the same call sequence and gaps twice in a row |
| P2.4 | Routing | AgentProcessBench converter to the trace format; `expected-steps` per task type derived from recorded runs (D1) | Converted traces pass the schema check; replayer runs them |
| P2.5 | Routing | First baseline: round-robin vs default picker, defined as `experiments/baseline.yaml` (D11), results to CSV + graph script | `make experiment NAME=baseline` regenerates the CSV and graph from scratch |
| P2.6 | UI | Angular: per-step badges (pod, cache hit, latency) | Badges match the recorder's log for the same run |
| P2.7 | Autoscaling | Simulator (llm-d-inference-sim) deployed as a pool backend | Replayer runs against simulated pods through the same gateway |
| P2.8 | Autoscaling | Calibration on the real 0.6B: TTFT and per-token time vs concurrency, cached vs uncached prefill, cold start time (D4) | Calibration table saved in `sim/`; simulator configured from it |
| P2.9 | Autoscaling | Validation: same replay on 1 real pod vs 1 simulated pod | Error within ~10–15%, table saved; if not, recalibrate |

**Gate 2:** baseline experiment regenerates from one command; simulator validated.

### Phase 3: Task affinity, overload guard, remaining work (9–15.11)

| ID | Track | Task | Done when |
|---|---|---|---|
| P3.1 | Routing | Spike: read the Endpoint Picker plugin interface; build the picker from source unchanged, import the image into k3s (`k3s ctr images import` or local registry) | Self-built image behaves exactly like upstream on the baseline replay |
| P3.2 | Routing | Plugin skeleton registered, no-op scoring, unit test harness, one feature flag per routing rule read from the profile (D11) | Tests run; picker loads the plugin; behavior unchanged |
| P3.3 | Routing | Task affinity: `run-id → pod` map with TTL, cleared on end-of-run; first step picked by load + prefix, later steps pinned | Table-driven tests pass; replay shows steps 2..n on the same pod; cache hit rate above baseline |
| P3.4 | Routing | Pod disappears → affected runs remapped | Test with a removed pod passes; no request fails in a replay with a pod deleted |
| P3.5 | Routing | Overload guard: spill when the pinned pod's queue passes a threshold; remap only if sustained (hysteresis) | Tests cover spike vs sustained overload; no remap on a short spike |
| P3.6 | Routing | Parallel calls: same `parallel-group` stays on the pod if it has room, otherwise spills | Tests pass; burst replay shows spill only under overload |
| P3.7 | Routing | Burst replay vs default picker | p95 latency not worse than default under overload; result in CSV |
| P3.8 | Autoscaling | Run tracker (D2) in runtime and replayer exports remaining work | Test: metric equals Σ(expected-steps − step) for a known set of runs; ended runs drop out |
| P3.9 | Autoscaling | KEDA ScaledObject on remaining work (simulated pool) | Burst replay: scale-up starts before the queue passes the threshold |
| P3.10 | Autoscaling | Baselines: HPA on CPU, KEDA on queue depth | Both run on the same replay; results in CSV |
| P3.11 | UI | Angular: infrastructure panel (pods, queues, remaining work) | Panel numbers match Grafana |

**Gate 3:** all routing rules except priority pass their tests and replays; KEDA scales on remaining work.

### Phase 4: Priority, drain, failover. Feature freeze (16–22.11)

| ID | Track | Task | Done when |
|---|---|---|---|
| P4.1 | Routing | Spike: vLLM priority scheduling reachable through the gateway (D3); implement priority by `step / expected-steps` with aging | Tests prove near-finished runs go first and no run waits beyond the aging bound |
| P4.2 | Autoscaling | Warm-up routing: new runs prefer the newest pod | Test + replay: new pod receives new runs right after Ready |
| P4.3 | Autoscaling | Drain: preStop hook, draining mark, no new runs, timeout | Scale-down during replay: 0 interrupted runs |
| P4.4 | Autoscaling | Failover: pod killed mid-run, runtime retries, run continues from Redis history | Replay with a pod kill: completion rate measured and logged |
| P4.5 | Autoscaling | Real-hardware validation (D5): CPU HPA vs KEDA queue vs remaining work at 1↔2 real vLLM pods | Same ranking as on the simulator, or the difference explained in `experiments/log.md` |
| P4.6 | UI | Angular: demo buttons ("start 20 tasks", "kill pod") | Buttons trigger the replayer and pod deletion |
| P4.7 | Demo | Practical task suite: each of the 3 demo scenarios run 10 times on the demo model | Every scenario ≥9/10 correct; success table saved in `experiments/` (goes into the thesis as proof of practical use) |

**Gate 4 = feature freeze (Sun 22.11).** Everything works end to end. From here on, only bug fixes.

### Phase 5: Experiments (23–29.11)

| ID | Task | Done when |
|---|---|---|
| P5.1 | E1 routing: round-robin vs default vs plugin + ablation (each rule off) | 3 repetitions, CSV + graphs, config hash saved |
| P5.2 | E2 autoscaling: CPU HPA vs KEDA on queue vs remaining work, on bursty traces (simulator at scale + real 1↔2) | Completion time, throughput, time to capacity, idle pod-minutes, interrupted runs |
| P5.3 | E3 failure: pod killed mid-run | Completion rate and recovery cost |
| P5.4 | Demo rehearsal | Full demo runs twice in a row without intervention |

**Gate 5:** every CSV and graph in the thesis regenerates from `make experiment`.

### Phase 6: Mentor review and final text (30.11–9.12)

- Mon 30.11: full draft to mentor (Checkpoint 3).
- Fixes from mentor feedback, chapter 7, second demo rehearsal.
- **Wed 9.12: mentor uploads the final text.**

### Phase 7: Procedure and defense (10–18.12)

Commission review, student services check, defense slides, final demo rehearsal. Only bug fixes in code.

---

## 6. Writing track (runs in parallel)

| Week | Chapter |
|---|---|
| 26.10–1.11 | 1. Вовед |
| 2–8.11 | 3. Позадина (vLLM, KV cache, Kubernetes, Gateway API Inference Extension) |
| 9–15.11 | 2. Поврзана работа → **ch. 1–3 to mentor 15.11** |
| 16–22.11 | 4. Дизајн на системот |
| 23–29.11 | 5. Имплементација; 6. Евалуација (setup and method first, results as they land) |
| 30.11 | **Full draft to mentor** |
| 1–8.12 | 7. Заклучок и идна работа, fixes from review |

---

## 7. Claude Code workflow per task

1. Copy the task card (ID, goal, files allowed, interfaces, done-when) into the session.
2. Plan mode: Claude proposes the change. You read it. Reject anything outside the card.
3. Tests first where the task has logic.
4. Implement.
5. You run the done-when check yourself and paste the output into the commit message or `experiments/log.md`.
6. A new session reviews the diff against the card and the checklist below.
7. Commit: `P3.3: task affinity scoring` (one task, one commit). Push.

---

## 8. Review checklist (every diff)

- Touches only the files the card allows?
- Any dead code, TODOs, commented-out blocks, or unexplained constants?
- Any broad `try/except` or ignored errors?
- Tests assert behavior, and none were changed to pass?
- New dependency? Is it pinned and justified?
- Logs and metrics added where the thesis needs measurements?
- Could you explain every line at the defense? If not, it doesn't go in.
