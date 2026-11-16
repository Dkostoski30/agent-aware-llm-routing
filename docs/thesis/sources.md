# Sources

References collected as they come up (`outline.md`, Литература), until the FCSE template decides between a `.bib` and a reference list. Versions are the ones pinned in `versions.md`. Dev tools (ruff, mypy, pytest and the like) are not listed: `versions.md` holds them.

How to read an entry:
- Every link was opened on 2026-11-16 and showed the title given here, unless the entry says "not opened", "check failed" or "from memory". Opened means the link works. It does not mean the page was read for a claim: read the source before citing what it says.
- A paper's title, authors and year are as its arXiv page shows them. A venue is given only where that page states one.
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
  - Priority scheduling (D3, P4.1): **not located.** Neither `--scheduling-policy` nor a per-request `priority` field was on the two serving pages read; the CLI reference was not opened. P4.1's spike should record the page.
- **OpenAI Chat Completions API reference**, the request format vLLM serves: https://platform.openai.com/docs/api-reference/chat (not opened: the site answered the automated check with HTTP 403; open it by hand).

## 2. Models and fine-tuning

- **Qwen3.** A. Yang, A. Li, B. Yang and 57 more, "Qwen3 Technical Report", 2025. https://arxiv.org/abs/2505.09388
- **Model cards**, all Apache-2.0; revisions in `versions.md`:
  - https://huggingface.co/Qwen/Qwen3-0.6B-GPTQ-Int8 (experiments, infrastructure demo, D8)
  - https://huggingface.co/Qwen/Qwen3-1.7B-GPTQ-Int8 (practical demo, D8)
  - https://huggingface.co/Qwen/Qwen3-0.6B (base of the P1.7 adapter)
- **GPTQ.** E. Frantar, S. Ashkboos, T. Hoefler and 1 more, "GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers", 2022, ICLR 2023. https://arxiv.org/abs/2210.17323
  - For: the Int8 weights that let two replicas fit in 4 GB (D8).
- **LoRA.** E. J. Hu, Y. Shen, P. Wallis and 5 more, "LoRA: Low-Rank Adaptation of Large Language Models", 2021. https://arxiv.org/abs/2106.09685 (no venue on the arXiv page; ICLR 2022 from memory, confirm)
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
- **Helm** (4.3.0; the docs showed 4.3.1): https://helm.sh/docs/

## 4. Gateway and routing

- **Gateway API** (CRDs v1.6.3): https://gateway-api.sigs.k8s.io/docs
- **Gateway API Inference Extension** (v1.6.2; InferencePool, the Endpoint Picker protocol): https://gateway-api-inference-extension.sigs.k8s.io/ · https://github.com/kubernetes-sigs/gateway-api-inference-extension
- **llm-d**: https://llm-d.ai/ (its docs are labelled v1.0)
- **llm-d-router** (v0.11.0, the Endpoint Picker the plugin is built into, P3.1-P3.6). "llm-d Router: The intelligent entry point for inference requests". https://github.com/llm-d/llm-d-router · releases, where v0.11.0 is listed: https://github.com/llm-d/llm-d-router/releases
  - The Inference Extension's README says the Endpoint Picker moved to this repository (from the check's summary of that README), as the spike of 2026-10-27 found (`experiments/log.md`).
- **agentgateway** (v1.6.0, the gateway, P1.2). "Next Generation Agentic Proxy for AI Agents and MCP servers". https://agentgateway.dev/ · https://github.com/agentgateway/agentgateway

## 5. The GPU in Kubernetes under WSL2

- **NVIDIA device plugin** (0.20.1): https://github.com/NVIDIA/k8s-device-plugin
  - Its README's "Shared Access to GPUs" section, "With CUDA Time-Slicing", is what gives the node 2 GPU slots (P0.7).
