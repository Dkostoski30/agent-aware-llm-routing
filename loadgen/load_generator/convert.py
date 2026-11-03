"""make convert (P2.4): AgentProcessBench trajectories as trace.v1 lines, for the replayer.

The demo's own runs take 2-4 calls. This published dataset gives runs of up to 94 calls, with their
messages and tool definitions but with no token counts and no timings. So:

- Counts come from vLLM's /tokenize, on the tokenizer the replays run on, one line per assistant
  message: several tool calls in one reply are one call of the model.
- Most of it does not fit the model's context: 1,431 of 8,259 calls do, none of tau2's. So each task
  type's prompts and replies shrink by one factor, which keeps the steps of each run it keeps, its
  growth, and each call's ratio of prompt to reply. Token counts are no longer literal, and fitting
  drops the largest runs, so the longest ones too: limitations the thesis states (decided with the
  user, docs/task-cards/27-P2.4-*.md).
- A trace's gap (D9) cannot come from the dataset, so it is the median tool time of a recording.

The trace holds counts only: no text of the dataset enters the repo.

Usage: python -m load_generator.convert EXPERIMENT_YAML   (experiments/convert-apb.yaml)
"""

import argparse
import hashlib
import json
import math
import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from load_generator.expected_steps import expected_steps
from load_generator.plan import TraceRun, load_trace, served_model
from load_generator.record import (
    REPO_ROOT,
    SCHEMA_VERSION,
    check_line,
    experiment_hash,
    kubectl,
    trace_validator,
    write_trace,
)
from load_generator.replay import TOKENIZE_PATH, VLLM_PORT, VLLM_SERVICE

# Outside the repo, like the fine-tuning data: the dataset's text stays out of git.
CACHE_DIR = Path.home() / ".cache" / "thesis" / "agentprocessbench"
DATASET_URL = "https://huggingface.co/datasets/{repo}/resolve/{revision}/{file}"
# Between two chunks of a download of at most 15 MB.
DOWNLOAD_TIMEOUT_SECONDS = 60
# The longest history is about 4 MB of text (gaia_dev). A failure here costs the whole conversion,
# so this only has to end a call that hangs.
TOKENIZE_TIMEOUT_SECONDS = 300
# tau2 opens every run with these words, before any user message: the environment's, not the
# model's.
TAU2_GREETING = "Hi! How can I help you today?"
# How bfcl's environment records its state after each turn, as a system message. 246 of its 250 runs
# end on one, after the model's last reply, so the benchmark's own record and not part of what its
# model was asked: inferred from the data, not stated by the dataset (decided with the user).
BFCL_STATE_SNAPSHOT = "<state_info"
# vLLM counts the token that ended a reply in completion_tokens, and the dataset's text does not
# hold it.
END_OF_REPLY_TOKENS = 1


class ConvertError(Exception):
    """The conversion must stop: no trace is written."""


@dataclass(frozen=True)
class ConvertSpec:
    path: Path
    # Stamps the trace (D11).
    hash: str
    # The config/ profile whose tokenizer counts and whose model.max_model_len every call must fit.
    profile: str
    repo: str
    revision: str
    # Each file of the revision, with its sha256, in the order the trace holds their runs.
    files: dict[str, str]
    # A trajectory's data_source and the trace's task_type it becomes.
    task_types: dict[str, str]
    keep_share: float
    max_reply_tokens: int
    gap_trace: Path


@dataclass(frozen=True)
class Trajectory:
    run_id: str
    task_type: str
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class Call:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class CountedRun:
    run_id: str
    task_type: str
    calls: list[Call]
    # The first prompt's token IDs, unscaled: what runs of a type share at the start.
    first_prompt: list[int]


@dataclass(frozen=True)
class TypeResult:
    task_type: str
    factor: float
    largest_kept_size: int
    runs_in: int
    too_large: int
    long_reply: int
    runs: list[CountedRun]
    shared_prefix_tokens: int


