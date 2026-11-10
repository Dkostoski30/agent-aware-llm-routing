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
| D1 | Calls carry headers `x-run-id`, `x-step`, `x-expected-steps`, `x-parallel-group`; the runtime sends an end-of-run signal (to the plugin, `POST /runs/{id}/end`, decided 2026-11-09, P3.3). `expected-steps` comes from recorded runs per task type; `max-steps` stays only as a safety cap. | Agents often finish long before `max-steps`; progress and remaining work based on it would be wrong. |
| D2 | A shared **run tracker** module (used by both the agent runtime and the replayer) keeps run state and exports remaining work Σ(expected-steps − step) over active runs as a Prometheus metric; a run past its expected steps still counts 1, so each run adds max(expected-steps − step, 1) (decided 2026-11-09, P3.8). | Autoscaling no longer depends on the plugin, so it's built in parallel with routing. |
| D3 | Progress priority: the plugin (or runtime) sets a per-request priority and vLLM's priority scheduling enforces it, if the P4.1 spike confirms it works through the gateway. | Simplest correct enforcement point; the queue lives in vLLM. |
| D4 | The simulator models cold start (measured model load time) and cached vs uncached prefill. | Without it, autoscaling and affinity results look unrealistically good. |
| D5 | Autoscaling strategies are validated on real vLLM at 1↔2 pods; the simulator extends them to larger scale. | CPU-based HPA on simulated pods is meaningless; real pods give a ground truth. |
| D6 | LLM calls from the agent are non-streaming; the UI streams steps over SSE. | Retry after a pod failure stays simple and correct. |
| D7 | Run history in Redis (agent runtime). Users, sessions, chats, messages and attachments in SQLite (app backend, 1 replica, persistent volume). | Runs are short-lived state shared by the runtime and replayer; users → chats → messages is relational product data. Keeping them apart lets experiments run without the app. SQLite allows only one writer pod, which is fine: only vLLM scales. |
| D8 | Qwen3-0.6B-GPTQ-Int8 for all experiments and the infrastructure demo (needs 2 replicas). Practical demo on Qwen3-1.7B-GPTQ-Int8 as a single replica: Qwen3-4B-AWQ did not fit (P0.3, 1312-token context), the FP16 Qwen3-1.7B is 4 GB of weights. | Only the 0.6B fits twice in 4GB; the 4B is much better at multi-step tool use. |
| D10 | Agent tools are high-level and deterministic (Python computes, the model orchestrates). Tool arguments are enums from fixed lists where possible. `agent.tool_choice` in `config/` picks one of two modes. `required`: vLLM constrains every call to the tool schemas (structured outputs) and the model ends the run by calling a `final_answer` tool. `auto`: the model either calls a tool or answers in text; the runtime validates every call against the tool's JSON schema, and an invalid call, or a text answer that contains a tool-call marker, goes back to the model with the error and the valid values, up to a retry limit in `config/`. Every retry is counted and logged. Profiles start as `demo: required`, `real` and `sim: auto`. | Small models pick the right tool but get arguments wrong. `required` makes a malformed call impossible, which the demo's correctness needs most. `auto` keeps the traffic shaped like a real agent's (the answer is plain text, no artificial final tool), which the recorded traces for experiments should be. The marker check catches a call the tool parser missed, which would otherwise end the run with a broken answer. P1.6b runs both modes on the demo model; the demo keeps the better one. Changed from structured outputs only on 2026-10-27. |
| D11 | One source of configuration: `config/` holds three profiles (`real`, `sim`, `demo`) with every tunable (model, replicas, thresholds, TTLs, KEDA targets, drain timeout). One more, `real-plain`, is `real` without `model.lora`, deployed only by P2.8's calibration to measure the adapter's cost; a test keeps it equal to `real` otherwise (added 2026-11-05). Each routing rule has a feature flag. Each experiment is a YAML file in `experiments/`; results are stamped with its hash. | Switching GPU/simulator/demo or swapping the model is one file; ablation needs flags; any result can be re-run from its file. |
| D12 | The headers (D1), the trace format (D9), the run request (app backend → agent runtime, D13), the run events (agent runtime → UI over SSE, D6, P1.4b-1) and the run calls (agent runtime → UI, P2.6b, added 2026-11-04) are versioned JSON schemas in `schemas/`; every component that reads or writes one runs contract tests against it. Metrics start with `thesis_`; logs are JSON with `run-id` on every line that belongs to a run; app backend lines outside a run (login, chat list) carry `request-id` instead. | Shared formats can't drift silently; one task can be followed across every component. |
| D14 | The UI follows the Tenax design system in `docs/design-system/` (tokens, component classes, screens). Where it disagrees with D1–D13 the decisions win: the model picks the scenario (D13) and the UI shows it read-only on the run; scenario cards only fill the composer with an example. Login, chat list and attachments, which the design has no cards for, are built from its tokens and components. The step badges differ from its Tag card on purpose (P2.6, 2026-11-04): the cache badge reads "99% cached" instead of hit or miss, and any cached token makes it teal, so a step that moved to a warm pod can be teal and only the percent shows the move; the pod name drops its ReplicaSet hash, with the full name on hover. The cluster panel sits beside the chat and a toggle shows or hides it. | One visual language for the whole demo; the panel beside the chat shows a step landing on a pod in one glance, and the toggle gives the chat the full width when the cluster doesn't matter. Decided 2026-10-26. |
| D9 | Traces record the time between calls (tool time) and the replayer reproduces it. | Otherwise replays put more load on the system than real agents. |
| D13 | One chat message = one run. Step 1 picks the scenario (enum structured output: `csv`, `contracts`, `support`, `out_of_scope`); later steps see only that scenario's tools. Step 1 sends a default `expected-steps` from `config/`; the run tracker updates it once the scenario is picked. The app backend sends attached files with the run request; the agent runtime keeps them for that run only (size limit in `config/`), and tools take them as enum IDs. History = user messages + final answers, capped by a token budget in `config/`; when over budget the oldest half is dropped. No chat-level header: turn-to-turn cache reuse relies on the stable prefix. | The model never chooses among all 9 tools at once; files never enter the 4096-token context; dropping in blocks keeps the prompt prefix stable across turns so prefix-cache hits survive. A header would change D1 and D12; add it only if experiments show turn-to-turn misses. Sending files with the request keeps the agent runtime unaware of the app. |