- **NVIDIA Container Toolkit** (1.20.1, P0.2): https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/index.html
- **CUDA on WSL User Guide** (P0.1): https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- **WSL advanced settings**, `.wslconfig` (P0.1's memory and swap): https://learn.microsoft.com/en-us/windows/wsl/wsl-config

## 6. Simulation

- **llm-d-inference-sim** (v1.0.0, P2.7), a simulator that mimics vLLM's behaviour without GPUs: https://github.com/llm-d/llm-d-inference-sim
- **Vidur.** A. Agrawal, N. Kedia, J. Mohan and 5 more, "Vidur: A Large-Scale Simulation Framework For LLM Inference", 2024. https://arxiv.org/abs/2405.05465 (no venue on the arXiv page; `outline.md` says MLSys 2024, confirm)

## 7. Monitoring

- **Prometheus** (v3.15.0): https://prometheus.io/docs/introduction/overview/
  - Exposition formats, the text format the run tracker writes by hand (P3.8): https://prometheus.io/docs/instrumenting/exposition_formats/
- **Prometheus Operator** (v0.94.1; PodMonitor and ServiceMonitor): https://prometheus-operator.dev/
- **kube-prometheus-stack** (chart 92.3.0, P1.1): https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack
- **kube-state-metrics** (v2.20.0; `kube_deployment_spec_replicas` in P3.9b's series): https://github.com/kubernetes/kube-state-metrics
- **Grafana** (13.2.3): https://grafana.com/docs/grafana/latest/

## 8. Agents, the workload and related serving work

- **The Transformer.** A. Vaswani, N. Shazeer, N. Parmar and 5 more, "Attention Is All You Need", 2017. https://arxiv.org/abs/1706.03762 (no venue on the arXiv page; NeurIPS 2017 from memory, confirm)
  - For: the attention keys and values that the KV cache keeps (chapter 3).
- **The agent loop.** S. Yao, J. Zhao, D. Yu and 4 more, "ReAct: Synergizing Reasoning and Acting in Language Models", 2022. https://arxiv.org/abs/2210.03629 (the arXiv page calls v3 the ICLR camera-ready version; the year, 2023, is from memory)
- **AgentProcessBench**, the long traces (P2.4). S. Fan, X. Ye, Y. Huo and 9 more, "AgentProcessBench: Diagnosing Step-Level Process Quality in Tool-Using Agents", 2026. https://arxiv.org/abs/2603.14465
  - Dataset, MIT on Hugging Face, revision in `versions.md`: https://huggingface.co/datasets/LulaCola/AgentProcessBench
- **The benchmarks it draws on** (the trace's task types):
  - G. Mialon, C. Fourrier, C. Swift and 3 more, "GAIA: a benchmark for General AI Assistants", 2023. https://arxiv.org/abs/2311.12983
  - Z. Yang, P. Qi, S. Zhang and 4 more, "HotpotQA: A Dataset for Diverse, Explainable Multi-hop Question Answering", 2018, EMNLP 2018. https://arxiv.org/abs/1809.09600
  - S. G. Patil, H. Mao, C. Cheng-Jie Ji, F. Yan, V. Suresh, I. Stoica, J. E. Gonzalez, "The Berkeley Function Calling Leaderboard (BFCL): From Tool Use to Agentic Evaluation of Large Language Models", ICML 2025, as the leaderboard's own page cites it. https://gorilla.cs.berkeley.edu/leaderboard.html
  - V. Barres, H. Dong, S. Ray and 2 more, "τ²-Bench: Evaluating Conversational Agents in a Dual-Control Environment", 2025. https://arxiv.org/abs/2506.07982
- **Serving agent programs** (chapter 2):
  - C. Lin, Z. Han, C. Zhang and 4 more, "Parrot: Efficient Serving of LLM-based Applications with Semantic Variable", 2024, OSDI 2024. https://arxiv.org/abs/2405.19888
  - M. Luo, X. Shi, C. Cai and 8 more, "Autellix: An Efficient Serving Engine for LLM Agents as General Programs", 2025. https://arxiv.org/abs/2502.13965

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

Found 2026-11-16 by one web search while planning cards 53-56; the arXiv pages were opened afterwards for the details below. None of these papers has been read. Read MARS and CONCUR before P4.9's text is written.

- **CONCUR.** Q. Chen, Z. Ye, T. Tang and 7 more, "CONCUR: High-Throughput Agentic Batch Inference of LLM via Congestion-Based Concurrency Control", 2026. https://arxiv.org/abs/2601.22705 · ICML 2026 poster, from the search and not opened: https://icml.cc/virtual/2026/poster/63743
  - Its abstract, as the check summarised it: batch inference for agents causes "middle-phase thrashing", KV cache efficiency collapsing as long-lived agents pile up state; CONCUR regulates how many agents are active from runtime cache signals; throughput up to 4.09x on Qwen3-32B and 1.9x on DeepSeek-V3. The abstract does not mention scaling replicas or pods.
  - An earlier listing gave the title "Concur: Proactive Agent-Level Admission Control for Efficient Agentic Batch Inference".
  - For this thesis: it cures thrashing by letting fewer agents in, on one engine; P4.9 measures the effect on two small pods and P4.12 adds pods instead.
- **MARS.** Y. Wang, T. Yang, H. Ye and 9 more, "MARS: Towards High Goodput and Low KV Thrashing in Agent Serving under GPU Memory Constraints", first submitted 2026, v3 of 10 Nov 2026. https://arxiv.org/abs/2604.26963
  - The search listed it under an older title, "MARS: Efficient, Adaptive Co-Scheduling for Heterogeneous Agentic Systems"; the page's comment says "Major revision with updated title, system design, and expanded evaluation".
  - For this thesis: by its title the closest to cache pressure on a memory-limited GPU. Check what it measures and whether it defines a ratio like P4.9's.
- **XPerf.** M. Wang, Y. Yue, S. Li and 3 more, "Benchmarking LLM Serving Systems for Agentic AI Workloads with XPerf", 2026. https://arxiv.org/abs/2608.20370
  - The check returned a first-submission date of 19 Jun 2026, which does not fit the identifier's month (2608 is August 2026). Look at the page before giving a date.
  - Search summary: a benchmark of agentic workloads that reports prefix-cache thrashing, the engine evicting an agent's context during tool calls and recomputing it.
- **CacheWise.** S. Tiwari, T. Chugh, N. Rickert and 3 more, "CacheWise: Understanding Workloads and Optimizing KVCache Management for Efficiently Serving LLM Coding Agents", 2026. https://arxiv.org/abs/2606.16824
  - Search summary: serving systems evict KV per request and ignore reuse across a session; it proposes an agent-aware KV management layer. Eviction inside the engine, where this thesis stays outside an unmodified vLLM.
- **The classical result the analogy rests on**, both from memory, no link looked up; confirm the details before citing:
  - P. J. Denning, "The Working Set Model for Program Behavior", Communications of the ACM 11(5), 1968.
  - P. J. Denning, "Thrashing: Its Causes and Prevention", AFIPS Fall Joint Computer Conference, 1968.
  - For: the KV cache as the memory and a run's context as its working set.
- **What the search did not find:** anyone scaling Kubernetes replicas on this signal. That is one search with one query, not a literature review: it supports "not found so far" and nothing stronger.