class Tokenizer(Protocol):
    def prompt(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> list[int]: ...

    def reply(self, text: str) -> int: ...


class VllmTokenizer:
    """vLLM's /tokenize on the pods' own Service: the gateway's route matches only /v1, and the
    picker must not learn these prompts."""

    def __init__(self, client: httpx.Client, model: str) -> None:
        self._client = client
        self._model = model

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        response = self._client.post(TOKENIZE_PATH, json={"model": self._model, **body})
        if response.is_error:
            raise ConvertError(f"/tokenize answered {response.status_code}: {response.text}")
        result: dict[str, Any] = response.json()
        return result

    def prompt(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> list[int]:
        """A call's prompt through the chat template, as P2.3's replays count theirs."""
        tokens: list[int] = self._post(
            {
                "messages": messages,
                "tools": tools,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )["tokens"]
        return tokens

    def reply(self, text: str) -> int:
        count: int = self._post({"prompt": text, "add_special_tokens": False})["count"]
        return count


def load_spec(path: Path) -> ConvertSpec:
    """The experiment file. A value that could not run is refused here by its name."""
    spec = yaml.safe_load(path.read_text())
    if not 0 < spec["keep_share"] <= 1:
        raise ConvertError(f"{path}: keep_share must be above 0 and at most 1")
    if type(spec["max_reply_tokens"]) is not int or spec["max_reply_tokens"] < 1:
        raise ConvertError(f"{path}: max_reply_tokens must be a whole number, at least 1")
    return ConvertSpec(
        path=path,
        hash=experiment_hash(path),
        profile=spec["profile"],
        repo=spec["dataset"]["repo"],
        revision=spec["dataset"]["revision"],
        files=dict(spec["dataset"]["files"]),
        task_types=dict(spec["task_types"]),
        keep_share=spec["keep_share"],
        max_reply_tokens=spec["max_reply_tokens"],
        gap_trace=path.parent / spec["gap_trace"],
    )


def output_path(spec: ConvertSpec) -> Path:
    """results/traces/<experiment>-<hash>-<revision>.jsonl next to the experiment file (D11)."""
    name = f"{spec.path.stem}-{spec.hash}-{spec.revision[:12]}.jsonl"
    return spec.path.parent / "results" / "traces" / name


def fetch_file(client: httpx.Client, url: str, sha256: str, target: Path) -> None:
    """Downloads the file unless it is there, and checks it either way. A file that fails the check
    stays where it is and has to be deleted, so that a changed pin cannot go unnoticed."""
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.partial")
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        partial.replace(target)
    with target.open("rb") as data:
        digest = hashlib.file_digest(data, "sha256").hexdigest()
    if digest != sha256:
        raise ConvertError(
            f"{target}: sha256 is {digest}, the experiment pins {sha256}: delete it to download "
            "again"
        )


def _is_state_snapshot(message: Mapping[str, Any]) -> bool:
    content: str = message["content"]
    return message["role"] == "system" and content.startswith(BFCL_STATE_SNAPSHOT)


def read_trajectories(path: Path, task_types: Mapping[str, str]) -> Iterator[Trajectory]:
    """One at a time, since only the counts stay."""
    with path.open() as lines:
        for number, text in enumerate(lines, start=1):
            record = json.loads(text)
            source = record["data_source"]
            if source not in task_types:
                raise ConvertError(
                    f"{path}:{number}: data_source {source} is not in the experiment's task_types"
                )
            task_type = task_types[source]
            yield Trajectory(
                run_id=f"{task_type}-{record['total_index']}",
                task_type=task_type,
                tools=record["tools"],
                messages=[m for m in record["messages"] if not _is_state_snapshot(m)],
            )


def _is_greeting(messages: Sequence[Mapping[str, Any]], index: int) -> bool:
    return messages[index]["content"] == TAU2_GREETING and all(
        message["role"] == "system" for message in messages[:index]
    )


def llm_call_indexes(messages: Sequence[Mapping[str, Any]]) -> list[int]:
    """Where the model answered: every assistant message but tau2's fixed greeting."""
    return [
        index
        for index, message in enumerate(messages)
        if message["role"] == "assistant" and not _is_greeting(messages, index)
    ]


def reply_text(message: Mapping[str, Any]) -> str:
    """What the model wrote: its text, then its tool calls as the chat template renders them. The
    arguments stay as written, since 32 replies of the dataset hold arguments that are not JSON."""
    parts = [message["content"]] if message["content"] else []
    for call in message.get("tool_calls", []):
        function = call["function"]
        parts.append(
            f'<tool_call>\n{{"name": "{function["name"]}", "arguments": {function["arguments"]}}}'
            "\n</tool_call>"
        )
    return "\n".join(parts)


def request_tools(tools: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """What a chat request would send. bfcl's system message already lists every tool, in all 250
    runs, and its definitions alone come in another shape, so sending them would count each tool
    twice."""
    return [tool for tool in tools if "function" in tool]


def count_run(trajectory: Trajectory, tokenizer: Tokenizer) -> CountedRun:
    """Each call's prompt, the history before it, and its reply."""
    indexes = llm_call_indexes(trajectory.messages)
    if not indexes:
        raise ConvertError(f"{trajectory.run_id}: no LLM call")
    tools = request_tools(trajectory.tools)
    calls = []
    first_prompt: list[int] = []
    for index in indexes:
        tokens = tokenizer.prompt(trajectory.messages[:index], tools)
        first_prompt = first_prompt or tokens
        reply = tokenizer.reply(reply_text(trajectory.messages[index]))
        calls.append(Call(len(tokens), reply + END_OF_REPLY_TOKENS))
    return CountedRun(trajectory.run_id, trajectory.task_type, calls, first_prompt)


def _size(run: CountedRun) -> int:
    """The largest prompt plus reply of a run: what max_model_len limits."""
    return max(call.prompt_tokens + call.completion_tokens for call in run.calls)


def largest_kept_size(sizes: Sequence[int], limit: int, keep_share: float) -> int:
    """The size a type's factor maps to the limit: the nearest rank of keep_share, so at least that
    share of runs stays, or the limit itself for a type that fits, which never grows."""
    return max(sorted(sizes)[math.ceil(keep_share * len(sizes)) - 1], limit)


def _scaled(run: CountedRun, limit: int, largest: int) -> CountedRun:
    """Integers, so that no rounding can pass the limit: for a call of at most largest tokens, the
    two floors add up to at most limit. A reply of 0 would be refused as max_tokens 0, and then the
    prompt's floor is at most limit - 1, so the 1 still fits."""
    return replace(
        run,
        calls=[
            Call(
                call.prompt_tokens * limit // largest,
                max(1, call.completion_tokens * limit // largest),
            )
            for call in run.calls
        ],
    )


def common_prefix_length(prompts: Sequence[Sequence[int]]) -> int:
    length = 0
    for tokens in zip(*prompts, strict=False):
        if len(set(tokens)) > 1:
            break
        length += 1
    return length


def convert_type(
    task_type: str,
    runs: Sequence[CountedRun],
    limit: int,
    keep_share: float,
    max_reply_tokens: int,
) -> TypeResult:
    """One type's runs fitted into the limit. A reply is checked after scaling, since that is what a
    replay would wait for."""
    largest = largest_kept_size([_size(run) for run in runs], limit, keep_share)
    fitting = [_scaled(run, limit, largest) for run in runs if _size(run) <= largest]
    kept = [
        run
        for run in fitting
        if max(call.completion_tokens for call in run.calls) <= max_reply_tokens
    ]
    shared = common_prefix_length([run.first_prompt for run in kept])
    return TypeResult(
        task_type=task_type,
        factor=limit / largest,
        largest_kept_size=largest,
        runs_in=len(runs),
        too_large=len(runs) - len(fitting),
        long_reply=len(fitting) - len(kept),
        runs=kept,
        shared_prefix_tokens=shared * limit // largest,
    )


def recorded_gap_ms(runs: Iterable[TraceRun]) -> int:
    """The median tool time of a recording's later steps; a first step has no gap to measure."""
    return statistics.median_low(
        call.gap_before_ms for run in runs for call in run.calls if call.step > 1
    )


def trace_lines_of(run: CountedRun, expected: int, gap_ms: int) -> list[dict[str, Any]]:
    """One line per call. Only what was counted: measurements and fan-out come from a replay."""
    return [
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run.run_id,
            "task_type": run.task_type,
            "step": step,
            "expected_steps": expected,
            "gap_before_ms": 0 if step == 1 else gap_ms,
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
        }
        for step, call in enumerate(run.calls, start=1)
    ]


def check_model(profile: str, running: str, wanted: Mapping[str, Any]) -> None:
    """The cluster must serve this profile's model: the simulator's /tokenize counts words, and
    another revision has another tokenizer. Only the model section, since nothing else changes a
    count and another edit to the profile would otherwise need a make up. running is the agent's
    ConfigMap, the profile make up deployed."""
    if yaml.safe_load(running)["model"] != wanted["model"]:
        raise ConvertError(
            f"the cluster serves another model than config/{profile}.yaml: run make up "
            f"PROFILE={profile} first"
        )


def config_block(expected: Mapping[str, int], results: Sequence[TypeResult]) -> str:
    """What the profiles must hold (P2.3 refuses a task type without its shared prefix). Printed to
    be pasted, since PyYAML would drop a profile's comments; a test fails until they agree."""
    return yaml.safe_dump(
        {
            "agent.expected_steps_by_type": dict(expected),
            "replay.shared_prefix_tokens.by_type": {
                result.task_type: result.shared_prefix_tokens for result in results
            },
        },
        sort_keys=False,
    )


def _type_meta(result: TypeResult, expected: Mapping[str, int]) -> dict[str, Any]:
    return {
        "factor": round(result.factor, 3),
        "largest_kept_size": result.largest_kept_size,
        "runs_in": result.runs_in,
        "too_large": result.too_large,
        "long_reply": result.long_reply,
        "runs": len(result.runs),
        "calls": sum(len(run.calls) for run in result.runs),
        "expected_steps": expected.get(result.task_type),
        "shared_prefix_tokens": result.shared_prefix_tokens,
    }


def _summary(result: TypeResult, expected: Mapping[str, int]) -> str:
    return (
        f"{result.task_type}: factor {result.factor:.3f}, {len(result.runs)} of {result.runs_in} "
        f"runs, dropped {result.too_large} too large and {result.long_reply} with a long reply, "
        f"{sum(len(run.calls) for run in result.runs)} calls, expected_steps "
        f"{expected.get(result.task_type)}, shared prefix {result.shared_prefix_tokens}"
    )


def _count_dataset(spec: ConvertSpec, tokenizer: Tokenizer) -> dict[str, list[CountedRun]]:
    counted: dict[str, list[CountedRun]] = {}
    done = 0
    for file in spec.files:
        for trajectory in read_trajectories(CACHE_DIR / spec.revision / file, spec.task_types):
            counted.setdefault(trajectory.task_type, []).append(count_run(trajectory, tokenizer))
            done += 1
            print(f"convert: {done} counted, {trajectory.run_id}", flush=True)
    return counted


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.4: AgentProcessBench as a trace.")
    parser.add_argument("experiment", type=Path, help="e.g. experiments/convert-apb.yaml")
    spec = load_spec(parser.parse_args().experiment)
    trace = output_path(spec)
    meta = trace.with_suffix(".meta.json")
    for existing in (trace, meta):
        if existing.exists():
            raise ConvertError(f"{existing} exists: delete it to convert again")

    profile = yaml.safe_load((REPO_ROOT / "config" / f"{spec.profile}.yaml").read_text())
    running = kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    check_model(spec.profile, running, profile)

    with httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as client:
        for file, sha256 in spec.files.items():
            url = DATASET_URL.format(repo=spec.repo, revision=spec.revision, file=file)
            fetch_file(client, url, sha256, CACHE_DIR / spec.revision / file)

    recorded = load_trace(spec.gap_trace)
    gap_ms = recorded_gap_ms(recorded)
    ip = kubectl("get", "service", VLLM_SERVICE, "-o", "jsonpath={.spec.clusterIP}")
    with httpx.Client(
        base_url=f"http://{ip}:{VLLM_PORT}", timeout=TOKENIZE_TIMEOUT_SECONDS
    ) as client:
        counted = _count_dataset(spec, VllmTokenizer(client, served_model(profile["model"])))

    limit = profile["model"]["max_model_len"]
    results = [
        convert_type(task_type, runs, limit, spec.keep_share, spec.max_reply_tokens)
        for task_type, runs in counted.items()
    ]
    converted = [(run.task_type, len(run.calls)) for result in results for run in result.runs]
    expected = expected_steps(converted)
    lines = [
        line
        for result in results
        for run in result.runs
        for line in trace_lines_of(run, expected=expected[result.task_type], gap_ms=gap_ms)
    ]
    validator = trace_validator()
    for line in lines:
        check_line(validator, line)
    write_trace(trace, lines)
    decided = {
        "dataset_revision": spec.revision,
        "model": {
            "name": profile["model"]["name"],
            "revision": profile["model"]["revision"],
            "served_as": served_model(profile["model"]),
        },
        "max_model_len": limit,
        "keep_share": spec.keep_share,
        "max_reply_tokens": spec.max_reply_tokens,
        "gap_trace": spec.gap_trace.name,
        "gap_ms": gap_ms,
        "task_types": {result.task_type: _type_meta(result, expected) for result in results},
    }
    meta.write_text(json.dumps(decided, indent=2) + "\n")

    for result in results:
        print(f"convert: {_summary(result, expected)}")
    print(f"convert: {len(lines)} lines, all valid trace.v1, gap {gap_ms} ms")
    print(f"convert: wrote {trace}")
    # Both traces' types, as the profiles hold them.
    both = expected_steps([*((run.task_type, len(run.calls)) for run in recorded), *converted])
    print("convert: paste into agent and replay of config/real.yaml, sim.yaml and demo.yaml:")
    print(config_block(both, results))


if __name__ == "__main__":
    main()
