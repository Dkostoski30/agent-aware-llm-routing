# Agent-aware LLM routing and autoscaling on Kubernetes

**Bachelor thesis, Faculty of Computer Science and Engineering (FCSE), Skopje**
*Рутирање на LLM повици и автоматско скалирање на vLLM инстанци во Kubernetes околина*

![Kubernetes](https://img.shields.io/badge/Kubernetes-k3s-326CE5?logo=kubernetes&logoColor=white)
![vLLM](https://img.shields.io/badge/serving-vLLM-30A2FF)
![Gateway API](https://img.shields.io/badge/Gateway%20API-Inference%20Extension-0F9D58)
![Go](https://img.shields.io/badge/picker%20plugin-Go-00ADD8?logo=go&logoColor=white)
![Python](https://img.shields.io/badge/agent%20%2B%20app-FastAPI-009688?logo=fastapi&logoColor=white)
![Angular](https://img.shields.io/badge/UI-Angular-DD0031?logo=angular&logoColor=white)
![KEDA](https://img.shields.io/badge/autoscaling-KEDA-1F6FEB)
![Status](https://img.shields.io/badge/status-Phase%204%20of%207-orange)

> An LLM agent doesn't make one request. It makes a **chain** of them, and every call resends the whole conversation so far.
> Today's inference routers match each call's prefix on its own. They don't know which calls belong to one task, how much of the task is left, or whether the live tasks' contexts still fit in the pods' memory. This project gives the cluster that knowledge and measures what it is worth.

---

## Contents

- [The problem in one picture](#the-problem-in-one-picture)
- [What this system does](#what-this-system-does)
- [Architecture](#architecture)
- [How a call is routed](#how-a-call-is-routed)
- [Autoscaling on remaining work](#autoscaling-on-remaining-work)
- [Scale-down and failure](#scale-down-and-failure)
- [The chat app on top](#the-chat-app-on-top)
- [The experiments, as designed](#the-experiments-as-designed)
- [Status](#status)
- [Results so far](#results-so-far)
- [Running it](#running-it)
- [Repository layout](#repository-layout)
- [What it was built and tested on](#what-it-was-built-and-tested-on)

---

## The problem in one picture

An agent task ("summarise this CSV and compare two regions") runs as a loop: the model picks a tool, the tool runs, the result is appended to the context, and the model is called again. Each call's prompt is the previous prompt **plus a little more**.

vLLM keeps the attention state (the **KV cache**) of prompts it has already processed. If the next call of the same task lands on the **same pod**, almost the entire prompt is a cache hit and only the new tail is computed. If it lands anywhere else, the whole prompt is processed again from scratch.

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent task
    participant LB as Load balancer
    participant P1 as vLLM pod 1
    participant P2 as vLLM pod 2

    Note over A,P2: A load balancer that knows nothing about tasks
    A->>LB: step 1 (800 tokens)
    LB->>P1: full prefill, 800 tokens
    A->>LB: step 2 (1,400 tokens)
    LB->>P2: full prefill, 1,400 tokens (cache is on pod 1)
    A->>LB: step 3 (2,100 tokens)
    LB->>P1: prefill 1,300 new tokens (pod 1 only knows step 1)

    Note over A,P2: Task-aware routing (this project)
    A->>LB: step 1 (800 tokens)
    LB->>P1: full prefill, 800 tokens, pin task → pod 1
    A->>LB: step 2 (1,400 tokens)
    LB->>P1: 800 cached, prefill 600
    A->>LB: step 3 (2,100 tokens)
    LB->>P1: 1,400 cached, prefill 700
```

*Illustrative token counts.*

The first half is a balancer with no notion of prefixes, such as round-robin. The Inference Extension's default picker already scores prefix matches and keeps much of this cache (hit rate 0.815 against 0.737 for near round-robin, Gate 2). It still moved 44 of 243 later steps to another pod in P3.3, because it scores each call on its own.

Three more things go wrong at scale:

| Problem | Why standard tools miss it |
|---|---|
| **Autoscaling scales on the wrong thing.** | CPU-based scaling sees a vLLM pod as busy or idle, nothing in between: one busy pod reads ~100% of its CPU request (P3.10). Queue-based scaling counts the calls waiting right now, so it can't tell 20 one-step tasks from 20 ten-step ones. |
| **Contexts outgrow the KV cache.** | When the live tasks' contexts no longer fit in the pods' KV cache, calls lose their cache and wait for memory: first tokens go from under a second to tens of seconds (P4.9). vLLM's KV metric reads the memory in use now, not the context a task between two calls will bring back. |
| **Scale-down and crashes break tasks.** | A pod removed mid-task loses that task's cache and, without care, the in-flight call. A multi-step task is only useful if it finishes. |

---

## What this system does

```mermaid
mindmap
  root((Agent-aware<br/>inference))
    Routing
      Task affinity: a task stays on the pod that has its context cached
      Overload guard: spill one call on a spike, move the task on sustained overload
      Parallel calls: stay together if there is room, spill otherwise
      Priority: tasks close to finishing go first, with aging
    Autoscaling
      Run tracker exports the remaining work of all active tasks
      KEDA scales vLLM pods on that number
      New pods get new tasks first (warm-up)
    Resilience
      Drain: a pod being removed takes no new tasks and finishes its own
      Failover: a dead pod's tasks continue elsewhere from saved history
    Product
      Chat web app: login, chats, file attachments
      Every answer shows its steps, the pod and the cache hit that served them
```

Every LLM call carries four headers so that the infrastructure can see the task it belongs to:

| Header | Meaning | Used by |
|---|---|---|
| `x-run-id` | Which task (run) this call belongs to | Affinity pin, end-of-run signal, logs |
| `x-step` | Which step of the run this is (1, 2, 3 …) | Priority, remaining work |
| `x-expected-steps` | How many steps this kind of task usually takes, learned from recorded runs | Priority, remaining work |
| `x-parallel-group` | Calls sent together in one model reply (e.g. reading three documents at once) | Parallel-call rule |

The headers, the trace format, the run request, the run events and the run calls are **versioned JSON schemas** in [`schemas/`](schemas/), and every component that reads or writes one runs contract tests against it.

---

## Architecture

```mermaid
flowchart LR
    user(["👤 User<br/>browser"])

    subgraph client["Client side"]
        ui["Angular UI<br/>chats · step view · cluster panel"]
        app["App backend<br/>FastAPI + SQLite<br/>users · chats · files"]
    end

    subgraph agentns["Agent layer"]
        agent["Agent runtime<br/>FastAPI · tools · run loop"]
        redis[("Redis<br/>run history")]
        tracker["Run tracker<br/>remaining work"]
        replay["Trace replayer<br/>recorded tasks"]
    end

    subgraph gw["Inference gateway"]
        gateway["agentgateway<br/>Gateway API"]
        epp["Endpoint Picker<br/>llm-d-router +<br/><b>thesis plugin</b>"]
    end

    subgraph pool["InferencePool"]
        v1["vLLM pod 1<br/>KV cache"]
        v2["vLLM pod 2<br/>KV cache"]
        vn["vLLM pod N<br/>KV cache"]
    end

    subgraph obs["Observability and scaling"]
        prom["Prometheus"]
        graf["Grafana"]
        keda["KEDA"]
    end

    user --> ui --> app --> agent
    agent <--> redis
    agent --- tracker
    replay --- tracker
    agent -- "LLM calls + headers" --> gateway
    replay -- "LLM calls + headers" --> gateway
    gateway <-- "which pod?" --> epp
    gateway --> v1 & v2 & vn
    agent -. "POST /runs/{id}/end" .-> epp
    replay -. "POST /runs/{id}/end" .-> epp
    v1 & v2 & vn -. metrics .-> prom
    tracker -. "thesis_remaining_work" .-> prom
    epp -. "queue · KV · pods" .-> v1
    prom --> graf
    prom --> keda
    keda -- "replicas" --> pool
```

| Component | Folder | Role |
|---|---|---|
| **Endpoint Picker plugin** | `gateway/` | Go filter plugin for [llm-d-router](https://github.com/llm-d) (the Gateway API Inference Extension's Endpoint Picker). Holds the `run-id → pod` pins, the overload guard and the parallel-call rule. Each rule has a feature flag. |
| **Agent runtime** | `agent/` | Runs the tool loop. Tools are high-level and deterministic (Python computes, the model only orchestrates); tool arguments are enums wherever possible. LLM calls are non-streaming, so a retry after a pod failure is simple. |
| **Run tracker** | `agent/` (shared with `loadgen/`) | Keeps the state of active runs and exports their remaining work as a Prometheus metric. |
| **App backend** | `app/` | Users, sessions, chats, messages, attachments in SQLite. One chat message = one agent run. |
| **UI** | `ui/` | Angular app: login, chats, step view with pod and cache badges, cluster panel. |
| **Load generator** | `loadgen/` | Records real agent runs into traces, converts public agent traces (AgentProcessBench) into the same format, replays them with realistic gaps and arrival patterns. |
| **Simulator** | `sim/` | [llm-d-inference-sim](https://github.com/llm-d/llm-d-inference-sim) pods calibrated from the real GPU (prefill cost, per-token time, cold start), to run autoscaling at scales one GPU can't hold. |
| **Infra** | `infra/` | Idempotent scripts and Helm values: `make up` builds the whole cluster from zero. |
| **Config** | `config/` | Every tunable in one place, in profiles (`real`, `sim`, `demo`). Nothing is hardcoded. |

---

## How a call is routed

The plugin is a **filter**, not a scorer: a scorer can be outvoted by other scorers, a filter is a hard rule. On the first step of a run, the stock scorers (queue, KV usage, prefix match) pick the pod; from then on, the pin decides, unless the pinned pod is in trouble.

```mermaid
flowchart TD
    req["LLM call arrives<br/>x-run-id · x-step · x-parallel-group"] --> pinned{"Run already<br/>pinned to a pod?"}

    pinned -- no --> first["First step: stock scorers<br/>queue + KV usage + prefix match"]
    first --> pin["Pin run-id → chosen pod<br/>(TTL as a safety net)"]

    pinned -- yes --> alive{"Pinned pod still<br/>in the pool?"}
    alive -- no --> remapPod["Remap the run<br/>to a healthy pod"]

    alive -- yes --> over{"Pinned pod's queue<br/>over the threshold?"}
    over -- no --> stay["✅ Stay on the pinned pod<br/>(cache hit)"]
    over -- "yes, short spike" --> spill["Spill this one call<br/>pin unchanged"]
    over -- "yes, sustained<br/>(hysteresis)" --> remapLoad["Remap the run<br/>to a pod under the threshold"]

    stay --> prio["Priority = step / expected-steps<br/>+ aging, enforced by vLLM's scheduler"]
    spill --> prio
    remapPod --> prio
    remapLoad --> prio
    pin --> prio

    endrun(["Run ends → POST /runs/{id}/end<br/>pin removed"]) -.-> pinned
```

**Why each rule exists:**

| Rule | Without it | With it |
|---|---|---|
| **Task affinity** | A task's calls scatter, each move re-processes the whole prompt | Steps 2…n stay on the pod that has their cache, and hit it while that pod's runs fit in its KV cache |
| **Overload guard** | A pinned task waits behind a long queue rather than move | A spike costs one spilled call; only sustained overload moves the task (and its cache) |
| **Parallel calls** | Calls fired together in one reply land wherever | They share the pod while it has room, spill only under overload |
| **Progress priority** | A task one step from done waits behind a brand-new one | A run's last steps go ahead of new runs' first steps; aging bounds how long any call can be overtaken |
| **Warm-up routing** | A fresh pod idles while warm pods queue | New runs go to a pod right after it becomes Ready |

What each rule was shown to do, and where it was not, is in [Results so far](#results-so-far).

---

## Autoscaling on remaining work

Each active run knows its current step and how many steps runs of its kind usually take. The run tracker sums what is left:

$$\text{remaining work} = \sum_{\text{active runs}} \max(\text{expected steps} - \text{step},\ 1)$$

That number is exported as a Prometheus metric and **KEDA** scales the vLLM Deployment on it. It counts the work the active tasks still have, not only the calls waiting right now, so it sizes the pool to the work.

```mermaid
flowchart LR
    subgraph t0["t = 0 s"]
        b["Burst: 5 agent tasks<br/>start at once"]
    end
    subgraph signals["What each autoscaler sees"]
        cpu["CPU HPA<br/>one busy pod reads ~100%<br/>(busy or idle, nothing between)"]
        queue["KEDA on queue depth<br/>the calls waiting now"]
        rw["KEDA on remaining work<br/>the steps the active<br/>tasks have left"]
    end
    subgraph outcome["Measured (P3.10)"]
        cpuR["Scales to the maximum<br/>whenever anything runs"]
        queueR["Same completion,<br/>up to 8 pods"]
        rwR["Same completion,<br/>4-7 pods, 31-56% fewer<br/>idle pod-minutes"]
    end
    b --> cpu --> cpuR
    b --> queue --> queueR
    b --> rw --> rwR
```

Cold start is real: a new vLLM pod needs minutes to load the model, and the simulator models it (140 s, calibrated from the real GPU). On a burst both KEDA signals scaled in the same scrape, because a burst's tasks start and queue at once, so cold start decided the time to capacity; on fan-out, remaining work scaled 30 s earlier. Where remaining work differs is how many pods it pays for (P3.10).

### Cache pressure (Phase 4)

A second signal, from the same run tracker:

$$\text{cache pressure} = \frac{\sum_{\text{live runs}} \text{prompt tokens of the run's latest call}}{\text{pods} \times \text{KV-cache tokens per pod}}$$

A run counts from its first call to its last, also while it waits on a tool and vLLM holds no request of it. P4.9 measured the curve on every real-pod replay (below); P4.10 exports the numerator as `thesis_context_tokens`; P4.11 shows pressure in the cluster panel; P4.12 has KEDA scale real pods on it, with a target that is a figure of the hardware: one pod's KV cache.

---

## Scale-down and failure

```mermaid
stateDiagram-v2
    [*] --> Starting: KEDA scales up
    Starting --> Ready: model loaded
    Ready --> Ready: serves pinned runs<br/>new runs prefer the newest pod
    Ready --> Draining: KEDA scales down<br/>(preStop hook)
    Draining --> Draining: no new runs<br/>pinned runs finish
    Draining --> [*]: last run ends<br/>or drain timeout
    Ready --> Failed: pod crashes or is killed
    Failed --> [*]: picker drops it from the pool

    note right of Failed
        Its runs are remapped to another pod.
        The agent runtime retries the call
        and continues from the run history in Redis.
    end note
```

| Event | Goal | Measured as |
|---|---|---|
| Scale-down | **0 interrupted runs** | Runs that lost a call while their pod drained |
| Pod killed mid-run | Runs continue, nothing lost | Completion rate and the extra time recovery costs |

---

## The chat app on top

The infrastructure has a real client: a chat web app where one message becomes one agent run. The model first picks one of three scenarios (or out of scope), then sees only that scenario's tools, so a small model never chooses among all nine tools at once.

| Scenario | Example question | Tools |
|---|---|---|
| **CSV analysis** | "Which region had the highest average sales in Q3?" | `csv_summary`, `group_by`, `compare`, `calculator` |
| **Contracts** | "What changed in the termination clause between these two versions?" | `read_document`, `extract_clauses`, `diff_clauses` (documents read in parallel) |
| **Customer support** | "Order 1042 arrived damaged, what can the customer do?" | `get_order`, `get_policy`, `draft_reply` |
| Out of scope | "Write me a poem" | None: a fixed polite reply |

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Angular UI
    participant App as App backend
    participant Ag as Agent runtime
    participant GW as Gateway + picker
    participant V as vLLM pod

    U->>UI: question + attached CSV
    UI->>App: POST message
    App->>Ag: run request (files by ID)
    loop each step
        Ag->>GW: LLM call + x-run-id, x-step, ...
        GW->>V: pinned pod
        V-->>Ag: tool call
        Ag->>Ag: run the tool (deterministic Python)
        Ag-->>UI: step event (SSE): tool, pod, cache %, latency
    end
    Ag->>GW: POST /runs/{id}/end
    Ag-->>App: final answer + run_id
    App-->>UI: answer, steps expandable underneath
```

Files never enter the model's context: tools take them by ID and return compact results. History is capped by a token budget and dropped in blocks, so the prompt prefix stays stable across turns and keeps hitting the cache.

---

## The experiments, as designed

This section describes the experiments **as they are meant to run on hardware built for the job**. What they actually ran on, and how the design was scaled down to fit, is in [What it was built and tested on](#what-it-was-built-and-tested-on).

### Ideal setup

```mermaid
flowchart TB
    subgraph cluster["Kubernetes cluster"]
        subgraph gpu["GPU node pool: one vLLM pod per GPU, scales 1 → 8"]
            g1["GPU 1<br/>vLLM"]
            g2["GPU 2<br/>vLLM"]
            g3["GPU 3<br/>vLLM"]
            gdots["…"]
            g8["GPU 8<br/>vLLM"]
        end
        subgraph cpu["CPU node pool"]
            gw2["Gateway + Endpoint Picker"]
            ag2["Agent runtimes · run tracker"]
            mon["Prometheus · KEDA · Grafana"]
            rp["Trace replayer"]
        end
    end
    rp --> gw2 --> gpu
    ag2 --> gw2
    mon -. scales .-> gpu
```

| | Ideal |
|---|---|
| **GPUs** | Up to 8 data-center GPUs (e.g. 80 GB class), one vLLM replica per GPU, no sharing |
| **Model** | A mid-size instruction model with reliable tool calling, long context (32k+), a KV cache that holds many concurrent agent contexts |
| **Scale** | Real pods all the way from 1 to N, so cold start, drain and failover are measured on real hardware at every size |
| **Workload** | Recorded agent traces (own demo tasks + converted AgentProcessBench tasks), replayed with their real tool-time gaps, in steady, bursty and fan-out arrival patterns |
| **Isolation** | Dedicated nodes, nothing else running, identical GPU clocks across runs |

### The three experiments

```mermaid
flowchart LR
    trace[("Recorded agent<br/>traces")] --> rep["Replayer<br/>fixed seed · real gaps<br/>steady / burst / fan-out"]
    rep --> E1 & E2 & E3

    subgraph E1["E1 · Routing"]
        direction TB
        e1a["round-robin"]
        e1b["default Endpoint Picker"]
        e1c["upstream session affinity"]
        e1d["thesis plugin"]
        e1e["thesis plugin,<br/>each rule switched off"]
    end
    subgraph E2["E2 · Autoscaling"]
        direction TB
        e2a["HPA on CPU"]
        e2b["KEDA on queue depth"]
        e2c["KEDA on remaining work"]
    end
    subgraph E3["E3 · Failure"]
        direction TB
        e3a["pod killed mid-run"]
        e3b["scale-down during load"]
    end

    E1 & E2 & E3 --> csv[("CSV + graphs<br/>stamped with the<br/>experiment's hash")]
```

| Experiment | Question | Variants | Metrics |
|---|---|---|---|
| **E1 Routing** | Does keeping a task on its pod make agent tasks faster, and what does each rule contribute? | Round-robin · default picker · upstream session affinity · thesis plugin · ablation (each rule off in turn) | Run completion time (p50, p95), time to first token, prefix-cache hit rate, uncached prompt tokens, pod switches per run |
| **E2 Autoscaling** | Does scaling on remaining work add capacity before users wait, without wasting GPUs? | HPA on CPU · KEDA on queue depth · KEDA on remaining work, on bursty traces | Completion time, throughput, time to capacity, idle pod-minutes, interrupted runs |
| **E3 Failure** | Do tasks survive a pod dying or being scaled away? | Pod killed mid-run · scale-down during a replay | Completion rate, interrupted runs, recovery cost in time |

### Method

- **Every experiment is a YAML file** in [`experiments/`](experiments/); results are stamped with the file's hash, so any number traces back to the exact configuration that produced it. `make experiment NAME=…` regenerates its CSV and graphs from scratch.
- **3 repetitions**, fixed seeds, warm-up runs discarded.
- **Both variant orders**, with a GPU log beside every run: the variant that runs second must not get an advantage from a warmer machine.
- **Feature flags per routing rule**, so ablation is a config change, not a code change.

### Hypotheses (what the design expects, not results)

| | Expectation |
|---|---|
| H1 | Task affinity raises the prefix-cache hit rate and cuts uncached prompt tokens on later steps, most for long tasks. |
| H2 | The overload guard keeps p95 latency under bursts no worse than the default picker, while keeping most of affinity's cache gain. |
| H3 | Scaling on remaining work reaches the needed capacity sooner than queue depth, and CPU-based HPA barely reacts. |
| H4 | Drain gives 0 interrupted runs on scale-down; failover keeps runs completing when a pod dies. |

---

## Status

Planned in 7 phases, each closed by a gate check and a git tag. Phases 0–3 are on `master`; Phase 4 is on `feature/phase-4`.

```mermaid
gantt
    dateFormat YYYY-MM-DD
    axisFormat %d.%m
    title Thesis timeline (2026)
    section Build
    Phase 0 · Environment (GPU in Kubernetes)    :done,   p0, 2026-10-21, 2026-10-25
    Phase 1 · Walking skeleton + fine-tuning     :done,   p1, 2026-10-26, 2026-11-01
    Phase 2 · Workload + simulator + app backend :done,   p2, 2026-11-02, 2026-11-09
    Phase 3 · Affinity, overload, remaining work :done,   p3, 2026-11-09, 2026-11-15
    Phase 4 · Priority, drain, failover          :active, p4, 2026-11-16, 2026-11-22
    section Evaluate
    Phase 5 · Experiments E1–E3                  :        p5, 2026-11-23, 2026-11-29
    section Write
    Phase 6 · Review + final text                :        p6, 2026-11-30, 2026-12-09
    Phase 7 · Defense                            :        p7, 2026-12-10, 2026-12-18
```

| Phase | | What it delivers |
|---|---|---|
| **0 · Environment** | ✅ Gate 0 | GPU inside Kubernetes under WSL2, two vLLM pods sharing one GPU, prefix caching visible |
| **1 · Walking skeleton** | ✅ Gate 1 | `make up` from zero, Prometheus + Grafana, gateway + Endpoint Picker, agent runtime with Redis history, Angular step view, `make smoke` end to end; LoRA fine-tune of the small model for tool calling |
| **2 · Workload + simulator** | ✅ Gate 2 | All three demo scenarios, call recorder, trace format + replayer, AgentProcessBench converter, first baseline, per-step badges in the UI, calibrated and validated simulator, app backend on SQLite |
| **3 · Affinity, overload, remaining work** | ✅ Gate 3 | Picker built from source, plugin with one flag per rule, task affinity, pod removed → runs remapped, overload guard, parallel calls, burst replay, run tracker, KEDA on remaining work, CPU and queue baselines, cluster panel, chats API, chat UI. Green with stated limits: P3.3 and P3.7 closed red with their results (below) |
| **4 · Priority, drain, failover** | 🔄 in progress | ✅ P4.1 progress priority · ✅ P4.2 warm-up routing · ✅ P4.3 drain · ✅ P4.4 failover · ✅ P4.9 cache pressure measured · 🔄 P4.10 context-tokens gauge · ⏳ real 1↔2 validation · demo buttons · scenario suite · chat routing step · pressure in the panel · KEDA on context tokens. **Feature freeze 22.11.2026** |
| **5 · Experiments** | ⏳ | E1, E2, E3 with repetitions; demo rehearsal |
| **6–7 · Text and defense** | ⏳ | Thesis text, mentor review, defense by 18.12.2026 |

---

## Results so far

All numbers below are measured on the laptop described at the end, with the scaled-down setup. Each comes from a hash-stamped experiment file and is logged in [`experiments/log.md`](experiments/log.md), green or red (Phase 4 entries are on the `feature/phase-4` branch until it is merged).

### A small model learned to call tools (Phase 1–2)

A 0.6B model picks the right tool but gets arguments wrong. A LoRA adapter trained on tool-call trajectories generated from the deterministic tools fixed that:

| | Plain Qwen3-0.6B-GPTQ-Int8 | + LoRA adapter |
|---|---|---|
| Held-out questions | 33 / 98 | **167 / 168** (after retraining on all three scenarios, both tool-choice modes) |
| Demo go/no-go (10 real runs) | 4 / 10 | **10 / 10** |

### Baseline: round-robin vs the default picker (Gate 2)

35 replayed agent runs on 2 real pods, 309 calls per variant, 0 failed in both.

![Baseline: default picker vs near round-robin](experiments/graphs/baseline-reversed-694f06470e98.png)

- The default picker's prefix scorer keeps more of each prompt cached: **0.815 vs 0.737** hit rate.
- It finished the runs **6.6% faster** in total over both variant orders (and only 2.9% in one order alone), which is why every real-pod comparison now runs in both orders.
- Calls over a pod's KV-cache limit are where the time goes: their first token takes seconds, not milliseconds.

### The simulator matches real pods on medians (Gate 2)

The same replay on 1 and 2 real pods, then on 1 and 2 calibrated simulated pods.

![Simulator validation: real vs simulated pods](experiments/graphs/validate-sim-5593507542a4.png)

| | 1 pod | 2 pods |
|---|---|---|
| Call duration p50, error | 14% | 1% |
| Run completion p50, error | 2.5% | 1% |
| p95 tails | 18–91% optimistic | 18–63% optimistic |

Medians are within 15%. Tails are optimistic: on a real pod, calls that don't fit the KV cache wait for free blocks, which the simulator doesn't model. **Simulated p95 numbers are therefore always labelled optimistic and shown next to real-pod numbers.**

### Task affinity (Phase 3, P3.3)

35 runs on 2 real pods, default picker vs the plugin's affinity rule.

| | Default picker | Task affinity |
|---|---|---|
| Later steps moved to another pod | 44 of 243 | **0 of 243** |
| Prompt tokens left uncached (calls under the KV limit) | 91,505 | **64,698 (−29%)** |
| Overall prefix-cache hit rate | 0.821 | 0.807 |

The pins hold, and where the default picker moved a run, affinity kept its cache: higher hit rate in 12 of those 15 runs. The overall rate did **not** go up: two long runs were pinned to the same pod at once, together over its KV capacity, and evicted each other, while the two pods together had room (this steady load passed a pool pressure of 1 in 11 of 2,908 calls, P4.9). Affinity pins without checking that a pod's runs still fit.

### A pod disappears (Phase 3, P3.4)

On simulated pods, the pod holding the most live runs was deleted mid-replay.

| | Calls | Failed |
|---|---|---|
| Finished before the delete | 23 | 0 |
| Running on the pod at the delete (drained) | 2 | 0 |
| Sent after the pod left the pool | 126 | **0** |

Both affected runs were remapped once to the surviving pod and stayed there; 20 of 20 runs sent their end-of-run signal to the picker.

### Overload guard and parallel calls (Phase 3, P3.5–P3.6)

On simulated pods, under bursts:

| Rule | Calls moved | Check |
|---|---|---|
| Overload guard | 12 spills on the burst, 0 remaps on short spikes | Every spill over the threshold; a remap on sustained overload shown by the unit tests only |
| Parallel calls | 28 spills on burst-fan-out, 1 on steady-fan-out | Every spill over the threshold; each run's next call back on its pin |

### The plugin under a burst on real pods (Phase 3, P3.7)

4 replays of 357 calls on 2 real pods, both variant orders, 0 failed.

| p95 | Plugin | Default picker |
|---|---|---|
| First token | 47.88 s | 49.54 s |
| Run completion | 426.48 s | 405.77 s (+5.1%, against a 5% tolerance) |

- Closed red on run completion, but the difference is smaller than the laptop's swing between files (11–21%), so it shows no difference either way.
- Two first-call rules tried after it (P3.7b) were not faster in both orders either: least work −7.5% / +4.0% total run time by order, least context −14.1% / +24.4%.
- Why: 86–89% of these calls started with more live context than the two pods' KV cache holds (next section). There, no placement can keep every run's cache.

### Cache pressure (Phase 4, P4.9)

Every later-step call of the real-pod replays, by the cache pressure at its start: 7,412 calls from 28 variants in 14 files, every real-pod variant with a fixed pod count (KV cache 4,384 tokens per pod).

![Cache pressure: first-token time and cached share by pressure](experiments/graphs/pressure-23bc873caac9.png)

| Pressure | Calls | First token p50 | p90 | Cached share |
|---|---|---|---|---|
| under 0.75 | 3,174 | 0.4 s | 0.7 s | 0.86 |
| 0.75–1 | 579 | 0.6 s | 4.9 s | 0.75 |
| 1–1.25 | 289 | 1.7 s | 15.8 s | 0.74 |
| 1.25–1.5 | 317 | 4.9 s | 22.6 s | 0.57 |
| 1.5–2 | 529 | 7.1 s | 27.9 s | 0.45 |
| 2–3 | 1,870 | 13.2 s | 30.5 s | 0.35 |
| 3 and over | 654 | 23.1 s | 41.7 s | 0.34 |

- The median stays under a second up to 1 and bends between 1 and 1.5; the p90 bends earlier, at 0.75–1.
- The default picker and the plugin give the same curve; on the burst files 89% and 86% of their calls were over 1.
- **Limit:** pressure and the queue rise together, and medians in one bucket spread widely between files, so this is the pooled shape, not a law per replay, and it does not separate the KV cache from the queue.

### Autoscaling: remaining work against the queue and CPU (Phase 3, P3.9b–P3.10)

Simulated pods (cold start 140 s), one seed each:

| Replay | Signal | Pods asked for | Time to capacity | Run time p50 / p95 | Idle pod-minutes |
|---|---|---|---|---|---|
| Burst | Remaining work | 4, then 5 | 145 s | 40.0 / 144.7 s | 9.1 |
| Burst | Queue | 5, then 8 | 140 s | 39.0 / 145.6 s | 14.9 |
| Fan-out | Remaining work | 2, 4, then 6 | 140 s | 57.1 / 186.0 s | 9.7 |
| Fan-out | Queue | 3, 7, then 8 | 140 s | 49.2 / 180.5 s | 19.0 |

- Completion is the same within noise. Remaining work asked for fewer pods and cost **31–56% fewer idle pod-minutes** in all four pairs (these and a first run of each).
- On the burst both scaled in the same scrape: remaining work did **not** scale before the queue built (P3.9b). It counts tasks that have started, and a burst's tasks start and queue at once. On fan-out it scaled 30 s earlier.
- CPU HPA on real 1↔2: one busy pod reads ~100% of its CPU request, so it scaled to 2 as soon as anything ran and would not scale down while anything ran.

### Priority, warm-up, drain and failover (Phase 4, P4.1–P4.4)

| Rule | Check | On | Off |
|---|---|---|---|
| Progress priority | 1 real pod: a run's last step sent behind 6 waiting first steps | Went ahead of all 6, first byte after 31.8 s | Behind all 6, 81.7 s |
| Warm-up routing | Sim burst with KEDA: first calls while a pod was new | 14 of 14 to a new pod | 8 of 14 |
| Drain | Real 2 → 1 pods during a replay | 0 interrupted runs | 2 |
| Failover (retry) | Sim burst, the pod with the most live runs force-deleted mid-replay | 30 of 30 runs completed | 27 of 30 |

- Each check shows the mechanism, on one seed. The effect on run times is for Phase 5: warm-up's one run had a worse run-time p50 (56.6 s against 41.8 s), each burst going whole to one new pod.
- Failover on live agent runs on real pods too: with a vLLM pod killed under 5 runs, and with the agent runtime killed under 5 more, all 10 runs finished with correct answers.

---

## Running it

> Needs an NVIDIA GPU. The scripts target Windows 11 + WSL2 (Ubuntu 24.04), the machine this was built on; a native Ubuntu 24.04 host with the NVIDIA driver should only need the WSL-specific steps skipped. Every tool version is pinned in [`versions.md`](versions.md).

```bash
infra/bootstrap.sh      # install the pinned dev tools
make up                 # build the cluster from zero: k3s, GPU, vLLM, gateway, monitoring, agent, app
make smoke              # one task through every layer, exits 0 on success
```

| Command | What it does |
|---|---|
| `make up PROFILE=sim` | The cluster on simulated vLLM pods (no model, agent or app; for replays) |
| `make down` | Tear the cluster down (app users and chats are kept) |
| `make test` | All unit tests, Go and Python |
| `make lint` | All pre-commit hooks |
| `make ui PROFILE=…` | Install and serve the Angular app for a profile |
| `make grafana` | Port-forward Grafana to `localhost:3000`: queues, KV usage, cache hit rate per pod |
| `make record` | Record real agent runs into a trace |
| `make replay TRACE=… PATTERN=…` | Replay recorded tasks (steady, burst, steady-fan-out) |
| `make experiment NAME=…` | Run an experiment file and regenerate its CSV and graph |
| `make experiment-compare CSVS=…` | Compare variants run by run |
| `make calibrate` | Measure real pods to calibrate the simulator |
| `make reset-app` | Wipe app users, chats and files |

Switching between the real GPU, the simulator and the demo model is one profile in [`config/`](config/): `real`, `sim`, `demo`.

---

## Repository layout

```
config/        profiles (real, sim, demo): every tunable, nothing hardcoded
schemas/       versioned JSON schemas: headers, trace, run request, run events, run calls
infra/         cluster setup scripts, Helm charts and values, manifests
gateway/       Go Endpoint Picker plugin
agent/         Python agent runtime, tools, run tracker
app/           Python app backend + SQLite: users, sessions, chats, attachments
loadgen/       trace recorder, replayer, converters, experiment runner, graphs
sim/           simulator calibration tables and validation
training/      LoRA fine-tuning of the small model (its own uv project)
ui/            Angular app: login, chats, step view, cluster panel
experiments/   experiment files, results (CSV, JSONL), graphs, log.md
docs/          design system, task cards, thesis outline
```

The design decisions (D1–D14), every task with its done-when check, and the fallback plan are in [`PLAN.md`](PLAN.md).

---

## What it was built and tested on

Everything above runs on **one gaming laptop**. The design was scaled down to fit it, and every shortcut is a stated limitation rather than a hidden one.

| | Value |
|---|---|
| Laptop | ASUS ROG Strix G15 (2021) |
| CPU | AMD Ryzen 7 |
| GPU | NVIDIA RTX 3050 Laptop, **4 GB** |
| RAM | 16 GB (10 GB given to WSL2, plus 8 GB swap) |
| OS | Windows 11 Pro + WSL2, Ubuntu 24.04.5 LTS |
| Kubernetes | k3s v1.36.5, single node, NVIDIA device plugin with time-slicing (2 GPU slots) |
| Serving | vLLM v0.31.0, two pods sharing the one GPU |
| Gateway | Gateway API v1.6.3, Inference Extension v1.6.2, llm-d-router v0.11.0 |
| Simulator | llm-d-inference-sim v1.0.0 |
| Monitoring | kube-prometheus-stack 92.3.0 (Prometheus v3.15.0, Grafana 13.2.3) |
| Cost | €0 |

**How the ideal design maps onto it:**

| Ideal | On the laptop | Consequence |
|---|---|---|
| One GPU per vLLM pod | Two pods share one 4 GB GPU | Real-pod scaling only 1 ↔ 2; pods slow each other down by an amount that varied between nights |
| Mid-size model, long context | **Qwen3-0.6B-GPTQ-Int8** + LoRA adapter for experiments; **Qwen3-1.7B-GPTQ-Int8** (one replica) for the practical demo; Qwen3-4B did not fit | Max context 4,096 tokens, KV cache ~4,400 tokens per pod, so long agent tasks hit the cache limit often |
| Real pods from 1 to N | Simulated pods calibrated from the real GPU (prefill cost, per-token time, cold start), validated against real pods at 1 and 2 | Autoscaling at scale runs on the simulator; its medians match real pods within 15%, its p95 tails are optimistic |
| Dedicated, isolated nodes | A laptop under WSL2 | Speed drifted between nights by about the validation tolerance; laptop use during a run moved results by ~15%. Hence: both variant orders, GPU logs, sleep disabled, the laptop left alone during runs |

---

<sub>Built with a strict workflow: one task per commit, tests first for logic, a done-when check run by hand for every task, and a fresh-eyes review of every diff. The full record, including the failed runs, is in [`experiments/log.md`](experiments/log.md).</sub>
