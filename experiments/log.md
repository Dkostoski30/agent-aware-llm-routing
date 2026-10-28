# Experiment and verification log

Newest entry first. Every task's done-when output goes here, plus anything surprising.

## Template

```
### YYYY-MM-DD · <task ID> · <green | red>
Command: <what you ran>
Result: <key lines of output, numbers>
Notes: <surprises, decisions, what changed>
```

---

### 2026-10-28 · P1.6d + P1.6e · red (no-go, both undone)
Command: `make up PROFILE=demo` · `make go-no-go`, twice: with P1.6d and P1.6e, then with P1.6d alone
Result:
- P1.6d + P1.6e (agent `thesis/agent:a7184f58c573`, prompt with the off-topic sentence): required 7/10, auto 7/10,
  0 invalid calls. No-go.
- P1.6d alone (same agent, sentence taken out as card 15e says): required 8/10, auto 6/10, 0 invalid calls. No-go.
  `demo-go-no-go-1f9b63bc24ca-a7184f58c573.csv` is on branch `wip/p1.6d-calculator-expression` with the code that
  made it; the first run's CSV had the same name and was overwritten.
- group-total ("total revenue of the North region"): 1/4 in the first run, 0/4 in the second, against 4/4 in P1.6c.
  In every run read from Redis the model called group_by(region, revenue, sum), got North's 14351.1, then called
  calculator("14351.1 + 11341.8 + 10622.2 + 9551.0") and answered 45,866.10.
- two-step, auto: 0/2 in both runs, as in P1.6c. The answer reads right ("Widget ... 15116.5"), but the model
  counted rows per product (all tie at 12) and compared only Widget and Gadget, so Widget is a guess and the
  scorer's expected group_by(product, units, sum) call is missing. required two-step lost one seed in the first run
  (Gizmo, the most revenue, named as the most units).
Notes:
- The whole-expression calculator is the cause, not the off-topic sentence: without the sentence group-total still
  fails every run. With one operation per call (P1.6c) the 1.7B never added the groups; an expression invites it.
- Rule 8: two no-gos with this calculator, so P1.6d and P1.6e are undone and Gate 1 stands on P1.6c (required
  10/10). Decided with the user: next, P1.6f offers the calculator only on runs without files, on a green gate, and
  starts from the saved branch. The 0.6B keeps answering 2x2x2 with 4 until then.
- go_no_go.py names its result after the experiment file and the agent image tag, not the profile, so a run with
  only a prompt change overwrites the previous result. Reported, not fixed (outside these cards).

### 2026-10-28 · P1.5 · green
Command: on the demo profile `make smoke` · `kubectl scale deployment/agent --replicas=0` · `make smoke` ·
`kubectl scale deployment/agent --replicas=1` · `make smoke` · `make agent-check`; then `make down && make up` (real)
· `make smoke` · `make agent-check`
Result:
- Demo (one Qwen3-1.7B pod, agent `thesis/agent:0d822ba16e8e`, tool_choice required), first run:
  ```
  ok pods: vllm 1/1 inference-gateway 1/1 vllm-pool-epp 1/1 redis 1/1 agent 1/1
  ok agent: run run-5b9078ffd703 (What is 7 times 8?, seed 7) done in 4 s, answer: 56
  ok gateway: 2 lines with run-id run-5b9078ffd703, steps [1,2], endpoints ["10.42.0.12:8000"]
  ok picker: 2 picks by vllm-pool-epp in the run's window, for 2 steps
  ok redis: thesis:run:run-5b9078ffd703 status done, step 2
  ok prometheus: vllm:num_requests_running has a sample for ["vllm-9d9d6db6f-x6thv"]
  ```
- Agent scaled to 0: `FAIL pods: agent: all of at least 1 pod Ready expected, 0 of 0 Ready`, exit 1.
- Agent back at 1: six ok lines again, run `run-deefd9f23c4d`, 4 s, same steps and pod.
- `make agent-check` OK on demo (run `run-380acb21befb`: calculator multiply(7,8), then final_answer "56") with the
  shared calls from `infra/checks/agent_calls.sh`.
- Real, smoke run right after `make up` exit 0 (two Qwen3-0.6B pods, same agent image):
  ```
  ok pods: vllm 2/2 inference-gateway 1/1 vllm-pool-epp 1/1 redis 1/1 agent 1/1
  ok agent: run run-ffd2dab2050d (What is 7 times 8?, seed 7) done in 14 s, answer: The result is 56.
  ok gateway: 2 lines with run-id run-ffd2dab2050d, steps [1,2], endpoints ["10.42.0.13:8000"]
  ok picker: 2 picks by vllm-pool-epp in the run's window, for 2 steps
  ok redis: thesis:run:run-ffd2dab2050d status done, step 2
  ok prometheus: vllm:num_requests_running has a sample for ["vllm-5fff45f674-4687g","vllm-5fff45f674-d5rrf"]
  ```
- `make agent-check` OK on real (run `run-a5ad24db34bf`, 2 steps on 10.42.0.13, text answer "The result is 56.").
Notes:
- Both D10 modes pass the same check: required ends with final_answer ("56"), auto answers in text. Both take 2 steps,
  so expected-steps 2 holds in both.
- The real smoke ran 12 s after the agent pod started and right after the vLLM pods loaded: the run took 14 s against
  4 s on the warm demo cluster, as in P1.3b's first run on fresh pods (26.8 s). It went to the agent pod's IP, so the
  Service lag seen in P1.6c could not hit it.
