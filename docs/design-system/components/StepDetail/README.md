The sheet for one step: call, result, routing, timing, headers.

- Opens over the right of the conversation column, never over the cluster panel. `scrim` behind it; Escape closes it and returns focus to the card.
- **Call** is the structured tool call as emitted; **Result** is what the tool computed. Both in `data`.
- **Routing** says which pod, which rule chose it, and how much of the prompt was cached, as a bar.
- **Timing** shows the trace fields for this call as six stats. **Headers** shows the four `x-*` headers exactly as sent.