---

## 3. Repo layout (monorepo)

```
thesis/
├── CLAUDE.md            # context + rules for Claude Code
├── PLAN.md              # this file
├── versions.md          # every pinned version + machine setup
├── Makefile             # up, down, test, smoke, replay, experiment
├── config/              # profiles: real.yaml, sim.yaml, demo.yaml, real-plain.yaml (D11)
├── schemas/             # versioned JSON schemas: headers, trace, run request, run events, run calls (D12)
├── infra/               # WSL2/k3s setup scripts, Helm values, manifests
├── gateway/             # Go: Endpoint Picker scheduling plugin
├── agent/               # Python/FastAPI: agent runtime, tools, run tracker
├── app/                 # Python/FastAPI + SQLite: users, sessions, chats, attachments (D7)
├── loadgen/             # Python: trace recorder, replayer, converters
├── sim/                 # simulator config + calibration data
├── ui/                  # Angular app: login, chats, step view, cluster panel (D14)
├── experiments/         # scripts, configs, results (CSV), graphs, log.md
├── docs/design-system/ # Tenax design system: tokens, components, screens (D14)
└── docs/thesis/         # chapters
```

All scripts are idempotent: `make up` rebuilds the whole cluster from zero. WSL2 resets and laptop sleeps must never cost more than one command.

---

## 4. Brick walls and fallbacks

