"""An experiment's numbers from its replay lines (P2.5). Pure: no cluster, no clock.

A call's times are rebuilt from its run's planned start and the run's gaps and durations, which all
come from the replay's monotonic clock. ts_start is never read: it is the wall clock, which moved
seconds against the monotonic one under WSL2 (P2.2a).

tokens_on_pod is the KV split P2.4 asks for, so that a capacity effect is not read as a cache
effect. It is an upper estimate: a call's own prompt and reply tokens plus those of every call on
its pod that overlaps it up to its first token, as if each held all of its tokens the whole time. A
failed call names no pod, so it adds no load.
"""

import csv
import dataclasses
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import groupby, pairwise
from pathlib import Path
from typing import Any

from load_generator.plan import run_position
from load_generator.report_config import load_report_config

Line = Mapping[str, Any]


@dataclass(frozen=True)
class CallRow:
    """One CSV row. None is a blank: a measurement the call has not got."""

    variant: str
    run: str
    task_type: str
    step: int
    pod: str | None
    ttft_ms: int | None
    duration_ms: int | None
    gap_before_ms: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    # Its pod served no call of the step before. None on a first step and on a failed call.
    moved: bool | None
    tokens_on_pod: int | None
    over_kv_limit: bool | None


CSV_FIELDS = tuple(field.name for field in dataclasses.fields(CallRow))
FEWEST_TOKENS_FOR_SPEED = load_report_config().fewest_tokens_for_speed


@dataclass(frozen=True)
class _Timed:
    """In milliseconds from the replay's start. ended is None for a failed call."""

    line: Line
    begun: int
    first_token: int | None
    ended: int | None


@dataclass(frozen=True)
class TtftStats:
    calls: int
    p50_ms: int | None
    p95_ms: int | None


@dataclass(frozen=True)
class VariantSummary:
    runs: int
    # A failed run has no completion time, so a variant's times leave out its worst runs: read them
    # together with this count.
    failed_runs: int
    calls: int
    failed_calls: int
    later_steps: int
    moved: int
    hit_rate: float | None
    completions_ms: list[int]
    # Median over calls of FEWEST_TOKENS_FOR_SPEED tokens or more. A picker moves this speed little,
    # through who shares a pod, so a large difference between variants points at the host or the
    # order they ran in: the first baseline's default variant was 15% slower for such a reason,
    # which run times alone hide.
    generation_ms_per_token: float | None


@dataclass(frozen=True)
class VariantTotals:
    # Over the finished runs only, so it means little without these two counts.
    run_ms: int
    finished_runs: int
    runs: int
    ttft_ms: int
    generation_ms: int
    # Over the calls with a cached count: too low by what the others hold.
    uncached_tokens: int
    calls_without_cached_count: int
    under_limit_calls: int
    under_limit_ttft_ms: int
    under_limit_uncached_tokens: int
    over_limit_calls: int
    over_limit_ttft_ms: int


def _timed_calls(lines: Sequence[Line], starts: Sequence[float]) -> list[_Timed]:
    """In the order of the lines. A step starts when the step before it ended, its last reply when
    it fanned out, plus its gap (D9)."""
    by_run: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        by_run.setdefault(line["run_id"], []).append(index)
    timed: dict[int, _Timed] = {}
    for run_id, indexes in by_run.items():
        clock = round(starts[run_position(run_id) - 1] * 1000)
        for _, step_indexes in groupby(indexes, key=lambda index: lines[index]["step"]):
            ends = []
            for index in step_indexes:
                line = lines[index]
                begun = clock + line["gap_before_ms"]
                if "duration_ms" not in line:
                    timed[index] = _Timed(line, begun, None, None)
                    continue
                ended = begun + line["duration_ms"]
                first_token = begun + line["ttft_ms"] if "ttft_ms" in line else ended
                timed[index] = _Timed(line, begun, first_token, ended)
                ends.append(ended)
            if ends:
                clock = max(ends)
    return [timed[index] for index in range(len(lines))]


def _tokens(line: Line) -> int:
    tokens: int = line["prompt_tokens"] + line["completion_tokens"]
    return tokens


def _tokens_on_pod(call: _Timed, answered: Sequence[_Timed]) -> int:
    """Its own tokens, and those of each call on its pod that was in flight between its start and
    its first token. A call that ended exactly as this one began has left."""
    until = call.first_token
    assert until is not None
    return _tokens(call.line) + sum(
        _tokens(other.line)
        for other in answered
        if other is not call
        and other.line["pod"] == call.line["pod"]
        and other.begun < until
        and other.ended is not None
        and other.ended > call.begun
    )


