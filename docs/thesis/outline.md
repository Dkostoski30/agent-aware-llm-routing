# Chapter outline

Working outline. Format, template and length follow FCSE rules (to be confirmed with the mentor). Deadlines from `PLAN.md`, section 6.

## 1. Вовед (by 1.11)
- Problem: agent applications send chains of LLM calls with a shared, growing context; standard routing and CPU-based autoscaling ignore that.
- Who it's for: companies that self-host LLMs for agent applications.
- Goal and contribution in 3–4 points (task affinity, progress priority, autoscaling on remaining work, safe scale-down and failover).
- Structure of the thesis.

## 3. Позадина (by 8.11)
- LLM inference: prefill vs decode, KV cache, prefix caching.
- vLLM: PagedAttention, continuous batching, metrics it exposes.
- Kubernetes: pods, services, HPA, KEDA, preStop and graceful termination.
- Gateway API and the Inference Extension: InferencePool, Endpoint Picker, plugins.
- Agents: the loop, tool calls, why one task becomes many calls.

## 2. Поврзана работа (by 15.11)
- Cache-aware routing: Gateway API Inference Extension, llm-d.
- Serving agent programs: Parrot (OSDI 2024), Autellix (2025).
- LLM serving systems: vLLM / PagedAttention.
- Simulation of LLM inference: Vidur (MLSys 2024), llm-d-inference-sim.
- What this thesis adds: agent awareness inside a standard Kubernetes gateway, with unmodified vLLM, plus autoscaling and safe scale-down.

## 4. Дизајн на системот (16–22.11)
- Architecture diagram: UI → app backend → agent runtime → gateway + plugin → vLLM pods; run tracker → Prometheus → KEDA.
- Chat app as one client of the agent runtime: one message = one run, scenario step, context budget (D13); app data in SQLite, runs in Redis (D7).
- Headers and run lifecycle (D1), run tracker (D2).
- Routing rules: affinity, overload guard, parallel spill, progress priority, warm-up routing.
- Autoscaling: remaining-work signal, scale-up, drain, failover.
- Design decisions D1–D13 and why.

## 5. Имплементација (23–29.11)
- Environment: WSL2, k3s, GPU sharing, versions.
- Plugin in Go, agent runtime and tools, app backend and chat UI, replayer, simulator calibration.
- Configuration profiles and feature flags, schemas and contract tests.
- Verification approach: task cards, gates, tests.

## 6. Евалуација (23–30.11)
- Setup: hardware, model, workload (recorded traces + AgentProcessBench), arrival patterns, repetitions.
- Simulator calibration and validation (P2.8, P2.9).
- E1 routing + ablation · E2 autoscaling · E3 failure.
- Practical use: demo task success rates (P4.7).
- Threats to validity: one GPU, small model, simulator.

## 7. Заклучок и идна работа (1–8.12)
- What was achieved, with the key numbers.
- Limitations.
- Future work: multi-node, larger models (e.g. agent-tuned models such as Ajax), fine-tuning for tool use, upstream contribution.

## Литература
Collect references as you go in a `.bib` (LaTeX) or reference list (Word), depending on the FCSE template.