| Risk | Detected in | Fallback |
|---|---|---|
| GPU not visible to Kubernetes under WSL2 | P0.6 | Ladder below; 2-day rule |
| Two vLLM replicas don't fit in 4GB | P0.4 | One real replica + simulator for the rest |
| Gateway API Inference Extension changes its API | P1.2 | Pin a release tag, never upgrade during the thesis. Happened before P1.2: the EPP moved to llm-d-router and kgateway dropped InferencePool (spike 2026-10-27, `experiments/log.md`) |
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
| P1.2 | Gateway (agentgateway) + Gateway API Inference Extension InferencePool, default Endpoint Picker (llm-d-router, where the EPP moved in GAIE v1.6.0; spike 2026-10-27). Pin version. | Requests through the gateway reach both pods; picker logs show its choice |
| P1.3 | Agent runtime v0: loop + calculator tool, non-streaming calls (D6), headers (D1), run history in Redis (D7) | `pytest` with a mocked LLM passes; a real task completes through the gateway; headers visible in gateway logs; history readable from Redis |
| P1.4 | Angular shell in the design system (D14): tokens and both themes, top bar, New task screen, run view with live step cards (SSE) | Typing a task in the UI shows each step as it happens |
| P1.5 | `make smoke`: UI-less end-to-end check | One command runs a task through every layer and exits 0 |
| P1.6 | Practical go/no-go: one demo task (CSV analysis) with high-level tools (D10) on the demo model, 10 runs | ≥9/10 correct results → go. Below that: narrow the tasks, or move the demo to a FINKI GPU server (ruled out by the user 2026-10-30: everything runs on the laptop) |
| P1.7 | Fine-tune the 0.6B (after Gate 1, agreed with the mentor at Checkpoint 1): LoRA on Qwen3-0.6B trained on tool-call trajectories that Python generates from the deterministic tools (D10), served on the GPTQ-Int8 0.6B (D8 unchanged). Steps: feasibility spike, data generator + held-out baseline, overnight training (`docs/task-cards/19-P1.7-fine-tune-tool-calls.md`) | The held-out score beats the baseline, and the demo go/no-go questions on the real profile with the adapter reach ≥9/10 in the kept mode |

