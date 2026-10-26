One step opened (P2.6, D1, D12): the sheet slides over the conversation column; the cluster panel stays visible.

- Call: the structured tool call exactly as the model emitted it (D10). Result: what the tool computed.
- Routing: the pod, why it was chosen (the rule that decided), and how much of the prompt was served from the prefix cache.
- Timing: the trace fields for this call. Headers: the four `x-*` headers as sent, validated against `schemas/headers.v1.json`.
- The selected card is tinted `accent-soft`; Escape or the close button returns focus to it.
