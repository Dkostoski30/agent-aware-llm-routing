Step metadata in mono: which pod served the call, whether the prefix cache hit, how long it took.

- Order on a step card: pod, cache, latency, then any event (`retry 1`, `spilled`).
- `hit` is tinted teal (`accent-soft`); `miss` is an outline (`line-strong`), never red: a miss is expected on the first step and after a move.
- `retry` uses the failed palette, `spilled` the draining palette, matching the pod states that cause them.
