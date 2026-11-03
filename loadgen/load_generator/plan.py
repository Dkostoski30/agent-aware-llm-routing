"""What a replay will send (P2.3): pure and seeded, with no network and no clock.

A trace holds token counts, not text, so prompts are synthetic. Each run gets a seeded text of
common words, one token each on the Qwen3 tokenizer, and step k's prompt is the words that make it
prompt_tokens(k) long, so each step extends the previous one as a real agent's history does. Runs
share a prefix as real runs do: by_type words between runs of one task type (the system prompt and
that type's tool schemas) and all_types words between any two runs, which the profiles set to 0
for the reason their comment gives.

Each random choice has its own stream, named by what it decides, so turning fan-out on or asking
for more runs does not move the others. A str seed is hashed by random itself, not by Python's
salted hash, so it gives the same stream in every process.
"""

import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from load_generator.record import trace_validator

# Space-separated, each is one token on the Qwen3 tokenizer, with or without its leading space
# (checked on 1500 random picks; make replay checks every prompt with /tokenize before it sends
# one). llm-d-inference-sim's fallback tokenizer also counts a word as one token, while it would
# count a run of digits as one (P2.7). At ~5 characters they are about as long per token as real
# prompts, which matters to the picker's prefix index, since it hashes text, not tokens.
WORDS = (
    "time", "year", "people", "way", "day", "man", "thing", "woman",
    "life", "child", "world", "school", "state", "family", "student", "group",
    "country", "problem", "hand", "part", "place", "case", "week", "company",
    "system", "program", "question", "work", "government", "number", "night", "point",
    "home", "water", "room", "mother", "area", "money", "story", "fact",
    "month", "lot", "right", "study", "book", "eye", "job", "word",
    "business", "issue", "side", "kind", "head", "house", "service", "friend",
    "father", "power", "hour", "game", "line", "end", "member", "law",
)  # fmt: skip


class ReplayError(Exception):
    """The replay must not start, or its output would not be valid: no output file is written."""


@dataclass(frozen=True)
class Burst:
    runs: int
    every_seconds: float


@dataclass(frozen=True)
class FanOut:
    # The share of a run's later steps that fan out; never step 1, since P3.6 needs a pinned pod.
    share: float
    # The calls such a step becomes.
    k: int


@dataclass(frozen=True)
class Pattern:
    runs: int
    # The mean of the Poisson arrivals.
    rate_per_second: float
    burst: Burst | None
    fan_out: FanOut | None


@dataclass(frozen=True)
class SharedPrefix:
    all_types: int
    by_type: dict[str, int]


@dataclass(frozen=True)
class ReplayConfig:
    seed: int
    gap_tolerance_ms: int
    shared_prefix: SharedPrefix
    patterns: dict[str, Pattern]


@dataclass(frozen=True)
class TraceCall:
    step: int
    expected_steps: int
    gap_before_ms: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class TraceRun:
    run_id: str
    task_type: str
    calls: list[TraceCall]


@dataclass(frozen=True)
class PlannedStep:
    step: int
    expected_steps: int
    gap_before_ms: int
    prompt_tokens: int
    completion_tokens: int
    content: str
    # 1, or a pattern's fan_out.k with parallel_group set.
    calls: int
    parallel_group: str | None


@dataclass(frozen=True)
class PlannedRun:
    run_id: str
    task_type: str
    start_seconds: float
    steps: list[PlannedStep]


def _is_count(value: Any, least: int) -> bool:
    """A float would pass a comparison and fail later as a slice or a range, and YAML's yes is an
    int to Python."""
    return type(value) is int and value >= least


def _pattern(name: str, section: Mapping[str, Any]) -> Pattern:
    """A value that could not run is refused here by its name, not later as a ZeroDivisionError or
    as a replay of nothing that passes."""
    burst = section.get("burst")
    fan_out = section.get("fan_out")
    rules = [
        ("runs", _is_count(section["runs"], 1), "a whole number, at least 1"),
        ("rate_per_second", section["rate_per_second"] > 0, "above 0"),
    ]
    if burst is not None:
        rules += [
            ("burst.runs", _is_count(burst["runs"], 1), "a whole number, at least 1"),
            ("burst.every_seconds", burst["every_seconds"] > 0, "above 0"),
        ]
    if fan_out is not None:
        rules += [
            ("fan_out.share", 0 < fan_out["share"] <= 1, "above 0 and at most 1"),
            ("fan_out.k", _is_count(fan_out["k"], 2), "a whole number, at least 2"),
        ]
    for field, holds, rule in rules:
        if not holds:
            raise ReplayError(f"replay.patterns.{name}.{field} must be {rule}")
    return Pattern(
        runs=section["runs"],
        rate_per_second=section["rate_per_second"],
        burst=None if burst is None else Burst(burst["runs"], burst["every_seconds"]),
        fan_out=None if fan_out is None else FanOut(fan_out["share"], fan_out["k"]),
    )