**Gate 1:** `make smoke` green; P1.6 result known. **Checkpoint 1:** show the walking skeleton to the mentor; ~~ask whether a FINKI GPU server is available for the demo~~ (ruled out by the user 2026-10-30: the demo stays on the laptop's RTX 3050).

### Phase 2: Workload and simulator (2–8.11)

| ID | Track | Task | Done when |
|---|---|---|---|
| P2.1 | Routing | Tools for the other 2 demo scenarios (D10): contracts (`read_document`, `extract_clauses`, `diff_clauses`), support (`get_order`, `get_policy`, `draft_reply`); the CSV tools (`csv_summary`, `group_by`, `compare`) exist since P1.6a; enum arguments, both D10 modes, parallel tool calls; files come from the run request by ID (D13), support's orders and policies included. P1.6f (calculator only on runs without files) folded in. Split: P2.1a-1 contracts, P2.1a-2 support, P2.1a-3 calculator + demo go/no-go rerun, P2.1b adapter retrain (cards 20–23, 2026-11-01). P2.1b: adapter `5554fcf81b09` in `config/real.yaml`, held-out 167/168 in both modes, real go/no-go 10/10 (2026-11-02) | Each tool has a unit test; the contracts task reads documents in parallel (several calls in one reply); demo go/no-go still ≥9/10 |
| P2.2 | Routing | Call recorder: every LLM call logged (run-id, step, prompt tokens, cached tokens, pod, duration, gap since previous call). No TTFT in agent calls: they stay non-streaming (D6); the replayer and P2.8 measure TTFT (decided 2026-11-01). Split: P2.2a agent records, P2.2b `make record` to trace.v1 | A run produces one JSONL line per call with all fields |
| P2.3 | Routing | Trace format + replayer: arrival patterns (steady, burst), inter-call gaps (D9), fixed seed, fan-out of steps into parallel calls with one `x-parallel-group` (the P3.6 workload; decided 2026-11-01) | Replaying 20 recorded runs reproduces the same call sequence and gaps twice in a row |
| P2.4 | Routing | AgentProcessBench converter to the trace format; `expected-steps` per task type derived from recorded runs (D1) | Converted traces pass the schema check; replayer runs them |
| P2.5 | Routing | First baseline: round-robin vs default picker, defined as `experiments/baseline.yaml` (D11), results to CSV + graph script (matplotlib, loadgen only; decided 2026-11-01). llm-d-router v0.11.0 has no round-robin picker: the variant is `max-score-picker` with no scorers, near round-robin, with the live share of picks that switch pod reported (spike 2026-11-03, card 28) | `make experiment NAME=baseline` regenerates the CSV and graph from scratch |
| P2.6 | UI | Angular: per-step badges (pod, cache hit, latency). Split 2026-11-04 (card 29): P2.6a file attach in the composer, pulled forward from P3.13 since a CSV question needs a file; P2.6b the badges, read from a new `GET /runs/{id}/calls`; P2.6c (2026-11-05) a warning on an answer no tool backs and `agent.max_result_chars`, after a free-form CSV run made up its answer. The step table, tabs and detail sheet have no task, at most Phase 6 polish | Badges match the recorder's log for the same run |
| P2.7 | Autoscaling | Simulator (llm-d-inference-sim) deployed as a pool backend | Replayer runs against simulated pods through the same gateway |
| P2.8 | Autoscaling | Calibration on the real 0.6B: TTFT and per-token time vs concurrency, cached vs uncached prefill, cold start time (D4). Split 2026-11-05 (card 31): P2.8a measures with `make calibrate` (P2.8a-1 the logic and its tests, P2.8a-2 the live driver and the overnight run), P2.8b sets `config/sim.yaml` from the table once its mappings are decided | Calibration table saved in `sim/`; simulator configured from it |
| P2.8c | Autoscaling | Recalibration after P2.9's first run missed at 1 pod (added 2026-11-07, card 31b): the lora 1-pod table measured again with `make calibrate`, `config/sim.yaml` remapped by P2.8b's rules | New table in `sim/`; the mapping test passes on it |
| P2.9 | Autoscaling | Validation: same replay on 1 real pod vs 1 simulated pod | Error within ~10–15%, table saved; if not, recalibrate |
| P2.10 | App | App backend skeleton: FastAPI + `sqlite3`, schema (users, sessions, chats, messages, attachments), register/login/logout, `hashlib.scrypt`, session cookie with TTL from config (D7); Deployment + PVC in `infra/` | pytest: register → login → authorized request passes; wrong password and expired session rejected; data survives a pod restart; `make down && make up` keeps users and chats, `make reset-app` wipes them |
| P2.11 | Infra | Fixes after P2.1b (added 2026-11-02, card 23b): `make smoke` checks only Running vLLM pods, so pods left Failed at GPU admission by a reboot or `make down && make up` no longer fail it; `make vllm-deploy` refuses a `model.lora` name or path that doesn't end with the first 12 digits of its sha256; the heldout comment gives 336 runs; lint leaves `experiments/results/` as the scripts wrote it | `make smoke` exits 0 with a Failed pod labelled vllm present; `make vllm-deploy` refuses P2.1b's name-only config edit before deploying anything |
| P2.12 | Infra | Fix after P2.8c (added 2026-11-08, card 35): `make vllm-deploy` and `make sim-deploy` pass helm 4's `--force-conflicts`, so a deploy over a Deployment that `kubectl scale` resized (`make calibrate`'s final restore, P2.8c) sets the profile's replicas instead of failing on `.spec.replicas` | `kubectl scale deployment/vllm --replicas=1`, then `make vllm-deploy PROFILE=real`: no conflict, 2 pods Running |

Order (2026-11-01): P2.1a-1 → P2.1a-2 → P2.1a-3 → P2.1b → P2.11 (added 2026-11-02), then P2.2a → P2.2b → P2.3 → P2.4 → P2.5 and P2.6 after P2.2a; P2.7 → P2.8 → P2.9 once P2.3 exists (P2.9's first run missed, so P2.9a → P2.8c → P2.9's second run, added 2026-11-07); P2.10a/b (split, cards 33–34) is the first to slip into Phase 3's week, since it is not on Gate 2. P2.12 (added 2026-11-08) comes last, before the Gate 2 check.

**Gate 2:** baseline experiment regenerates from one command; simulator validated. Green 2026-11-08 (`experiments/log.md`): simulator validated with a stated limit, call-duration and run-completion medians within 15% of real pods at 1 and 2 pods, tails 18-91% optimistic under GPU-memory pressure (P2.9).

