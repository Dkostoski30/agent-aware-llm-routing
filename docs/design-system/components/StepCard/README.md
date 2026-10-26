One LLM call of the run, as evidence: its number, the tool the model chose, the arguments, and how it was served.

- Pass `n`, `tool`, `args`, `pod`, `cache`, `ms`, `par` (the `x-parallel-group`, if any) and `state`.
- States: `done`, `running` (accent border, no latency yet), `pending` (dashed, up to the expected count), `selected` (its sheet is open), `retried` (failed border, `retry n` tag). A spilled call adds the `spilled` tag and a one-line reason.
- Cards sit in a three-column grid in step order. Parallel calls keep their own numbers and show `∥ group`.
- The whole card opens the step-detail sheet.