- The picker check is linked to the run by time only: the EPP's "Request handled" line has its own x-request-id but
  no run-id, and the access log does not carry that id.
- The pods check compares each Deployment's Ready pods with its own `spec.replicas`, not with the profile in
  `config/`: that works on any profile without a PROFILE argument and stays right once KEDA sets the vllm count, but a
  vllm Deployment scaled down by hand would pass.
- After the review, every failure path prints its "FAIL <layer>" line (vllm pod list, a run state that is not JSON,
  EPP log read and parse reported apart); the passing output above is unchanged.
- The cluster is on the real profile again.

### 2026-10-28 · P1.6c · green (go)
Command: `make test` · `make lint` · `make agent-deploy PROFILE=demo` · `make go-no-go`
Result:
- `make go-no-go`: 20 rows in `experiments/results/demo-go-no-go-1f9b63bc24ca-0d822ba16e8e.csv` (experiment file
  `1f9b63bc24ca`, agent `thesis/agent:0d822ba16e8e`). required 10/10 correct, 0 invalid calls; auto 8/10 correct,
  1 invalid call (compare seed 2, corrected on the retry). Decision: go; config/demo.yaml keeps agent.tool_choice
  required.
- Against P1.6b (required 6/10, auto 7/10), same questions and seeds:
  - compare: required 0/2 -> 2/2, auto 1/2 -> 2/2. required copies the new summary sentence word for word ("North's
    sum of revenue (14351.1) is 3728.9 higher than South's (10622.2), 35.1% more."); auto words it itself, with the
    right direction.
  - two-step: required 0/2 -> 2/2: it now sums units per product ("Widget ... 617.0 units ... 15116.5"). auto
    0/2 -> 0/2: both runs name Gizmo with Widget's 617 units and Gizmo's revenue, so the products from the two
    group_by results get mixed up.
  - summary, group-total, group-mean: 12/12 as before. Numbers come rounded ("17.83", not "17.833333333333332").
- required takes 3-6 steps and 6-14 s per run, auto 2-5 steps and 3-24 s.
Notes:
- The tools changed, not the model or the prompt: compare returns higher, lower, a non-negative difference,
  percent_more and a summary sentence; agg count is now row_count; CSV tool numbers are rounded to
  agent.result_decimals (2).
- Three reruns failed before this one, none of them on the model:
  - The first crashed in the switch to auto with a connect timeout. The auto pod was Ready, but the node's Service
    rules still led to the deleted required pod more than 10 s after it was gone (k3s API stalls, as in P0.7, P1.1,
    P1.2). Second failure of reaching the restarted agent through the Service, so per rule 8 it stopped there; decided
    with the user: go_no_go.py sends the runs to the one Ready agent pod's IP.
  - The same run scored both required compare answers wrong: wrong_if_matches read the "35.1% more" after South's
    value as a claim about South. It now needs the comparative's "than" within two words. That sentence is a test.
  - The next one stopped at the first switch: ready_pod_ip compared whole condition dicts, and real conditions carry
    timestamps too, so no pod was Ready. The test pods now carry them.
- Result files now carry the agent image tag (`<experiment>-<yaml hash>-<agent tag>.csv`), so P1.6b's
  `demo-go-no-go-9440b109d5fa.csv` belongs to agent `c61bb199c383`.
- The demo should use required: auto's remaining failure is a two-step question, the kind of multi-call question the
  demo needs most. Open for P2: auto stays the mode of the real and sim profiles (D10), where traffic shape matters
  more than answers.

### 2026-10-28 · P1.6b · red (no-go)
Command: `make down && make up PROFILE=demo` · `make test` · `make go-no-go`, run three times (see notes)
Result:
- `make up PROFILE=demo` exit 0: `thesis/agent:c61bb199c383` imported in 8.3 s; redis and agent Running 1/1 next to
  one vLLM pod (Qwen3-1.7B-GPTQ-Int8).
- `make test`: 311 passed, 32 of them the new scorer tests.
- Final scorer, `experiments/results/demo-go-no-go-9440b109d5fa.csv`, 20 rows: required 6/10 correct, 0 invalid
  calls; auto 7/10 correct, 1 invalid call (group-mean seed 1, corrected on the retry). Decision: no-go.
  config/demo.yaml keeps agent.tool_choice required.
- Per question, both modes and seeds together: summary 4/4, group-total 4/4, group-mean 4/4, compare 1/4,
  two-step 0/4.
- required takes 3-6 steps and 6-15 s per run, auto 2-4 steps and 3-13 s: in required mode the model opens with
  csv_summary on most questions and needs one more call for final_answer.
Notes:
- First run, scored as planned (a number in the answer within 1%, the expected tool ran): required 10/10,
  auto 9/10, "go". The run histories in Redis showed it passed wrong runs:
  - compare: all 4 runs called `compare(left=North, right=South)`, got `difference: -3728.9`, and 3 answered
    "North made $3,728.90 less than South". The scorer read numbers without their sign.
  - two-step: all 4 runs ranked the products with `group_by(agg=count)`. Every product has 12 rows, so the top
    product was a guess: Widget 3 times (right by luck, one of them also saying Widget sold "a total of 12" units,
    not 617), Gadget once.
- Scorer strengthened (decided 2026-10-28): `expected_calls` names a call's arguments where the number does not
  prove the call (two-step: group_by by product, value units, agg sum), and `wrong_if_matches` rejects a compare
  answer that says North is lower. Re-scoring the first run's 20 histories from Redis gave 6/10 and 7/10, the
  same as by hand.
- Second run crashed when it switched to auto: its first auto run ended with final_answer, so the old required
  pod answered it. That pod was still terminating after `kubectl rollout status` returned, and the script's pooled
  keep-alive connection still led to it; the next poll was refused when it exited. Fixed in go_no_go.py: the
  switch waits until the old pod is deleted, each mode gets a new HTTP client, and every done run is checked for
  the mode that served it (final_answer only in required). The first run was clean: all 10 required runs ended
  with final_answer, none of the 10 auto runs did.
- Third run, before that fix: the same 20 answers word for word as the first run. With fixed seeds the demo model
  is deterministic here, so a tool change can be compared run for run.
- The failures are in how two tool results read, not in the model: the three single-aggregate kinds are 12/12 in
  both modes. Next (decided 2026-10-28): a card that makes compare name the higher group with a positive
  difference and gives group_by's row count a name that cannot pass for a total, then `make go-no-go` on the
  unchanged experiment. Fine-tuning was considered and dropped: GPTQ weights cannot be trained, QLoRA on the FP16
  1.7B barely fits the ~3.2 GiB of free VRAM, it needs new dependencies and training data, and it does not target
  the two misreadings.
- Answers copy floats as they come ("17.833333333333332"). Not wrong, but worth rounding in the tool results in the
  same card.

### 2026-10-28 · P1.3b · green
Command: `make down && make up` on a fresh cluster (the night left 4 vLLM pods Unknown and 2 never Ready after k3s
restarted), then `make agent-check`
Result:
- `make up` (2026-10-27) exit 0: deploy-agent built `thesis/agent:1027ead9b883` (~220 MB), imported it into k3s in
  9.3 s, and rolled out redis and agent; both Running 1/1 next to the two vLLM pods, the Gateway and the EPP.
- `make agent-check` OK: run `run-bcf08c6a0ef0`, "What is 7 times 8?" seed 7. Step 1 `calculator multiply(7,8)` ->
  56, step 2 "The result is 56.". Access log: `{"step":"1","expected-steps":"2","endpoint":"10.42.0.12:8000"}` and
  step 2 on the same pod. Redis `thesis:run:run-bcf08c6a0ef0`: status done, step 2, expected_steps 2,
  invalid_calls 0, 5 messages.
Notes:
- expected-steps 2 in the access log is the profile's new value, so a ConfigMap change reaches the pod through the
  thesis/config-hash restart (P1.6b switches the D10 mode this way).
- The run took 26.8 s (created_ms to updated_ms), against ~3 s per run on the warm cluster the day before: most likely
  the first requests to freshly loaded pods. RUN_TIMEOUT_SECONDS (300 s) covers it; watch it in P1.5.
- Both steps landed on one pod, as on 2026-10-27: nothing pins a run before P3.3, so this is the default picker's
  prefix-cache scorer matching the shared prompt.

### 2026-10-27 · P1.3b model check · done
Command: `make down && make up` (exit 0; redis and agent Running 1/1 next to both vLLM pods), then `make agent-check`
with the first task "What is 7 times 8 plus 3?" (answer 59), then 10 seeds per variant: the agent loop run from the host
against the Gateway's ClusterIP (scratch script, not in the repo), config/real.yaml unless noted
Result:
- Infrastructure green on the first task: access-log lines `{"step":"1","expected-steps":"3","endpoint":"10.42.0.13:8000"}`
  and step 2 on the same pod; Redis hash `thesis:run:run-58ebd4b24aa6` with status done, step 2, 5 messages. The answer
  was 21, so the check failed.
- Config prompt, seeds 1-10: 0/10. 9 seeds called `multiply(7,3)` and answered 21; seed 5 called `multiply(7,8)` and
  answered 56 without the second operation.
- Prompt telling it to split the question into operations and use the previous result: 0/10. Both calls in one response
  (`multiply(7,8)` and `add(7,3)` in parallel, before 56 is known), answers like "56 and 10".
- `parallel_tool_calls: false` (vLLM v0.31.0 keeps only the first generated call, `tool_calls[:1]`), either prompt: 0/10.
  The model answers right after the first result; no run made a second call.
- "What is 7 times 8?" with the config prompt: 10/10, `multiply(7,8)` then "The result is 56.", 2 calls each.
Notes:
- The 0.6B cannot chain two dependent tool calls with these prompts: it merges the operations, sends them at once with
  the question's numbers, or stops after the first result. CLAUDE.md rule 8 stopped the prompt attempts after two.
- Decision (2026-10-27): the calculator task of make agent-check and make smoke (P1.5) is "What is 7 times 8?", answer 56;
  agent.expected_steps_default is 2 in every profile. It still proves a chain of calls sharing one context through every
  layer. Multi-step quality is measured by P1.6b on the 1.7B demo model.
- For the thesis: real-profile runs recorded on the 0.6B (P2.2) will be short, so long agent traces for the experiments
  come from the converted AgentProcessBench traces (P2.4), not from the 0.6B's own runs.

### 2026-10-27 · P1.2 · green
Command: `make down && make up` · `make gateway-check` · `kubectl top pods -A` · `kubectl top nodes`, one sequence
on a fresh cluster with the final files (16:21 UTC, after the review fixes below)
Result:
- `make up` exit 0: CRDs applied; the three charts pulled by digest (`agentgateway-crds@sha256:dd7f965d…`,
  `agentgateway@sha256:d3edf2b3…`, `llm-d-router-gateway@sha256:63a79a84…`); `agentgateway` (agentgateway-system),
  `inference-gateway` and `vllm-pool-epp` (default) Running next to both vLLM pods.
- `make gateway-check` OK, about a minute after the proxy rolled out (cold Gateway): 21 requests, 9 to
  vllm-5fff45f674-gcsrk and 12 to vllm-5fff45f674-ldld8. The header request's access-log line carries
  `"run-id":"p12-check","step":"1","expected-steps":"1"`, endpoint `10.42.0.13:8000` and `"duration":"95ms"`; no
  `parallel-group`, as the request sent none. `vllm-pool-epp logged 21 choices for 21 requests`.
- `kubectl top` (same run): agentgateway controller 3m / 63Mi, inference-gateway proxy 2m / 50Mi, vllm-pool-epp
  229m / 63Mi; gateway total 176Mi. vLLM pods 226m / 2737Mi and 517m / 2742Mi. Grafana 674Mi, Prometheus 430Mi.
  Node: 1337m CPU (8%), 8854Mi memory (89%).
- The run before the review fixes (16:01 UTC, also cold) passed the same way: 10/11, 66ms, gateway total 169Mi.
Notes:
- The EPP is the busiest pod outside vLLM at idle (177m and 229m CPU in the two runs): its `metrics-data-source` plugin scrapes every pod's `/metrics` on
  a short interval, and `--v=3` logging adds to it. Negligible at 2 pods; in the simulator experiments (P2.7+) the
  scrape cost grows with the pod count, so measure it there instead of assuming it stays small.
- Memory headroom: Grafana and Prometheus together (1.1Gi) use more than the whole gateway stack. If P1.3b
  (agent + Redis) runs short of RAM, they are the first place to look.
- Review fixes between the two runs (fresh-eyes review, 2026-10-27): the access log uses the D12 keys `run-id`, `step`,
  `expected-steps`, `parallel-group`; the check also matches `step` and `expected-steps`; its prompts are one fixed
  short prompt, below the EPP's 64-token prefix-cache block, so only the queue and KV-cache scorers decide; charts
  are installed by digest; comments in `down.sh`, `router-values.yaml` and `gateway_check.sh` corrected.
- The first `make gateway-check` of the day, ~90 s after the proxy rolled out, failed: 3 requests timed out at 60 s, later
  batches took 13 s and 30 s, the last ones 0.1-3 s. The EPP picked every pod in ~10 ms and vLLM showed
  `Running: 0 reqs` throughout, so the time was lost in the proxy between the pick and vLLM. In the same minutes the
  agentgateway controller lost its leader lease 3 times (`Failed to renew lease ... context deadline exceeded`,
  `leader election lost`), and the proxy logged xDS `Connection refused` while the controller restarted: the k3s
  API server stalled again (same family as P0.7 and P1.1). The warm re-run passed, and so did both cold runs above.
  Open: the stall is intermittent, so two cold passes don't rule it out; P1.5 runs right after `make up` and will
  show whether it needs a wait for a warm Gateway.
- The first check also had a bug: the EPP logs OpenTelemetry-style JSON (`body`, `timestamp`), not zap's
  `msg`/`ts`. Fixed in `gateway_check.sh`; the `zap-devel` flag added for it was removed.

### 2026-10-27 · P1.2 spike · done
Command: read the release pages and the tagged source (shallow clones) of gateway-api-inference-extension,
llm-d-router, kgateway, agentgateway and envoyproxy/gateway; `helm show` of the published charts; a ghcr.io manifest
lookup for the EPP image.
Result:
- Since GAIE v1.6.0 (2026-08-17) the full Endpoint Picker lives in `llm-d/llm-d-router`; GAIE keeps only the
  InferencePool API, a conformance-only lightweight EPP (`lwepp`) and the conformance tests.
- llm-d-router v0.11.0 (2026-09-27): built on GAIE v1.5.0. Chart `oci://ghcr.io/llm-d/charts/llm-d-router-gateway`
  v0.11.0 (chart digest `sha256:63a79a842974691be73d44918c7086bcc52d6fee546ff39113517a23c03eeab0`), image
  `ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.11.0`. v1.0.0 (2026-10-16) is 11 days old and fails the
  two-week rule.
- Plugins: a rule is a Go `Scorer` (`Score(ctx, request, pods) map[Endpoint]float64`) registered with
  `plugin.Register(type, stability, factory)` and enabled in a YAML scheduling profile. A custom plugin needs a
  self-built EPP image (as P3.1 planned).
- llm-d-router already ships `sessionaffinity` and `headerlabelaffinity` scorers: related work for chapter 2,
  and P3.3 has to differ from them.
- kgateway removed InferencePool support in v2.3.0 (2026-05-15); only the 2.2 line (GAIE v1.1.0, last release
  2026-07-13) has it. Envoy Gateway v1.9.2 has InferencePool only in an example extension server.
- agentgateway v1.6.0 (2026-10-02): native InferencePool, built and conformance-tested against GAIE v1.6.2,
  `inferenceExtension.enabled` in its chart, controller requests 128Mi.
- The InferencePool v1 fields the router and agentgateway use (`selector`, `targetPorts`, `endpointPickerRef`)
  are the same in the GAIE v1.5.0 and v1.6.2 CRDs.
- Gateway API: v1.6.3 (2026-10-06) is the newest release at least two weeks old.
Notes:
- Decided with the user: llm-d-router v0.11.0 in gateway mode, agentgateway v1.6.0, GAIE CRDs v1.6.2, Gateway API
  CRDs v1.6.3 (standard channel). If the v0.11.0 EPP and agentgateway don't work together, the fallback is
  llm-d-router v1.0.0, made for GAIE v1.6.2, recorded as an exception to the two-week rule.
- RAM was not measured: it needs the install, so it moved into P1.2's done-when.
- PLAN.md's P1.2 row named Envoy Gateway or kgateway; neither supports InferencePool any more (brick wall in
  PLAN.md section 4).

