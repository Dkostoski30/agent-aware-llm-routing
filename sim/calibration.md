# Calibration: from the table to the simulator (P2.8b, measured again in P4.13b)

`make calibrate` (P2.8a) measures real vLLM pods of the 0.6B, with the adapter (`lora`, profile `real`) and without it
(`plain`, profile `real-plain`), on one pod alone and on both pods at once. P4.13b measured them again on pods with
compiled CUDA graphs and the compile cache, under experiment hash 050859dea9d9:

| File | What it holds |
|---|---|
| `calibration-050859dea9d9-{lora,plain}-{1pod,2pods}.csv` | One row per pod and cell: concurrency 1, 2, 4, 8 × prompt 256, 1,024, 2,048, 3,584 tokens × cache mode uncached, full, partial; 20 calls of 64 tokens each; TTFT and per-token p50 and p95, cached_tokens p50 |
| `cold-start-050859dea9d9-{lora,plain}.csv` | One row per pod started: created → Ready, Ready → first answer, the first call's TTFT, warm-up calls |
| `*.meta.json` | The profile's sha256, the clock's drift over the file, the warm-up TTFTs |

The cell tables were measured before the compile cache was added, and the cold starts after it: a cache changes only a
pod's start. The cold starts without it are in `experiments/results/p4-13b-no-cache/`. The eager pods' files
(5553d79b34d7, P2.8a and P2.8c) stay in `sim/`. The results and their oddities are in `experiments/log.md`
(2026-11-06 · P2.8a, 2026-11-07 · P2.8c, P4.13b). `config/sim.yaml`'s simulator section comes from
`calibration-050859dea9d9-lora-1pod.csv` and `cold-start-050859dea9d9-lora.csv` through `calibrator/mapping.py`;
`tests/test_calibration_mapping.py` fails if the profile and the table differ.

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
  real runs.
- **One pod alone, not both at once.** A simulated pod stands for a pod with its own GPU, as in a real cluster. Two
  real pods share the laptop's one GPU: with both busy the adapter's per-token time more than doubles (12.1 → 27.9 ms)
  and so does the plain model's (9.5 → 22.4 ms). That is the laptop's limitation, and it shows in the 1↔2 real
  validation (D5).

## The mapping

| Value | From | Table gives | Profile |
|---|---|---|---|
| `prefill_overhead` | Intercept of the least-squares line through the TTFT p50 of the 12 concurrency-1 cells against their uncached tokens (prompt − cached_tokens p50), all cache modes together | 126.8 ms | `127ms` |
| `prefill_time_per_token` | Slope of the same line | 0.0913 ms | `91us` |
| `inter_token_latency` | Median per-token p50 of the same 12 cells | 12.1 ms | `12ms` |
| `time_factor_under_load` | Median per-token p50 at concurrency `max_num_seqs` over that at 1, never under 1 | 13.0 / 12.1 = 1.08 | `1.08` |
| `max_num_seqs` | Not from the table: real vLLM's 4,384 KV tokens over the 2,195-token median prompt of the converted trace P5 replays (P2.4) | | `2` |
| `kv_cache_blocks` | Not from the table: `max_num_seqs` × 256 blocks, so running prompts always fit (P2.7) | | `512` |
| `startup_duration` | Median created → Ready of the 5 pods started alone, plus their median first TTFT less a warm pod's TTFT (the line at the 1,024-token warm-up prompt, 220 ms) | 140 + 1.04 − 0.22 s | `141s` |

Concurrency 1 for the line and the token gap, because the simulator puts its load factor on top. Values are rounded to
whole ms, µs per prompt token, whole s and two decimals: finer than the cells differ by.

Eager pods' table (P2.8c) gave 298 ms, 146 µs, 154 ms, 1.0 and 140 s. A compiled pod without the compile cache took
254 s to Ready (P4.13b).

## What the simulator cannot express

Each of these is a stated limitation; P2.9 measures how far a simulated pod ends up from a real one, and P4.13b ran it
again on these values.

- **The table's speed against a replay's.** On eager pods P2.9's real 1-pod replay generated at 123.1 ms per token
  against the table's 154.2, and why a replay's calls ran faster than the table's 64-token calls was not known. How far
  the compiled pods' replays are from this table is in P4.13b's validation (`experiments/log.md`).
- **The load factor is one factor.** The per-token time grows with concurrency (1.08 at 2, 1.12 at 4, 1.20 at 8;
  plain 1.10 at 2), and the simulator scales TTFT by the same factor. Real TTFT under load grows by more: a fully
  cached 256-token prompt takes 266 ms at concurrency 2 against 149 alone, where the simulator gives 149 × 1.08.
- **Waiting for KV tokens, not request slots.** A real pod runs as many calls as fit 4,384 tokens: with 1,024-token
  prompts about 4, with 3,584-token prompts 1. The simulator runs `max_num_seqs` = 2 whatever their length. Short-prompt
  traces (the demo trace's median is 828 tokens) queue earlier than on real, long ones run 2 where real runs 1.
- **The KV cache is 1.9× the real one** (512 blocks against 274), so blocks are evicted later and prefix-cache hits can
  read higher than on real pods (P2.7's decision, at the new `max_num_seqs`).
- **The slow first call.** A fresh real pod answers its first call at 0.9-1.5 s to the first token (lora alone) and is
  warm after 1-2 calls. A simulated pod has no slow call; `startup_duration` holds that time back from Ready instead.
  On eager pods the first call took 13.7-15.0 s; graphs are captured while the pod starts.
- **Two pods starting at once** on one GPU and disk (127-157 s to Ready, 1.7-4.3 s first TTFT): simulated pods start
  independently.
- **The first pod after a change.** The compile cache holds one entry per model and flags. The first pod after an
  image, model, adapter or flag change compiles again and starts as a pod without the cache did (254 s); the simulator
  always starts in 141 s.
- **The slow first cell at each new batch size.** At concurrency 4 and 8 the first cell on a pod still has a slow call
  (TTFT p95 2.9-3.1 s against a p50 of 0.3 s on lora alone), though graphs are captured for those sizes at start. Not
  explained.
- **No randomness.** The tails are many times the p50s and come from the effects above, while every simulator std-dev
  is capped at 30% of its mean (v1.0.0 docs). So the profile sets no std-dev, the simulated timings are the means, and
  the chart passes no std-dev flags.

## Not corrected: the clock

The VM's monotonic clock ran 0-4.9% slow against Windows' over each file (P2.8a's files: 2.5-4.4%), so every TTFT
and per-token time reads up to 4.9% low. A simulated pod sleeps on the same VM clock that took these times, so it
waits the same real time as the measured pod worked, and the replayer, on that clock too, reads the same numbers from
both. Correcting would make simulated pods slower than real ones as the replayer sees them.