def load_replay_config(section: Mapping[str, Any]) -> ReplayConfig:
    """A profile's replay section; a missing key raises KeyError."""
    shared = section["shared_prefix_tokens"]
    if not _is_count(shared["all_types"], 0):
        raise ReplayError(
            f"replay.shared_prefix_tokens.all_types is {shared['all_types']}, it must be a whole "
            "number, at least 0"
        )
    for task_type, tokens in shared["by_type"].items():
        if not _is_count(tokens, shared["all_types"]):
            raise ReplayError(
                f"replay.shared_prefix_tokens: {task_type} shares {tokens} tokens, it must be a "
                f"whole number, at least all_types {shared['all_types']}"
            )
    return ReplayConfig(
        seed=section["seed"],
        gap_tolerance_ms=section["gap_tolerance_ms"],
        shared_prefix=SharedPrefix(all_types=shared["all_types"], by_type=dict(shared["by_type"])),
        patterns={name: _pattern(name, pattern) for name, pattern in section["patterns"].items()},
    )


def named_pattern(config: ReplayConfig, name: str) -> Pattern:
    if name not in config.patterns:
        raise ReplayError(
            f"no pattern {name} in the profile's replay.patterns, it has {sorted(config.patterns)}"
        )
    return config.patterns[name]


def check_trace_line(validator: Draft202012Validator, line: Mapping[str, Any], where: str) -> None:
    violation = best_match(validator.iter_errors(line))
    if violation is not None:
        raise ReplayError(f"{where}: not a trace.v1 line: {violation.message}")


def _check_replayable(line: Mapping[str, Any], where: str) -> None:
    if line["completion_tokens"] < 1:
        raise ReplayError(f"{where}: completion_tokens is 0, and a server refuses max_tokens 0")
    if "parallel_group" in line:
        raise ReplayError(f"{where}: has a parallel_group: a pattern's fan_out makes those")


def load_trace(path: Path) -> list[TraceRun]:
    """The trace's runs in file order. Refuses what the replayer could not reproduce."""
    validator = trace_validator()
    by_run: dict[str, list[dict[str, Any]]] = {}
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        line = json.loads(text)
        where = f"{path}:{number}"
        check_trace_line(validator, line, where)
        _check_replayable(line, where)
        by_run.setdefault(line["run_id"], []).append(line)
    if not by_run:
        raise ReplayError(f"{path}: no calls")
    runs = []
    for run_id, lines in by_run.items():
        steps = [line["step"] for line in lines]
        if steps != list(range(1, len(lines) + 1)):
            raise ReplayError(f"{path}: {run_id} has steps {steps}, expected 1..{len(lines)}")
        calls = [
            TraceCall(
                step=line["step"],
                expected_steps=line["expected_steps"],
                gap_before_ms=line["gap_before_ms"],
                prompt_tokens=line["prompt_tokens"],
                completion_tokens=line["completion_tokens"],
            )
            for line in lines
        ]
        runs.append(TraceRun(run_id=run_id, task_type=lines[0]["task_type"], calls=calls))
    return runs


def start_times(pattern: Pattern, seed: int) -> list[float]:
    """Seconds from the replay's start, in order, pattern.runs of them. Poisson arrivals at the mean
    rate, the first at once, plus a pattern's bursts exactly every burst.every_seconds."""
    rng = random.Random(f"{seed}/starts")
    base = [0.0]
    while len(base) < pattern.runs:
        base.append(base[-1] + rng.expovariate(pattern.rate_per_second))
    bursts: list[float] = []
    if pattern.burst is not None:
        count = math.ceil(pattern.runs / pattern.burst.runs)
        bursts = [
            pattern.burst.every_seconds * number
            for number in range(1, count + 1)
            for _ in range(pattern.burst.runs)
        ]
    return sorted(base + bursts)[: pattern.runs]


def choose_runs(runs: Sequence[TraceRun], count: int, seed: int) -> list[TraceRun]:
    """count runs in a seeded order; every run of the trace is used once before any repeats."""
    rng = random.Random(f"{seed}/order")
    chosen: list[TraceRun] = []
    while len(chosen) < count:
        chosen.extend(rng.sample(runs, len(runs)))
    return chosen[:count]


