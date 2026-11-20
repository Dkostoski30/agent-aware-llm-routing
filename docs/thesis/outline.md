# Chapter outline

Working outline. Format, template and length follow FCSE rules (to be confirmed with the mentor). Deadlines from `PLAN.md`, section 6.

## 1. Вовед (by 1.11)
- Problem: agent applications send chains of LLM calls with a shared, growing context; standard routing and CPU-based autoscaling ignore that.
- Who it's for: companies that self-host LLMs for agent applications.
- Goal and contribution in 3–4 points, each with its measured result (`experiments/log.md`):
  - Task affinity: 0 of 243 later steps moved (default 44), 29% fewer uncached prompt tokens while a pod's runs fit its KV cache (P3.3); no gain once the live contexts outgrow the cache (P3.7, P4.9).
  - Cache pressure, the live runs' context over the pods' KV cache, as the point where routing stops helping: first tokens under 1 s up to 1, seconds to tens of seconds past 1.25, the same curve on every picker (P4.9).
  - Autoscaling on remaining work: the same completion as KEDA on the queue with 31–56% fewer idle pod-minutes; not earlier on bursts (P3.9b, P3.10).
  - Safe scale-down and failover: drain 0 interrupted runs against 2 (P4.3); retry 30 of 30 runs against 27 of 30, live agent runs surviving a vLLM and an agent-runtime kill (P4.4). Progress priority and warm-up routing shown as mechanisms (P4.1, P4.2).
- Structure of the thesis.

## 3. Позадина (by 8.11)
- LLM inference: prefill vs decode, KV cache, prefix caching.
- vLLM: PagedAttention, continuous batching, metrics it exposes.
- Kubernetes: pods, services, HPA, KEDA, preStop and graceful termination.
- Gateway API and the Inference Extension: InferencePool, Endpoint Picker, plugins.
- Agents: the loop, tool calls, why one task becomes many calls.

## 2. Поврзана работа (by 15.11)
Sources and what each one is for: `sources.md`, sections 4, 8, 10 and 11.
- Cache-aware routing across replicas:
  - Gateway API Inference Extension and llm-d: the default prefix scorer estimates from the picker's own routing history; `session-affinity-filter`.
  - vLLM production stack (session, prefix-aware and KV-aware routing), AIBrix (prefix-cache and session affinity), Dynamo (from the engine's cache events), Preble (ICLR 2025).
  - LiteLLM: session stickiness, no view of a pod's cache.
- Placing a whole conversation: ConServe (2026), pinned to one decoder and placed by KV occupancy, the closest to task affinity. Llumnix (OSDI 2024) live-migrates requests with their KV state.
- Serving agent programs in one engine: Parrot (OSDI 2024), Autellix (2025), SGLang (NeurIPS 2024), InferCept (ICML 2024).
- KV cache under agent load: CONCUR (admission), MARS, CacheWise, Continuum (TTL on tool calls), KVFlow (NeurIPS 2025, eviction by steps to execution). Working set and thrashing: Denning (1968).
- Autoscaling LLM serving: the production stack's KEDA on the queue, AIBrix's PodAutoscaler on engine metrics.
- LLM serving systems: vLLM / PagedAttention.
- Simulation and replay: Vidur (MLSys 2024), llm-d-inference-sim, XPerf (trace replay).
- What this thesis adds: agent awareness inside a standard Kubernetes gateway with unmodified vLLM; autoscaling on the runs' remaining work; safe scale-down; the pool's cache pressure as the line below which routing helps. Answer ConServe and AIBrix by name.

## 4. Дизајн на системот (16–22.11)
- Architecture diagram: UI → app backend → agent runtime → gateway + plugin → vLLM pods; run tracker → Prometheus → KEDA.
- Chat app as one client of the agent runtime: one message = one run, scenario step, context budget (D13); app data in SQLite, runs in Redis (D7).
- Headers and run lifecycle (D1), run tracker (D2).
- Routing rules: affinity, overload guard, parallel spill, progress priority, warm-up routing.
- Autoscaling: remaining-work signal, scale-up, drain, failover.
- Cache pressure: the second signal from the run tracker, `thesis_context_tokens` (P4.9-P4.12).
- Design decisions D1–D14 and why.

## 5. Имплементација (23–29.11)
- Environment: WSL2, k3s, GPU sharing, versions.
- Plugin in Go, agent runtime and tools, app backend and chat UI, replayer, simulator calibration.
- LoRA fine-tune of the 0.6B for tool calls (P1.7, P2.1b): held-out 33/98 → 167/168, demo go/no-go 4/10 → 10/10.
- Configuration profiles and feature flags, schemas and contract tests.
- Verification approach: task cards, gates, tests.

## 6. Евалуација (23–30.11)
- Setup: hardware, model, workload (recorded traces + AgentProcessBench), arrival patterns, repetitions.
- Simulator calibration and validation (P2.8, P2.9).
- Phase 3-4 results as measured: affinity (P3.3), the burst against default (P3.7, P3.7b), autoscaling baselines (P3.9b, P3.10), priority, warm-up, drain, failover (P4.1-P4.4).
- Cache pressure curve (P4.9), and its limit: pressure and the queue rise together.
- E1 routing + ablation, with upstream's `session-affinity-filter` as a baseline (P5.1) · E2 autoscaling · E3 failure.
- Practical use: demo task success rates (P4.7).
- Threats to validity: one GPU, small model, simulator.

## 7. Заклучок и идна работа (1–8.12)
- What was achieved, with the key numbers.
- Limitations.
- Future work: multi-node, larger models (e.g. agent-tuned models such as Ajax), admission control as the other answer to cache pressure (as CONCUR does on one engine), upstream contribution.

## Литература
Collect references as you go in a `.bib` (LaTeX) or reference list (Word), depending on the FCSE template.
