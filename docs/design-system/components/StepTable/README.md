All steps of a run as rows: the Steps tab. Columns are the trace fields of `schemas/trace.v1.json`.

- Tool call, `pod`, cache, `ttft_ms`, `duration_ms`, `gap_before_ms`, and `prompt_tokens` / `completion_tokens`.
- Numbers are tabular; times follow the latency rule (ms under a second, seconds with one decimal above).
- It must agree with the recorder's JSONL line for line (P2.6).
