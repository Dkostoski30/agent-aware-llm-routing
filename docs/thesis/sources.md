# Sources

References collected as they come up (`outline.md`, Литература), until the FCSE template decides between a `.bib` and a reference list. Versions are the ones pinned in `versions.md`. Dev tools (ruff, mypy, pytest and the like) are not listed: `versions.md` holds them.

How to read an entry:
- Every link was opened on 2026-11-16, or on the date its entry or section gives, and showed the title given here, unless the entry says "not opened", "check failed" or "from memory". Opened means the link works. It does not mean the page was read for a claim: read the source before citing what it says.
- A paper's title, authors and year are as its arXiv page shows them. A venue is given only where that page, or the venue's own page linked in the entry, states one. Where an arXiv page's first version is older than its identifier's month (Preble, AIBrix, XPerf), the entry gives the date the page shows.
- Documentation links point at the current docs, which may describe a newer version than the pinned one. KEDA's are the 2.21 docs, the pinned version.

## 1. LLM serving: vLLM

- **vLLM** (image `vllm/vllm-openai` v0.31.0). "A high-throughput and memory-efficient inference and serving engine for LLMs", Apache-2.0.
  - Repository: https://github.com/vllm-project/vllm
  - Docs: https://docs.vllm.ai/en/latest/ (the developer preview; it names https://docs.vllm.ai/en/stable/ for the stable release, not opened). No docs URL for v0.31.0 was confirmed.
- **The vLLM paper.** W. Kwon, Z. Li, S. Zhuang and 6 more, "Efficient Memory Management for Large Language Model Serving with PagedAttention", 2023, SOSP 2023. https://arxiv.org/abs/2309.06180
  - For: the KV cache in blocks and continuous batching (chapter 3).