### 2026-10-26 · P1.1 · green
Command: `make up` · `kubectl get pods -n monitoring` · `make service-check` · `make grafana`, dashboard `thesis-vllm`
Result:
- kube-prometheus-stack 92.3.0 installed by `make up` before the vLLM release; 4 pods Running in `monitoring`
  (grafana 3/3, operator, kube-state-metrics, prometheus 2/2).
- `make service-check` OK (12 / 10 requests per pod); the "vLLM pods" dashboard shows both pods in the queue,
  KV-cache and prefix-cache hit rate panels (user checked in the browser).
Notes:
- Operator and kube-state-metrics restarted once at 20:53:30 UTC, while both vLLM pods were loading the model:
  kube-state-metrics logged `Failed to contact API server for /livez`, failed its liveness probe and was
  restarted; the operator exited cleanly on SIGTERM. The k3s API server stopped answering for a moment under the
  model load. Not a failure of this task; watch for it in later `make up` runs (same family as the P0.7
  datastore slowness).

### 2026-10-26 · P1.0 · green
Command: `make down` · `systemctl is-active k3s; nvidia-smi` · `time make up` · `make service-check`
Result:
- `make down`: releases vllm, monitoring, nvidia-device-plugin uninstalled, both namespaces deleted, k3s stopped
  by `k3s-killall.sh`; `inactive`, `nvidia-smi` lists no process (335 MiB used by the desktop).