def _pods_by_step(lines: Iterable[Line]) -> dict[tuple[str, int], set[str]]:
    pods: dict[tuple[str, int], set[str]] = {}
    for line in lines:
        if "pod" in line:
            pods.setdefault((line["run_id"], line["step"]), set()).add(line["pod"])
    return pods


def call_rows(
    variant: str,
    lines: Sequence[Line],
    starts: Sequence[float],
    kv_capacity_tokens: int,
    runs_discarded: int,
) -> list[CallRow]:
    """One row per line, in their order, without the first runs by start time. The dropped runs
    still count in tokens_on_pod, since their calls did fill the pods."""
    timed = _timed_calls(lines, starts)
    answered = [call for call in timed if call.ended is not None and "pod" in call.line]
    pods = _pods_by_step(lines)
    rows = []
    for call in timed:
        line = call.line
        if run_position(line["run_id"]) <= runs_discarded:
            continue
        has_pod = call.ended is not None and "pod" in line
        tokens = _tokens_on_pod(call, answered) if has_pod else None
        before = pods.get((line["run_id"], line["step"] - 1))
        rows.append(
            CallRow(
                variant=variant,
                run=line["run_id"],
                task_type=line["task_type"],
                step=line["step"],
                pod=line.get("pod"),
                ttft_ms=line.get("ttft_ms"),
                duration_ms=line.get("duration_ms"),
                gap_before_ms=line["gap_before_ms"],
                prompt_tokens=line["prompt_tokens"],
                completion_tokens=line["completion_tokens"],
                cached_tokens=line.get("cached_tokens"),
                moved=None if before is None or not has_pod else line["pod"] not in before,
                tokens_on_pod=tokens,
                over_kv_limit=None if tokens is None else tokens > kv_capacity_tokens,
            )
        )
    return rows


def pod_switch_share(lines: Sequence[Line], starts: Sequence[float]) -> float | None:
    """The share of picks that went to another pod than the pick before, in order of arrival at the
    gateway. Strict round-robin on 2 pods gives 1, a coin toss 0.5. None with under 2 picks."""
    picks = sorted(
        (call for call in _timed_calls(lines, starts) if "pod" in call.line),
        key=lambda call: call.begun,
    )
    if len(picks) < 2:
        return None
    pods: list[str] = [call.line["pod"] for call in picks]
    switches = sum(one != other for one, other in pairwise(pods))
    return switches / (len(pods) - 1)


def calls_per_pod(lines: Iterable[Line]) -> dict[str, int]:
    return dict(Counter(line["pod"] for line in lines if "pod" in line))


def run_completion_ms(calls: Sequence[CallRow]) -> int | None:
    """A run's time from its first call to its last reply: each step's slowest call and its gap.
    None if a call failed, since the run did not complete."""
    if any(call.duration_ms is None for call in calls):
        return None
    total = 0
    for _, step_calls in groupby(calls, key=lambda call: call.step):
        total += max(call.gap_before_ms + (call.duration_ms or 0) for call in step_calls)
    return total


def percentile(values: Iterable[int], share: float) -> int:
    """The nearest rank: a value that was measured, not a mix of two."""
    ordered = sorted(values)
    return ordered[math.ceil(share * len(ordered)) - 1]


def ttft_stats(rows: Iterable[CallRow], over_limit: bool, moved: bool | None = None) -> TtftStats:
    """The calls over or under the KV limit, and among them only moved or stayed if asked."""
    times = [
        row.ttft_ms
        for row in rows
        if row.ttft_ms is not None
        and row.over_kv_limit is over_limit
        and (moved is None or row.moved is moved)
    ]
    if not times:
        return TtftStats(0, None, None)
    return TtftStats(len(times), percentile(times, 0.5), percentile(times, 0.95))


def _uncached_tokens(row: CallRow) -> int:
    return row.prompt_tokens - (row.cached_tokens or 0)


def _hit_rate(rows: Sequence[CallRow]) -> float | None:
    """A call with no count is unknown, not a miss, so its prompt is left out too."""
    counted = [row for row in rows if row.cached_tokens is not None]
    prompt = sum(row.prompt_tokens for row in counted)
    return sum(row.cached_tokens or 0 for row in counted) / prompt if prompt else None


def _generation_ms_per_token(rows: Iterable[CallRow]) -> float | None:
    """Time after the first token over the tokens after it, so no queue or prefill counts."""
    speeds = [
        (row.duration_ms - row.ttft_ms) / (row.completion_tokens - 1)
        for row in rows
        if row.duration_ms is not None
        and row.ttft_ms is not None
        and row.completion_tokens >= FEWEST_TOKENS_FOR_SPEED
    ]
    return statistics.median(speeds) if speeds else None


