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

### 2026-11-05 · P2.7 · green (Grafana check pending)
Command: `make test` · `make lint` · `make down` · `make up PROFILE=sim` · `kubectl get pods -l app.kubernetes.io/name=vllm` · `make replay TRACE=experiments/results/traces/record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl PATTERN=steady PROFILE=sim OUT=experiments/results/replays/sim-steady-1.jsonl` · a `jq` count of its lines · `kubectl logs deployment/inference-gateway --tail=5` · `kubectl top pods -l app.kubernetes.io/name=vllm`. Before them, one `docker run` of the pinned image with the chart's flags (removed after).
Result:
- `make test`: 1,263 + 22 passed. `make lint`: passed.
- `make up PROFILE=sim`: two sim pods Ready about 2 min after they started (simulator.startup_duration 115 s plus the probe period), no vLLM pod, no agent. The gateway, the InferencePool and the picker installed unchanged on them.
- Replay: 48 calls, 0 failed, all 48 answered. 44 have cached_tokens above 0; the 4 without are the first call of each task type, on a cold pod. Each line names its pod from the x-inference-pod header, which reaches the replayer through agentgateway: 37 calls on r8ghq, 11 on qhp8h (contracts on one pod, the other types on the other, as P2.3 saw on real pods). The access log has the sim pods' IPs as endpoints and the D1 headers.
- The simulator kept its configuration: time to first token is 340 ms + 1.19 ms x uncached prompt tokens, plus 5-12 ms for most calls (p50 8, p90 12, max 187); later tokens 146.6 ms each (146.4-153.9). Later steps on the same pod cache the previous prompt less 2-16 tokens, a cut to vLLM's 16-token block, as on real pods (9-22 there, whose chat template adds 12 tokens to the simulator's 5). First steps after another run of the type cache exactly replay.shared_prefix_tokens.by_type.
- Per sim pod after the replay: 31-33 MiB and 87-88 millicores while idle (`kubectl top`); 13.5 MiB in the Docker run.
- Docker run of the image: /health/ready 503 until --startup-duration, /health 200 at once; cached_tokens 0 then 96 on a 105-token prompt with no render sidecar; another model name answers 404; the vllm:* metrics the dashboard reads exist, each with a model_name label.
Notes:
- Decided in plan mode (card 30): image v1.0.0 by digest (v1.1.0 was 2 days old); a new chart infra/charts/sim, named and labelled like the vllm chart, release vllm, so make down needs no change and moving between profiles needs make down first; the replayer reads x-inference-pod when a reply has no system_fingerprint.
- **KV cache 1024 blocks, not the real 274: a stated limitation.** A full cache makes v1.0.0 answer HTTP 500 (pkg/kvcache/block_cache.go), where real vLLM makes the call wait, and 61.5% of the converted trace's prompts are over 2,000 tokens, so two of them would fill 274 blocks. 1024 is max_num_seqs 4 x 256 blocks, so a call never fails that way; a test fails if it gets too small. The cost: 3.7x the real cache, so blocks are evicted later and hit rates can read higher than on real pods. P2.9 measures it.
- Cached share 0.801 here against 0.874 in P2.3's final real replay at the same seed. Not a like-for-like comparison: that real replay ran on pods already warmed by earlier replays of the same prompts (7 of its later steps cached more than their previous prompt), while these sim pods were new. P2.9 compares the two from equal starts.
- The values in config/sim.yaml's simulator section are earlier measurements, not a calibration: P2.8 replaces them. The simulator cannot express the 26-29 s first call on a pod that served nothing (P2.4), waiting by tokens rather than request count, or two pods sharing one GPU. P2.8 decides how to handle each.
- Not set: the simulator's waiting queue per pod, at v1.0.0's default of 1000 (docs/configuration.md); a request beyond it is refused, which real vLLM would not do. P2.7's loads stay far below it; a burst experiment must too.
- From the fresh-eyes review (subagent, 2026-11-05, nothing blocking): the sim chart, unlike the vllm one, keeps Kubernetes' service variables (VLLM_PORT), which the simulator does not read; it pulls its small image on the first rollout instead of before it.
- The host's clock jumped 27 times between make up (21:20) and the analysis, the replay included (`journalctl -b 0 -q -o short-iso --since "2026-11-05 21:20" | grep -c "Clock change detected"`). The timings above match the configuration to a few ms, so the replay looks unaffected.

---

