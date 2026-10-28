The Steps tab (P2.6): every step of the run as one row, in the recorder's own fields.

- Columns follow `schemas/trace.v1.json`: step, pod, `ttft_ms`, `duration_ms`, `gap_before_ms`, `prompt_tokens`, `completion_tokens`, plus the cache result.
- This table must match the recorder's JSONL for the same run line for line; it is the check for P2.6.
- Latency under a second in ms, above in seconds with one decimal.