- `make up` from a stopped k3s: exit 0, **8 min 2 s** wall time. Device plugin back with
  `allocatable nvidia.com/gpu: 2`; model already in the cache (hf only checked 10 files); both vLLM pods
  `1/1 Running` after the rollout (4 min 29 s from install to both Ready).
- `make service-check` OK through the Service, 12 / 10 requests per pod.
Notes:
- The 8 min include the first pull of the monitoring images (P1.1, same run); the vLLM image and the model came
  from k3s's containerd and the HF cache, as `make down` keeps them.
- In k3s both pods became Ready after 4.5 min, slower than the 2-3 min in Docker (P0.3, P0.4): they load at the
  same time from one disk and one GPU.
- The second `make down` (idempotency) was not part of this run.

### 2026-10-26 · P0.7 · green · Gate 0 green
Command: `make k3s` · `make vllm-deploy PROFILE=real` · `kubectl get pods` · `make service-check`
Result:
- `make k3s`: device plugin with time-slicing, node reports `allocatable nvidia.com/gpu: 2`.
- `kubectl get pods`: `vllm-5fff45f674-5qvc7` and `vllm-5fff45f674-csj8f`, both `1/1 Running`, 0 restarts.
- `make service-check` through ClusterIP `10.43.129.213:8000`: chat answer and tool call
  `get_weather({"city": "Ohrid"})` OK; chat requests per pod **9 and 13** (2 from chat-check + 20 spread requests).
