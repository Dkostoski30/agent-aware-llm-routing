# CLAUDE.md

## Project
Bachelor thesis (FCSE): LLM call routing and vLLM autoscaling on Kubernetes.
Agent tasks make chains of LLM calls sharing one context. A custom Endpoint Picker plugin (Gateway API Inference Extension) keeps a task on the pod that has its context cached, prioritizes tasks close to finishing, spills parallel calls only under overload. A shared run tracker (agent runtime + replayer) exports the remaining work of active tasks, and KEDA scales vLLM pods on it. Scale-down drains; failed pods don't lose tasks.

The full plan with task IDs is in `PLAN.md`. Every piece of work maps to one task ID. Design decisions D1–D12 in `PLAN.md` are binding; don't deviate without asking.

## Environment
- Windows + WSL2, RTX 3050 4GB, 16GB RAM. All commands run inside WSL2.
- Model: Qwen3-0.6B-GPTQ-Int8 for experiments and the infrastructure demo; Qwen3-1.7B-GPTQ-Int8 as a single replica for the practical demo (the 4B didn't fit, P0.3). Thinking mode off.
- Agent tools are high-level and deterministic: Python computes, the model only orchestrates. Tool arguments are enums where possible; tool calls use vLLM structured outputs (D10).
- Every LLM call carries headers `x-run-id`, `x-step`, `x-expected-steps`, `x-parallel-group`. Agent LLM calls are non-streaming. Run history lives in Redis.
- Autoscaling experiments run on simulated pods (llm-d-inference-sim) calibrated from the real GPU, including cold start; validated on real vLLM at 1↔2 pods.
- Pinned versions live in `versions.md`. Never upgrade anything without being asked.
- Every tunable lives in a `config/` profile (`real`, `sim`, `demo`); nothing is hardcoded. Each routing rule has a feature flag. Experiments are YAML files in `experiments/` (D11).
- Infra wait times (install and readiness timeouts in `infra/` scripts) are not experiment tunables: they stay in the script as a named constant with a comment saying why, not in `config/`.
- Headers and the trace format follow the versioned schemas in `schemas/`; contract tests must pass in every component that reads or writes them. Metrics start with `thesis_`; logs are JSON with `run-id` on every line (D12).

## Layout
- `config/` profiles: real, sim, demo
- `schemas/` versioned JSON schemas for headers and traces
- `infra/` cluster setup, Helm values, manifests
- `gateway/` Go scheduling plugin
- `agent/` Python FastAPI agent runtime and tools
- `loadgen/` trace recorder, replayer, converters
- `sim/` simulator config and calibration
- `ui/` Angular app
- `experiments/` scripts, configs, results

## Commands
- `make up` / `make down`: build or tear down the whole cluster
- `make test`: all unit tests (Go + Python)
- `make smoke`: end-to-end check through every layer
- `make replay TRACE=... PATTERN=...`: replay recorded tasks
- `make experiment NAME=...`: run an experiment and regenerate its CSV and graphs

## Rules
1. Work on exactly one task ID at a time. Touch only the files the task needs. If something outside the task looks broken, report it, don't fix it.
2. Plan first. Wait for approval before writing code.
3. Logic gets tests first. Tests assert behavior.
4. Never edit, skip or weaken a test to make it pass. Report the failure instead.
5. No new dependencies without asking. Pin every version.
6. No dead code, no TODOs, no commented-out code, no broad exception handling, no unexplained constants. Thresholds go in config with a comment saying why.
7. Never claim something works. Say which command proves it and let the user run it.
8. If an approach fails twice, stop and explain why instead of trying a third variation.
9. Everything must be reproducible from the repo. No manual steps that aren't in a script.
10. Keep code explainable: the user must be able to defend every line.
