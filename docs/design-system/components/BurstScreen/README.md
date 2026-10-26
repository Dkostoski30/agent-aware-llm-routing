"Start 20 tasks" pressed (P4.6, P3.6, P3.9, P4.1, P4.2) on the simulated pool.

- The notice says what happened in one sentence and why the next card looks different.
- Step 3 spilled: its parallel group could not fit on the pinned pod, so it went to `sim-1` (cache miss, `spilled` tag). The run itself stays pinned to `sim-0`.
- Remaining work jumps before the queues do; KEDA scales 2 → 4 on it and the new pods show their cold start as a blue bar.
- Active runs is sorted by progress: the run nearest to done is served first.
- Start is disabled while the replay runs; Kill pod stays available.