- Gate 0: two vLLM pods on the GPU answer through a Service (this entry), prefix caching visible (P0.5).
Notes:
- Getting the image into k3s, attempt 1 (`docker save | k3s ctr images import -`): after 23 min **k3s crashed**
  (`level=fatal msg="Transaction commit failed"`, then systemd restarted it). The 32 GB of image writes starved
  k3s's SQLite datastore on the same disk: 102 "Slow SQL" warnings during the import vs 13 in the two hours
  before; no journal gap long enough for a laptop sleep before the crash. Replaced (user decision) by
  `sudo k3s crictl pull`, k3s pulling the image itself; it was already complete when the deploy ran.
- First rollout: both pods in CrashLoopBackOff, `ValueError: VLLM_PORT 'tcp://10.43.129.213:8000' appears to be
  a URI`. Kubernetes injects `<SERVICE>_PORT` variables for every Service, and for the Service `vllm` that is
  `VLLM_PORT`, which vLLM reads as its own setting. Fixed with `enableServiceLinks: false` in the pod spec.
- `make vllm-deploy` waits with `kubectl rollout status` (prints each pod as it becomes Ready) instead of a silent
  `helm --wait`; the Deployment uses `Recreate`, since a rolling update's third pod could never get a GPU slot.
- Pods mount `~/.cache/huggingface` read-only with `HF_HUB_OFFLINE=1`; the model must be downloaded by
  `make vllm-run` first.
- `service_check.sh` calls the ClusterIP from the host (the host is the node) instead of `kubectl port-forward`,
  which tunnels to one pod (user decision). Readiness delay and the KV size come from `config/` (user decision).

### 2026-10-26 · P0.5 · green
Command: `make vllm-run PROFILE=real` · `make prefix-check`
Result:
- Metric names in vLLM 0.31.0: `vllm:prefix_cache_queries_total`, `vllm:prefix_cache_hits_total` (both in tokens,
  label `engine="0"`), plus `vllm:external_prefix_cache_{queries,hits}_total` (KV connector, 0 here).