### Phase 3: Task affinity, overload guard, remaining work (9–15.11)

| ID | Track | Task | Done when |
|---|---|---|---|
| P3.1 | Routing | Spike: read the Endpoint Picker plugin interface; build the picker from source unchanged: `gateway/` requires llm-d-router v0.11.0 and runs its runner, `make picker-image` imports `thesis/picker:<hash of gateway/>` into k3s (`k3s ctr images import`), `make up` deploys it, `infra/helm/router-upstream-values.yaml` puts upstream's image back | On sim, the self-built image's hit rate, moved steps and pod switches lie within the spread of two upstream runs, or no further outside it than that spread's width (card 36, decided 2026-11-09) |
| P3.2 | Routing | Plugin skeleton registered, a no-op filter (a filter is a hard rule, a scorer can be outvoted; decided 2026-11-09, card 37), unit test harness, one feature flag per routing rule read from the profile (D11); `make up` and `set-routing.sh` apply the profile's routing, a variant may lay `routing:` rules over it (decided 2026-11-10) | Tests run; picker loads the plugin; behavior unchanged |
| P3.3 | Routing | Task affinity: `run-id → pod` map with TTL, cleared on end-of-run (`POST /runs/{id}/end`, D1); first step picked by load + prefix, later steps pinned | Table-driven tests pass; replay on real pods, one order, shows steps 2..n on the same pod; cache hit rate above the default variant's, read against default's four-run range 0.794-0.836 (card 38) |
| P3.4 | Routing | Pod disappears → affected runs remapped | Test with a removed pod passes; in a replay with a pod deleted, no call sent after the pod left the pool fails and the run's later steps land on another pod; calls running on it are counted, retry is P4.4 (narrowed 2026-11-09, card 39) |
| P3.5 | Routing | Overload guard: spill when the pinned pod's queue passes a threshold; remap only if sustained (hysteresis) | Tests cover spike vs sustained overload; no remap on a short spike; replay on sim (card 40) |
| P3.6 | Routing | Parallel calls: same `parallel-group` stays on the pod if it has room, otherwise spills | Tests pass; burst replay on sim shows spill only under overload (card 41) |
| P3.7 | Routing | Burst replay vs default picker | p95 latency not worse than default under overload, on real pods in both orders with a GPU log (Phase 5's rule, decided 2026-11-09, card 42); result in CSV |
| P3.8 | Autoscaling | Run tracker (D2) in runtime and replayer exports remaining work | Test: metric equals Σ max(expected-steps − step, 1) for a known set of runs, one of them past its expected steps; ended runs drop out; updating a run's expected-steps after its first step (D13) changes the metric |
| P3.9 | Autoscaling | KEDA ScaledObject on remaining work (simulated pool). Split 2026-11-09: P3.9a KEDA pinned in `make up` (card 44), P3.9b the ScaledObject and experiment variants (card 45) | Burst replay: scale-up starts before the queue passes the threshold |
| P3.10 | Autoscaling | Baselines: HPA on CPU on real 1↔2 pods only, since D5 finds it meaningless on sim; KEDA on queue depth on the simulator (decided 2026-11-09, card 46) | Both run on the same replay shape; results in CSV; whether CPU scaled at all is recorded |
| P3.11 | UI | Angular: infrastructure panel (pods, queues, remaining work), from the app backend's `GET /cluster` (card 47) | Panel numbers match Grafana |
| P3.12 | App | Chats, messages, attachments API; one message → one agent-runtime call with the chat's newest files, up to `agent.max_files` (decided 2026-11-09), in the request (`schemas/run-request`, D12, D13); `run_id` stored on the assistant message; steps fetched from the agent runtime by `run_id` | pytest with mocked runtime passes; run-request contract tests pass in app and agent; a real message through every layer returns an answer and its run_id; another user's chat returns 404; `make smoke` goes through the app backend. Split 2026-11-09: P3.12a the API on a mocked runtime (card 48), P3.12b through every layer (card 49) |
| P3.13 | UI | Angular: login/register, chat list, open chat with file attach (the composer's attach exists since P2.6a and posts to the agent; here it goes through the app backend); step view (P1.4 + P2.6 components) expandable under each answer; P3.11 panel and P4.6 buttons in the cluster panel beside the chat, shown or hidden by a toggle (D14) | Register, create chat, attach CSV, ask, see answer with steps; reload keeps history. Split 2026-11-09: P3.13a login (card 50), P3.13b chats (card 51), P3.13c steps and panel (card 52) |

Order (2026-11-09): routing P3.1 → P3.2 → P3.3 → P3.4 → P3.5 → P3.6 → P3.7; autoscaling P3.8 → P3.9a → P3.9b → P3.10; app and UI P3.12a → P3.12b → P3.13a → P3.13b → P3.11 → P3.13c. The tracks are independent until P3.11 and P3.13c. P3.8, P3.12a and P3.13a need no cluster, so they can be written while a simulated replay runs; nothing else runs during a real-pod replay (P2.5). If the week runs over, these slip into Phase 4's week, in this order: P3.13, P3.11, P3.10. None is on Gate 3. P3.12 stays, since P4.8 needs the chat path; the freeze on 22.11 does not move.

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
| P4.8 | App | Agent: routing step + context slicing (D13); tokens counted via vLLM `/tokenize`; the routing step and the tool steps share one prompt prefix so affinity (P3.3) lands on a matching cache; demo traces re-recorded afterwards so replays include the routing step | Tests: out-of-scope message gets a fixed reply with no tool call; history never exceeds the budget; prompt prefix identical across turns until a drop; steps 1..n of a run share the prefix up to the tool list; P4.7 runs through the chat path |

**Gate 4 = feature freeze (Sun 22.11).** Everything works end to end. From here on, only bug fixes.

### Phase 5: Experiments (23–29.11)

| ID | Task | Done when |
|---|---|---|
| P5.1 | E1 routing: round-robin vs default vs upstream's `session-affinity-filter` on `x-run-id` (decided 2026-11-09, card 38) vs plugin + ablation (each rule off) | 3 repetitions, CSV + graphs, config hash saved |
| P5.2 | E2 autoscaling: CPU HPA vs KEDA on queue vs remaining work, on bursty traces; KEDA's two on the simulator at scale and real 1↔2, CPU HPA on real 1↔2 only (D5, decided 2026-11-09) | Completion time, throughput, time to capacity, idle pod-minutes, interrupted runs; simulated p95 figures labelled optimistic and shown next to the real 1↔2 numbers (Gate 2, P2.9) |
| P5.3 | E3 failure: pod killed mid-run | Completion rate and recovery cost |
| P5.4 | Demo rehearsal | Full demo runs twice in a row without intervention |

**Real-pod comparisons (P5.1, and P5.2's real 1↔2):** each one runs in both variant orders and is repeated, with a GPU log beside every run that `make experiment` writes itself, and reports first-token and generation time apart. On 2026-11-08 one baseline run was 20-29% slower for a reason not found, and the variant run second generated faster in all four baseline chains, so one order overstated default's gain, 9.4% against 6.6% over both (`experiments/log.md`, Gate 2).

**Gate 5:** every CSV and graph in the thesis regenerates from `make experiment`.

### Phase 6: Mentor review and final text (30.11–9.12)

- Mon 30.11: full draft to mentor (Checkpoint 3).
- Fixes from mentor feedback, chapter 7, second demo rehearsal.
- UI polish: loading, error and out-of-scope states, consistent layout. Done when the P5.4 rehearsal shows no raw errors or stuck spinners.
- UI cohesion (the user, 2026-11-04: too much going on, not cohesive; left until now on purpose, since P3.11 and P3.13 change these screens). Look again once the chat and panel exist. The list then: one horse per page; a thin, quiet progress bar once a run ends; scenario cards with only a title and a question, no invented file names or tool tags; a shorter New task paragraph, since "fixed data" stopped being true with attach (P2.6a); fewer font switches and look-alike pills. Departures from the design system's previews get recorded under D14.
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