### 2026-11-03 · P2.5 · green
Command: the spike (read-only, findings in `docs/task-cards/28-P2.5-baseline-experiment.md`) · `make experiment NAME=baseline-check` · `make experiment NAME=baseline` (twice, the
first run's results moved to `experiments/results/first-baseline-laptop-in-use/`) · `make experiment NAME=baseline-reversed` · `make experiment-compare CSVS="experiments/results/baseline-2069d38644d6.csv experiments/results/baseline-reversed-899a3bf28f10.csv" STALL_SECONDS=5` · `kubectl get configmap vllm-pool-epp -o jsonpath='{.data.default-plugins\.yaml}'` · `nvidia-smi --query-gpu=timestamp,temperature.gpu,utilization.gpu,clocks.gr,power.draw,clocks_throttle_reasons.active --format=csv -l 30` beside them
Result:
- On this one load, the default picker took 10.6% less total run time than near round-robin with the order cancelled (12.7% and 8.3% per chain). The two runs in the largest stall carry 60% of that gain: without them it is 4.7%.
  Each variant replays the same 40 runs, 309 calls, at 0.02 runs/s, seed 6. The numbers leave out the first 5 runs (35 runs, 278 calls, 243 later steps), except the picks, switch share, gap deviation and jumps, which cover all 309 calls. No call failed, and each CSV has 556 rows on every run.
  | | 1: round-robin | 1: default | 2: default | 2: round-robin |
  |---|---|---|---|---|
  | ran, 2026-11-03 | 16:18-16:56 | 17:01-17:39 | 17:43-18:22 | 18:26-19:05 |
  | total run time, s | 1,255.0 | 1,096.0 | 1,076.7 | 1,174.2 |
  | median run, s | 27.89 | 27.15 | 27.53 | 28.38 |
  | p95 run, s | 97.3 | 75.8 | 77.2 | 81.9 |
  | slowest run, s | 99.3 | 97.9 | 91.3 | 94.0 |
  | prefix-cache hit rate | 0.719 | 0.836 | 0.816 | 0.727 |
  | uncached prompt tokens | 171,380 | 100,164 | 111,876 | 166,532 |
  | later steps moved, of 243 | 161 | 27 | 31 | 164 |
  | share of picks that switched pod | 0.740 | 0.321 | 0.289 | 0.740 |
  | picks per pod | 156 + 153 | 129 + 180 | 153 + 156 | 153 + 156 |
  | total TTFT, s | 289.0 | 154.4 | 137.3 | 245.4 |
  | total generation, s | 965.4 | 941.1 | 938.8 | 928.2 |
  | calls over the KV limit | 32 | 7 | 12 | 28 |
  | their total TTFT, s | 173.2 | 32.5 | 22.9 | 139.2 |
  | calls under the limit | 246 | 271 | 266 | 250 |
  | their total TTFT, s | 115.9 | 121.8 | 114.4 | 106.3 |
  | their TTFT p50 / p95, s | 0.37 / 0.94 | 0.36 / 0.81 | 0.36 / 0.84 | 0.36 / 0.82 |
  | generation, ms per token | 147.7 | 146.0 | 149.0 | 143.5 |
  | warm-up calls, ms to first token (they skip the picker) | 371.2 | 369.2 | 343.1 | 389.4 |
  | largest gap deviation, ms | 12 | 3 | 10 | 2 |
  | clock jumps | 67 | 69 | 70 | 70 |
- Averaged over both orders, total run time is 1,214.6 s against 1,086.3 s, 128.2 s less on default. Paired by run, default was faster in 29 of 35 runs in chain 1 and 21 of 35 in chain 2, and in 27 of 35 on the two chains' averages.
- Graphs: `experiments/graphs/baseline-2069d38644d6.png`, `experiments/graphs/baseline-reversed-899a3bf28f10.png`, redrawn from their CSVs after a variant's colour stopped depending on run order. GPU log: `experiments/results/baseline-gpu-2026-11-03.csv`.
- Where they come from: `make experiment`'s report gives the table's medians, hit rates, moved counts, picks, switch share, generation, warm-up, gap deviation, jumps and the under-limit p50 and p95. The `ran` row is the `.meta.json`'s times. `make experiment-compare` above gives the totals, paired counts, two-chain average, leaving out the top runs, one variant between chains and the stall lists, from the CSVs alone. Everything else here comes from one-off reads of the CSVs, replay files, Prometheus, the journal and `nvidia-smi`, so it is exploratory: the p95 and slowest run, the stall timings, the queue numbers, the clock numbers and the GPU figures.
- `make test` and `make lint` on the user's run after the fixes: root 1208 passed, training 22 passed, every lint hook passed (golangci-lint skipped, no `gateway/` yet). `make experiment-compare` printed the numbers above. `bash infra/set-routing.sh real` with the probe's new headers: `default is serving, on try 1`.
Notes:
- **The first run's slowdown came from the host, not from routing or order.** That run (hash 2069d38644d6, kept as the "before") made default look slower: 1,221.8 s for round-robin against 1,286.6 s, default slower in 28 of 35 paired runs. But its default variant generated at 175.1 ms per token, against 152.3 in its own round-robin variant.
  The user was using the laptop, on Armoury Crate's Turbo plan with sleep at 10 min. On Performance with the laptop mostly left alone, the four variants span 143.5-149.0 ms per token, and second place is not slower: round-robin 147.7 first and 143.5 second, default 149.0 first and 146.0 second. This rests on generation speed. The warm-up calls agree in the first run (407.6 against 354.6 ms), but they already span 343.1-389.4 ms in the quiet chains, so they are weak evidence.
  "Mostly": the user ran a few `journalctl` counts during the chains and ended IntelliJ at a time they no longer remember, possibly during chain 1's variants. Crossing the order cancels steady drift, not a one-off disturbance. Chain 1's round-robin has the largest total and generation time (though chain 2's default has the slowest ms per token), and about half of round-robin's 80.8 s between chains is first-token stalls (43.6 s), which could come from different picks as well as from IntelliJ. So 12.7% and 8.3% stand side by side, unexplained.
- **Where the 128.2 s went.** Total TTFT is 121.4 s of it and generation 6.9 s. Calls over the KV limit account for 128.4 s (156.2 s against 27.8 s), and calls under the limit go the other way by 7.0 s (111.1 s against 118.1 s, over more calls on default). Under the limit, TTFT per call is about equal, at 0.47 and 0.43 s
  against 0.45 and 0.43 s, although default sends about 38,000 fewer uncached tokens there (129,862 against 91,826; about 63,000 over all calls).
- **A few stalls carry it.** Calls of 5 s or more to first token, all of them over the limit: 10 and 7 on round-robin, summing to 121.8 and 91.3 s, against 2 and 1 on default, 20.7 and 6.8 s. The largest is the same event in all three round-robin variants: `tau2_airline-764-7` step 3 takes 34-35 s over a 211-token reply on a pod, and `bfcl-615-6` (step 6, 10 or 9) lands behind it and gets its first token 579-769 ms after that reply ends.
  `gaia_dev-310-8` waits too: in chains 1 and 2 step 3, 940 tokens, for 27-30 s, first token alongside `bfcl-615-6`'s; in the first run step 5, 1,748 tokens, for 26.9 s, first token 3.84 s after the reply ended, with its step 3 on the other pod at 472 ms. `bfcl-615-6` alone is the p95 of 97.3 s in chain 1.
  Prometheus over those windows (5 s steps, one-off, retention 10 d) shows that pod with 1 request running, 1 then 2 waiting and KV usage 0.44-0.48. `bfcl-615-6`'s whole prompt is 59-63% of the pod's KV capacity, so it would fit only once the reply ends, cached tokens or not (only 344 of its tokens were uncached in chain 1). Step 3's 940 tokens would fit, and on default that call ran beside the same long reply in 450-558 ms, so in the chains it looks like waiting behind a queued call. All of this is inferred: vLLM's scheduling log was not read.
  Requests waiting, across both pods, in samples of 5 s (first run, chain 1, chain 2): round-robin 26, 38, 27 at up to 2; default 7, 5, 2 at up to 1. One preemption in each round-robin variant, none on default.
- **What this experiment can't separate.** Default's profile has three scorers: queue 2, KV-cache utilization 2, prefix-cache 3 (the live ConfigMap, command above, and the chart is pinned by digest). Near round-robin has none. So the gain may come from keeping tasks on their pods, from seeing load, or from both. `bfcl-615-6` had no move on default in all three runs, which fits keeping but is one case.
- **Limits of the size.** Every chain used one seed, so identical arrivals: the stall repeating in three runs is not independent evidence, and another seed may not contain it. The same variant differs between chains by 19.3 s on default and 80.8 s on round-robin. Without the 5 runs that favour default most, the gain is 2.6%.
  Whether a typical run gains is not settled. The median paired difference is 1.28 s in chain 1 and 0.23 s in chain 2 (29 and 21 of 35 runs favouring default), and 0.96 s on the average of both (27 of 35). Two runs of one variant differ per run by a median size of 1.42 and 1.24 s, and round-robin shifted by a median of 1.12 s between chains. P5 runs three repetitions; varying the seed there is an open question for that card.
- **What this means for the thesis (P3.3, P3.7, P3.8):** generation is 77-87% of the time spent in calls, the P2.2b risk again, and picks seem to change it little (the review measured 12-24 ms per token more for a call generating beside another on its pod; not rechecked). What moved here is stalls on a full pod. P3's experiments should report stalls, the queue and the KV split as well as hit rate, and need a variant that separates prefix affinity from load, such as a prefix-only profile. Not decided here.
- **The VM's kernel clock ran slow during the chains, so their durations read about 2% low on average.** Measured with the cluster idle at 20:00, against Windows' clock: CLOCK_MONOTONIC behind by 2.17%, 2.27% and 2.30% after 2, 4 and 6 minutes, with the VM's wall clock within 0.7 s of Windows'. Again after a WSL restart, at 22:43 with vLLM starting up: 2.41%, 1.94% and 1.84%, with 15 jumps in the boot's first 8 minutes.
  Wall clock minus monotonic across each replay is a staircase of forward steps, with no backward step over 0.3 s: 1.57%, 1.68%, 2.11%, 2.24% across the chains, and by 10-minute window 0.8-1.6% in chain 1 and 1.5-2.0% in chain 2, so the rate was not steady and rose. This measures the monotonic clock's error only if the wall clock was right, which was checked against Windows only after the chains. The first run differs: its round-robin variant is flat apart from one 8.65 s step at 14:51:35, right after its standby, and a 0.5 s step, and its default variant is flat until 15:29:38 and then runs at about 1.1%. So its 15% slowdown, from 15:10, came before the clock drift and is not this.
  The replayer's times are monotonic on purpose (P2.2a), so the chains' durations and ms per token are low by this average, not by a known scale on each call, and some of "the chains are faster than the first run" is this. The order cancels it for the comparison: 1.905% on round-robin and 1.895% on default. P2.8's calibration would inherit it, so it must be handled there.
  **The clock jumps look like an effect of this.** `timedatectl timesync-status` shows `systemd-timesyncd` at its minimum poll of 32 s, and jumps come about every 33 s. The smallest step per replay is 0.39-0.63 s, around timesyncd's 0.4 s limit between slewing and stepping (the review read that in upstream source, not the installed version). So it appears timesyncd steps the wall clock back into line, leaving the monotonic clock behind; a smaller offset, like the +386 ms sampled, would be slewed. The stepper's identity is inferred, and why the kernel clock runs slow, and only in some stretches, is not known: for P2.8. Stretches on that boot: 101 an hour from 23:39 to 06:28, 1-2 an hour until 15:29, then 96-109 an hour until it ended. They resumed straight after the WSL restart at 22:40, on clocksource `tsc`. IntelliJ was not needed to keep them going, as they went on after it was ended, though the 15:29 onset is unexplained.
- The picker labelled round-robin is near round-robin, as decided in the spike: live switch share 0.740, 0.740 and 0.727 against strict round-robin's 1, a coin toss's 0.5 and the standalone copy's 0.78. Hit rate and moved counts repeat closely across all three runs of each variant (round-robin 0.719-0.727, 157-164 moved; default 0.816-0.836, 27-31 moved). Default's picks per pod do not: 129 + 180 in two runs and 153 + 156 in one.
- Host during the two chains: Performance (`27fa6203-3987-4dcc-918d-748559d549ec`), sleep and screen-off on AC at never, read back with `powercfg.exe` at the end. This is the ID `versions.md` already names. Armoury Crate listed Turbo in that place during the first run and Performance after the user chose it, so the mode needs checking before a run. No check does this yet; it would suit P2.8. A Modern Standby of 10 s at 14:50:51 fell in the first run's round-robin variant.
  GPU: 59-62 °C at most per variant and about 14-15 W at median while busy, with no rising trend (means about 46-52 °C). `nvidia-smi -q` shows 0 µs of hardware thermal and power-brake slowdown. The event mask read `0x400` in 46 of the 306 samples during the variants, spread evenly and not decoded.
- Largest gap deviations were 2-12 ms, inside `replay.gap_tolerance_ms`'s 50. They were 64 ms in the first run and 65 ms in P2.4's replay, so P2.4's open point looks like load on the host: left as it is.
- Warm-up holds up. A pod's first call took 4,146 / 18,089, 4,396 / 19,595, 17,235 / 3,265 and 17,468 / 3,984 ms, then 278-490 ms. One pod is always near 4 s and the other near 18 s. set-routing's probe is itself a first call on one pod, which may be why: not verified. The 12-14 s stall under the limit seen in `baseline-check` did not repeat; the largest under the limit in these runs is 4.6 s. `baseline.yaml`'s comment says the discarded runs take about 4 min; at seed 6, run 6 starts at 175 s. Left, since an edit changes the file's hash.
- `results.py` gives P2.4's split on its committed replays (28/81/13/11 and 13/99/16/7, moved 41 of 133, hit 0.759). Medians here are nearest rank, a value that was measured, so with an even count they read lower than an interpolated one (18.4 s against the log's 21.9 s on 16 calls).
- `generation_ms_per_token` and the warm-up time joined the report after the first run, so a host change shows without a one-off script. `experiments/baseline-reversed.yaml` is `baseline.yaml` with the variants swapped, which a test holds; its comment says the first run was 14.5% slower where the medians give 15%, left since an edit changes its hash. 58 bugs planted one at a time were all caught, on a copy, before the reviews' fixes; the first pass missed 2, which gave the tests for hyphenated run IDs and for passing on the KV capacity.
- **Two fresh-eyes reviews (subagents) recomputed the table and the compare numbers from the data, all correct, and found this entry's earlier versions wrong in places, all fixed above:** it said default "has no capacity rule", which the repo contradicts; it called the stall two calls colliding, missing the long reply; it read 1 s per run first as a gain and then as no gain; and it missed the clock gap, then overstated what explained it.
  Code fixed, with tests first: the default picker was not restored if the restart before a default variant failed, or if switching back failed once; a failed restore after the last variant lost the CSV and meta, which are now written and reported first; `profile_sha256` was computed after the long run and is now taken at the start; a variant's colour followed run order; `set-routing.sh`'s probe sent no D1 headers. The card's done-when `rm` also matched `baseline-reversed-*` and the GPU log, so it now names the hash. `make experiment-compare` is new, so these numbers regenerate from the repo (rule 9), and the second review's bugs in it are fixed: a call with no cached count lost its times, a variant with no finished run crashed the report, totals did not say how many runs they cover, and files of one name could not be told apart. None of this changes a row.
  Report-only settings moved to the new `config/report.yaml`, decided with the user, so rule 6 holds without changing `config/real.yaml`, which the results are stamped with: the fewest tokens for a speed, the runs left out, and the five layout numbers the review named. CLAUDE.md's layout lists only the three profiles under `config/`. Still outside it: `STALL_SECONDS`, a required make argument recorded in the command above; the 2 variants compared, which the code assumes; the graph's colours, sizes and font sizes, as named style. No third review ran, by the user's choice, so the second round's fixes and this move have had tests but no review.
  Left as they are: `make experiment-graph` on a same-named CSV replaces its PNG, which regenerates from the CSV; a variant's colour holds for a fixed set of variants, not if one is added between them; `check_ready` does not prove the cluster runs the profile the file names (`sim` also has 2 replicas of that model), and the meta has no git SHA; a variant that fails still loses the earlier variants' meta, though their replay lines stay; if the retry of the switch back also fails, the picker is left as it was and the run ends with that error.
- Reported, not changed: `CLAUDE.md`'s command list lacks `make experiment-graph`, `make convert`, `make record` and `make replay-compare`, and `.gitignore` lacks `*.partial`.

### 2026-11-03 · P2.4 · green
Command: the spike (read-only, findings in `docs/task-cards/27-P2.4-agentprocessbench.md`) · `make convert EXPERIMENT=experiments/convert-apb.yaml` ·
`make replay TRACE=<converted> PATTERN=steady-long SEED=6` · `make test` · `make lint`
Result:
- Trace: `experiments/results/traces/convert-apb-ca5fc26bbb1d-cd81f326aece.jsonl`, with `.meta.json` beside it. 879 of AgentProcessBench's 1,000
  runs, 6,318 of its 8,259 calls, all valid trace.v1, largest call 4,095 of 4,096 tokens, no prompt shrinks, every later gap 2 ms, median 6 calls per run, 513
  runs of 5 or more, 257 of 10 or more, longest 37. Dropped 99 runs as too large and 22 for a long reply:
  | task_type | factor | runs kept of | too large | long reply | calls | median first prompt | median last prompt | expected_steps | shared prefix |
  |---|---|---|---|---|---|---|---|---|---|
  | hotpotqa | 0.716 | 220 of 250 | 25 | 5 | 545 | 221 | 1,184 | 2 | 199 |
  | gaia_dev | 0.262 | 217 of 250 | 25 | 8 | 1,092 | 208 | 1,045 | 4 | 190 |
  | tau2_airline | 0.352 | 73 of 85 | 8 | 4 | 713 | 1,673 | 2,604 | 9 | 1,649 |
  | tau2_retail | 0.358 | 75 of 85 | 8 | 2 | 817 | 1,657 | 2,937 | 10 | 1,628 |
  | tau2_telecom | 0.282 | 72 of 80 | 8 | 0 | 919 | 1,906 | 2,592 | 12 | 1,887 |
  | bfcl | 0.476 | 222 of 250 | 25 | 3 | 2,232 | 2,945 | 3,331 | 9 | 227 |
- expected_steps of the P2.2b trace: csv 2, contracts 3, support 3, arithmetic 2. The dataset's medians of all runs and of the runs labelled good
  differ by at most 1 step. The medians rest on few independent runs: about 200 distinct tasks with 5 samples each, about 17 distinct demo runs.
- Replay at seed 6 on the real profile (`...ca5fc26bbb1d-cd81f326aece-steady-long-6.jsonl`): 153 calls, 0 failed, every call's tokens as planned, 17.4 min, no clock
  jumps (the journal's last was 12:27:45, the replay ran 12:32-12:49), no `.partial` left. Duration p50 5.4 s, p95 46.7 s, slowest 87.0 s against the 120 s
  deadline, a first call with a 199-token reply. TTFT p50 0.65 s, p95 18.2 s, max 53.4 s. 237 ms per generated token, median. Cached share 0.759. Calls in flight
  on the monotonic rebuild: mean 1.68, at most 5. vLLM's own counters over the window agree: 304,112 prompt tokens, 230,944 cached, 77 and 76 requests per pod.
  Largest gap deviation 65 ms, over `replay.gap_tolerance_ms`'s 50: two calls of bfcl-624-13 at 40 and 65 ms, every other within 24, p95 2. `make replay-compare`
  is not in this card's done-when, but it would refuse this file, so P2.5 should look at the tolerance under this load.
- `make test` and `make lint` passed on the user's run; root 1063 passed, 0 skipped, on Claude's. 80 of 83 bugs planted one at a time were caught, on a copy; the 3 left
  give the same output on this dataset (the clamp below put back, reading with `splitlines`, the greeting matched only at position 1).
Notes:
- **The first conversion counted bfcl wrongly, and the tests could not see it; a fresh-eyes review did.** bfcl's tool definitions have another shape than the other
  subsets', which the converter took as needing wrapping. In fact its system message already lists every tool, in 250 of 250 runs and in no other subset, so passing them
  counted each tool twice: 6,986 tokens against 4,003 at bfcl-500's first prompt. Also, all 1,105 mid-run system messages are bfcl's `<state_info>` snapshots, and
  246 of its 250 runs end on one, so they look like the benchmark's record and not something the model read: inferred from the data, not stated by the dataset. Both
  are out, decided with the user. So bfcl's factor went 0.271 -> 0.476, expected_steps 10 -> 9, shared prefix 129 -> 227, 225 -> 222 runs, reply p50 8 -> 14. The other
  five types did not change. The first trace is in `~/.cache/thesis/agentprocessbench/superseded/`, not in git; its replay, `...79f134ab29df-cd81f326aece-steady-long-6.jsonl`,
  stays as the "before". The rule that a prompt takes the length of the one before it if it would shrink is gone too: it changed nothing in the trace, and a test now
  checks the committed trace instead.
- **What dropping costs: the long runs.** Runs are dropped by size, and the largest are mostly the longest. The user chose to keep 0.9 and state this:
  | | runs | calls | runs of 15+ calls | of 30+ calls | longest | gaia_dev's factor |
  |---|---|---|---|---|---|---|
  | dataset | 1,000 | 8,259 | 116 | 30 | 94 | |
  | this trace, keep_share 0.9 | 879 | 6,318 | 66 | 8 | 37 | 0.262 |
  | not built, 0.95 | 936 | 7,139 | 85 | 16 | 56 | 0.144 |
  | not built, 0.98 | 973 | 7,820 | 104 | 24 | 89 | 0.111 |
  The card's goal was long runs, and P3.8's and P4.1's "close to finishing" should be read with this in mind. Taken literally, 1,431 of 8,259 calls and 314 whole
  runs fit 4096 (28 of 5+ calls, none of tau2's or bfcl's), so scaling stays the choice.
- **What scaling keeps, exactly.** A call's prompt:reply ratio, a run's steps and the shape of its growth. Not the uncached tokens per reply token, which rise (by up to
  29% by type, on the first trace's counts), since the chat template's 9 tokens and the block remainder do not scale.
- **The default picker splits runs under concurrent load, which P2.2b could not show one run at a time.** 9 of the 19 runs with more than one call used both pods, and 41 of
  the 133 later steps moved. A step that stayed cached 0.927 of its prompt and one that moved 0.548. P3.3 has something to beat. tau2_airline and bfcl have 33 of the 41
  moves. Time to first token by what the step's pod was doing when it arrived:
  | step | pod | pod idle: calls, p50 | pod busy: calls, p50, max |
  |---|---|---|---|
  | stayed | h7xzw | 25, 0.50 s | 22, 0.90 s, 49.9 s |
  | stayed | s67zx | 34, 0.54 s | 11, 1.00 s, 30.0 s |
  | moved | h7xzw | 12, 0.53 s | 9, 3.12 s, 53.4 s |
  | moved | s67zx | 12, 0.62 s | 8, 5.33 s, 18.6 s |
- **But most of that difference goes with the pod's KV limit, not with the move, so it does not answer P2.2b's risk that cache hits save little time.** Splitting calls by
  the tokens on their pod up to their first token (their own plus every overlapping call's) against 274 x 16 = 4,384 tokens per pod, in both replays:
  | | first trace, pods 10 h old | this replay, new pods |
  |---|---|---|
  | under the limit, moved | 0.46 s, n=13 | 0.58 s, n=28 |
  | under the limit, stayed | 0.43 s, n=99 | 0.59 s, n=81 |
  | over the limit, moved | 21.9 s, n=16 | 6.24 s, n=13 |
  | over the limit, stayed | 4.26 s, n=7 | 4.40 s, n=11 |
  Under the limit a move costs almost nothing visible, although its median uncached tokens are 424 against 68. That supports the risk rather than answering it. Over the
  limit calls are far slower either way, and moved against stayed swings from 5x to 1.4x on too few calls to say. The split is an estimate from the replay file, and capacity
  is not proven as the mechanism: 8 of this replay's 11 first tokens over 10 s are over the limit, but 7 of the 25 calls over it took under 2 s, there was 1 preemption, and
  KV usage peaked at 0.989 and 0.791 at 5 s samples (the first replay: 0 preemptions, peak 0.88). **P2.5's `make experiment` must report calls over and under the limit apart**, or P3.3
  could read a capacity effect as a cache effect.
- **The first call on a new pod is slow.** These pods had served 0 requests (Prometheus), since the conversion only tokenizes. The first call on each took 26.5 s and 28.7 s to
  its first token with 1,658 and 1,906 uncached tokens, with nothing else on that pod, against 4.8 s for 2,828 uncached later in the run. The other under-limit stall over 10 s,
  18.2 s, arrived during the second of them. Cause not measured. So a pod that is Ready is not yet fast, which P2.7/P2.8's cold start and P4.2's warm-up need, and this replay's
  latencies cannot be compared with the first replay's, whose pods were warm. All 11 stalls fall in minutes 0-5.2 of the 17, when the most calls were in flight.
- **For P2.7-P2.9: where a first token's time goes is not settled.** Of 445 s of TTFT in vLLM's counters, 225 s is queue time, all with reason `capacity`. Inference minus decode
  is 199 s for 73,168 uncached tokens, 2.77 and 2.67 ms each on the two pods, so the first replay's one slow pod (6.0 against 1.03 ms) did not repeat and was no property of a pod.
  Calls alone on both pods fit TTFT = 479 ms + 0.933 ms x uncached (n=35, r=0.66, without the two first calls), against 328 ms + 0.291 ms (n=59, r=0.63) in the first replay and
  P2.2b's 1.19 ms. A slope that moves 3x between replays gives no prefill figure, so P2.8 has to measure one. P2.2b's fit gave 20.1 min of durations against the 29.1 measured, so
  a simulator with only prefill and per-token time (D4) may miss P2.9's 10-15% here. Two pods share one GPU, at 3,879 of 4,096 MiB after the replay: a lead only.
- **0.02 is not the same load as steady.** The plan said it would be, by P2.2b's fit of ~49 s a run. It measured 1.68 calls in flight against steady's 0.70, and 87 s of calls per
  run. It finished, with the slowest call at 87 s of the 120 s deadline, but P2.5 should choose its rates from measurements, and bursts may start failing calls.
- **For P2.5, P3, P5:** 20 of 879 runs are few. Seeds 1-12 of `steady-long` give 81-167 calls and, by P2.2b's fit, 0.48-1.88 runs at once (seed 6: 1.18 by the fit). Seed 1 draws 1 tau2
  run of 20, a 2.4% chance: the sampling is unbiased, 5.15 of 20 on average over seeds 1-40. The done-when's seed comes from a stated rule, the first seed with 4-6 runs from each of
  hotpotqa, gaia_dev, tau2 and bfcl: seed 6, with 6, 4, 6, 4 and a 30-call run. So it is not a random draw. Use more runs, or several seeds. The profile's default stays seed 1, which no
  replay of this trace has run.
- A stated limitation of the shared prefix: bfcl's 227 is the minimum over all pairs of kept runs. The median pair shares 227 and the 90th percentile 1,842, so a replay gives some pairs
  less to share than the dataset did. tau2_telecom's runs hold tool calls on the user's side, about 1% of the tokens (factor 0.282, or 0.292
  without them): left in, since the model reads them.
- Later steps that stayed follow P2.3's rule, cached == floor((previous prompt - 9) / 16) * 16, on 87 of 92. 5 lost 16-704 tokens more, so a pod does evict a run's history here. P3.3's
  check on a replay needs previous prompt - 24 (the 9 and a block's 15), not P2.2b's - 19, which holds for real agent runs.
- The spike's numbers: it predicted 874 runs and 6,287 calls, on counts that left out 13 runs and only estimated prompts over 60k characters. Its rank was also one too high, the code's is
  the nearest rank.
- These breakdowns come from one-off scripts over the replay file and Prometheus, not in the repo, so they are exploratory. P2.5's `make experiment` is what regenerates numbers for the thesis.
- For P3.8: half of a type's runs go past its median by definition, so remaining work must not go negative. Reported, not changed: CLAUDE.md's command list lacks `make convert`, `make record`
  and `make replay-compare`.

### 2026-11-02 · P2.3 · green
Command: `make replay TRACE=<P2.2b trace> PATTERN=steady`, twice (second with `OUT=`), first and again on the final code ·
`make replay-compare PATTERN=steady` · `make replay ... PATTERN=steady-fan-out`, before and after the first review's fixes ·
`grep -c parallel_group` on its output · `journalctl` counted for `Clock change detected` ·
`kubectl logs deployment/inference-gateway` counted for `"parallel-group"` · pods per task type with jq · `make test` · `make lint`
Result:
- Trace: `experiments/results/traces/record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl`, profile real, seed 1,
  `replay.shared_prefix_tokens.all_types` 0. Outputs in `experiments/results/replays/`.
- On the final code (`...-steady-1-final.jsonl`, `...-steady-1-final-second.jsonl`): `make replay` steady 48 calls, 0 failed, both
  times. `make replay-compare PATTERN=steady`: `same sequence, 48 calls, the plan's calls, tokens as in the trace, a reply and TTFT
  on every call, all gaps in tolerance. Not checked: arrival times, prompt text, pods`; max gap deviation 1 ms and 1 ms against a
  tolerance of 50 ms. No clock jumps during either (695 lines in the journal before and after). Slowest call 34.2 and 41.0 s against
  the 120 s deadline. No `.partial` file left behind. `make test`: 933 passed, training 22 passed. `make lint` passed. These
  pods had already run this seed's fan-out, so the text is not fresh: cached share 0.874 and 0.908.
- The older steady replays (`...-steady-1.jsonl`, `...-steady-1-second.jsonl`): 48 calls, 0 failed, both times, before the fresh-eyes
  reviews' fixes, so without the new stream reading or the deadline. The final compare passes them too, max gap deviation 9 ms and 2 ms.
- `make replay` steady-fan-out: 15 calls, 0 failed. 6 lines with a `parallel_group`, 6 access-log lines with `parallel-group`
  (3 groups of 2 calls). Largest gap deviation 24 ms, on cold pods.
- The same pattern again after the review's fixes, proving the new reading path through the real gateway
  (`...-steady-fan-out-1-after-review.jsonl`): 15 calls, 0 failed, 6 and 6, largest gap deviation 1 ms, every type on the pod
  it had before (csv and arithmetic on lzx5v, support on mrwpq). The same seed on warm pods, so a check of the code, not a load:
  cached share 0.988 against 0.741 on the first run, and the picker already knew this text, so it is not a third placement.
- Before its first call, each `make replay` counted every planned prompt of its pattern on vLLM's `/tokenize`, all matching the
  trace. A one-off script did the same for all three patterns, so burst's prompts are checked too, but no replay has run burst.
Notes:
- The first done-when passed but sent all 111 calls to one pod, while P2.2b's recording split by type. llm-d-router v0.11.0
  (`tokenizer/estimate.go`, read in the source) hashes a request's tools first, then its messages, so real runs of different
  types share nothing in the picker's view. Replays send no tools and shared their first 192 words, so the pod that saw them
  first won every prefix score. With `all_types` 0 each type stays on one pod and both pods serve, as in the recording:
  | task_type | steady, older two | fan-out, on new pods after a restart | steady, final two, same pods as fan-out |
  |---|---|---|---|
  | csv | g8fs5 (16 calls) | lzx5v (9) | lzx5v (16) |
  | contracts | g8fs5 (11) | not in this pattern | mrwpq (11) |
  | arithmetic | g8fs5 (10) | lzx5v (3) | lzx5v (10) |
  | support | b75wv (11) | mrwpq (3) | mrwpq (11) |
  The split is partly chance: it comes from a few independent placements, and gives 37 and 11 calls on the older steady replays and
  26 and 22 on the final ones, where the recording had 40 and 38. Ties between idle pods can still put everything on one pod.
  The limitation this leaves: inside vLLM real runs of different types share 192-256 tokens and replays share none, up to
  256 more uncached tokens, about 0.3 s, on the first call after a pod switches type.
- **Two pods sharing one GPU: the slowdown of 2-3 times was mostly not the pods. The same load at the same seed, rerun with no clock
  jumps, shows about 1.1 times.** Time per generated token, (duration - TTFT) / (completion tokens - 1), on the monotonic clock,
  leaving out calls with a TTFT over 4 s:
  | replay | other pod busy >= 75% of the call, median ms (calls) | other pod busy < 10%, median ms (calls) | ratio | clock jumps per hour | mean calls in flight |
  |---|---|---|---|---|---|
  | steady, first | 682 (13) | 244 (22) | 2.8 | about 100 | 1.67 |
  | steady, second | 467 (19) | 218 (21) | 2.1 | about 100 | 1.47 |
  | fan-out, cold pods | 440 (6) | 227 (2) | 1.9 | about 100 | 1.98 |
  | fan-out, after the review | 209 (9) | 199 (3) | 1.05 | 0 | 0.90 |
  | steady, final | 191 (5) | 173 (36) | 1.10 | 0 | 0.71 |
  | steady, final, second | 207 (7) | 183 (35) | 1.13 | 0 | 0.69 |
  The older and the final steady replays have the same arrivals. Older against final: calls with nothing else running 245, 217
  against 170, 179 ms per token; call p50 12.4, 10.8 against 7.1, 7.3 s; p95 70.1, 57.4 against 23.5, 21.4 s; the 142-token support
  call 97.3, 105.7 against 34.2, 41.0 s. No other pod can slow a call with nothing else running, so something on the host was slowing
  everything while the clock jumped, and those older times read low too, since that clock ran slow. What it was is unknown, and the
  jumps may be a symptom. This is the likeliest reading, not a proven one: the split between the pods changed as well (37 and 11
  calls against 26 and 22), and a slower host makes calls overlap more, so the two effects feed each other. The 1.1 times that remains
  rests on 5 and 7 calls, and these replays barely test sharing, with at most 2 calls in flight, so heavy sharing is still unmeasured.
  All of this is exploratory, with no script in the repo. An earlier version of this note called the direction real and it was raised
  as a thesis risk; that is withdrawn. P2.8's measurement replaces it, should count the jumps and use only runs without them, and
  should run at 1 pod and under real overload. If a slowdown shows there, round-robin in P2.5 may lose for that reason rather than for
  cache misses, and the simulator (P2.7, P2.9), which treats pods as independent, would differ from real pods at 2. The plan already
  allows for this: autoscaling experiments run on the simulator, P2.9's main check is 1 real pod against 1 simulated, P2.8 measures
  one pod against both, and P4.5 accepts a difference that is explained. For P2.5: at 0.7 calls in flight steady is a light load, so
  it may not separate routing policies at this rate.
- WSL2's wall and monotonic clocks disagree by an amount that changes (found by the reviews, one-off checks, no script in the
  repo). An earlier version of this note said the monotonic clock is about 8% slow and comparisons stay fair; that is wrong:
  | run | wall time between a run's calls / what was measured | `Clock change detected` lines per hour in the journal |
  |---|---|---|
  | recording, 11-02 14:39 | 1.042 | 54 |
  | four replays, 11-02 19:21-23:46 | 1.069-1.087 | about 100 |
  | after-review replay, 11-03 07:59 | 1.000 | 0 |
  | final steady replays, 11-03 08:52 and 09:02 | 1.000 | 0 |
  With no jumps `ts_start` keeps to the plan's arrivals within 0.075 s; with them it is up to 42 s off. The jumps, about 2.5 s each,
  stopped at 06:28:43 on 11-03, with no known cause. In one 60 s test while they ran, Windows measured
  66.05 s, the WSL wall clock 66.51 s and the monotonic clock 60.32 s, so the monotonic clock looks like the slow one, on that one
  test. Durations from different runs can therefore be on different scales: by 3-4% between the recording and the older replays,
  by 7-9% between those and the newest. One correction factor will not do, so P2.8 must count the jumps during each calibration run
  (`journalctl -b 0 -q -o short-iso | grep "Clock change detected"`) and not calibrate across a change. The rule against `ts_start`
  holds for short spans, where a jump swamps the call. Kubernetes and Prometheus timestamps, on the wall clock, will not line up
  with durations from the monotonic clock while the jumps last.
- The 120 s deadline can decide results under overload. It holds the whole call, as a real agent's non-streaming call is held, and
  the older steady replays' slowest calls took 97.3 and 105.7 s before it existed. support's 142-token step ran there at 684-735 ms
  per token and would fail above about 845; on the final replays it ran at 239-286 ms and took 34.2 and 41.0 s. So at this load the
  deadline matters only on a slowed host, but under overload long task types fail first, and P3.4's and P4.4's counts of completed
  runs lean toward the short ones unless they report failures per task type.
- Cache reuse matches the prediction: on the first replay of each `all_types` setting,
  cached == floor((previous prompt - 9) / 16) * 16 on 28 of 28 later steps. First steps cached each type's `by_type` value
  once warm (csv 752, contracts 736, support 800, arithmetic 336). This corrects P2.2b's note below: a one-message prompt loses
  9 template tokens at its end against the agent's 4, so the replay's shared prefix is 5 tokens shorter per step, not 4 longer.
  Cached counts then differ by 0 or 16 tokens, about 19 ms by P2.2b's fit.
- A second replay with the same seed finds the first one's text: 7 of 28 later steps cached above that bound, up to 1264 of 1277
  tokens, and the cached share rose from 0.797 to 0.897. Its match with the recording's 0.896 is a coincidence. On fresh text
  replays reuse less than the recording: first steps 0.763 against 0.879, later steps 0.814 against 0.907, and overall 0.855 against
  0.929 without each type's first call. Neither gap is a fault in the prompts. First steps are mostly the cold calls: without each
  type's first call they are 0.955 against 0.973, and the raw gap comes from the same 4 cold calls falling among 20 runs rather
  than 34. Later steps lose the repeats: on 22 of the recording's 44 later steps a run found an earlier run's repeat of the same
  question (P2.2b's note below). On a run's own previous step alone the recording would reach 0.823, or 0.818 with a replay's 9
  template tokens, against the replay's 0.814. Replayed runs each have their own text, so they cannot reproduce that, and hit rates
  in P2.5 and P3.3 will sit below the recording's.
- Repetitions need another seed or fresh pods (P2.5, P5), and so does another pattern after this one: run IDs and text depend only
  on the seed, the position and the run the order picks, so at one seed the patterns start with the same runs, and burst after
  steady on warm pods would inflate its cache hits. Another seed also changes the load, since so few runs leave it a say: steady's
  runs start 27.6 s apart at seed 1 and 15.1-24.3 s at seeds 2-5, up to 1.8 times the load.
- TTFT partly measures the warm-up P2.2b found: the first calls took 35.5 and 49.4 s to first token, both on g8fs5, while b75wv,
  already warm, took 1.2 s; after the restart 38.9 and 33.6 s; the median is 0.8 s. Warm-up does not explain all of it: the second
  replay has a 34.0 s TTFT on g8fs5 when it was warm. The slow calls stay slow after the first token too (94 s for 20 tokens). With
  no clock jumps these shrink: the final replays' largest are 12.4 s, two calls together on warm mrwpq, unexplained, and 1.0 s. P2.5
  and P2.8 need warm-up on every pod, and P2.8 should look for slow calls on warm pods.
- Replays with everything on one pod generated faster than the recording: 181 ms per token (fit on 48 warm calls, R2 0.990)
  against 226.5. The cause is open, and the host's state clouds it: the recording ran at 54 clock jumps per hour. The final two-pod
  replays fit 210 and 226 ms (R2 0.82 and 0.90), near the recording, and the surviving one-pod fan-out fits 192, so by fit two pods
  look slower. By median per token they do not: 179 and 190 against that fan-out's 187. Replays also send no tools or seed, and they
  stream. The fit on the two older steady replays is poor (R2 0.26-0.46), which the slowed host above would explain.
- Parallel branches have identical prompts, so one can reuse the other's prefill: cached 912 against 768, 1056 against 912, 400 against 352.
- Gaps are 2 ms at the median, so as P2.2b expected the gap check mostly measures timer jitter. On this trace it cannot even tell a
  replay with no gaps from a correct one, since every recorded gap is within the tolerance of 0. Until P2.4's traces have longer
  gaps, D9 is proven only by the fake-clock tests.
- Two fresh-eyes reviews ran. The second's fixes were not reviewed again, by the user's choice, against `docs/REVIEW_PROMPT.md`'s
  rule; they were checked by 24 planted bugs and the final steady replays. P2.3 went in as three commits, about 3,500 changed lines
  against PLAN's rough limit of 200 per task.
- What a passing `make replay-compare` means: two different replays with the plan's calls, the trace's tokens, a reply and TTFT on
  every call, and gaps in tolerance. It does not check arrival times, prompt text or pods, so a rate or shared prefix changed in the
  profile since the replays ran still passes: the old `all_types` 192 fan-out passes under `all_types` 0.
- Reported, not changed: `.gitignore` is outside this card, so a leftover `.partial` file shows up as untracked. A failed call's
  reason goes only to stdout, as trace.v1 has no field for it, so P3.4 must save stdout or change the schema to tell a 503 from a
  timeout. A replay's file name has no profile or config hash, so P2.9's comparison needs `OUT=`. Its only position in time is
  `ts_start`, on the wall clock. A second replay running at the same time is not refused: the output file is safe, the load doubles.
  Whether llm-d-router releases its in-flight count when a client cancels a call at the deadline is open, for P3.4 on the cluster.
  Progress lines are plain text rather than D12's JSON, like `make record`'s: a command's progress, not a component's log.
  CLAUDE.md's command list lacks `make replay-compare` and `make record`. On the simulator lines have no pod until P2.7 reads
  `x-inference-pod` in `loadgen/`, which card 30 does not list.
- Lost files: WSL shut down at 19:57, during the first fan-out replay after the fix. Pasting that command block again, its plain
  `mv` renamed the two finished steady replays over the original `-all-types-192` ones, which the block had moved aside on its first
  paste. The steady files were moved back with `mv -n` and are the ones reported here; the original steady replays are gone. Their
  numbers above come from an analysis made before that. Only the original fan-out survives, as
  `...-steady-fan-out-1-all-types-192.jsonl`: 15 of 15 calls on one pod.

### 2026-11-02 · P2.2b · green
Command: `make up PROFILE=real` · `make record EXPERIMENT=experiments/record-demo.yaml` · `wc -l`, a trace.v1 check with
jsonschema, jq and `GET /runs/{id}` over the trace · `make test` · `make lint`
Result:
- Trace: `experiments/results/traces/record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl`, agent image
  `thesis/agent:d86ed436c5b2`, adapter `5554fcf81b09`, auto mode, 13.5 min from the first call to the last reply.
- `make record`: 34 runs, 0 failed, all 34 with more than one step, 78 lines, 78 steps in `GET /runs/{id}`:
  | task_type | runs | lines | runs of 2 steps | runs of 3 steps | prompt tokens | completion tokens |
  |---|---|---|---|---|---|---|
  | csv | 10 | 22 | 8 | 2 | 777-1067 | 18-50 |
  | contracts | 8 | 20 | 4 | 4 | 772-1286 | 31-50 |
  | support | 8 | 20 | 4 | 4 | 818-1277 | 14-142 |
  | arithmetic | 8 | 16 | 8 | 0 | 358-409 | 10-26 |
- Checks outside the recorder: `wc -l` 78; 78 of 78 lines valid trace.v1; 78 lines carry every P2.2a field; no step 1
  with a gap; the sum of steps over the 34 runs' `GET /runs/{id}` is 78; pods `vllm-5c9565db76-rn28q` and `-z8jbl`, the
  two Running pods.
- `make test`: 743 passed, training 22 passed. `make lint` passed, the trace included. These are Claude's runs on the
  final tree, after the review's fixes (tests, write_trace's mode, a comment); the user's own make test and make lint
  passed on the tree before those fixes (734 + 22 on Claude's run of that tree). The fixes touch neither the experiment
  file nor the agent's build context: its hash and tag, recomputed, still give the trace's name.
Notes:
- Tool time is almost nothing: gaps have a median of 2 ms and a maximum of 21 ms, 0.134 s against 748 s of calls (0.018%).
  The tools are deterministic Python (D10) and gap_before_ms also holds the step's Redis save. So D9's gaps change little
  on these traces: a run sends its next call as soon as the last one ends, the heaviest load a run of this length can
  make. P2.3's replay.gap_tolerance_ms will mostly measure jitter. Whether P2.4's AgentProcessBench traces have longer
  gaps is still to be seen.
- The default picker kept all 34 runs on one pod (44 of 44 later steps), with calls split 40 / 38 between the pods. One
  run at a time is the easy case, so P3.3 has to show its gain under concurrent load (P2.5, P3.7), not on this.
- Correction to P2.2a's bound (card 24, "step 2's cached_tokens > step 1's prompt_tokens - 16"): on the same pod a later
  step's cached count can be up to 19 tokens below the previous prompt. 4 of the 44 later steps fall 16-19 below. Every
  count is a multiple of 16, as the KV block predicts, and Qwen3's chat template ends a generation prompt with
  `<think>\n\n</think>\n\n`, 4 tokens on vLLM's /tokenize, which the next step's history does not hold. So the shared
  prefix ends 4 tokens before the prompt does, and 15 more can sit in a partly filled block: 4 + 15 = 19. The 22 rows that
  reused only the previous step fall 4-19 below, exactly that range, and cached == floor((previous - 4) / 16) * 16 holds
  on all 22. P3.3's affinity check should be cached >= previous prompt - 19. P2.3's synthetic prompts extend each step
  strictly, so their shared prefix is 4 tokens longer per step than the agent's: 0 or 16 cached tokens at the block's
  resolution.
- The other 22 later steps cached more than the whole previous prompt (26-357 tokens more), since the same question had
  just run on that pod with the other seed. So a trace's cached_tokens also depend on the recording's order.
- The seeds add almost no variety: 16 of the 17 questions have identical token counts on both seeds. P2.3 is unaffected,
  since its prompts are seeded per run, but P2.4's expected-steps per type rest on about 17 different runs, not 34.
- Time: 13.5 min against the 9 min estimated from the held-out medians, which is in record-demo.yaml's comment. That file
  stays as recorded, since editing it would change the hash in the trace's name; the Makefile comment says 13 min.
- A cache hit saves little time here, since generation takes most of it. A least-squares fit over the 76 lines without
  each pod's first call: duration = 340 ms + 1.19 ms x uncached prompt tokens + 226.5 ms x completion tokens, R2 0.976.
  So generation is about 661 s of the 748 s, and had nothing been cached the fit adds about 69 s (9.2%). This is one run
  at a time with no contention, and a fit, not a measurement of prefill: P2.8 measures cached against uncached directly.
  It means the routing experiments may show hit-rate gains well above their latency gains; P3.3's done-when asserts hit
  rate only, and P2.5's graph has both.
- The first call on each pod is slow even long after the pod started: 34.1 s and 36.8 s, on pods up for about 89 min and
  with 0 cached tokens, against a median of 9.3 s (maximum 13.7 s) for the other step 1 calls of the csv and contracts
  runs. P2.2a saw the same on a fresh pod (25.7 s). It is not prefill: the two calls sit 28.3 s and 24.2 s above the fit
  above. The cause is not established. The pod's only line from the first call is vLLM's warning about per-LoRA
  tokenizers (input_processor.py, 13:39:57 on vllm-5c9565db76-z8jbl), which fits one-time work on the adapter but does
  not prove it. So Ready does not mean fast: P2.8 should time this apart from the model load (D4), and experiments need
  warm-up runs on every pod.
- For P2.3: replay.shared_prefix_tokens (card 26) cannot be one number. Step 1's cached count after another question of
  the same type: csv 752, contracts 736, support 800, arithmetic 336. Across types (only two samples, since other changes
  of type moved to the other pod): contracts to support 256, csv to arithmetic 192. The prompt holds the tools a run's
  files bring, so the prefix is shared by type. The shortest step 1 prompt is 358 tokens.
- For P3.8: 10 lines send x-step 3 with x-expected-steps 2, since every call still carries agent.expected_steps_default.
  Remaining work Σ(expected-steps − step) can go negative for such a run; P2.4's per-type values make it rarer, not
  impossible.
- pytest's importlib mode registers the directory loadgen/ as the module loadgen, which hid an inner package of that name
  (ModuleNotFoundError), so the package is loadgen/load_generator (card 25).
- From the fresh-eyes review: an exclude for check-json was removed before the commit. It rested on Claude misreading
  identify's output: .jsonl has no json tag, so check-json never reads a trace. main and the order of the status and
  Redis reads had no tests; both have them now, each seen catching a deliberate bug (41 of 41 in all). write_trace opens
  with mode x, so a second make record cannot replace the trace after the check at the start. A test checks every
  committed trace against trace.v1. The second review docs/REVIEW_PROMPT.md asks for after fixes was skipped by the
  user's choice, since the fixes were small.
- Reported, not changed: vLLM's default fingerprint vllm-0.31.0-<hash8> would still pass as a pod name; P2.7's simulator
  has to set one. The experiment's hash covers its YAML only, not the attached files, and the trace's name does not cover
  config/real.yaml (system prompt, sampling): after a profile change the refusal says to delete the trace, and recording
  again reuses its name, as go_no_go's results do. A trace holds no run status: this one has no failed runs, but a later
  recording's failed runs could not be told apart from the trace alone; P2.4 decides (the user, 2026-11-02). CLAUDE.md's
  command list lacks make record.

### 2026-11-02 · P2.2a · green
Command: `make test` · `make lint` · `make up PROFILE=real` · `make smoke` ·
`kubectl exec deployment/redis -- redis-cli GET thesis:run:<run-id>:calls`, checked with jq
Result:
- `make test`: 664 passed, training 22 passed. `make lint` passed.
- `make up PROFILE=real`: vLLM with the three new flags (helm revision 5), pods vllm-5c9565db76-6bq8s and -kb852 Ready
  about 4 min after the upgrade; agent image `thesis/agent:cdaa1178225d`.
- `make smoke`: exit 0. `run-26776955616d` ("What is 7 times 8?", seed 7) done in 33 s, 2 steps, both on
  10.42.0.79:8000; prometheus skipped the two old Failed pods (P2.11).
- `thesis:run:run-26776955616d:calls`:
  ```
  {"step":1,"ts_start":1793606515407,"duration_ms":25708,"gap_before_ms":0,"expected_steps":2,"prompt_tokens":358,"completion_tokens":21,"cached_tokens":0,"pod":"vllm-5c9565db76-6bq8s"}
  {"step":2,"ts_start":1793606543527,"duration_ms":4181,"gap_before_ms":11,"expected_steps":2,"prompt_tokens":398,"completion_tokens":10,"cached_tokens":352,"pod":"vllm-5c9565db76-6bq8s"}
  ```
- jq check: steps 1..n, every field on every step, gap_before_ms 0 on step 1: true. Both steps on
  vllm-5c9565db76-6bq8s, one of the two Running pods, at 10.42.0.79 (the Gateway's endpoint). Step 2 cached 352 of
  398 prompt tokens, more than step 1's 358 - 16 (the prompt up to its last, partly filled block). Step 1 cached 0 on
  the fresh pod: with --enable-prompt-tokens-details a miss reads 0.
Notes:
- Taken over 2026-11-02 by the ~/thesis session from the worktree ~/thesis-p2.2a (its diff applied as a patch).
  experiments/finetune_data.py's scripted LLM broke on ChatReply's new fields; they default to None (card 24). A
  fresh-eyes review led to the same-pod cache check, tests for a wall clock set back, null fields and wrong-typed
  cached_tokens / usage / system_fingerprint, a generic _optional, and app.py on the loop's clock (card 24).
- Step 1 took 25.7 s (the Gateway's log: 25636 ms), step 2 4.2 s: the first request on a pod that had just started.
  Likely also why the P2.11 smoke runs took 45 s and 33 s right after pods started, against 8-10 s warm; cause not
  measured. P2.8's cold start should time the first request apart from readiness.
- The two clocks disagreed for real: step 2's ts_start is 28,120 ms after step 1's, but step 1's duration plus the gap
  is 25,719 ms. The Gateway agrees with the monotonic side (step 1 took 25636 ms; by its log times step 2 started about
  18 ms after step 1 ended), and its step 1 start (end time minus duration) is 2.47 s after the agent's ts_start: the
  wall clock moved about 2.4 s more than the monotonic clock during step 1, either a WSL2 clock correction or a paused
  VM (not told apart). Durations and gaps were unaffected, as the new test expects. P2.2b and P2.3 time replays from
  duration_ms and gap_before_ms, never from differences of ts_start.

### 2026-11-02 · P2.11 · green
Command: `make lint` · `make test` · `make vllm-deploy PROFILE=slip` (two edits of a copy of config/real.yaml) ·
`make vllm-deploy PROFILE=real` · `make smoke` with a bare test pod labelled vllm that exits at once
Result:
- Name-only edit (`model.lora.name` thesis-b03ea3894e7c, path and sha256 at `5554fcf81b09`): "vllm-deploy:
  model.lora.name thesis-b03ea3894e7c does not end with 5554fcf81b09, the start of model.lora.sha256", make Error 1,
  no helm output.
- Path edit (name back, path `~/.cache/thesis/finetune/adapters/b03ea3894e7c`): "vllm-deploy: model.lora.path
  /home/kosto/.cache/thesis/finetune/adapters/b03ea3894e7c does not end with 5554fcf81b09, the start of
  model.lora.sha256", make Error 1, no helm output.
- `make vllm-deploy PROFILE=real`: no refusal, helm revision 4, rolled out at once; vllm-fb5697bb5-4mj28 and -6nlrk
  Running, 29 min old, 0 restarts.
- First `make smoke`, exit 0 (the test pod's phase was read right after `kubectl run` and printed Pending):
  ```
  ok pods: vllm 2/2 inference-gateway 1/1 vllm-pool-epp 1/1 redis 1/1 agent 1/1
  ok agent: run run-4636541bb246 (What is 7 times 8?, seed 7) done in 45 s, answer: 7 times 8 is 56.
  ok gateway: 2 lines with run-id run-4636541bb246, steps [1,2], endpoints ["10.42.0.74:8000"]
  ok picker: 2 picks by vllm-pool-epp in the run's window, for 2 steps
  ok redis: thesis:run:run-4636541bb246 status done, step 2
  ok prometheus: vllm:num_requests_running has a sample for ["vllm-fb5697bb5-4mj28","vllm-fb5697bb5-6nlrk"]; not Running, skipped: ["smoke-failed-pod","vllm-fb5697bb5-dpnp6","vllm-fb5697bb5-rj8m2"]
  ```
- The two skipped vLLM pods: phase Failed, reason UnexpectedAdmissionError, 8 h old (STATUS Unknown in kubectl get
  pods). Pods in that state made `make smoke` fail its prometheus check until they were deleted by hand (2026-10-28
  after a reboot, 2026-11-01 after make down + up, P2.1b).
- Rerun with the card's wait line: `kubectl wait pod/smoke-failed-pod --for=jsonpath='{.status.phase}'=Failed
  --timeout=60s` -> "condition met"; `make smoke` exit 0, `run-da8104172257` done in 10 s, 2 steps on
  10.42.0.74:8000, the same pods, gateway, picker, redis and prometheus ok lines (skipped: smoke-failed-pod, -dpnp6,
  -rj8m2).
- `make lint` passed. `make test`: 630 passed, training 22 passed.
Notes:
- The card's phase line was `kubectl get pod ... -o jsonpath='{.status.phase}'`; pasted together with the next lines
  it read the phase too early, so it now waits for Failed (rerun above).
- The model.lora checks moved before the image pull, so a refused config pulls and deploys nothing.
- The first smoke run took 45 s, the rerun 10 s (P2.1b: 8 s); cause not measured.
- pre-commit's trailing-whitespace and end-of-file-fixer skip experiments/results/ from now on; the space lint
  removed from P2.1b's baseline CSV stays removed.

### 2026-11-02 · P2.1b · green
Command: `make test` · `make lint` · `make finetune-data` · `make heldout` (baseline) · `make finetune` ·
`make heldout BASELINE=experiments/results/finetune-heldout-091b52c4a457-323935f068c4-b03ea3894e7c.csv` ·
`make go-no-go EXPERIMENT=experiments/real-go-no-go.yaml` · `make smoke`
Result:
- Data (hash `091b52c4a457`): 12 kinds, 100 training and 14 held-out questions each; 5,200 training examples
  (4.94 M tokens, longest 1533, longest reply 157), 168 held-out questions, 336 runs per pass.
- Baseline: the held-out pass on the P1.7 adapter `b03ea3894e7c`, which config/real.yaml served (card 23): required
  118/168, auto 124/168 (`finetune-heldout-091b52c4a457-323935f068c4-b03ea3894e7c.csv`, 92 min of run time).
- `make finetune`: adapter `5554fcf81b09` (sha256 `5554fcf8…f54629b1`), run `b3db3c532641`, 650 steps, 9826 s of
  training (163.8 min in all, 15.12 s/step), loss 0.8163 -> 2.316e-05 (mean 0.01589), peak 1884 MiB.
- Held-out on `5554fcf81b09` (`finetune-heldout-091b52c4a457-323935f068c4-5554fcf81b09.csv`, 86 min of run time):
  required 118 -> 167/168, 49 fixed, 0 broken, McNemar p = 3.55e-15; auto 124 -> 167/168, 43 fixed, 0 broken,
  p = 2.27e-13. Without off-topic: required 118 -> 153/154 (p = 5.82e-11), auto 124 -> 153/154 (p = 3.73e-09).
  Every kind 14/14 in both modes except clause-text, 13/14. Baseline -> now (required, auto): policy 2, 8 -> 14, 14;
  clause-diff 6, 4 -> 14, 14; clause-text 4, 9 -> 13, 13; reply 11, 8 -> 14, 14; order-status 12, 12 -> 14, 14;
  compare 13, 13 -> 14, 14; off-topic 0, 0 -> 14, 14; summary, group-total, group-mean, two-step, arithmetic 14/14
  before and after.
- Real go/no-go on agent `thesis/agent:323935f068c4` with `5554fcf81b09`
  (`real-go-no-go-2b9641086502-323935f068c4-5554fcf81b09.csv`): required 10/10, auto 10/10, 0 invalid calls;
  "go: auto reaches 9/10; config/real.yaml keeps agent.tool_choice auto".
- `make smoke`: `run-015af7c3e5e4` ("What is 7 times 8?", seed 7) done in 8 s, 2 steps, both on 10.42.0.43:8000;
  pods, agent, gateway, picker, redis and prometheus ok.
- `make lint` passed. `make test`: 628 passed; training 22 collected, every one a dot (the summary line is not in
  the pasted output).
Notes:
- The one held-out miss is `heldout-clause-text-0128` in both modes ("Quote the governing law clause of
  lease-2025.txt."): get_policy(lease-2025, "governing law") instead of extract_clauses, so the quoted clause ("This
  contract is governed by the law of Norway.") is right but the expected call is missing (`run-d828cde1c2f0`
  required, `run-5a78ad53f2b9` auto). Many baseline clause-text misses were the same: get_policy is offered on every
  run with a text file (P2.1a-2), so it reads a contract's sections too. Not changed.
- Review finding: order-status is scored by the status word alone, so a contradictory answer passes. One did, in the
  baseline: `heldout-order-status-0090` (required), shipped order 5512 answered "has been shipped; … the order was
  delivered on the same day", scored correct. The baseline is really 117/168 in required (order-status 11/14) and
  the adapter fixed 50; the error favours the baseline, so the result stands. No adapter answer has it. Matching is
  also not on whole words ("5 working days" is in "15 working days"); no row was hit. Both are tightened at the P4.8
  retrain (user's choice), since changing the scorer now rewrites heldout.jsonl and needs both passes again.
- Review fixes, no data change (train.jsonl and heldout.jsonl regenerate byte for byte): replies() refuses a
  calls_per_reply that does not add up to the calls; is_correct()'s docstring says contains questions skip
  wrong_if_matches; a test comment says the name prefixes, not the years, keep document IDs apart.
- The "before" for the go/no-go, committed by the user's choice:
  `real-go-no-go-2b9641086502-323935f068c4-b03ea3894e7c.csv` (2026-11-01), the old adapter on the P2.1 agent:
  required 8/10 (2 invalid calls), auto 10/10. It ran because
  config/real.yaml first got only the new model.lora.name, with path and sha256 still at `b03ea3894e7c`, so vLLM
  served the old weights under the new name; a second deploy before the full edit gave the same 8/10 and 10/10.
  Follow-up: make vllm-deploy refuses a name or path that does not end with the sha256's first 12 digits (infra card
  after P2.1b).
- go_no_go.py's last line follows the profile now (decided 2026-11-02, card 23): a tie keeps the profile's
  agent.tool_choice, so real keeps auto at 10/10 in both modes; the no-go line no longer names the FINKI server.
- Kept mode on real: auto, agent.tool_choice unchanged. model.lora stays in config/real.yaml until P2.8, which
  measures with and without it. The adapter is stale again after P4.8 and is retrained then (card 23, option a).
- The `make up PROFILE=real` of 2026-11-01 19:20 stopped after vllm-deploy (no gateway, agent or Redis; the cause was
  not kept). WSL restarts then left 6 vLLM pods Failed at GPU admission, deleted by hand before `make smoke`.
  Follow-up: smoke checks only Running pods (infra card).

### 2026-11-01 · P2.1a-3 · go by the user's decision at 8/10 (card's bar: 9/10)
Command: `make test` · `make lint` · `make agent-deploy PROFILE=demo` · `make go-no-go` · UI questions through
`make agent-forward` + `make ui PROFILE=demo`
Result:
- `make test`: 599 passed, training 22 passed. `make lint` passed.
- A first go/no-go ran on the P2.1a-2 image (`c096322c2fdf`) with this card's prompt in the ConfigMap, since the agent
  was not redeployed: required 8/10, auto 6/10. Not this card's result; its CSV
  (`demo-go-no-go-1f9b63bc24ca-c096322c2fdf.csv`) is not committed.
- Go/no-go on agent `thesis/agent:323935f068c4` (`experiments/results/demo-go-no-go-1f9b63bc24ca-323935f068c4.csv`):
  required 8/10 correct, 0 invalid calls; auto 8/10 correct, 0 invalid calls. P1.6c had required 10/10, auto 8/10.
- All four misses are two-step, both seeds, both modes; summary, group-total, group-mean and compare are 16/16.
  Required names Widget with a wrong revenue ($2,224.00, expected $15,116.50). Auto answers $15,116.50 but is scored
  wrong, so it lacks the scorer's group_by(product, units, sum) call: the right number by another route. The runs'
  calls were not opened.
- UI on the same image: "what is 8 to the power of 3?" -> calculator("8*8*8"), 512, 2 steps. "what is the square root
  of 128?" -> sqrt(128) (pattern refused), 128**0.5 (evaluator refused), then 128**0.5 again at every step, answered
  by the repeat guard each time, until agent.max_steps: failed.
Notes:
- The user called it a go at 8/10 in required (the kept mode, D10), below card 22's 9/10. Recorded as such.
- The calculator plays no part in two-step (CSV tools only). Since P1.6c the prompt gained the support and contracts
  clauses and the new calculator clause, and the repeat guard came in (P2.1a-2); which of them cost two-step is not
  measured. P4.7 measures the scenarios after P4.8.
- Follow-up, not changed: a repeat of a call whose result was an error gets "its result is above; use that result",
  and repeats do not count toward agent.max_invalid_calls, so such a run ends only at agent.max_steps. Counting
  repeats as invalid, or another message when the earlier result was an error, would end it sooner.
- Card 22's two checks on `323935f068c4` (POST /runs, seed 7): "what is 2x2x2?" (`run-45b690d5a295`) ->
  calculator("2x2x2"), "The result of 2x2x2 is 8."; "what is 7 times 8 plus 3?" (`run-32732b43488e`) ->
  calculator("7*8+3"), "59". One calculator call each. Earlier, on the old image, the UI showed multiply(2, 2) -> 4.
- `make smoke` on `323935f068c4`: passed (exit 0).

### 2026-11-01 · P2.1a-2 · green (fourth demo run, after two red and one stuck)
Command: `make test` · `make lint` · `make agent-deploy PROFILE=demo` · `make agent-forward` · a POST /runs with
`agent/tests/fixtures/orders.csv` (type csv) and `policies.txt` (type text), seed 7, "Order 1042 arrived damaged,
what can the customer do?" · GET /runs/{id}, after each change below
Result:
- `make test`: 554 passed, training 22 passed. `make lint` passed.
- Run 1 (`run-b9df47bce692`, every tool offered, support clause after the CSV clause): failed, 3 invalid calls,
  group_by(orders, by status, value "damaged", row_count) three times. "damaged" is no column; the runtime's check
  rejected it with the allowed columns and the model repeated it unchanged.
- Run 2 (`run-7cbd121d0b5a`, prompt reworded: support clause first, "For other questions about the attached CSV
  files"): the same three calls, word for word. The prompt was live: the contracts question on it
  (`run-1861021aaa96`) went straight to diff_clauses and was done in 2 steps (4 before), answer correct. Rule 8:
  stopped, cause brought to the user.
- Fix (user's choice): a CSV with an order_id column gets no CSV tools until P4.8.
- Run 3 (`run-e33d8fe3cf2c`): get_order(1042), get_policy(damaged items), draft_reply, then draft_reply again with
  the same arguments at every step; stopped reading at step 6, the reply right each time.
- Fix (user's choice, both together): draft_reply's reply under "summary" and its description saying the reply is
  the final answer; loop.py answers a call identical to an earlier one in the run with a tool error pointing to the
  final answer instead of running it again.
- Run 4 (`run-8a35b723d2b3`): done in 6 steps: get_order, get_policy, draft_reply, calculator(divide, 14, 24),
  draft_reply (answered by the repeat guard), final_answer. Answer: "Dear Marko Ilievski, about your order 1042
  (item: Office chair, status: delivered, ordered_on: 2026-10-10, delivered_on: 2026-10-15, amount: 189.00): our
  Damaged items policy says: "An item that arrived damaged can be returned or replaced free of charge within 14 days
  of delivery. Send a photo of the damage with the request." Counted from the delivery on 2026-10-15, the 14 days end
  on 2026-10-29."
Notes:
- The repeat guard ended the run: the summary key and description alone did not stop the second draft_reply
  (step 5). Since both changes went in together, run 4 cannot say whether the summary key helped at all.
- calculator(divide, 14, 24) means nothing: the calculator is still offered on runs with files; P2.1a-3 removes it.
- Required mode does not keep argument values inside their enums: "damaged" was generated in demo mode
  (tool_choice required). vLLM v0.31.0 constrains required mode with the hermes structural tag (P1.7 step 1 notes),
  and the P1.7 baseline already had 24 invalid calls in required mode. D10's "a malformed call is impossible" holds
  for the call's format, not for its values; the runtime's check is what catches them. Reported, not changed.
- What the 1.7B does with a question is decided by which tools a file brings more than by the prompt's order. P4.8's
  scenario step replaces the order_id rule.

### 2026-11-01 · P2.1a-1 · green
Command: `make test` · `make lint` · `make up PROFILE=demo` · `make agent-forward` · a POST /runs with the two fixture
contracts (`agent/tests/fixtures/contract-2025.txt`, `contract-2026.txt`, `type: text`, seed 7) and "What changed
in the termination clause between the two contracts?" · GET /runs/{id}
Result:
- `make test`: 523 passed, training 22 passed (run as two halves: with the demo cluster up, the full target was
  killed with exit 137, out of memory). `make lint` passed.
- First run (`run-7204dbeaf8df`), done in 4 steps: read_document(contract-2025), read_document(contract-2026),
  diff_clauses(contract-2025, contract-2026, termination), final_answer. Answer: "The termination clause has 1 change
  between contract-2025 and contract-2026." The right tools, but the model copied diff_clauses' summary, which only
  counted the changes; the 30 -> 60 days was in the result's changes list and not in the answer.
- Fix (with the user): diff_clauses returns no changes list; its summary holds every shown change.
- Second run (`run-1d65bce23438`, agent `thesis/agent:57ce6de40346`), done in the same 4 steps. Answer: "The
  termination clause changed from contract-2025 to contract-2026: "this agreement with 30 days' written notice."
  became "this agreement with 60 days' written notice."."
Notes: the 1.7B read the two contracts in two steps both times, not with two calls in one reply; the runtime runs
several calls of one reply as one step (unit test), and the parallel LLM calls P3.6 needs come from the replayer's
fan-out (P2.3). The answer copies the tool's sentence word for word, as with compare in P1.6c: a tool's summary is
what the demo model answers with, so it has to carry the facts. Whether the model gets the clause right across
questions is P4.7's measure. The P1.7 adapter on the real profile is stale from here (tools and prompt changed);
P2.1b retrains it.

### 2026-10-31 · P1.7 step 3 · green
Command: `make test` · `make lint` · `make down` · `make finetune` · `make up PROFILE=real` ·
`make heldout BASELINE=experiments/results/finetune-heldout-d384c6bd2721-99df636ae969.csv` ·
`make go-no-go EXPERIMENT=experiments/real-go-no-go.yaml`; on 2026-11-01 `make smoke` · `make down` · `make finetune`
(the resume check)
Result:
- `make finetune` (experiments/finetune-train.yaml, run `13105c818230`, data `6e3ac3eda752`): 3196 examples, 400
  optimizer steps (2 epochs x 200), 7246 s of training (2 h 01 min, 18.1 s per step), 125.7 min with loading and
  tokenizing. Loss 1.044 at step 1, 0.03 by epoch 0.15, under 1e-3 from epoch 0.55, 2.832e-05 at the end; mean 0.0224.
  Peak reserved by torch 1652 MiB. Adapter `~/.cache/thesis/finetune/adapters/b03ea3894e7c`, sha256
  `b03ea3894e7c…b9ef`. Loss per log step in `~/.cache/thesis/finetune/runs/13105c818230/loss.jsonl`.
- `make test`: 468 passed, training 22 passed. `make lint` passed.
- Held-out with the adapter (agent `thesis/agent:f1dd57208cf9`, CSV
  `experiments/results/finetune-heldout-6e3ac3eda752-f1dd57208cf9-b03ea3894e7c.csv`), against the baseline:

  | kind | required | auto |
  |---|---|---|
  | summary | 13 -> 14/14 | 13 -> 14/14 |
  | group-total | 11 -> 14/14 | 8 -> 14/14 |
  | group-mean | 7 -> 14/14 | 7 -> 14/14 |
  | compare | 2 -> 14/14 | 2 -> 14/14 |
  | two-step | 0 -> 13/14 | 0 -> 14/14 |
  | arithmetic | 0 -> 14/14 | 0 -> 14/14 |
  | off-topic | 0 -> 14/14 | 0 -> 14/14 |
  | all | 33 -> 97/98 | 30 -> 98/98 |
  | without off-topic | 33 -> 83/84 | 30 -> 84/84 |

  Paired by question: required 64 fixed, 0 broken, McNemar p = 1.08e-19 (without off-topic 50 fixed, p = 1.78e-15);
  auto 68 fixed, 0 broken, p = 6.78e-21 (54 fixed, p = 1.11e-16). Invalid calls 24 -> 1 (required), 15 -> 0 (auto).
- Real go/no-go with the adapter (`experiments/results/real-go-no-go-2b9641086502-f1dd57208cf9-b03ea3894e7c.csv`):
  required 10/10, auto 10/10, 0 invalid calls, against 4/10 and 4/10 for the plain 0.6B and 10/10, 8/10 for the
  demo's 1.7B (P1.6 rerun). Auto two-step, which the 1.7B gets wrong in both seeds, is right in both.
- `make smoke` on the adapter: exits 0; run `run-e34304c21a56` ("What is 7 times 8?") done in 54 s, answer "7 times 8
  is 56.", 2 gateway lines and 2 picks for steps [1,2], both on 10.42.0.13:8000.
- Resume check, `make finetune` again on the finished run: "resuming from .../runs/13105c818230/checkpoint-400", 0
  steps trained, loss 1.044 -> 2.832e-05 read back from the checkpoint, adapter
  `~/.cache/thesis/finetune/adapters/b03ea3894e7c` with the same sha256, peak 1666 MiB.
Notes:
- vLLM applies the adapter's weights, which step 1 could not show: the answers changed on 64-68 of 98 questions.
- The one miss, required heldout-two-step-0061: the model found the route with the highest total weight, then
  divided that total by 4 itself ("15040.23 / 4 = 3760.06") instead of calling group_by with mean; the pass's only
  invalid call was in that run.
- Kept mode: auto. The card's rule (decided 2026-10-30 with the user) is the mode the adapter does best in: auto 98/98
  against required 97/98 on held-out, 10/10 each on the go/no-go. config/real.yaml already has auto, so it does not
  change. go-no-go printed "config/real.yaml keeps agent.tool_choice required": its decide() is the demo's rule
  (required on a tie, D10), and it names the profile's file without reading the profile's mode. Reported, not fixed.
- Runs take about twice as long on the adapter at the same number of steps: held-out median 8.1 -> 16.1 s (required)
  and 7.0 -> 11.1 s (auto); go/no-go 11.6 -> 18.1 s and 6.0 -> 15.1 s; mean steps per run 2.46/1.88 -> 2.28/2.28.
  Each LLM call is slower with --enable-lora; not measured per call. This matters for P2.8, which calibrates the
  simulator on the real profile. Decided 2026-11-01 with the user: model.lora stays in config/real.yaml until P2.8,
  which measures time to first token and time per output token with and without the adapter, then picks the profile
  to calibrate on.
- The smoke run took 54 s for 2 steps, against 14 s on the plain 0.6B (P1.5) and the held-out median of 11-16 s; one
  run, cause not measured.
- The resume check is not the card's Ctrl+C mid-run (decided 2026-11-01 with the user: that needs a new run
  directory and another 2 h). It shows that a run with checkpoints resumes from the newest one and that the same
  weights publish to the same adapter directory; resuming mid-epoch is covered only by transformers' Trainer.
- Training took 2 h, not the planned 1.5 h: 1.2 s per example against the spike's ~1 s estimate. The loss went from
  7.9e-05 at epoch 0.85 to 2.8e-05 at the end; with no epoch-1 adapter kept, whether it helped is not known.
- The held-out set shares the generator's phrasings; the go/no-go's hand-written questions on sales.csv are the test
  of unseen wording, and they are 20/20.
- The adapter is tied to today's system prompt, tool definitions and result format: P1.6f and P2.1 change them, and
  then make finetune-data and make finetune have to run again.

### 2026-10-30 · P1.7 step 2 · green (baselines in)
Command: `make up` · `make heldout` (data hash `d384c6bd2721`) · `make test` · `make lint` · `make finetune-data`
(data hash `6e3ac3eda752`) · `sha256sum ~/.cache/thesis/finetune/data/*/heldout.jsonl` ·
`make go-no-go EXPERIMENT=experiments/real-go-no-go.yaml` (on 2026-10-29, same agent `thesis/agent:99df636ae969`)
Result:
- `make finetune-data` (experiments/finetune-data.yaml, data hash `6e3ac3eda752`): 3196 training examples from 100
  trajectories per kind and mode (summary, group-total, group-mean, compare 200 examples each; two-step 300; arithmetic
  398; off-topic 100), 98 held-out questions (14 per kind). sha256 of train.jsonl + heldout.jsonl
  `a00a8e7650e4…03ae`. `render.py`: 2,856,844 tokens in all, length p50 926, p95 1156, max 1297; reply max 54 tokens;
  0 over model.max_model_len 4096.
- heldout.jsonl is byte-identical under `d384c6bd2721` and `6e3ac3eda752` (sha256 `f919af5aab35…7995`): the held-out
  split has its own seed, so the baseline scored on `d384c6bd2721` holds for the data step 3 trains on.
- Held-out baseline, plain Qwen3-0.6B-GPTQ-Int8, real profile, CSV
  `experiments/results/finetune-heldout-d384c6bd2721-99df636ae969.csv`, 196 rows:

  | kind | required | auto |
  |---|---|---|
  | summary | 13/14 | 13/14 |
  | group-total | 11/14 | 8/14 |
  | group-mean | 7/14 | 7/14 |
  | compare | 2/14 | 2/14 |
  | two-step | 0/14 | 0/14 |
  | arithmetic | 0/14 | 0/14 |
  | off-topic | 0/14 | 0/14 |
  | all | 33/98 | 30/98 |

- Real go/no-go baseline (`experiments/results/real-go-no-go-2b9641086502-99df636ae969.csv`): required 4/10 correct,
  13 invalid calls; auto 4/10 correct, 7 invalid calls.
- `make test`: 449 passed, training 9 passed. `make lint`: mypy failed on test_heldout.py's FailingLLM
  (`raise self._failure` after a branch mypy cannot narrow); it now raises an exception first and a response's status
  error after, and a success status fails the test.
Notes:
- The zeros are the model, not the scorer (required rows read back): arithmetic answers a wrong number, in 11 of 14
  runs after a single calculator call for a 2-4 operation question (e.g. 3.17 for 209); two-step answers the first
  step's group or value, not the second; off-topic answers the question itself (14 of 14 runs one step, no tool), as
  expected: the fixed sentence is only in the training data. Compare mostly subtracts the wrong totals or answers in
  prose.
- Training cut from 400 to 100 questions per kind on 2026-10-30, with the user, after this baseline started: ~3,200
  examples at p50 926 tokens, ~45 min per epoch at step 1's speed.
- Held-out cut from 100 to 14 per kind on 2026-10-30, with the user: 1400 runs took 4-6 h per pass, and the set is
  scored twice. ±10 points per mode at 95% still shows a clear gain; per kind (±26) it is only a hint. Step 3 compares
  against this baseline with BASELINE=<csv>, paired by question (McNemar).
- The first 1400-run baseline (data hash `0b29e29f172e`) stopped twice: Windows sleep on 2026-10-29 at 287 runs (a
  re-run on a cluster that wasn't ready wrote over its CSV; heldout.py now appends and resumes), and at 321 runs on
  heldout-compare-0322's 400 (now a wrong run: only a 5xx or no connection stops the run). Its rows are dropped. Partial
  required-mode scores at 100 per kind: summary 60/100, group-total 73/100, group-mean 66/100, compare 6/21.
- An earlier `make heldout` killed before its first row left a 0-byte CSV, which the next run refused (no header).
  result_csv now flushes the header when it creates the file (test_result_csv_has_the_header_on_disk_before_the_first_row).

### 2026-10-29 · P1.7 step 1, second run · green (go for step 2)
Command: `make finetune-spike` · `make lora-check` · `make lint`, cluster down; after the first run's two changes
(logits for the reply only, lora-check comparing base and adapter), decided with the user on 2026-10-28
Result:
- Tokenizer files the same as in the first run. 796 MiB of the GPU used, 3300 MiB free before loading.
- Length 2048: torch peak 1782 MiB, whole GPU 2612 MiB, 1.50 s/step. Length 4096: torch peak 2118 MiB, whole GPU
  2948 MiB, 3.26 s/step. Both fit, with 1182 MiB left at 4096. LoRA rank 16: 10,092,544 trainable parameters.
- Spike adapter: `~/.cache/thesis/finetune/spike-adapter`, sha256 `fae2e213e325…7106`.
- `make lora-check` OK. GPU memory used: 1923 MiB without the adapter, 1954 MiB with it, 31 MiB more (21 MiB in the
  first run); two real pods with the adapter stay inside the ~160 MiB they leave free.
- "What is 7 times 8?" with the agent's calculator tool, temperature 0: base and adapter both called
  calculator(multiply, 7, 8), in auto mode and in required mode. In required mode the content is "" and the call
  comes back parsed by the hermes parser, which shows the model writes `<tool_call>` tags there too, as the vLLM code
  says (first run's notes). Step 2 trains both modes on the same turn format.
- `make lint`: `mypy-training` failed on `loss.backward()`, which torch leaves without type annotations; replaced by
  the annotated `torch.autograd.backward(loss)`, the same call. `make lint` then passed, `mypy (strict, training)`
  included.
Notes:
- Go for step 2: training fits at max_model_len, vLLM v0.31.0 serves a LoRA adapter on the GPTQ-Int8 0.6B for 21-31
  MiB, and the template vLLM serves is the base model's.
- Not shown: that vLLM applies the adapter's weights. The answers were identical, and the spike's loss was 0.000 from
  the first step (the last 64 tokens of a repeated text are predictable), so its adapter barely moved from zero.
  Step 3's held-out run on the trained adapter shows it; identical answers there would point to this.
- Speed for step 2's training size: ~1.5 s per 2048-token example and 3.3 s per 4096-token one, at batch size 1. The
  real examples' lengths come from step 2's generator.
- The spike took longer than its 1-hour box, over two sessions: the downloads (~6.5 GB) and the rerun after the
  first run's failure.

### 2026-10-28 · P1.7 step 1, first run · red (training does not fit as measured)
Command: `make down` · `uv sync --locked --project training` · `make finetune-spike` · `make lora-check`
Result:
- Tokenizer: `tokenizer.json` (sha256 `aeb13307…dae4`) and `tokenizer_config.json`, which holds the chat template
  (`d5d09f07…8101`), are the same for Qwen/Qwen3-0.6B `c1899de2` and Qwen3-0.6B-GPTQ-Int8 `d3f20e7e`.
- LoRA rank 16 on all 7 projections: 10,092,544 trainable parameters. 796 MiB of the GPU used before loading.
- Length 2048: torch reserved 5940 MiB at peak, more than the 4096 MiB GPU, so it ran out of shared system memory
  (WSL's sysmem fallback); 2.29 s/step. Length 4096: 10688 MiB reserved, an OOM warning, 22.48 s/step. Neither fits.
- Cause: the logits over the whole vocabulary. The failed allocation was 2,489,319,424 bytes = 4096 tokens x 151,936
  x 4 bytes, the fp32 logits of one sample; at 2048 they are 1187 MiB, plus their bf16 copy and both gradients.
- `make lora-check`: vLLM v0.31.0 loads the spike's adapter on the GPTQ-Int8 model and answers as `thesis-spike`.
  GPU memory used 1891 MiB without the adapter and 1912 MiB with it: 21 MiB per pod, well inside the ~160 MiB two real
  pods leave. Auto mode answered "The result is 56." without a tool call, so the check failed before required mode.
- `make lint`: `mypy-training` failed on `enable_input_require_grads` (untyped).
Notes:
- vLLM v0.31.0 code, read in the pinned image: the hermes parser has `structural_tag_model = "hermes"`
  (`tool_parsers/hermes_tool_parser.py:35`), which sets `supports_required_and_named = False`
  (`tool_parsers/abstract_tool_parser.py:67-70`), so required mode is constrained by a hermes structural tag
  (`parser/abstract_parser.py:519-549`) and parsed like auto (`:384`). The model writes `<tool_call>` tags in both
  modes, not a JSON list. Not yet seen in a response: lora-check stopped before its required-mode call.
- The spike's adapter is not neutral: 10 steps at lr 1e-4 on repeated text. Expecting the base model's tool call from
  it was the check's mistake; the request alone does not show whether base or adapter skipped the tool.
- `enable_input_require_grads` removed: transformers 5.19 checkpoints non-reentrantly by default
  (`modeling_utils.py:3137`), which does not need it.
- The spike measured "fits" only by printing numbers; peak reserved above the free VRAM should be a failure.

### 2026-10-28 · P1.6 rerun for Gate 1 · green (go)
Command: `make agent-deploy PROFILE=demo` · `kubectl delete pod -l app.kubernetes.io/name=vllm
--field-selector=status.phase=Failed` · `make go-no-go`
Result:
- `make go-no-go`: 20 rows in `experiments/results/demo-go-no-go-1f9b63bc24ca-99df636ae969.csv` (agent
  `thesis/agent:99df636ae969`). required 10/10 correct, 0 invalid calls; auto 8/10 correct, 1 invalid call. Go;
  config/demo.yaml keeps agent.tool_choice required.
- All 20 answers are word for word the ones of P1.6c (`0d822ba16e8e`), auto two-step wrong in both seeds as there.
Notes:
- The tag is not P1.6c's: P1.4b-1 added the events endpoint (app.py, config.py, events_poll_seconds). loop.py,
  tools.py, csv_tools.py, llm.py and the system prompt are unchanged since P1.6c, so the model got the same requests;
  this run ties the result to the code Gate 1 is tagged on.
- The first `make smoke` after the deploy failed at prometheus: `vllm-9d9d6db6f-778ld` was left in phase Failed
  (UnexpectedAdmissionError) after k3s restarted, the second time this happened. Open: a card so a restart costs no
  manual delete.

### 2026-10-28 · P1.6d + P1.6e · red (no-go, both undone)
Command: `make up PROFILE=demo` · `make go-no-go`, twice: with P1.6d and P1.6e, then with P1.6d alone
Result:
- P1.6d + P1.6e (agent `thesis/agent:a7184f58c573`, prompt with the off-topic sentence): required 7/10, auto 7/10,
  0 invalid calls. No-go.
- P1.6d alone (same agent, sentence taken out as card 15e says): required 8/10, auto 6/10, 0 invalid calls. No-go.
  `demo-go-no-go-1f9b63bc24ca-a7184f58c573.csv` is on branch `wip/p1.6d-calculator-expression` with the code that
  made it; the first run's CSV had the same name and was overwritten.
- group-total ("total revenue of the North region"): 1/4 in the first run, 0/4 in the second, against 4/4 in P1.6c.
  In every run read from Redis the model called group_by(region, revenue, sum), got North's 14351.1, then called
  calculator("14351.1 + 11341.8 + 10622.2 + 9551.0") and answered 45,866.10.
- two-step, auto: 0/2 in both runs, as in P1.6c. The answer reads right ("Widget ... 15116.5"), but the model
  counted rows per product (all tie at 12) and compared only Widget and Gadget, so Widget is a guess and the
  scorer's expected group_by(product, units, sum) call is missing. required two-step lost one seed in the first run
  (Gizmo, the most revenue, named as the most units).
Notes:
- The whole-expression calculator is the cause, not the off-topic sentence: without the sentence group-total still
  fails every run. With one operation per call (P1.6c) the 1.7B never added the groups; an expression invites it.
- Rule 8: two no-gos with this calculator, so P1.6d and P1.6e are undone and Gate 1 stands on P1.6c (required
  10/10). Decided with the user: next, P1.6f offers the calculator only on runs without files, on a green gate, and
  starts from the saved branch. The 0.6B keeps answering 2x2x2 with 4 until then.
- go_no_go.py names its result after the experiment file and the agent image tag, not the profile, so a run with
  only a prompt change overwrites the previous result. Reported, not fixed (outside these cards).

### 2026-10-28 · P1.5 · green
Command: on the demo profile `make smoke` · `kubectl scale deployment/agent --replicas=0` · `make smoke` ·
`kubectl scale deployment/agent --replicas=1` · `make smoke` · `make agent-check`; then `make down && make up` (real)
· `make smoke` · `make agent-check`
Result:
- Demo (one Qwen3-1.7B pod, agent `thesis/agent:0d822ba16e8e`, tool_choice required), first run:
  ```
  ok pods: vllm 1/1 inference-gateway 1/1 vllm-pool-epp 1/1 redis 1/1 agent 1/1
  ok agent: run run-5b9078ffd703 (What is 7 times 8?, seed 7) done in 4 s, answer: 56
  ok gateway: 2 lines with run-id run-5b9078ffd703, steps [1,2], endpoints ["10.42.0.12:8000"]
  ok picker: 2 picks by vllm-pool-epp in the run's window, for 2 steps
  ok redis: thesis:run:run-5b9078ffd703 status done, step 2
  ok prometheus: vllm:num_requests_running has a sample for ["vllm-9d9d6db6f-x6thv"]
  ```
- Agent scaled to 0: `FAIL pods: agent: all of at least 1 pod Ready expected, 0 of 0 Ready`, exit 1.
- Agent back at 1: six ok lines again, run `run-deefd9f23c4d`, 4 s, same steps and pod.
- `make agent-check` OK on demo (run `run-380acb21befb`: calculator multiply(7,8), then final_answer "56") with the
  shared calls from `infra/checks/agent_calls.sh`.
- Real, smoke run right after `make up` exit 0 (two Qwen3-0.6B pods, same agent image):
  ```
  ok pods: vllm 2/2 inference-gateway 1/1 vllm-pool-epp 1/1 redis 1/1 agent 1/1
  ok agent: run run-ffd2dab2050d (What is 7 times 8?, seed 7) done in 14 s, answer: The result is 56.
  ok gateway: 2 lines with run-id run-ffd2dab2050d, steps [1,2], endpoints ["10.42.0.13:8000"]
  ok picker: 2 picks by vllm-pool-epp in the run's window, for 2 steps
  ok redis: thesis:run:run-ffd2dab2050d status done, step 2
  ok prometheus: vllm:num_requests_running has a sample for ["vllm-5fff45f674-4687g","vllm-5fff45f674-d5rrf"]
  ```
- `make agent-check` OK on real (run `run-a5ad24db34bf`, 2 steps on 10.42.0.13, text answer "The result is 56.").
Notes:
- Both D10 modes pass the same check: required ends with final_answer ("56"), auto answers in text. Both take 2 steps,
  so expected-steps 2 holds in both.
- The real smoke ran 12 s after the agent pod started and right after the vLLM pods loaded: the run took 14 s against
  4 s on the warm demo cluster, as in P1.3b's first run on fresh pods (26.8 s). It went to the agent pod's IP, so the
  Service lag seen in P1.6c could not hit it.
- The picker check is linked to the run by time only: the EPP's "Request handled" line has its own x-request-id but
  no run-id, and the access log does not carry that id.
- The pods check compares each Deployment's Ready pods with its own `spec.replicas`, not with the profile in
  `config/`: that works on any profile without a PROFILE argument and stays right once KEDA sets the vllm count, but a
  vllm Deployment scaled down by hand would pass.
- After the review, every failure path prints its "FAIL <layer>" line (vllm pod list, a run state that is not JSON,
  EPP log read and parse reported apart); the passing output above is unchanged.
- The cluster is on the real profile again.

### 2026-10-28 · P1.6c · green (go)
Command: `make test` · `make lint` · `make agent-deploy PROFILE=demo` · `make go-no-go`
Result:
- `make go-no-go`: 20 rows in `experiments/results/demo-go-no-go-1f9b63bc24ca-0d822ba16e8e.csv` (experiment file
  `1f9b63bc24ca`, agent `thesis/agent:0d822ba16e8e`). required 10/10 correct, 0 invalid calls; auto 8/10 correct,
  1 invalid call (compare seed 2, corrected on the retry). Decision: go; config/demo.yaml keeps agent.tool_choice
  required.
- Against P1.6b (required 6/10, auto 7/10), same questions and seeds:
  - compare: required 0/2 -> 2/2, auto 1/2 -> 2/2. required copies the new summary sentence word for word ("North's
    sum of revenue (14351.1) is 3728.9 higher than South's (10622.2), 35.1% more."); auto words it itself, with the
    right direction.
  - two-step: required 0/2 -> 2/2: it now sums units per product ("Widget ... 617.0 units ... 15116.5"). auto
    0/2 -> 0/2: both runs name Gizmo with Widget's 617 units and Gizmo's revenue, so the products from the two
    group_by results get mixed up.
  - summary, group-total, group-mean: 12/12 as before. Numbers come rounded ("17.83", not "17.833333333333332").
- required takes 3-6 steps and 6-14 s per run, auto 2-5 steps and 3-24 s.
Notes:
- The tools changed, not the model or the prompt: compare returns higher, lower, a non-negative difference,
  percent_more and a summary sentence; agg count is now row_count; CSV tool numbers are rounded to
  agent.result_decimals (2).
- Three reruns failed before this one, none of them on the model:
  - The first crashed in the switch to auto with a connect timeout. The auto pod was Ready, but the node's Service
    rules still led to the deleted required pod more than 10 s after it was gone (k3s API stalls, as in P0.7, P1.1,
    P1.2). Second failure of reaching the restarted agent through the Service, so per rule 8 it stopped there; decided
    with the user: go_no_go.py sends the runs to the one Ready agent pod's IP.
  - The same run scored both required compare answers wrong: wrong_if_matches read the "35.1% more" after South's
    value as a claim about South. It now needs the comparative's "than" within two words. That sentence is a test.
  - The next one stopped at the first switch: ready_pod_ip compared whole condition dicts, and real conditions carry
    timestamps too, so no pod was Ready. The test pods now carry them.
- Result files now carry the agent image tag (`<experiment>-<yaml hash>-<agent tag>.csv`), so P1.6b's
  `demo-go-no-go-9440b109d5fa.csv` belongs to agent `c61bb199c383`.
- The demo should use required: auto's remaining failure is a two-step question, the kind of multi-call question the
  demo needs most. Open for P2: auto stays the mode of the real and sim profiles (D10), where traffic shape matters
  more than answers.

### 2026-10-28 · P1.6b · red (no-go)
Command: `make down && make up PROFILE=demo` · `make test` · `make go-no-go`, run three times (see notes)
Result:
- `make up PROFILE=demo` exit 0: `thesis/agent:c61bb199c383` imported in 8.3 s; redis and agent Running 1/1 next to
  one vLLM pod (Qwen3-1.7B-GPTQ-Int8).
- `make test`: 311 passed, 32 of them the new scorer tests.
- Final scorer, `experiments/results/demo-go-no-go-9440b109d5fa.csv`, 20 rows: required 6/10 correct, 0 invalid
  calls; auto 7/10 correct, 1 invalid call (group-mean seed 1, corrected on the retry). Decision: no-go.
  config/demo.yaml keeps agent.tool_choice required.
- Per question, both modes and seeds together: summary 4/4, group-total 4/4, group-mean 4/4, compare 1/4,
  two-step 0/4.
- required takes 3-6 steps and 6-15 s per run, auto 2-4 steps and 3-13 s: in required mode the model opens with
  csv_summary on most questions and needs one more call for final_answer.
Notes:
- First run, scored as planned (a number in the answer within 1%, the expected tool ran): required 10/10,
  auto 9/10, "go". The run histories in Redis showed it passed wrong runs:
  - compare: all 4 runs called `compare(left=North, right=South)`, got `difference: -3728.9`, and 3 answered
    "North made $3,728.90 less than South". The scorer read numbers without their sign.
  - two-step: all 4 runs ranked the products with `group_by(agg=count)`. Every product has 12 rows, so the top
    product was a guess: Widget 3 times (right by luck, one of them also saying Widget sold "a total of 12" units,
    not 617), Gadget once.
- Scorer strengthened (decided 2026-10-28): `expected_calls` names a call's arguments where the number does not
  prove the call (two-step: group_by by product, value units, agg sum), and `wrong_if_matches` rejects a compare
  answer that says North is lower. Re-scoring the first run's 20 histories from Redis gave 6/10 and 7/10, the
  same as by hand.
- Second run crashed when it switched to auto: its first auto run ended with final_answer, so the old required
  pod answered it. That pod was still terminating after `kubectl rollout status` returned, and the script's pooled
  keep-alive connection still led to it; the next poll was refused when it exited. Fixed in go_no_go.py: the
  switch waits until the old pod is deleted, each mode gets a new HTTP client, and every done run is checked for
  the mode that served it (final_answer only in required). The first run was clean: all 10 required runs ended
  with final_answer, none of the 10 auto runs did.
- Third run, before that fix: the same 20 answers word for word as the first run. With fixed seeds the demo model
  is deterministic here, so a tool change can be compared run for run.
- The failures are in how two tool results read, not in the model: the three single-aggregate kinds are 12/12 in
  both modes. Next (decided 2026-10-28): a card that makes compare name the higher group with a positive
  difference and gives group_by's row count a name that cannot pass for a total, then `make go-no-go` on the
  unchanged experiment. Fine-tuning was considered and dropped: GPTQ weights cannot be trained, QLoRA on the FP16
  1.7B barely fits the ~3.2 GiB of free VRAM, it needs new dependencies and training data, and it does not target
  the two misreadings.
- Answers copy floats as they come ("17.833333333333332"). Not wrong, but worth rounding in the tool results in the
  same card.

### 2026-10-28 · P1.3b · green
Command: `make down && make up` on a fresh cluster (the night left 4 vLLM pods Unknown and 2 never Ready after k3s
restarted), then `make agent-check`
Result:
- `make up` (2026-10-27) exit 0: deploy-agent built `thesis/agent:1027ead9b883` (~220 MB), imported it into k3s in
  9.3 s, and rolled out redis and agent; both Running 1/1 next to the two vLLM pods, the Gateway and the EPP.
- `make agent-check` OK: run `run-bcf08c6a0ef0`, "What is 7 times 8?" seed 7. Step 1 `calculator multiply(7,8)` ->
  56, step 2 "The result is 56.". Access log: `{"step":"1","expected-steps":"2","endpoint":"10.42.0.12:8000"}` and
  step 2 on the same pod. Redis `thesis:run:run-bcf08c6a0ef0`: status done, step 2, expected_steps 2,
  invalid_calls 0, 5 messages.
Notes:
- expected-steps 2 in the access log is the profile's new value, so a ConfigMap change reaches the pod through the
  thesis/config-hash restart (P1.6b switches the D10 mode this way).
- The run took 26.8 s (created_ms to updated_ms), against ~3 s per run on the warm cluster the day before: most likely
  the first requests to freshly loaded pods. RUN_TIMEOUT_SECONDS (300 s) covers it; watch it in P1.5.
- Both steps landed on one pod, as on 2026-10-27: nothing pins a run before P3.3, so this is the default picker's
  prefix-cache scorer matching the shared prompt.

### 2026-10-27 · P1.3b model check · done
Command: `make down && make up` (exit 0; redis and agent Running 1/1 next to both vLLM pods), then `make agent-check`
with the first task "What is 7 times 8 plus 3?" (answer 59), then 10 seeds per variant: the agent loop run from the host
against the Gateway's ClusterIP (scratch script, not in the repo), config/real.yaml unless noted
Result:
- Infrastructure green on the first task: access-log lines `{"step":"1","expected-steps":"3","endpoint":"10.42.0.13:8000"}`
  and step 2 on the same pod; Redis hash `thesis:run:run-58ebd4b24aa6` with status done, step 2, 5 messages. The answer
  was 21, so the check failed.
- Config prompt, seeds 1-10: 0/10. 9 seeds called `multiply(7,3)` and answered 21; seed 5 called `multiply(7,8)` and
  answered 56 without the second operation.
- Prompt telling it to split the question into operations and use the previous result: 0/10. Both calls in one response
  (`multiply(7,8)` and `add(7,3)` in parallel, before 56 is known), answers like "56 and 10".
- `parallel_tool_calls: false` (vLLM v0.31.0 keeps only the first generated call, `tool_calls[:1]`), either prompt: 0/10.
  The model answers right after the first result; no run made a second call.
- "What is 7 times 8?" with the config prompt: 10/10, `multiply(7,8)` then "The result is 56.", 2 calls each.
Notes:
- The 0.6B cannot chain two dependent tool calls with these prompts: it merges the operations, sends them at once with
  the question's numbers, or stops after the first result. CLAUDE.md rule 8 stopped the prompt attempts after two.
- Decision (2026-10-27): the calculator task of make agent-check and make smoke (P1.5) is "What is 7 times 8?", answer 56;
  agent.expected_steps_default is 2 in every profile. It still proves a chain of calls sharing one context through every
  layer. Multi-step quality is measured by P1.6b on the 1.7B demo model.
- For the thesis: real-profile runs recorded on the 0.6B (P2.2) will be short, so long agent traces for the experiments
  come from the converted AgentProcessBench traces (P2.4), not from the 0.6B's own runs.

### 2026-10-27 · P1.2 · green
Command: `make down && make up` · `make gateway-check` · `kubectl top pods -A` · `kubectl top nodes`, one sequence
on a fresh cluster with the final files (16:21 UTC, after the review fixes below)
Result:
- `make up` exit 0: CRDs applied; the three charts pulled by digest (`agentgateway-crds@sha256:dd7f965d…`,
  `agentgateway@sha256:d3edf2b3…`, `llm-d-router-gateway@sha256:63a79a84…`); `agentgateway` (agentgateway-system),
  `inference-gateway` and `vllm-pool-epp` (default) Running next to both vLLM pods.
- `make gateway-check` OK, about a minute after the proxy rolled out (cold Gateway): 21 requests, 9 to
  vllm-5fff45f674-gcsrk and 12 to vllm-5fff45f674-ldld8. The header request's access-log line carries
  `"run-id":"p12-check","step":"1","expected-steps":"1"`, endpoint `10.42.0.13:8000` and `"duration":"95ms"`; no
  `parallel-group`, as the request sent none. `vllm-pool-epp logged 21 choices for 21 requests`.
- `kubectl top` (same run): agentgateway controller 3m / 63Mi, inference-gateway proxy 2m / 50Mi, vllm-pool-epp
  229m / 63Mi; gateway total 176Mi. vLLM pods 226m / 2737Mi and 517m / 2742Mi. Grafana 674Mi, Prometheus 430Mi.
  Node: 1337m CPU (8%), 8854Mi memory (89%).
- The run before the review fixes (16:01 UTC, also cold) passed the same way: 10/11, 66ms, gateway total 169Mi.
Notes:
- The EPP is the busiest pod outside vLLM at idle (177m and 229m CPU in the two runs): its `metrics-data-source` plugin scrapes every pod's `/metrics` on
  a short interval, and `--v=3` logging adds to it. Negligible at 2 pods; in the simulator experiments (P2.7+) the
  scrape cost grows with the pod count, so measure it there instead of assuming it stays small.
- Memory headroom: Grafana and Prometheus together (1.1Gi) use more than the whole gateway stack. If P1.3b
  (agent + Redis) runs short of RAM, they are the first place to look.
- Review fixes between the two runs (fresh-eyes review, 2026-10-27): the access log uses the D12 keys `run-id`, `step`,
  `expected-steps`, `parallel-group`; the check also matches `step` and `expected-steps`; its prompts are one fixed
  short prompt, below the EPP's 64-token prefix-cache block, so only the queue and KV-cache scorers decide; charts
  are installed by digest; comments in `down.sh`, `router-values.yaml` and `gateway_check.sh` corrected.
- The first `make gateway-check` of the day, ~90 s after the proxy rolled out, failed: 3 requests timed out at 60 s, later
  batches took 13 s and 30 s, the last ones 0.1-3 s. The EPP picked every pod in ~10 ms and vLLM showed
  `Running: 0 reqs` throughout, so the time was lost in the proxy between the pick and vLLM. In the same minutes the
  agentgateway controller lost its leader lease 3 times (`Failed to renew lease ... context deadline exceeded`,
  `leader election lost`), and the proxy logged xDS `Connection refused` while the controller restarted: the k3s
  API server stalled again (same family as P0.7 and P1.1). The warm re-run passed, and so did both cold runs above.
  Open: the stall is intermittent, so two cold passes don't rule it out; P1.5 runs right after `make up` and will
  show whether it needs a wait for a warm Gateway.
- The first check also had a bug: the EPP logs OpenTelemetry-style JSON (`body`, `timestamp`), not zap's
  `msg`/`ts`. Fixed in `gateway_check.sh`; the `zap-devel` flag added for it was removed.

### 2026-10-27 · P1.2 spike · done
Command: read the release pages and the tagged source (shallow clones) of gateway-api-inference-extension,
llm-d-router, kgateway, agentgateway and envoyproxy/gateway; `helm show` of the published charts; a ghcr.io manifest
lookup for the EPP image.
Result:
- Since GAIE v1.6.0 (2026-08-17) the full Endpoint Picker lives in `llm-d/llm-d-router`; GAIE keeps only the
  InferencePool API, a conformance-only lightweight EPP (`lwepp`) and the conformance tests.
- llm-d-router v0.11.0 (2026-09-27): built on GAIE v1.5.0. Chart `oci://ghcr.io/llm-d/charts/llm-d-router-gateway`
  v0.11.0 (chart digest `sha256:63a79a842974691be73d44918c7086bcc52d6fee546ff39113517a23c03eeab0`), image
  `ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.11.0`. v1.0.0 (2026-10-16) is 11 days old and fails the
  two-week rule.
- Plugins: a rule is a Go `Scorer` (`Score(ctx, request, pods) map[Endpoint]float64`) registered with
  `plugin.Register(type, stability, factory)` and enabled in a YAML scheduling profile. A custom plugin needs a
  self-built EPP image (as P3.1 planned).
- llm-d-router already ships `sessionaffinity` and `headerlabelaffinity` scorers: related work for chapter 2,
  and P3.3 has to differ from them.
- kgateway removed InferencePool support in v2.3.0 (2026-05-15); only the 2.2 line (GAIE v1.1.0, last release
  2026-07-13) has it. Envoy Gateway v1.9.2 has InferencePool only in an example extension server.
- agentgateway v1.6.0 (2026-10-02): native InferencePool, built and conformance-tested against GAIE v1.6.2,
  `inferenceExtension.enabled` in its chart, controller requests 128Mi.
- The InferencePool v1 fields the router and agentgateway use (`selector`, `targetPorts`, `endpointPickerRef`)
  are the same in the GAIE v1.5.0 and v1.6.2 CRDs.
- Gateway API: v1.6.3 (2026-10-06) is the newest release at least two weeks old.
Notes:
- Decided with the user: llm-d-router v0.11.0 in gateway mode, agentgateway v1.6.0, GAIE CRDs v1.6.2, Gateway API
  CRDs v1.6.3 (standard channel). If the v0.11.0 EPP and agentgateway don't work together, the fallback is
  llm-d-router v1.0.0, made for GAIE v1.6.2, recorded as an exception to the two-week rule.
- RAM was not measured: it needs the install, so it moved into P1.2's done-when.
- PLAN.md's P1.2 row named Envoy Gateway or kgateway; neither supports InferencePool any more (brick wall in
  PLAN.md section 4).

### 2026-10-26 · P1.1 · green
Command: `make up` · `kubectl get pods -n monitoring` · `make service-check` · `make grafana`, dashboard `thesis-vllm`
Result:
- kube-prometheus-stack 92.3.0 installed by `make up` before the vLLM release; 4 pods Running in `monitoring`
  (grafana 3/3, operator, kube-state-metrics, prometheus 2/2).
- `make service-check` OK (12 / 10 requests per pod); the "vLLM pods" dashboard shows both pods in the queue,
  KV-cache and prefix-cache hit rate panels (user checked in the browser).
Notes:
- Operator and kube-state-metrics restarted once at 20:53:30 UTC, while both vLLM pods were loading the model:
  kube-state-metrics logged `Failed to contact API server for /livez`, failed its liveness probe and was
  restarted; the operator exited cleanly on SIGTERM. The k3s API server stopped answering for a moment under the
  model load. Not a failure of this task; watch for it in later `make up` runs (same family as the P0.7
  datastore slowness).

### 2026-10-26 · P1.0 · green
Command: `make down` · `systemctl is-active k3s; nvidia-smi` · `time make up` · `make service-check`
Result:
- `make down`: releases vllm, monitoring, nvidia-device-plugin uninstalled, both namespaces deleted, k3s stopped
  by `k3s-killall.sh`; `inactive`, `nvidia-smi` lists no process (335 MiB used by the desktop).
- `make up` from a stopped k3s: exit 0, **8 min 2 s** wall time. Device plugin back with
  `allocatable nvidia.com/gpu: 2`; model already in the cache (hf only checked 10 files); both vLLM pods
  `1/1 Running` after the rollout (4 min 29 s from install to both Ready).
- `make service-check` OK through the Service, 12 / 10 requests per pod.
Notes:
- The 8 min include the first pull of the monitoring images (P1.1, same run); the vLLM image and the model came
  from k3s's containerd and the HF cache, as `make down` keeps them.
- In k3s both pods became Ready after 4.5 min, slower than the 2-3 min in Docker (P0.3, P0.4): they load at the
  same time from one disk and one GPU.
- The second `make down` (idempotency) was not part of this run.

### 2026-10-26 · P0.7 · green · Gate 0 green
Command: `make k3s` · `make vllm-deploy PROFILE=real` · `kubectl get pods` · `make service-check`
Result:
- `make k3s`: device plugin with time-slicing, node reports `allocatable nvidia.com/gpu: 2`.
- `kubectl get pods`: `vllm-5fff45f674-5qvc7` and `vllm-5fff45f674-csj8f`, both `1/1 Running`, 0 restarts.
- `make service-check` through ClusterIP `10.43.129.213:8000`: chat answer and tool call
  `get_weather({"city": "Ohrid"})` OK; chat requests per pod **9 and 13** (2 from chat-check + 20 spread requests).
- Gate 0: two vLLM pods on the GPU answer through a Service (this entry), prefix caching visible (P0.5).
Notes:
- Getting the image into k3s, attempt 1 (`docker save | k3s ctr images import -`): after 23 min **k3s crashed**
  (`level=fatal msg="Transaction commit failed"`, then systemd restarted it). The 32 GB of image writes starved
  k3s's SQLite datastore on the same disk: 102 "Slow SQL" warnings during the import vs 13 in the two hours
  before; no journal gap long enough for a laptop sleep before the crash. Replaced (user decision) by
  `sudo k3s crictl pull`, k3s pulling the image itself; it was already complete when the deploy ran.
- First rollout: both pods in CrashLoopBackOff, `ValueError: VLLM_PORT 'tcp://10.43.129.213:8000' appears to be
  a URI`. Kubernetes injects `<SERVICE>_PORT` variables for every Service, and for the Service `vllm` that is
  `VLLM_PORT`, which vLLM reads as its own setting. Fixed with `enableServiceLinks: false` in the pod spec.
- `make vllm-deploy` waits with `kubectl rollout status` (prints each pod as it becomes Ready) instead of a silent
  `helm --wait`; the Deployment uses `Recreate`, since a rolling update's third pod could never get a GPU slot.
- Pods mount `~/.cache/huggingface` read-only with `HF_HUB_OFFLINE=1`; the model must be downloaded by
  `make vllm-run` first.
- `service_check.sh` calls the ClusterIP from the host (the host is the node) instead of `kubectl port-forward`,
  which tunnels to one pod (user decision). Readiness delay and the KV size come from `config/` (user decision).

### 2026-10-26 · P0.5 · green
Command: `make vllm-run PROFILE=real` · `make prefix-check`
Result:
- Metric names in vLLM 0.31.0: `vllm:prefix_cache_queries_total`, `vllm:prefix_cache_hits_total` (both in tokens,
  label `engine="0"`), plus `vllm:external_prefix_cache_{queries,hits}_total` (KV connector, 0 here).
- ~1980-token prompt sent twice: hits added by the first request **0**, by the second **1968** (all but the last
  partial 16-token block).
- **TTFT first 0.524 s, second 0.081 s** (6.5× lower). TTFT = total time of a `max_tokens: 1` request.
Notes:
- First run used 150 prompt lines (~3385 tokens, hits 3376, TTFT 0.758 s -> 0.089 s); the script's per-line
  estimate was wrong (22 tokens per line, not 14), so it now uses 90 lines for the card's ~2000 tokens.
- The run marker at the start of the prompt makes every run start uncached, so the check can be rerun.
- The check fails unless the second request adds more hits than the first, not just "hits went up": the first
  request could hit shared chat-template blocks.

### 2026-10-26 · P0.4 · green (attempt 2)
Command: `make vllm-pair` · `make concurrency-check ARGS="--ports 8001 8002 --concurrency 4 --minutes 10"`
Result:
- port 8001: 344 requests, **0 errors**, p50 6.35 s, p95 9.55 s; port 8002: 335 requests, **0 errors**, p50 6.38 s,
  p95 9.43 s (4 in flight per port, max_tokens 128).
- Peak GPU memory 3468 / 4096 MiB (whole GPU, 558 MiB of it the desktop). Peak RAM vllm-1 2.79 GiB, vllm-2 2.92 GiB:
  under the card's ~3 GB, so no `--swap-space` change.
- Both replicas: KV cache 0.47 GiB = 4384 tokens each; healthy after 114 s and 120 s.
Notes:
- Attempt 1 (KV cache sized by `gpu_memory_utilization` 0.42): replica 1 fine (0.62 GiB KV), replica 2 computed
  **-0.79 GiB** and exited. Under WSL vLLM sees only device-wide memory (both replicas log "Initial free memory
  3.22 GiB"), so replica 2 counted replica 1's memory as its own. Fix, suggested by vLLM's own log: a fixed KV
  size, `kv_cache_memory_bytes` in `config/` (480 MiB real/sim, 800 MiB demo) passed as `--kv-cache-memory`.
- With it set, `gpu_memory_utilization` no longer sizes anything; vLLM only checks at startup that this share of
  VRAM is free (`vllm/v1/worker/utils.py`, `request_memory`).
- Replicas start one after the other (`vllm-pair.sh`), so they never profile memory at the same time.
- `concurrency_check.py` uses only the standard library (user decision, no httpx); summary math unit-tested.

### 2026-10-26 · P0.3 · green (demo model changed)
Command: `make vllm-run PROFILE=real` · `make chat-check` · `make vllm-run PROFILE=demo` · `make chat-check`
Result:
- 0.6B: answer "Kubernetes is a system for automating the deployment, scaling, and management of containerized
  applications."; tool call `get_weather({"city": "Ohrid"})`. VRAM 2271 MiB whole GPU, RAM 3.42 GiB.
- **4B AWQ does not fit.** util 0.85: refused at startup (only 3.22 of 4.0 GiB free next to the desktop and the
  CUDA context). util 0.78: weights 2.5 GiB, KV 0.18 GiB, max context **1312 tokens** (4096 needs 0.56 GiB).
- The D8 fallback `Qwen/Qwen3-1.7B` is FP16, 4.06 GB of weights: cannot fit either. Demo model is now
  **Qwen/Qwen3-1.7B-GPTQ-Int8** (user decision): weights 1.92 GiB, KV 0.86 GiB = 8016 tokens at util 0.78,
  chat + tool call OK, VRAM 3213 MiB, RAM 3.85 GiB. After the P0.4 change (fixed 800 MiB KV): 7312 tokens, OK.
- Startup (0.6B, model cached): ~160 s to healthy: 37 s Python imports, 32 s config resolution (HF hub lookup),
  38 s engine process start, 19 s model load (weights 2.7 s), 12 s KV/profiling. 115 s with `HF_HUB_OFFLINE=1`.
  This is the cold start the simulator has to model (D4).
Notes:
- vLLM v0.31.0 (2026-10-05), the newest release at least two weeks old; v0.32.0 was 8 days old. Pinned by digest.
- `max_model_len` 8192 did not fit the 0.6B at util 0.42 (needs 0.88 GiB KV, 0.61 GiB available, max 5728):
  real and sim now use 4096 (user decision).
- Model revisions pinned in `config/` as `model.revision` (user decision), passed as `--revision` and
  `--tokenizer-revision` (without the second, the tokenizer and chat template come from `main`).
- Containers run as the calling user with `HF_HOME=/hf`, so files downloaded into `~/.cache/huggingface` are not
  owned by root. Read-only mount + `HF_HUB_OFFLINE=1` (as the P0.7 pods use it) also tested: chat-check OK.
- Manual step, once per machine: `hf auth login` with a read token (huggingface.co/settings/tokens). Optional:
  without it downloads work, only with lower rate limits. The token lives in `~/.cache/huggingface/token`,
  outside the repo.
- `chat_check.sh` takes a base URL instead of a port (user decision), so P0.7 reuses it against the Service.
- vLLM warnings on this GPU, harmless: pinned memory not available under WSL; FlashInfer top-k sampling needs more
  than 16 SMs, so vLLM falls back.

### 2026-10-26 · P0.6 · green (ladder step 1)
Command: `make k3s && make gpu-check`
Result:
- `make k3s`: k3s v1.36.5+k3s1 installed, nvidia runtime found in the k3s containerd config, RuntimeClass
  `nvidia` applied, device plugin chart 0.20.1 deployed, `node reports allocatable nvidia.com/gpu: 1`.
- `make gpu-check`: plugin variant (`nvidia.com/gpu: 1` limit), pod Succeeded after 6 s (image already pulled):
  `Compute 8.6 CUDA device: [NVIDIA GeForce RTX 3050 Laptop GPU]`, 16384 bodies, 10 iterations in 16.144 ms,
  **3325.434 single-precision GFLOP/s** (P0.2 in Docker: 3321.221, same within noise (one run each)).
Notes:
- **Ladder step 1 worked**: k3s + NVIDIA Container Toolkit + device plugin. The plugin detects WSL2 and reports
  one device `all` backed by `/dev/dxg`, so the node shows exactly one GPU. The no-plugin variant (step 3)
  was not needed and was not run. P0.7 needs 2 GPU slots for 2 pods: time-slicing on top of this, or step 3.
- Proof of step 1 is the allocatable `nvidia.com/gpu: 1`, not nbody alone: the image sets
  `NVIDIA_VISIBLE_DEVICES=all` itself, so with `runtimeClassName: nvidia` it would get the GPU without the plugin.
- The chart's default affinity needs a PCI/NFD node label that never appears under WSL2 (no PCI GPU), so the
  values set `affinity: null`; without it the plugin pod is never scheduled.
- k3s already ships an identical `RuntimeClass nvidia` (packaged addon); ours is applied server-side so the
  cluster does not silently depend on that addon.
- The card's `command: ["nbody", ...]` failed (`exec: "nbody": executable file not found in $PATH`,
  exit 128): in Kubernetes `command` replaces the ENTRYPOINT (`/cuda-samples/sample`, a symlink to
  `/cuda-samples/nbody`), while in P0.2's `docker run` the words after the image were only arguments to it.
  Fixed to `command: ["/cuda-samples/nbody", "-gpu", "-benchmark"]`.
- Departures from the card: (1) the kubeconfig mode is set as `write-kubeconfig-mode: "0644"` in
  `/etc/rancher/k3s/config.yaml` instead of the card's `--write-kubeconfig-mode 644` install flag, so changing
  it does not need a reinstall; (2) Traefik is disabled, which the card did not ask for (P1.2 brings its own
  gateway); ServiceLB kept for LoadBalancer Services.
- k3s pinned to the `stable` channel (v1.36.5), not the newest release (v1.37.1).
- The first pull of the nbody image into k3s's containerd took 1m13s (122 MB): k3s does not share Docker's
  image store.

### 2026-10-25 · P0.2 · green
Command: `make docker-gpu` (twice, `wsl.exe --shutdown` in between) · `docker run --rm --gpus all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark`
Result:
- nbody: `Compute 8.6 CUDA device: [NVIDIA GeForce RTX 3050 Laptop GPU]`, 16384 bodies, 10 iterations in 16.165 ms,
  **3321.221 single-precision GFLOP/s**.
- Image digest: `nvcr.io/nvidia/k8s/cuda-sample@sha256:59261e419d6d48a772aad5bb213f9f1588fcdb042b115ceb7166c89a51f03363`
  (the `nbody` tag is not pinned; the digest records what ran).
- Second `make docker-gpu`: no apt install, no group change, every package at its pin (docker-ce 29.9.2,
  containerd 2.4.2, buildx 0.38.1, Toolkit 1.20.1), runtimes `io.containerd.runc.v2 nvidia runc`.
  Docker was not restarted: started 23:32:28 at WSL boot, `daemon.json` rewritten at 23:37:12 with
  identical content.
- `command -v docker` -> `/usr/bin/docker`; the user is in group `docker` after the WSL restart.
Notes:
- Pins are the newest versions available on 2026-10-25 (user decision), not the two-week rule used for
  the dev tools. No `apt-mark hold` (user decision); rerunning the script moves packages back to their pins.
- Only Engine + buildx; compose and rootless-extras left out (Kubernetes, not Compose).
- Neither Docker's nor NVIDIA's install page lists its key fingerprint. The script pins the fingerprints
  of the keys served over HTTPS on 2026-10-25 (Docker 9DC8...CD88, NVIDIA C95B...ECB0) and stops if they change.
- systemd was already on (`/etc/wsl.conf`), so that step of the script did nothing here.
- Docker Desktop stays installed on Windows with autostart off; its `docker-desktop` distro shares the
  WSL2 VM's memory. The script warns if it is running.
- `wsl --shutdown` only exists on Windows; inside Ubuntu it is `wsl.exe --shutdown`. The script's
  messages say so after the first run hit this.
- `nvidia-ctk` rewrites `daemon.json` on every run and prints INFO lines, and gpg printed keybox messages
  from its temporary home: both made quiet after the second run (`--quiet`). Output-only change, not re-run.

### 2026-10-25 · P0.0 · green
Command: `bash infra/bootstrap.sh` (twice) · `make lint` · `make test`
Result:
- Second bootstrap run installed nothing, printed every version, exit 0: git 2.43.0, make 4.3, jq 1.7,
  yq 4.54.1, Python 3.12.3, uv 0.13.0, Node.js 24.21.0 (npm 11.19.0, bundled), Angular CLI 22.2.2,
  Go 1.27.2, golangci-lint 2.14.0, Helm 4.3.0, k9s 0.51.0, hf 2.2.0. Full list in `versions.md`.
- `make lint`: all hooks pass; golangci-lint, eslint and tsc skipped (no `gateway/` or `ui/` yet), exit 0.
- `make test`: 57 passed (schema tests: valid examples accepted, invalid rejected, one rule per case), exit 0.
Notes:
- Headers keep the `x-` prefix (`x-run-id`, `x-step`, `x-expected-steps`, `x-parallel-group`), as in the
  card. CLAUDE.md still says `run-id`, `step`, ...; it gets aligned in a separate commit.
- `x-step` and `x-expected-steps` are validated as digit strings (`^[1-9][0-9]*$`), not integers as the
  card says: HTTP header values are always strings on the wire. Readers parse them into integers.
- Header names are case-insensitive in HTTP (Go's `net/http` sends `X-Run-Id`), so readers lowercase
  them before validating; written into the schema description after review.
- `types-jsonschema` added beyond the card's tool list, approved: jsonschema ships no type information,
  so `mypy --strict` fails without it.
- `docs/task-cards/05-P0.3-vllm-single.md` is outside the card's files but in this commit: the
  trailing-whitespace hook strips one space from it, and without that `make lint` fails on a fresh clone.
- Fresh-eyes review done in a separate session. Fixed: case-insensitive header names (above) and a test
  docstring that claimed every example file is checked. Left as is: a test that only checks both example
  files exist (the accept/reject tests would fail anyway), and `pre-commit-hooks` pinned by tag `v6.0.0`,
  not by commit hash.
- Open for P1.3: agent calls are non-streaming (D6), so `ttft_ms` in the trace can't be measured by the
  client and has to come from vLLM or the gateway.

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