- ~1980-token prompt sent twice: hits added by the first request **0**, by the second **1968** (all but the last
  partial 16-token block).
- **TTFT first 0.524 s, second 0.081 s** (6.5× lower). TTFT = total time of a `max_tokens: 1` request.
Notes:
- First run used 150 prompt lines (~3385 tokens, hits 3376, TTFT 0.758 s -> 0.089 s); the script's per-line
  estimate was wrong (22 tokens per line, not 14), so it now uses 90 lines for the card's ~2000 tokens.
- The run marker at the start of the prompt makes every run start uncached, so the check can be rerun.
- The check fails unless the second request adds more hits than the first, not just "hits went up": the first
  request could hit shared chat-template blocks.

### 2026-10-26 · P0.4 · green (attempt 2)
Command: `make vllm-pair` · `make concurrency-check ARGS="--ports 8001 8002 --concurrency 4 --minutes 10"`
Result:
- port 8001: 344 requests, **0 errors**, p50 6.35 s, p95 9.55 s; port 8002: 335 requests, **0 errors**, p50 6.38 s,
  p95 9.43 s (4 in flight per port, max_tokens 128).
- Peak GPU memory 3468 / 4096 MiB (whole GPU, 558 MiB of it the desktop). Peak RAM vllm-1 2.79 GiB, vllm-2 2.92 GiB:
  under the card's ~3 GB, so no `--swap-space` change.
- Both replicas: KV cache 0.47 GiB = 4384 tokens each; healthy after 114 s and 120 s.
Notes:
- Attempt 1 (KV cache sized by `gpu_memory_utilization` 0.42): replica 1 fine (0.62 GiB KV), replica 2 computed
  **-0.79 GiB** and exited. Under WSL vLLM sees only device-wide memory (both replicas log "Initial free memory
  3.22 GiB"), so replica 2 counted replica 1's memory as its own. Fix, suggested by vLLM's own log: a fixed KV
  size, `kv_cache_memory_bytes` in `config/` (480 MiB real/sim, 800 MiB demo) passed as `--kv-cache-memory`.
- With it set, `gpu_memory_utilization` no longer sizes anything; vLLM only checks at startup that this share of
  VRAM is free (`vllm/v1/worker/utils.py`, `request_memory`).
- Replicas start one after the other (`vllm-pair.sh`), so they never profile memory at the same time.
- `concurrency_check.py` uses only the standard library (user decision, no httpx); summary math unit-tested.

### 2026-10-26 · P0.3 · green (demo model changed)
Command: `make vllm-run PROFILE=real` · `make chat-check` · `make vllm-run PROFILE=demo` · `make chat-check`
Result:
- 0.6B: answer "Kubernetes is a system for automating the deployment, scaling, and management of containerized
  applications."; tool call `get_weather({"city": "Ohrid"})`. VRAM 2271 MiB whole GPU, RAM 3.42 GiB.
- **4B AWQ does not fit.** util 0.85: refused at startup (only 3.22 of 4.0 GiB free next to the desktop and the
  CUDA context). util 0.78: weights 2.5 GiB, KV 0.18 GiB, max context **1312 tokens** (4096 needs 0.56 GiB).
- The D8 fallback `Qwen/Qwen3-1.7B` is FP16, 4.06 GB of weights: cannot fit either. Demo model is now
  **Qwen/Qwen3-1.7B-GPTQ-Int8** (user decision): weights 1.92 GiB, KV 0.86 GiB = 8016 tokens at util 0.78,
  chat + tool call OK, VRAM 3213 MiB, RAM 3.85 GiB. After the P0.4 change (fixed 800 MiB KV): 7312 tokens, OK.
- Startup (0.6B, model cached): ~160 s to healthy: 37 s Python imports, 32 s config resolution (HF hub lookup),
  38 s engine process start, 19 s model load (weights 2.7 s), 12 s KV/profiling. 115 s with `HF_HUB_OFFLINE=1`.
  This is the cold start the simulator has to model (D4).
Notes:
- vLLM v0.31.0 (2026-10-05), the newest release at least two weeks old; v0.32.0 was 8 days old. Pinned by digest.
- `max_model_len` 8192 did not fit the 0.6B at util 0.42 (needs 0.88 GiB KV, 0.61 GiB available, max 5728):
  real and sim now use 4096 (user decision).
- Model revisions pinned in `config/` as `model.revision` (user decision), passed as `--revision` and
  `--tokenizer-revision` (without the second, the tokenizer and chat template come from `main`).
- Containers run as the calling user with `HF_HOME=/hf`, so files downloaded into `~/.cache/huggingface` are not
  owned by root. Read-only mount + `HF_HUB_OFFLINE=1` (as the P0.7 pods use it) also tested: chat-check OK.
- Manual step, once per machine: `hf auth login` with a read token (huggingface.co/settings/tokens). Optional:
  without it downloads work, only with lower rate limits. The token lives in `~/.cache/huggingface/token`,
  outside the repo.
- `chat_check.sh` takes a base URL instead of a port (user decision), so P0.7 reuses it against the Service.
- vLLM warnings on this GPU, harmless: pinned memory not available under WSL; FlashInfer top-k sampling needs more
  than 16 SMs, so vLLM falls back.

### 2026-10-26 · P0.6 · green (ladder step 1)
Command: `make k3s && make gpu-check`
Result:
- `make k3s`: k3s v1.36.5+k3s1 installed, nvidia runtime found in the k3s containerd config, RuntimeClass
  `nvidia` applied, device plugin chart 0.20.1 deployed, `node reports allocatable nvidia.com/gpu: 1`.
- `make gpu-check`: plugin variant (`nvidia.com/gpu: 1` limit), pod Succeeded after 6 s (image already pulled):
  `Compute 8.6 CUDA device: [NVIDIA GeForce RTX 3050 Laptop GPU]`, 16384 bodies, 10 iterations in 16.144 ms,
  **3325.434 single-precision GFLOP/s** (P0.2 in Docker: 3321.221, same within noise (one run each)).
Notes:
- **Ladder step 1 worked**: k3s + NVIDIA Container Toolkit + device plugin. The plugin detects WSL2 and reports
  one device `all` backed by `/dev/dxg`, so the node shows exactly one GPU. The no-plugin variant (step 3)
  was not needed and was not run. P0.7 needs 2 GPU slots for 2 pods: time-slicing on top of this, or step 3.
- Proof of step 1 is the allocatable `nvidia.com/gpu: 1`, not nbody alone: the image sets
  `NVIDIA_VISIBLE_DEVICES=all` itself, so with `runtimeClassName: nvidia` it would get the GPU without the plugin.
- The chart's default affinity needs a PCI/NFD node label that never appears under WSL2 (no PCI GPU), so the
  values set `affinity: null`; without it the plugin pod is never scheduled.
- k3s already ships an identical `RuntimeClass nvidia` (packaged addon); ours is applied server-side so the
  cluster does not silently depend on that addon.
- The card's `command: ["nbody", ...]` failed (`exec: "nbody": executable file not found in $PATH`,
  exit 128): in Kubernetes `command` replaces the ENTRYPOINT (`/cuda-samples/sample`, a symlink to
  `/cuda-samples/nbody`), while in P0.2's `docker run` the words after the image were only arguments to it.
  Fixed to `command: ["/cuda-samples/nbody", "-gpu", "-benchmark"]`.
