One vLLM pod: name, state, queue, KV-cache use, prefix-cache hit rate, and a line saying what it is doing.

- Data: `vllm:num_requests_running` / `waiting`, KV-cache usage and prefix-cache hit rate from the pod's metrics; pinned runs from the router.
- Ready: solid. Starting: dashed blue border, a cold-start bar, metrics dimmed. Draining: dashed amber border, a note naming the run it waits for and the drain timeout. Killed: solid `failed` border, metrics dimmed, a note saying where its runs went.
- A KV-cache meter above 85% turns amber.
- Pod names in mono, exactly as Kubernetes reports them.