def replayed_run_id(trace_run: str, position: int) -> str:
    """The x-run-id of a run's replay: unique when a run repeats, the same for the same seed."""
    return f"{trace_run}-{position}"


def trace_run_id(replayed: str) -> str:
    """The run of the trace that a replayed run comes from."""
    return replayed.rsplit("-", 1)[0]


def _words(stream: str, count: int) -> list[str]:
    return random.Random(stream).choices(WORDS, k=count)


def _run_words(
    run: TraceRun, run_id: str, shared: SharedPrefix, seed: int, longest: int
) -> list[str]:
    """Words the seed does not touch, as a real system prompt does not change between replays, then
    the run's own."""
    type_tokens = shared.by_type[run.task_type]
    return [
        *_words("all-types", shared.all_types),
        *_words(f"type/{run.task_type}", type_tokens - shared.all_types),
        *_words(f"{seed}/text/{run_id}", max(longest - type_tokens, 0)),
    ]


def _planned_run(
    run: TraceRun,
    position: int,
    start: float,
    pattern: Pattern,
    shared: SharedPrefix,
    seed: int,
    template_tokens: int,
) -> PlannedRun:
    run_id = replayed_run_id(run.run_id, position)
    if run.task_type not in shared.by_type:
        raise ReplayError(
            f"{run.run_id}: task type {run.task_type} has no replay.shared_prefix_tokens.by_type "
            "value in the profile"
        )
    lengths = [call.prompt_tokens - template_tokens for call in run.calls]
    if min(lengths) < 1:
        raise ReplayError(
            f"{run.run_id}: a prompt of {min(lengths) + template_tokens} tokens does not fit the "
            f"chat template's {template_tokens}"
        )
    words = _run_words(run, run_id, shared, seed, max(lengths))
    fan_rng = random.Random(f"{seed}/fan-out/{run_id}")
    steps = []
    for call, length in zip(run.calls, lengths, strict=True):
        fans_out = (
            pattern.fan_out is not None
            and call.step > 1
            and fan_rng.random() < pattern.fan_out.share
        )
        steps.append(
            PlannedStep(
                step=call.step,
                expected_steps=call.expected_steps,
                gap_before_ms=call.gap_before_ms,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                content=" ".join(words[:length]),
                calls=pattern.fan_out.k if fans_out and pattern.fan_out is not None else 1,
                parallel_group=f"step-{call.step}" if fans_out else None,
            )
        )
    return PlannedRun(run_id=run_id, task_type=run.task_type, start_seconds=start, steps=steps)


def build_plan(
    runs: Sequence[TraceRun],
    pattern: Pattern,
    shared: SharedPrefix,
    seed: int,
    template_tokens: int,
) -> list[PlannedRun]:
    """Every run a replay will start, in start order. template_tokens is what the chat template adds
    around one user message, measured with /tokenize."""
    starts = start_times(pattern, seed)
    chosen = choose_runs(runs, pattern.runs, seed)
    return [
        _planned_run(run, position, start, pattern, shared, seed, template_tokens)
        for position, (run, start) in enumerate(zip(chosen, starts, strict=True), start=1)
    ]


def served_model(model: Mapping[str, Any]) -> str:
    """The name a request uses, as the agent names it: the adapter's when the profile serves one
    (agent_runtime.config.load_config), so a replay runs on the weights it was recorded on."""
    name: str = model["lora"]["name"] if "lora" in model else model["name"]
    return name


def call_headers(run_id: str, step: PlannedStep) -> dict[str, str]:
    """The D1 headers (schemas/headers.v1.json)."""
    headers = {
        "x-run-id": run_id,
        "x-step": str(step.step),
        "x-expected-steps": str(step.expected_steps),
    }
    if step.parallel_group is not None:
        headers["x-parallel-group"] = step.parallel_group
    return headers


def _messages(content: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content}]


def chat_request(model: str, content: str, completion_tokens: int) -> dict[str, Any]:
    """A streaming call: a replay never retries, so D6 does not apply, and streaming gives TTFT.
    include_usage adds the last chunk with the token counts, the cached tokens and the pod.
    ignore_eos makes the reply exactly as long as the trace's, so sampling is left at the server's
    default: it can change the words, not the length."""
    return {
        "model": model,
        "messages": _messages(content),
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": completion_tokens,
        "ignore_eos": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def tokenize_request(model: str, content: str) -> dict[str, Any]:
    """The same message through the same template as chat_request."""
    return {
        "model": model,
        "messages": _messages(content),
        "chat_template_kwargs": {"enable_thinking": False},
    }