- Departures from the card: (1) the kubeconfig mode is set as `write-kubeconfig-mode: "0644"` in
  `/etc/rancher/k3s/config.yaml` instead of the card's `--write-kubeconfig-mode 644` install flag, so changing
  it does not need a reinstall; (2) Traefik is disabled, which the card did not ask for (P1.2 brings its own
  gateway); ServiceLB kept for LoadBalancer Services.
- k3s pinned to the `stable` channel (v1.36.5), not the newest release (v1.37.1).
- The first pull of the nbody image into k3s's containerd took 1m13s (122 MB): k3s does not share Docker's
  image store.

### 2026-10-25 · P0.2 · green
Command: `make docker-gpu` (twice, `wsl.exe --shutdown` in between) · `docker run --rm --gpus all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark`
Result:
- nbody: `Compute 8.6 CUDA device: [NVIDIA GeForce RTX 3050 Laptop GPU]`, 16384 bodies, 10 iterations in 16.165 ms,
  **3321.221 single-precision GFLOP/s**.
- Image digest: `nvcr.io/nvidia/k8s/cuda-sample@sha256:59261e419d6d48a772aad5bb213f9f1588fcdb042b115ceb7166c89a51f03363`
  (the `nbody` tag is not pinned; the digest records what ran).
- Second `make docker-gpu`: no apt install, no group change, every package at its pin (docker-ce 29.9.2,
  containerd 2.4.2, buildx 0.38.1, Toolkit 1.20.1), runtimes `io.containerd.runc.v2 nvidia runc`.
  Docker was not restarted: started 23:32:28 at WSL boot, `daemon.json` rewritten at 23:37:12 with
  identical content.
- `command -v docker` -> `/usr/bin/docker`; the user is in group `docker` after the WSL restart.
Notes:
- Pins are the newest versions available on 2026-10-25 (user decision), not the two-week rule used for
  the dev tools. No `apt-mark hold` (user decision); rerunning the script moves packages back to their pins.
- Only Engine + buildx; compose and rootless-extras left out (Kubernetes, not Compose).
- Neither Docker's nor NVIDIA's install page lists its key fingerprint. The script pins the fingerprints
  of the keys served over HTTPS on 2026-10-25 (Docker 9DC8...CD88, NVIDIA C95B...ECB0) and stops if they change.
- systemd was already on (`/etc/wsl.conf`), so that step of the script did nothing here.
- Docker Desktop stays installed on Windows with autostart off; its `docker-desktop` distro shares the
  WSL2 VM's memory. The script warns if it is running.
- `wsl --shutdown` only exists on Windows; inside Ubuntu it is `wsl.exe --shutdown`. The script's
  messages say so after the first run hit this.
- `nvidia-ctk` rewrites `daemon.json` on every run and prints INFO lines, and gpg printed keybox messages
  from its temporary home: both made quiet after the second run (`--quiet`). Output-only change, not re-run.

### 2026-10-25 · P0.0 · green
Command: `bash infra/bootstrap.sh` (twice) · `make lint` · `make test`
Result:
- Second bootstrap run installed nothing, printed every version, exit 0: git 2.43.0, make 4.3, jq 1.7,
  yq 4.54.1, Python 3.12.3, uv 0.13.0, Node.js 24.21.0 (npm 11.19.0, bundled), Angular CLI 22.2.2,
  Go 1.27.2, golangci-lint 2.14.0, Helm 4.3.0, k9s 0.51.0, hf 2.2.0. Full list in `versions.md`.
