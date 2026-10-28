"Kill pod" pressed mid-run (P4.4, P3.4): the run loses its pod and carries on.

- The notice names the pod, the step it died in, and what happens next, in two sentences.
- The retried step has a `failed` border and a `retry 1` tag; it is a cache miss because the new pod has never seen this run.
- The panel shows the killed pod with its state in words, and where its runs went. Every moved run now lists `vllm-1`.
- The run's progress is not reset: the header still counts from step 4.
