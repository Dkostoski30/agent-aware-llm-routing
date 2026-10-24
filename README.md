# LLM call routing and vLLM autoscaling on Kubernetes

Bachelor thesis, FCSE. **Рутирање на LLM повици и автоматско скалирање на vLLM инстанци во Kubernetes околина.**

Agent tasks make chains of LLM calls that share one context. This system routes every call of a task to the vLLM pod that already has the context cached, prioritizes tasks close to finishing, and scales vLLM pods on the remaining work of active tasks. Scale-down drains pods first; a failed pod doesn't lose tasks.

## Where to start

| File | What it is |
|---|---|
| `PLAN.md` | Phases, atomic tasks, design decisions D1–D12, key dates |
| `CLAUDE.md` | Context and rules Claude Code reads every session |
| `docs/task-cards/` | Ready task cards; paste one into Claude Code per task |
| `docs/TASK_CARD_TEMPLATE.md` | Template for writing new cards |
| `docs/REVIEW_PROMPT.md` | Prompt for the fresh-eyes review before every commit |
| `versions.md` | Every pinned version, machine setup, measurements |
| `experiments/log.md` | Done-when outputs and experiment notes, newest first |
| `docs/thesis/` | Confirmed topic and chapter outline |

## Requirements

Windows + WSL2 (Ubuntu 24.04), NVIDIA GPU with a current Windows driver. Everything else is installed by `infra/bootstrap.sh` (created in P0.0).

## Commands

```bash
make up          # build the cluster from zero
make down        # tear it down
make test        # all unit tests
make smoke       # end-to-end check through every layer
make replay TRACE=... PATTERN=...
make experiment NAME=...
```

## Layout

```
config/        profiles: real, sim, demo
schemas/       versioned JSON schemas: headers, trace
infra/         setup scripts, Helm values, manifests
gateway/       Go Endpoint Picker plugin
agent/         Python agent runtime, tools, run tracker
loadgen/       trace recorder, replayer, converters
sim/           simulator config and calibration
ui/            Angular demo app
experiments/   experiment configs, results, graphs, log
docs/          task cards, thesis outline
```
