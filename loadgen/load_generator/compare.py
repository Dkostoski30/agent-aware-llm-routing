"""make replay-compare (P2.3): the done-when's check of two replays of one trace.

Each must be the pattern's plan at its seed, call for call, so that calls lost by both are found.
The two must have the same call sequence, exactly, and be two replays, since two runs are never
equal line for line. Each against the trace: the same token counts, a reply with a duration and a
TTFT on every call, and every gap within replay.gap_tolerance_ms of the recorded one. Durations,
TTFT and pods are measurements of the moment, so they may differ.

A pass says nothing about arrival times, which steps in the wall clock blur (experiments/log.md),
or about a prompt's text, which a line does not hold. So a rate or a shared prefix changed in the
profile since the replays ran still passes.

Usage: python -m load_generator.compare TRACE PATTERN FIRST SECOND [--profile real] [--seed N]
"""

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from load_generator.plan import (
    PlannedRun,
    ReplayError,
    build_plan,
    check_trace_line,
    load_replay_config,
    load_trace,
    named_pattern,
    trace_run_id,
)
from load_generator.record import REPO_ROOT, trace_validator

SEQUENCE_FIELDS = (
    "run_id",
    "task_type",
    "step",
    "expected_steps",
    "prompt_tokens",
    "completion_tokens",
    "parallel_group",
)
# build_plan takes the chat template's tokens off a prompt's words and refuses a prompt shorter than
# the template, which 0 never does. Which calls a plan makes does not depend on them, and this check
# reads only files, so it asks no server.
NO_TEMPLATE_TOKENS = 0
Line = Mapping[str, Any]


def _call(line: Line) -> tuple[Any, ...]:
    return tuple(line.get(name) for name in SEQUENCE_FIELDS)


def _traced_calls(trace: Sequence[Line]) -> dict[tuple[str, int], Line]:
    return {(line["run_id"], line["step"]): line for line in trace}


def _traced_call(traced: Mapping[tuple[str, int], Line], line: Line) -> Line | None:
    return traced.get((trace_run_id(line["run_id"]), line["step"]))


def _sequence_differences(first: Sequence[Line], second: Sequence[Line]) -> list[str]:
    for number, (one, other) in enumerate(zip(first, second, strict=False), start=1):
        if _call(one) != _call(other):
            return [f"sequence: line {number} is {_call(one)}, then {_call(other)}"]
    if len(first) != len(second):
        return [f"sequence: {len(first)} calls, then {len(second)}"]
    return []


def _planned_calls(plan: Sequence[PlannedRun]) -> list[tuple[Any, ...]]:
    """What _call gives for each line of a replay of the plan, in the order of its lines."""
    return [
        (
            run.run_id,
            run.task_type,
            step.step,
            step.expected_steps,
            step.prompt_tokens,
            step.completion_tokens,
            step.parallel_group,
        )
        for run in plan
        for step in run.steps
        for _ in range(step.calls)
    ]


def plan_differences(plan: Sequence[PlannedRun], lines: Sequence[Line]) -> list[str]:
    planned = _planned_calls(plan)
    for number, (line, call) in enumerate(zip(lines, planned, strict=False), start=1):
        if _call(line) != call:
            return [f"line {number} is {_call(line)}, the plan has {call}"]
    if len(lines) != len(planned):
        return [f"{len(lines)} calls, the plan has {len(planned)}"]
    return []


def _trace_differences(trace: Sequence[Line], lines: Sequence[Line], tolerance: int) -> list[str]:
    traced = _traced_calls(trace)
    found = []
    for line in lines:
        where = f"{line['run_id']} step {line['step']}"
        recorded = _traced_call(traced, line)
        if recorded is None:
            found.append(f"{where}: not in the trace")
            continue
        if not line.get("duration_ms"):
            found.append(f"{where}: no reply")
        elif "ttft_ms" not in line:
            found.append(f"{where}: no TTFT")
        for name in ("prompt_tokens", "completion_tokens"):
            if line[name] != recorded[name]:
                found.append(f"{where}: {name} {line[name]}, the trace has {recorded[name]}")
        deviation = abs(line["gap_before_ms"] - recorded["gap_before_ms"])
        if deviation > tolerance:
            found.append(
                f"{where}: gap {line['gap_before_ms']} ms, the trace has "
                f"{recorded['gap_before_ms']} ms, tolerance {tolerance} ms"
            )
    return found


def differences(
    trace: Sequence[Line], first: Sequence[Line], second: Sequence[Line], tolerance: int
) -> list[str]:
    return [
        *_sequence_differences(first, second),
        *_trace_differences(trace, first, tolerance),
        *_trace_differences(trace, second, tolerance),
    ]


def max_gap_deviation(trace: Sequence[Line], lines: Sequence[Line]) -> int:
    """The largest gap's distance from the recorded one, what the tolerance was guessed for."""
    traced = _traced_calls(trace)
    recorded = ((line, _traced_call(traced, line)) for line in lines)
    return max(
        (abs(line["gap_before_ms"] - call["gap_before_ms"]) for line, call in recorded if call),
        default=0,
    )


def _read_lines(path: Path, validator: Draft202012Validator) -> list[dict[str, Any]]:
    lines = [json.loads(text) for text in path.read_text().splitlines()]
    for number, line in enumerate(lines, start=1):
        check_trace_line(validator, line, f"{path}:{number}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.3: compare two replays of one trace.")
    parser.add_argument("trace", type=Path)
    parser.add_argument("pattern", help="the replay.patterns name both replays ran")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--profile", default="real", help="config/<profile>.yaml")
    parser.add_argument("--seed", type=int, help="default: replay.seed of the profile")
    args = parser.parse_args()

    profile = yaml.safe_load((REPO_ROOT / "config" / f"{args.profile}.yaml").read_text())
    config = load_replay_config(profile["replay"])
    tolerance = config.gap_tolerance_ms
    seed = config.seed if args.seed is None else args.seed
    validator = trace_validator()
    try:
        plan = build_plan(
            load_trace(args.trace),
            named_pattern(config, args.pattern),
            config.shared_prefix,
            seed,
            NO_TEMPLATE_TOKENS,
        )
        trace, first, second = (
            _read_lines(path, validator) for path in (args.trace, args.first, args.second)
        )
    except ReplayError as error:
        print(f"compare: {error}")
        raise SystemExit(1) from error

    found = [
        *(["first and second are the same replay, equal line for line"] if first == second else []),
        *(f"first: {text}" for text in plan_differences(plan, first)),
        *(f"second: {text}" for text in plan_differences(plan, second)),
        *differences(trace, first, second, tolerance),
    ]
    for text in found:
        print(f"compare: {text}")
    for name, lines in (("first", first), ("second", second)):
        print(
            f"compare: {name}: max gap deviation {max_gap_deviation(trace, lines)} ms, tolerance "
            f"{tolerance} ms"
        )
    if found:
        raise SystemExit(1)
    print(
        f"compare: same sequence, {len(first)} calls, the plan's calls, tokens as in the trace, a "
        "reply and TTFT on every call, all gaps in tolerance. Not checked: arrival times, prompt "
        "text, pods"
    )


if __name__ == "__main__":
    main()