- `make lint`: all hooks pass; golangci-lint, eslint and tsc skipped (no `gateway/` or `ui/` yet), exit 0.
- `make test`: 57 passed (schema tests: valid examples accepted, invalid rejected, one rule per case), exit 0.
Notes:
- Headers keep the `x-` prefix (`x-run-id`, `x-step`, `x-expected-steps`, `x-parallel-group`), as in the
  card. CLAUDE.md still says `run-id`, `step`, ...; it gets aligned in a separate commit.
- `x-step` and `x-expected-steps` are validated as digit strings (`^[1-9][0-9]*$`), not integers as the
  card says: HTTP header values are always strings on the wire. Readers parse them into integers.
- Header names are case-insensitive in HTTP (Go's `net/http` sends `X-Run-Id`), so readers lowercase
  them before validating; written into the schema description after review.
- `types-jsonschema` added beyond the card's tool list, approved: jsonschema ships no type information,
  so `mypy --strict` fails without it.
- `docs/task-cards/05-P0.3-vllm-single.md` is outside the card's files but in this commit: the
  trailing-whitespace hook strips one space from it, and without that `make lint` fails on a fresh clone.
- Fresh-eyes review done in a separate session. Fixed: case-insensitive header names (above) and a test
  docstring that claimed every example file is checked. Left as is: a test that only checks both example
  files exist (the accept/reject tests would fail anyway), and `pre-commit-hooks` pinned by tag `v6.0.0`,
  not by commit hash.
- Open for P1.3: agent calls are non-streaming (D6), so `ttft_ms` in the trace can't be measured by the
  client and has to come from vLLM or the gateway.

### 2026-10-25 · P0.0 prep · green
Command: `df -T ~/thesis` · `nvidia-smi` · `free -h` · `git ls-files --eol` · `ssh -T git@github.com` · `git ls-remote origin` · `curl -s -o /dev/null -w '%{http_code}' https://github.com/Dkostoski30/agent-aware-llm-routing`
Result:
- Repo at `~/thesis` on ext4 in `Ubuntu-24.04`. Line endings all LF, all files mode 100644.
- GPU still visible after the move: RTX 3050 Laptop, driver 621.01, 36 MiB / 4096 MiB used.
- `ssh -T`: "Hi Dkostoski30! You've successfully authenticated".
- `git ls-remote origin`: `refs/heads/master` = `2a781a7`, same as local. Anonymous HTTP returns 404, so the repo is private.
Notes:
- Repo created with `git clone /mnt/c/Users/kosto/Desktop/dipl/thesis ~/thesis`. A plain `cp -r`
  of the kit fails: git object directories are read-only, so `cp` recreates them without write
  permission before writing the objects into them.
- Remote is `git@github.com:Dkostoski30/agent-aware-llm-routing.git`. Auth is an ed25519 SSH key in
  `~/.ssh/id_ed25519`, added to GitHub by hand: one manual step per machine, kept out of the repo
  because it is a secret. No `gh` needed. The GitHub repo already exists, so P0.0 pushes to it
  instead of running `gh repo create`.
- Git identity set in the repo: `Daniel Kostoski <kostoskid66@gmail.com>`.
- Default WSL distro is now `Ubuntu-24.04` (corrects the P0.1 note below).
- `windows/.wslconfig` kept on purpose, although the P0.0 card says to copy the kit without
  `windows/`: it is the repo's only record of the locked memory/swap setting (rule 9).
- `.idea/` is tracked by the Initial commit; untracking it (`.gitignore` + `git rm -r --cached .idea`)
  is part of P0.0.
- `user__n_pass` (plaintext Linux login) exists only in the Windows copy, was never committed, and
  must stay out of the repo.

### 2026-10-24 · P0.1 · green
Command: `wsl -d Ubuntu-24.04 nvidia-smi` · `wsl --version` · `nvidia-smi` (Windows) · `free -h` and `ls -l /dev/dxg` inside WSL · `powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`
Result:
- WSL2 `nvidia-smi`: `NVIDIA GeForce RTX 3050 Laptop GPU`, 127MiB / 4096MiB used, 0% util, no processes. Exit 0.
- Windows `nvidia-smi`: same GPU, driver 621.01, 127MiB used by desktop processes (NVIDIA Overlay, shell, IDE CEF) before anything of ours runs.
- WSL 3.0.3.0, kernel 6.18.54.1-microsoft-standard-WSL2, Ubuntu 24.04.5 LTS, `/dev/dxg` present.
- `.wslconfig` applied: 9.7Gi RAM, 8.0Gi swap.
- No `nvidia*` or `cuda*` package installed inside Ubuntu (as required: the WSL libraries come from the Windows driver).
- Windows power plan Performance, sleep on AC never. C: 140.5 GB free.
Notes:
- Two Ubuntu 24.04 distros exist: `Ubuntu` (24.04.1, user `danielko`) and `Ubuntu-24.04` (24.04.5, user `kosto`). The default distro is `Ubuntu`, so a bare `wsl` lands in the wrong one. All thesis work uses `Ubuntu-24.04`.
- Docker Desktop is installed on Windows (a `docker-desktop` WSL distro exists) and `docker` inside `Ubuntu-24.04` resolves to the Windows shim with WSL integration off. P0.2 has to decide between Docker Desktop integration and a native Docker Engine in the distro.
- Armoury Crate power mode / GPU mode are not machine-checkable; confirmed only by the Windows power plan being Performance.