- **vLLM docs pages for what the repo relies on:**
  - Automatic prefix caching (P0.5, the premise of task affinity): https://docs.vllm.ai/en/latest/features/automatic_prefix_caching.html and the design note https://docs.vllm.ai/en/latest/design/prefix_caching/
  - Tool calling (D10, `auto`): https://docs.vllm.ai/en/latest/features/tool_calling.html
  - Structured outputs (D10, `required`; D13's scenario step): https://docs.vllm.ai/en/latest/features/structured_outputs.html
  - LoRA adapters (P1.7, serving the adapter): https://docs.vllm.ai/en/latest/features/lora.html
  - Production metrics, which list `vllm:num_requests_waiting` and `vllm:kv_cache_usage_perc` (P1.1, the overload guard, KEDA on the queue): https://docs.vllm.ai/en/latest/usage/metrics.html
  - Online serving, the OpenAI-compatible server: https://docs.vllm.ai/en/latest/serving/online_serving/ (the older `serving/openai_compatible_server.html` redirects here)
  - Priority scheduling (D3, P4.1), opened 2026-11-20:
    - `--scheduling-policy`, `fcfs` (the default) or `priority`, where "requests are handled based on given priority (lower value means earlier handling) and time of arrival deciding any ties": https://docs.vllm.ai/en/latest/cli/serve/#-scheduling-policy ("serve - vLLM"). The same text is on https://docs.vllm.ai/en/latest/configuration/engine_args/#-scheduling-policy ("Engine Arguments - vLLM").
    - The per-request `priority` field (default 0; any other value is an error unless the model is served with priority scheduling) and the `X-Vllm-Priority` header, which overrides it: https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/ ("OpenAI-Compatible Server - vLLM"), in the Chat API's "Extra parameters" and in "Extra HTTP Headers". The plugin writes the body field (P4.1).
    - These are the latest docs. That the pinned v0.31.0 honours the field is shown by `make priority-check` (P4.1), not by these pages.
- **OpenAI Chat Completions API reference**, the request format vLLM serves: https://platform.openai.com/docs/api-reference/chat (not opened: the site answered the automated check with HTTP 403; open it by hand).

## 2. Models and fine-tuning

- **Qwen3.** A. Yang, A. Li, B. Yang and 57 more, "Qwen3 Technical Report", 2025. https://arxiv.org/abs/2505.09388
- **Model cards**, all Apache-2.0; revisions in `versions.md`:
  - https://huggingface.co/Qwen/Qwen3-0.6B-GPTQ-Int8 (experiments, infrastructure demo, D8)
  - https://huggingface.co/Qwen/Qwen3-1.7B-GPTQ-Int8 (practical demo, D8)
  - https://huggingface.co/Qwen/Qwen3-0.6B (base of the P1.7 adapter)
- **GPTQ.** E. Frantar, S. Ashkboos, T. Hoefler and 1 more, "GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers", 2022, ICLR 2023. https://arxiv.org/abs/2210.17323
  - For: the Int8 weights that let two replicas fit in 4 GB (D8).
- **LoRA.** E. J. Hu, Y. Shen, P. Wallis and 5 more, "LoRA: Low-Rank Adaptation of Large Language Models", 2021, ICLR 2022. https://arxiv.org/abs/2106.09685
  - Venue from the conference page, which reads "ICLR 2022", "2022 Poster" (opened 2026-11-20): https://iclr.cc/virtual/2022/poster/6319
  - For: the tool-calling adapter (P1.7, P2.1b).
- **PEFT** (peft 0.21.2), the library the adapter is trained with: https://huggingface.co/docs/peft/index

## 3. Kubernetes and autoscaling

- **k3s** (v1.36.5+k3s1): https://docs.k3s.io/ · https://github.com/k3s-io/k3s
- **Horizontal Pod Autoscaling**: https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/
  - For: the CPU baseline (P3.10), and the HPA that KEDA creates, with its sync period and scale-down stabilization (P3.9b).
- **Pod lifecycle**, termination: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination (the anchor came from the check's summary of the page; the site also lists https://kubernetes.io/docs/concepts/workloads/pods/pod-termination/, not opened)
  - For: drain on scale-down (P4.3).
- **Container lifecycle hooks**, the preStop hook (P4.3): https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/
- **Deployments**: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
- **Service**: https://kubernetes.io/docs/concepts/services-networking/service/
- **KEDA** (2.21.0, P3.9a): https://keda.sh/ · https://github.com/kedacore/keda
  - Docs for 2.21: https://keda.sh/docs/2.21/
  - ScaledObject specification (P3.9b's object, `pollingInterval`, `cooldownPeriod`): https://keda.sh/docs/2.21/reference/scaledobject-spec/
  - Prometheus scaler (`thesis_remaining_work` and the queue as triggers): https://keda.sh/docs/2.21/scalers/prometheus/
- **vLLM production stack, KEDA autoscaling.** "Autoscaling with KEDA — production-stack" (opened 2026-11-20): https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/autoscaling-keda.html
  - What it sets up: one Prometheus trigger on `vllm:num_requests_waiting` with threshold 5 ("Scale up when the queue exceeds 5 pending requests"), 1 to 3 replicas, `pollingInterval` 15, `cooldownPeriod` 360. A scale-to-zero variant adds a keepalive trigger on `vllm:num_incoming_requests_total`.
  - For: the reference way to scale vLLM with KEDA. P3.9b-P3.10's queue baseline scales on the same metric with KEDA, at the thesis's own threshold (`queue_target` 1 per pod in `config/real.yaml`). Name it as the baseline's origin in chapters 2 and 6. The rest of the production stack is in section 11.
- **Helm** (4.3.0; the docs showed 4.3.1): https://helm.sh/docs/

## 4. Gateway and routing

- **Gateway API** (CRDs v1.6.3): https://gateway-api.sigs.k8s.io/docs
- **Gateway API Inference Extension** (v1.6.2; InferencePool, the Endpoint Picker protocol). "Introduction - Kubernetes Gateway API Inference Extension" (re-opened 2026-11-20): https://gateway-api-inference-extension.sigs.k8s.io/ · https://github.com/kubernetes-sigs/gateway-api-inference-extension
- **llm-d**: https://llm-d.ai/ (its docs are labelled v1.0)
- **llm-d-router** (v0.11.0, the Endpoint Picker the plugin is built into: P3.1-P3.6, P3.7b, P4.1-P4.3). "llm-d Router: The intelligent entry point for inference requests". https://github.com/llm-d/llm-d-router · releases, where v0.11.0 is listed: https://github.com/llm-d/llm-d-router/releases
  - The Inference Extension's README says the Endpoint Picker moved to this repository (from the check's summary of that README), as the spike of 2026-10-27 found (`experiments/log.md`).
  - **Plugin READMEs at v0.11.0**, opened 2026-11-20. Each page's title has the form "llm-d-router/<path>/README.md at v0.11.0 · llm-d/llm-d-router · GitHub".
    - `session-affinity-filter`, P5.1's baseline: https://github.com/llm-d/llm-d-router/blob/v0.11.0/pkg/epp/framework/plugins/scheduling/filter/sessionaffinity/README.md
      - A hard filter: it returns only the session's pod while that pod is a candidate, otherwise all candidates, never an empty set.
      - Two strategies. `encoded_endpoint_header`, the default, is stateless: an `x-session-token` header echoed back. `session_id` binds the session on the server with a TTL (header `x-session-id`, `evictionTtlSeconds` 300).
    - `prefix-cache-scorer`: https://github.com/llm-d/llm-d-router/blob/v0.11.0/pkg/epp/framework/plugins/scheduling/scorer/prefix/README.md
      - Score 0–1, the cached share of the prompt's blocks.
      - It reads its match from `approx-prefix-cache-producer`, which records "which endpoints have recently served requests with a matching prefix": https://github.com/llm-d/llm-d-router/blob/v0.11.0/pkg/epp/framework/plugins/requestcontrol/dataproducer/approximateprefix/README.md
      - For chapter 3: the default's prefix score is an estimate from the picker's own routing history, not a reading of vLLM's cache. Read the producer's code before saying whether it models eviction.
    - `queue-scorer`: https://github.com/llm-d/llm-d-router/blob/v0.11.0/pkg/epp/framework/plugins/scheduling/scorer/queuedepth/README.md
      - Scores `WaitingQueueSize`, min-max normalised: the shortest queue 1.0, the longest 0.0, all equal 1.0.
    - `kv-cache-utilization-scorer`: https://github.com/llm-d/llm-d-router/blob/v0.11.0/pkg/epp/framework/plugins/scheduling/scorer/kvcacheutilization/README.md
      - Score = 1 − `kvCacheUsagePercent`.
    - `max-score-picker`: https://github.com/llm-d/llm-d-router/blob/v0.11.0/pkg/epp/framework/plugins/scheduling/picker/maxscore/README.md
      - The highest score wins, with a stable sort and round-robin among ties; `maxNumOfEndpoints` defaults to 1. It is the picker used when none is configured.
    - For: the default the thesis compares against, these three scorers (queue 2, KV 2, prefix 3) and this picker.
- **agentgateway** (v1.6.0, the gateway, P1.2). "Next Generation Agentic Proxy for AI Agents and MCP servers". https://agentgateway.dev/ · https://github.com/agentgateway/agentgateway

## 5. The GPU in Kubernetes under WSL2

- **NVIDIA device plugin** (0.20.1): https://github.com/NVIDIA/k8s-device-plugin
  - Its README's "Shared Access to GPUs" section, "With CUDA Time-Slicing", is what gives the node 2 GPU slots (P0.7).
- **Time-slicing**, the GPU Operator's docs. "Time-Slicing GPUs in Kubernetes — NVIDIA GPU Operator" (opened 2026-11-20; the link was given as a Google redirect, which resolves here): https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html
  - The same `sharing.timeSlicing.resources` config as `infra/helm/device-plugin-values.yaml`. The repo installs the device plugin on its own, not the GPU Operator.
  - For threats to validity (chapter 6): "there is no memory or fault-isolation between replicas", so the two pods share one GPU (D8).
- **NVIDIA Container Toolkit** (1.20.1, P0.2): https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/index.html
- **CUDA on WSL User Guide** (P0.1): https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- **WSL advanced settings**, `.wslconfig` (P0.1's memory and swap): https://learn.microsoft.com/en-us/windows/wsl/wsl-config

## 6. Simulation

- **llm-d-inference-sim** (v1.0.0, P2.7), a simulator that mimics vLLM's behaviour without GPUs: https://github.com/llm-d/llm-d-inference-sim
- **Vidur.** A. Agrawal, N. Kedia, J. Mohan and 5 more, "Vidur: A Large-Scale Simulation Framework For LLM Inference", 2024, MLSys 2024. https://arxiv.org/abs/2405.05465
  - Venue from the conference page, which reads "MLSys 2024", "2024 Poster", with the title written "VIDUR: …" (opened 2026-11-20): https://mlsys.org/virtual/2024/poster/2667

## 7. Monitoring

- **Prometheus** (v3.15.0): https://prometheus.io/docs/introduction/overview/
  - Exposition formats, the text format the run tracker writes by hand (P3.8): https://prometheus.io/docs/instrumenting/exposition_formats/
- **Prometheus Operator** (v0.94.1; PodMonitor and ServiceMonitor): https://prometheus-operator.dev/
- **kube-prometheus-stack** (chart 92.3.0, P1.1): https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack
- **kube-state-metrics** (v2.20.0; `kube_deployment_spec_replicas` in P3.9b's series): https://github.com/kubernetes/kube-state-metrics
- **Grafana** (13.2.3): https://grafana.com/docs/grafana/latest/

## 8. Agents, the workload and related serving work

- **The Transformer.** A. Vaswani, N. Shazeer, N. Parmar and 5 more, "Attention Is All You Need", 2017, NeurIPS 2017. https://arxiv.org/abs/1706.03762
  - Venue from the proceedings page, which reads "Advances in Neural Information Processing Systems 30 (NIPS 2017)" and writes the title "Attention is All you Need" (opened 2026-11-20): https://proceedings.neurips.cc/paper_files/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html
  - For: the attention keys and values that the KV cache keeps (chapter 3).
- **The agent loop.** S. Yao, J. Zhao, D. Yu and 4 more, "ReAct: Synergizing Reasoning and Acting in Language Models", 2022, ICLR 2023. https://arxiv.org/abs/2210.03629
  - Venue from the conference page, which reads "ICLR 2023", an oral, "top 5% paper" (opened 2026-11-20): https://iclr.cc/virtual/2023/oral/12647. The arXiv page calls v3 the ICLR camera-ready version.
- **AgentProcessBench**, the long traces (P2.4). S. Fan, X. Ye, Y. Huo and 9 more, "AgentProcessBench: Diagnosing Step-Level Process Quality in Tool-Using Agents", 2026. https://arxiv.org/abs/2603.14465
  - Dataset, MIT on Hugging Face, revision in `versions.md`: https://huggingface.co/datasets/LulaCola/AgentProcessBench
- **The benchmarks it draws on** (the trace's task types):
  - G. Mialon, C. Fourrier, C. Swift and 3 more, "GAIA: a benchmark for General AI Assistants", 2023. https://arxiv.org/abs/2311.12983
  - Z. Yang, P. Qi, S. Zhang and 4 more, "HotpotQA: A Dataset for Diverse, Explainable Multi-hop Question Answering", 2018, EMNLP 2018. https://arxiv.org/abs/1809.09600
  - S. G. Patil, H. Mao, C. Cheng-Jie Ji, F. Yan, V. Suresh, I. Stoica, J. E. Gonzalez, "The Berkeley Function Calling Leaderboard (BFCL): From Tool Use to Agentic Evaluation of Large Language Models", ICML 2025, as the leaderboard's own page cites it. https://gorilla.cs.berkeley.edu/leaderboard.html
  - V. Barres, H. Dong, S. Ray and 2 more, "τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment", 2025. https://arxiv.org/abs/2506.07982
- **Trace replay for agent workloads.** M. Wang, Y. Yue, S. Li and 3 more, "Benchmarking LLM Serving Systems for Agentic AI Workloads with XPerf", 2026, one version, v1 of 19 Jun 2026. https://arxiv.org/abs/2608.20370 (re-opened 2026-11-20)
  - What its abstract describes: a trace-replay benchmark. It collects traces from real agent applications, synthesizes workloads from them and replays them reproducibly, over eight agentic applications.
  - Corrected 2026-11-20: section 10's first search listed it for prefix-cache thrashing during tool calls. Its abstract does not mention that.
  - For: the same method as `loadgen/`'s recorder and replayer (chapter 6).
- **Serving agent programs** (chapter 2):
  - C. Lin, Z. Han, C. Zhang and 4 more, "Parrot: Efficient Serving of LLM-based Applications with Semantic Variable", 2024, OSDI 2024. https://arxiv.org/abs/2405.19888
  - M. Luo, X. Shi, C. Cai and 8 more, "Autellix: An Efficient Serving Engine for LLM Agents as General Programs", 2025. https://arxiv.org/abs/2502.13965
  - L. Zheng, L. Yin, Z. Xie and 9 more, "SGLang: Efficient Execution of Structured Language Model Programs", 2023, NeurIPS 2024. https://arxiv.org/abs/2312.07104 (opened 2026-11-20)
    - Venue from the proceedings page, "Advances in Neural Information Processing Systems 37 (NeurIPS 2024)", Main Conference Track: https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html
    - For: RadixAttention, KV cache reuse across the calls of one program, agent control and multi-turn chat included, inside one engine. The abstract does not mention cache-aware scheduling across replicas; read the paper before saying it does.
  - R. Abhyankar, Z. He, V. Srivatsa and 2 more, "InferCept: Efficient Intercept Support for Augmented Large Language Model Inference", 2024, ICML 2024. https://arxiv.org/abs/2402.01869 (opened 2026-11-20)
    - Venue from PMLR, vol. 235, pp. 81–95: https://proceedings.mlr.press/v235/abhyankar24a.html
    - For: engines end generation at each tool call and recompute the context afterwards, 37–40% of forwarding time by its count. It handles the interception inside the engine instead, for 1.6–2x the throughput. The one-engine form of the cost that task affinity avoids across pods.

## 9. Application stack and formats

- **FastAPI** (0.143.0; agent runtime and app backend): https://fastapi.tiangolo.com/
- **Angular** (22.2): https://angular.dev/
- **Redis** (8.10.2; run history, D7): https://redis.io/ (the home page opened; `https://redis.io/docs/` and `https://redis.io/docs/latest/` answered the check with 404, so no docs link is given)
- **SQLite** (app data, D7): https://www.sqlite.org/docs.html
- **uv** (0.13.0): https://docs.astral.sh/uv/
- **JSON Schema**, the specification the files in `schemas/` follow (D12): https://json-schema.org/specification
- **Server-sent events**, HTML Standard section 9.2 (run events to the UI, D6): https://html.spec.whatwg.org/multipage/server-sent-events.html
- **scrypt**, RFC 7914 (password hashing, P2.10): https://www.rfc-editor.org/rfc/rfc7914

## 10. Cache pressure and thrashing (P4.9-P4.12)

Found 2026-11-16 by one web search while planning cards 53-56; the arXiv pages were opened afterwards for the details below. Continuum and KVFlow were added on 2026-11-20 by a second search. None of these papers has been read. Read MARS and CONCUR before P4.9's text is written.

- **CONCUR.** Q. Chen, Z. Ye, T. Tang and 7 more, "CONCUR: High-Throughput Agentic Batch Inference of LLM via Congestion-Based Concurrency Control", 2026. https://arxiv.org/abs/2601.22705 · ICML 2026 poster, from the search and not opened: https://icml.cc/virtual/2026/poster/63743
  - Its abstract, as the check summarised it: batch inference for agents causes "middle-phase thrashing", KV cache efficiency collapsing as long-lived agents pile up state; CONCUR regulates how many agents are active from runtime cache signals; throughput up to 4.09x on Qwen3-32B and 1.9x on DeepSeek-V3. The abstract does not mention scaling replicas or pods.
  - An earlier listing gave the title "Concur: Proactive Agent-Level Admission Control for Efficient Agentic Batch Inference".
  - For this thesis: it cures thrashing by letting fewer agents in, on one engine; P4.9 measures the effect on two small pods and P4.12 adds pods instead.
- **MARS.** Y. Wang, T. Yang, H. Ye and 9 more, "MARS: Towards High Goodput and Low KV Thrashing in Agent Serving under GPU Memory Constraints", first submitted 2026, v3 of 10 Nov 2026. https://arxiv.org/abs/2604.26963
  - The search listed it under an older title, "MARS: Efficient, Adaptive Co-Scheduling for Heterogeneous Agentic Systems"; the page's comment says "Major revision with updated title, system design, and expanded evaluation".
  - For this thesis: by its title the closest to cache pressure on a memory-limited GPU. Check what it measures and whether it defines a ratio like P4.9's.
- **CacheWise.** S. Tiwari, T. Chugh, N. Rickert and 3 more, "CacheWise: Understanding Workloads and Optimizing KVCache Management for Efficiently Serving LLM Coding Agents", 2026. https://arxiv.org/abs/2606.16824
  - Search summary: serving systems evict KV per request and ignore reuse across a session; it proposes an agent-aware KV management layer. Eviction inside the engine, where this thesis stays outside an unmodified vLLM.
- **Continuum.** H. Li, R. He, Q. Mang and 8 more, "Continuum: Efficient and Robust Multi-Turn LLM Agent Scheduling with KV Cache Time-to-Live", first submitted 4 Nov 2025, v7 of 8 Sep 2026. https://arxiv.org/abs/2511.02230 (opened 2026-11-20)
  - Venue: a poster at the ICLR 2026 workshop "Lifelong Agents: Learning, Aligning, Evolving", not the main conference: https://iclr.cc/virtual/2026/10012473. That page lists 10 authors; arXiv lists 11, adding Huanchen Zhang.
  - Its abstract: on a tool call it pins the agent's KV cache in GPU memory for a time-to-live set from the reload cost and the queueing an eviction would cause; with program-level first-come-first-served scheduling it improves job completion time by over 8x. Built on vLLM.
  - For this thesis: the engine's answer to the cache a run holds while it waits on a tool, which P4.9's pressure counts.
- **KVFlow.** Z. Pan, A. Patel, Z. Hu and 6 more, "KVFlow: Efficient Prefix Caching for Accelerating LLM-Based Multi-Agent Workflows", 2025, NeurIPS 2025. https://arxiv.org/abs/2507.07400 (opened 2026-11-20)
  - Venue from the proceedings page, "Advances in Neural Information Processing Systems 38 Main Conference (NeurIPS 2025)", which orders Shen before Hu: https://papers.neurips.cc/paper_files/paper/2025/hash/b7971d31a7d5eb0f1eed2f8f6f368195-Abstract-Conference.html
  - Its abstract: LRU eviction drops an agent's KV just before it is reused; KVFlow evicts by "steps-to-execution" from an Agent Step Graph and prefetches from CPU to GPU, up to 2.19x over SGLang.
  - For this thesis: ranking by steps left, inside one engine's eviction, where remaining work ranks it for the pool's size.
- **Opened 2026-11-20 but not recorded in detail** (take the title and authors from the page before citing):
  - https://arxiv.org/abs/2609.18849, "Ask the Tool, Don't Guess": tool calls report their progress to drive KV retention. The nearest found to the remaining-work signal.
  - https://arxiv.org/abs/2606.09613 (AgentServeSim) · https://arxiv.org/abs/2608.14624 (CacheScout) · https://arxiv.org/abs/2605.06472 (PBKV)
- **The classical result the analogy rests on.** The ACM Digital Library refused the automated check (HTTP 403), so neither ACM page was opened. The details are from Crossref, OpenAlex and Denning's publication list, https://ae-info.org/ae/User/Denning_Peter/Publications, checked 2026-11-20:
  - P. J. Denning, "The Working Set Model for Program Behavior", Communications of the ACM 11(5), pp. 323–333, May 1968. https://doi.org/10.1145/363095.363141
  - P. J. Denning, "Thrashing: Its Causes and Prevention", Proceedings of the December 9-11, 1968, Fall Joint Computer Conference, Part I (AFIPS '68 Fall, part I), pp. 915–922. https://doi.org/10.1145/1476589.1476705. Crossref gives the start page and the short title "Thrashing"; the end page is from Denning's list only.
  - For: the KV cache as the memory and a run's context as its working set.
- **What the searches did not find:** anyone scaling Kubernetes replicas on the live runs' context. The autoscalers in section 11 read engine metrics: AIBrix names the queue, and the production stack's KEDA page scales on it. These are searches, not a literature review: they support "not found so far" and nothing stronger.

## 11. Routing and scaling across replicas (chapter 2)

Opened 2026-11-20. The systems and papers that this thesis's routing and autoscaling stand next to. None of the papers has been read; the systems' entries quote their docs.

- **ConServe.** J. Ding, R. Hosseini, P. M. Gholami and 2 more, "Observation, Not Prediction: Conversation-Level Disaggregated Scheduling for Agentic Serving", 2026, v1 of 1 Jun 2026. https://arxiv.org/abs/2606.01839
  - Its abstract: it schedules a whole conversation as one unit, pins it to one decoder for its tail, and places it by its first turn's length and each decoder's KV occupancy.
  - For this thesis: the closest found to task affinity with least context first, in disaggregated (prefill and decode) serving rather than a Kubernetes gateway. Read it first: it is the paper chapter 2 has to answer.
- **Preble.** V. Srivatsa, Z. He, R. Abhyankar and 2 more, "Preble: Efficient Distributed Prompt Scheduling for LLM Serving", first submitted 8 May 2024, ICLR 2025. https://arxiv.org/abs/2407.00023
  - Venue from the proceedings page, "International Conference on Learning Representations 2025 (ICLR 2025)": https://proceedings.iclr.cc/paper_files/paper/2025/hash/5bc342f48de8264779952fac378f96dc-Abstract-Conference.html
  - Its abstract: the first distributed platform built for prompt sharing; its scheduler weighs KV reuse against load across GPUs. AIBrix ships a routing strategy named after it (`prefix-cache-preble`).
- **Llumnix.** B. Sun, Z. Huang, H. Zhao and 4 more, "Llumnix: Dynamic Scheduling for Large Language Model Serving", 2024, OSDI 2024. https://arxiv.org/abs/2406.03243
  - Venue: the arXiv comment says "To appear at OSDI '24"; the USENIX page gives pp. 173–191: https://www.usenix.org/conference/osdi24/presentation/sun-biao
  - Its abstract: it reschedules requests across instances at runtime by live-migrating them with their KV state, and differentiates priorities.
  - For this thesis: the alternative to what happens here when a run moves (P3.4, P4.3, P4.4): its next call recomputes the context on the new pod.
- **AIBrix.** The AIBrix Team, J. Shan, V. Gupta and 24 more, "AIBrix: Towards Scalable, Cost-Effective Large Language Model Inference Infrastructure", 2025, v1 of 22 Feb 2025. https://arxiv.org/abs/2504.03648
  - Repository: https://github.com/vllm-project/aibrix ("GitHub - vllm-project/aibrix: Cost-efficient and pluggable Infrastructure components for GenAI inference · GitHub")
  - Routing, "Gateway Routing — AIBrix": https://aibrix.readthedocs.io/latest/features/gateway-routing/gateway-plugins.html
    - Strategies include `prefix-cache`, `prefix-cache-preble`, `least-kv-cache`, `least-request`, `throughput` and the `slo*` family.
    - `session-affinity`, "sticky session routing that keeps related requests on the same pod", keyed on `x-session-id` or `x-aibrix-session-key` "for concurrent agentic requests".
  - Prefix-cache routing "sends each request to a pod that already holds the longest matching prefix": https://aibrix.readthedocs.io/latest/features/gateway-routing/prefix-cache-aware-routing.html ("Prefix-Cache Aware Routing — AIBrix")
  - Autoscaling, "Autoscaling — AIBrix": https://aibrix.readthedocs.io/latest/features/scaling-performance/autoscaling/autoscaling.html
    - A `PodAutoscaler` with HPA, KPA and APA strategies and an optimizer-based option.
    - It scales on the engine's metrics ("vllm:num_requests_waiting for vLLM").
  - For this thesis: the nearest Kubernetes-native system, with cache-aware and session routing and an LLM autoscaler, co-designed with vLLM. Its docs show session affinity; they did not show a task's steps left or its context as a signal.
- **vLLM production stack.** https://github.com/vllm-project/production-stack ("GitHub - vllm-project/production-stack: vLLM’s reference system for K8S-native cluster-wide deployment with community-driven performance optimization · GitHub")
  - The router's routing logics in the code (`src/vllm_router/parsers/parser.py`): `roundrobin, session, kvaware, loadaware, prefixaware, disaggregated_prefill, disaggregated_prefill_orchestrated, priority`.
    - Session routing hashes a header onto a consistent hash ring (`--routing-logic session`, `--session-key`).
    - The router's README still lists only `roundrobin` and `session`.
  - "Prefix Aware Routing — production-stack": https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html
  - "KV Cache Aware Routing — production-stack", requests go "to the instance with the highest KV cache hit rate": https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/kv-cache-aware-routing.html
  - Its KEDA page is in section 3.
- **NVIDIA Dynamo.** https://github.com/ai-dynamo/dynamo ("GitHub - ai-dynamo/dynamo: A Datacenter Scale Distributed Inference Serving Framework · GitHub")
  - Router docs, "Router | NVIDIA Dynamo Documentation": https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/router/overview (the README's link, https://docs.nvidia.com/dynamo/components/router, redirects here)
  - "The router scores eligible workers using cache overlap and projected load." Workers report their cache state through events, and the router estimates it when they cannot. The README lists a Gateway API Inference Extension Endpoint Picker plugin for Dynamo.
  - For this thesis: cache-aware routing from the engine's own cache events, where llm-d's default prefix scorer estimates from routing history (section 4).
- **LiteLLM.** "LiteLLM — Open-Source AI Gateway & LLM Proxy": https://www.litellm.ai/ · https://github.com/BerriAI/litellm
  - Router strategies, "Router - Load Balancing | liteLLM": https://docs.litellm.ai/docs/routing
    - `simple-shuffle` (the default), `least-busy`, `latency-based-routing`, `usage-based-routing` and its v2, `cost-based-routing`, and custom. None scores a replica's prefix or KV cache.
    - A "Session Affinity" pre-call check pins a conversation to the deployment that served its first request. The id comes from `x-litellm-session-id`, any `x-<vendor>-session-id` header or `metadata.session_id`; the pin has an idle TTL, `deployment_affinity_ttl_seconds`, default 3600.
  - "Cache-Aware Routing | liteLLM": https://docs.litellm.ai/docs/proxy/cache_aware_routing
    - Session-to-pod stickiness for pods behind a headless Service: "LiteLLM provides session affinity; it does not inspect the contents of each pod's cache or guarantee a cache hit."
  - For this thesis: the common gateway outside Kubernetes-native inference. It has session stickiness, and no view of a pod's cache or a task's progress.