def variant_rows(rows: Iterable[CallRow]) -> dict[str, list[CallRow]]:
    """Each variant's rows, in the order the variants ran."""
    by_variant: dict[str, list[CallRow]] = {}
    for row in rows:
        by_variant.setdefault(row.variant, []).append(row)
    return by_variant


def _summary(rows: Sequence[CallRow]) -> VariantSummary:
    by_run: dict[str, list[CallRow]] = {}
    for row in rows:
        by_run.setdefault(row.run, []).append(row)
    times = [run_completion_ms(calls) for calls in by_run.values()]
    finished = sorted(time for time in times if time is not None)
    return VariantSummary(
        runs=len(by_run),
        failed_runs=len(by_run) - len(finished),
        calls=len(rows),
        failed_calls=sum(row.duration_ms is None for row in rows),
        later_steps=sum(row.moved is not None for row in rows),
        moved=sum(row.moved is True for row in rows),
        hit_rate=_hit_rate(rows),
        completions_ms=finished,
        generation_ms_per_token=_generation_ms_per_token(rows),
    )


def summarise(rows: Iterable[CallRow]) -> dict[str, VariantSummary]:
    return {variant: _summary(calls) for variant, calls in variant_rows(rows).items()}


def run_times_ms(rows: Iterable[CallRow]) -> dict[str, int | None]:
    """Each run's completion time, in one variant. None for a run with a failed call."""
    by_run: dict[str, list[CallRow]] = {}
    for row in rows:
        by_run.setdefault(row.run, []).append(row)
    return {run: run_completion_ms(calls) for run, calls in by_run.items()}


def variant_totals(rows: Iterable[CallRow]) -> VariantTotals:
    """Sums over one variant's answered calls. A sum of times shows where a difference between
    variants came from, which medians cannot. Like ttft_stats, a call with no split is in neither
    band."""
    calls = list(rows)
    answered = [call for call in calls if call.duration_ms is not None and call.ttft_ms is not None]
    counted = [call for call in answered if call.cached_tokens is not None]
    over = [call for call in answered if call.over_kv_limit is True]
    under = [call for call in answered if call.over_kv_limit is False]
    times = run_times_ms(calls)
    finished = [time for time in times.values() if time is not None]
    return VariantTotals(
        run_ms=sum(finished),
        finished_runs=len(finished),
        runs=len(times),
        ttft_ms=sum(call.ttft_ms or 0 for call in answered),
        generation_ms=sum((call.duration_ms or 0) - (call.ttft_ms or 0) for call in answered),
        uncached_tokens=sum(_uncached_tokens(call) for call in counted),
        calls_without_cached_count=len(answered) - len(counted),
        under_limit_calls=len(under),
        under_limit_ttft_ms=sum(call.ttft_ms or 0 for call in under),
        under_limit_uncached_tokens=sum(
            _uncached_tokens(call) for call in under if call.cached_tokens is not None
        ),
        over_limit_calls=len(over),
        over_limit_ttft_ms=sum(call.ttft_ms or 0 for call in over),
    )


def slow_first_tokens(rows: Iterable[CallRow], threshold_ms: int) -> list[CallRow]:
    """Calls whose first token took at least threshold_ms, slowest first."""
    slow = [row for row in rows if row.ttft_ms is not None and row.ttft_ms >= threshold_ms]
    return sorted(slow, key=lambda row: row.ttft_ms or 0, reverse=True)


def write_csv(path: Path, rows: Iterable[CallRow]) -> None:
    """Mode x: a result is never written over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(dataclasses.asdict(row) for row in rows)


def _int(text: str) -> int | None:
    return int(text) if text else None


def _bool(text: str) -> bool | None:
    return text == "True" if text else None


def read_csv(path: Path) -> list[CallRow]:
    with path.open(newline="") as file:
        return [
            CallRow(
                variant=fields["variant"],
                run=fields["run"],
                task_type=fields["task_type"],
                step=int(fields["step"]),
                pod=fields["pod"] or None,
                ttft_ms=_int(fields["ttft_ms"]),
                duration_ms=_int(fields["duration_ms"]),
                gap_before_ms=int(fields["gap_before_ms"]),
                prompt_tokens=int(fields["prompt_tokens"]),
                completion_tokens=int(fields["completion_tokens"]),
                cached_tokens=_int(fields["cached_tokens"]),
                moved=_bool(fields["moved"]),
                tokens_on_pod=_int(fields["tokens_on_pod"]),
                over_kv_limit=_bool(fields["over_kv_limit"]),
            )
            for fields in csv.DictReader(file)
        ]
