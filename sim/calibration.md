# Calibration: from the table to the simulator (P2.8b)

`make calibrate` (P2.8a) measured real vLLM pods of the 0.6B, with the adapter (`lora`, profile `real`) and without it
(`plain`, profile `real-plain`), on one pod alone and on both pods at once, all under experiment hash 5553d79b34d7:

| File | What it holds |
|---|---|
| `calibration-5553d79b34d7-{lora,plain}-{1pod,2pods}.csv` | One row per pod and cell: concurrency 1, 2, 4, 8 × prompt 256, 1,024, 2,048, 3,584 tokens × cache mode uncached, full, partial; 20 calls of 64 tokens each; TTFT and per-token p50 and p95, cached_tokens p50 |
| `cold-start-5553d79b34d7-{lora,plain}.csv` | One row per pod started: created → Ready, Ready → first answer, the first call's TTFT, warm-up calls |
| `*.meta.json` | The profile's sha256, the clock's drift over the file, the warm-up TTFTs |

P2.8c measured `calibration-5553d79b34d7-lora-1pod.csv` again on 2026-11-07, after P2.9's first run missed at 1 pod;
the first table stays in git (638d336). The results and their oddities are in `experiments/log.md` (2026-11-06 · P2.8a,
2026-11-07 · P2.8c). `config/sim.yaml`'s simulator section comes from `calibration-5553d79b34d7-lora-1pod.csv` and
`cold-start-5553d79b34d7-lora.csv` through `calibrator/mapping.py`; `tests/test_calibration_mapping.py` fails if the
profile and the table differ.

## The simulator's model

llm-d-inference-sim v1.0.0, `--latency-calculator=per-token` (`pkg/simulator/latencies.go`):

```
TTFT        = (prefill_overhead + uncached prompt tokens × prefill_time_per_token) × f
token gap   = inter_token_latency × f
f           = 1 + (time_factor_under_load − 1) × (running − 1) / (max_num_seqs − 1)
```

One factor `f` scales TTFT and the token gaps alike. A pod runs at most `max_num_seqs` requests; the rest wait in
its queue. A pod is Ready `startup_duration` after its container starts.

## Which measurements (decided 2026-11-06)

- **The adapter's pods, not the plain model's.** Real serves the adapter, so P2.9 compares a simulated pod with what
  real runs. P2.8a's lora 1-pod file had an unexplained slow stretch; P2.8c measured it again (below).
- **One pod alone, not both at once.** A simulated pod stands for a pod with its own GPU, as in a real cluster. Two
  real pods share the laptop's one GPU: with both busy the adapter's per-token time doubles (150 → 311 ms) and the
  plain model's rises ~30% (62 → 81 ms). That is the laptop's limitation, and it shows in the 1↔2 real validation (D5).

## The mapping

| Value | From | Table gives | Profile |
|---|---|---|---|
| `prefill_overhead` | Intercept of the least-squares line through the TTFT p50 of the 12 concurrency-1 cells against their uncached tokens (prompt − cached_tokens p50), all cache modes together | 298.5 ms | `298ms` |
| `prefill_time_per_token` | Slope of the same line | 0.1461 ms | `146us` |
| `inter_token_latency` | Median per-token p50 of the same 12 cells | 154.2 ms | `154ms` |
| `time_factor_under_load` | Median per-token p50 at concurrency `max_num_seqs` over that at 1, never under 1 | 152.9 / 154.2 = 0.99 | `1.0` |
| `max_num_seqs` | Not from the table: real vLLM's 4,384 KV tokens over the 2,195-token median prompt of the converted trace P5 replays (P2.4) | | `2` |
| `kv_cache_blocks` | Not from the table: `max_num_seqs` × 256 blocks, so running prompts always fit (P2.7) | | `512` |
| `startup_duration` | Median created → Ready of the 5 pods started alone, plus their median first TTFT less a warm pod's TTFT (the line at the 1,024-token warm-up prompt, 448 ms) | 126 + 14.3 − 0.45 s | `140s` |

Concurrency 1 for the line and the token gap, because the simulator puts its load factor on top. Values are rounded to
whole ms, µs per prompt token, whole s and two decimals: finer than the cells differ by.

## What the simulator cannot express

Each of these is a stated limitation; P2.9 measures how far a simulated pod ends up from a real one.

- **The table's speed against a replay's.** P2.8a's lora 1-pod file ran its first 9 cells (concurrency 1 up to 2,048
  tokens) at 187-198 ms per token and the rest at 135-166, unexplained; mapped from it, simulated replies took 191.7 ms
  per token in P2.9's first run. P2.8c's file has no such stretch: 147-186 ms at concurrency 1, 146-164 above it. But
  P2.9's real 1-pod replay, the same night just before, generated at 123.1 ms per token, by the same formula (time
  after the first token over the tokens after it), and P2.5's replays at 143.5-149. Why a replay's calls generate faster
  than the table's 64-token calls is not known, so simulated replies can still run slower than a real replay's.
- **The load factor.** Kept at 1.0: the per-token time barely moves with concurrency (152.9 at 2 against 154.2 at 1,
  1.02 at 4, 1.01 at 8; plain 1.02 at 2).
  TTFT under load grows by an added amount, not a factor: on plain at concurrency 2 a fully cached 256-token prompt
  takes 160 ms against 122 alone, about one decode step. The simulator gives the alone value.
- **Waiting for KV tokens, not request slots.** A real pod runs as many calls as fit 4,384 tokens: with 1,024-token
  prompts about 4, with 3,584-token prompts 1. The simulator runs `max_num_seqs` = 2 whatever their length. Short-prompt
  traces (the demo trace's median is 828 tokens) queue earlier than on real, long ones run 2 where real runs 1.
- **The KV cache is 1.9× the real one** (512 blocks against 274), so blocks are evicted later and prefix-cache hits can
  read higher than on real pods (P2.7's decision, at the new `max_num_seqs`).
- **The slow first call.** A fresh real pod answers its first call at 13.7-15.0 s to the first token (lora alone) and is
  warm after 2 calls. A simulated pod has no slow call; `startup_duration` holds that time back from Ready instead. So a
  new pod adds capacity at about the same time on both, but calls sent to a real pod during that time wait on it, and
  on a simulated pod they wait elsewhere.
- **Two pods starting at once** on one GPU and disk (124-143 s to Ready, 19.4-27.0 s first TTFT): simulated pods start
  independently.
- **The slow first cell at each new batch size.** The first cell at each new concurrency has 2 or more slow calls on a
  pod that has not run that batch size yet (TTFT p95 2.7-16.5 s against a p50 of 0.2-0.6 s), which fits vLLM compiling
  kernels (`--enforce-eager`); a guess, not checked.
- **No randomness.** The tails are 10-50× the p50s and come from the effects above, while every simulator std-dev is
  capped at 30% of its mean (v1.0.0 docs). So the profile sets no std-dev, the simulated timings are the means, and the
  chart passes no std-dev flags.

## Not corrected: the clock

The VM's monotonic clock ran 2.5-4.4% slow against Windows' over each file, so every TTFT and per-token time reads
2.5-4.4% low. A simulated pod sleeps on the same VM clock that took these times, so it waits the same real time as the
measured pod worked, and the replayer, on that clock too, reads the same numbers from both. Correcting would make
simulated pods 2.5-4.4% slower than real ones as the replayer sees them.
