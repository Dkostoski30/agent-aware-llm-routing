"""make pressure (P4.9): cache pressure at each later-step call's start, on the real-pod replays.

Cache pressure at a moment is the context the live runs hold over the KV cache of the pods. A run
is live from its first call's start to its last call's end, and holds the prompt tokens of its
latest call begun by then, so between its calls too; a fanned-out step's calls share one context
and count once (docs/task-cards/53-P4.9-cache-pressure-curve.md). The pods hold replicas x
kv_capacity_tokens_per_pod, from the experiment's .meta.json.

Decided with the user (2026-11-20): every real-pod variant with a fixed pod count is read, as
experiments/pressure.yaml lists them; the warm-up runs a report discards are live, since the pods
hold their context, but give no row; a run whose call failed stops being live when that call was
sent, and how many did is printed.

Times are rebuilt as results.py rebuilds them, from each run's planned start and the replay's
gaps and durations; ts_start, the wall clock, is never read (P2.2a). The planned starts come from
today's profile's pattern at the file's seed: the patterns' numbers have not changed since the
first of these files (git log -p config/real.yaml), and a run past the pattern's count is refused.
A simulated variant is refused: the simulator does not model KV admission (P3.7b).

Writes experiments/results/pressure-<hash>.csv (one row per later-step call of a kept run that has
a first token) and experiments/graphs/pressure-<hash>.png, and prints per variant and pooled, per
bucket of config/report.yaml's pressure_bucket_edges: the calls, first-token median and p90, and
the mean cached share. The hash is experiments/pressure.yaml's (D11).

Usage: python -m load_generator.pressure FILE
"""

import argparse
import csv
import dataclasses
import json
import statistics
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from load_generator.graph import render_pressure
from load_generator.plan import Pattern, load_replay_config, run_position, start_times
from load_generator.record import REPO_ROOT, experiment_hash
from load_generator.report_config import load_report_config
from load_generator.results import Line, Timed, percentile, timed_calls

SIM_PROFILE = "sim"
# A share of tokens, so 4 places keep a token in 10,000 while the CSV stays readable.
PLACES = 4


class PressureError(Exception):
    pass


@dataclass(frozen=True)
class PressureRow:
    experiment: str
    variant: str
    run: str
    step: int
    pressure: float
    ttft_ms: int
    # None when vLLM gave no cached count.
    cached_share: float | None


CSV_FIELDS = tuple(field.name for field in dataclasses.fields(PressureRow))


@dataclass(frozen=True)
class BucketStats:
    label: str
    calls: int
    ttft_p50_ms: int | None
    ttft_p90_ms: int | None
    # The mean over the bucket's calls that have a cached count.
    cached_share: float | None


@dataclass(frozen=True)
class VariantInput:
    capacity_tokens: int
    starts: list[float]
    runs_discarded: int


@dataclass(frozen=True)
class Source:
    experiment: str
    variants: list[str]


def variant_profile(meta: Mapping[str, Any], variant: str) -> str:
    """The variant's profile; the first baselines' .meta.json name it once, for the file."""
    if variant not in meta["variants"]:
        raise PressureError(
            f"{meta.get('experiment', 'the experiment')} has no variant {variant}, it has "
            f"{sorted(meta['variants'])}"
        )
    profile: str = meta["variants"][variant].get("profile") or meta["profile"]
    return profile


def variant_input(
    meta: Mapping[str, Any],
    variant: str,
    profiles: Mapping[str, Mapping[str, Any]],
    patterns: Mapping[str, Pattern],
) -> VariantInput:
    """The pods' capacity, the planned starts and the warm-up count of one variant's replay."""
    profile = variant_profile(meta, variant)
    section = meta["variants"][variant]
    if profile == SIM_PROFILE:
        raise PressureError(
            f"variant {variant} ran on simulated pods, and the simulator does not model KV "
            "admission (P3.7b), so its pressure would read nothing"
        )
    if section.get("autoscale") is not None:
        raise PressureError(
            f"variant {variant} was autoscaled: its pods changed during the replay, so their "
            "capacity is not one number"
        )
    replicas: int = section.get("replicas") or profiles[profile]["replicas"]
    return VariantInput(
        capacity_tokens=replicas * meta["kv_capacity_tokens_per_pod"],
        starts=start_times(patterns[meta["pattern"]], meta["seed"]),
        runs_discarded=meta["runs_discarded"],
    )


def _spans(calls: Sequence[Timed]) -> tuple[int, int]:
    """When the run is live: its first call's start to its last call's end, or to the start of
    its first failed call."""
    begun = min(call.begun for call in calls)
    failed = [call.begun for call in calls if call.ended is None]
    if failed:
        return begun, min(failed)
    return begun, max(call.ended for call in calls if call.ended is not None)


def _held(calls: Sequence[Timed], at: int) -> int:
    """The prompt of the run's latest step begun by at, once however many calls it fanned out to."""
    begun = [call for call in calls if call.begun <= at]
    latest = max(call.line["step"] for call in begun)
    held: int = max(call.line["prompt_tokens"] for call in begun if call.line["step"] == latest)
    return held


def pressure_rows(
    experiment: str,
    variant: str,
    lines: Sequence[Line],
    starts: Sequence[float],
    runs_discarded: int,
    capacity_tokens: int,
) -> tuple[list[PressureRow], int]:
    """The rows, in the lines' order, and how many runs a failed call ended."""
    for line in lines:
        if run_position(line["run_id"]) > len(starts):
            raise PressureError(
                f"run {line['run_id']} is number {run_position(line['run_id'])}, the pattern "
                f"starts {len(starts)}"
            )
    timed = timed_calls(lines, starts)
    by_run: dict[str, list[Timed]] = {}
    for call in timed:
        by_run.setdefault(call.line["run_id"], []).append(call)
    spans = {run: _spans(calls) for run, calls in by_run.items()}
    found = []
    for call in timed:
        line = call.line
        if line["step"] == 1 or "ttft_ms" not in line:
            continue
        if run_position(line["run_id"]) <= runs_discarded:
            continue
        held = sum(
            _held(by_run[run], call.begun)
            for run, (begun, ended) in spans.items()
            if begun <= call.begun < ended
        )
        cached = line.get("cached_tokens")
        found.append(
            PressureRow(
                experiment=experiment,
                variant=variant,
                run=line["run_id"],
                step=line["step"],
                pressure=round(held / capacity_tokens, PLACES),
                ttft_ms=line["ttft_ms"],
                cached_share=(
                    None if cached is None else round(cached / line["prompt_tokens"], PLACES)
                ),
            )
        )
    ended_by_failure = sum(any(call.ended is None for call in calls) for calls in by_run.values())
    return found, ended_by_failure


def bucket_of(pressure: float, edges: Sequence[float]) -> int:
    """A bucket holds its lower edge."""
    return bisect_right(edges, pressure)


def bucket_labels(edges: Sequence[float]) -> list[str]:
    inner = [f"{low:g}-{high:g}" for low, high in pairwise(edges)]
    return [f"under {edges[0]:g}", *inner, f"{edges[-1]:g} and over"]


def bucket_stats(rows: Iterable[PressureRow], edges: Sequence[float]) -> list[BucketStats]:
    buckets: list[list[PressureRow]] = [[] for _ in range(len(edges) + 1)]
    for row in rows:
        buckets[bucket_of(row.pressure, edges)].append(row)
    stats = []
    for label, members in zip(bucket_labels(edges), buckets, strict=True):
        times = [row.ttft_ms for row in members]
        shares = [row.cached_share for row in members if row.cached_share is not None]
        stats.append(
            BucketStats(
                label=label,
                calls=len(members),
                ttft_p50_ms=percentile(times, 0.5) if times else None,
                ttft_p90_ms=percentile(times, 0.9) if times else None,
                cached_share=round(statistics.fmean(shares), PLACES) if shares else None,
            )
        )
    return stats


def write_csv(path: Path, rows: Iterable[PressureRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_FIELDS)
        for row in rows:
            writer.writerow(["" if value is None else value for value in dataclasses.astuple(row)])


def load_sources(path: Path) -> list[Source]:
    """A missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    sources = [Source(entry["experiment"], list(entry["variants"])) for entry in spec["sources"]]
    for source in sources:
        if not source.variants:
            raise PressureError(f"{source.experiment} names no variant")
    return sources


def _seconds(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else f"{milliseconds / 1000:.1f} s"


def table_lines(title: str, stats: Sequence[BucketStats]) -> list[str]:
    lines = [title, f"  {'pressure':<14}{'calls':>7}{'p50':>10}{'p90':>10}{'cached':>8}"]
    for bucket in stats:
        share = "-" if bucket.cached_share is None else f"{bucket.cached_share:.2f}"
        lines.append(
            f"  {bucket.label:<14}{bucket.calls:>7}{_seconds(bucket.ttft_p50_ms):>10}"
            f"{_seconds(bucket.ttft_p90_ms):>10}{share:>8}"
        )
    return lines


def _profile(name: str) -> dict[str, Any]:
    profile: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config" / f"{name}.yaml").read_text())
    return profile


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="experiments/pressure.yaml")
    args = parser.parse_args(argv)
    edges = load_report_config().pressure_bucket_edges
    results = args.file.parent / "results"
    stem = f"pressure-{experiment_hash(args.file)}"
    profiles: dict[str, dict[str, Any]] = {}
    rows: list[PressureRow] = []
    printed: list[str] = []
    for source in load_sources(args.file):
        meta = json.loads((results / f"{source.experiment}.meta.json").read_text())
        for variant in source.variants:
            name = variant_profile(meta, variant)
            if name not in profiles:
                profiles[name] = _profile(name)
            patterns = load_replay_config(profiles[name]["replay"]).patterns
            given = variant_input(meta, variant, profiles, patterns)
            replay = results / f"{source.experiment}-{variant}.jsonl"
            lines = [json.loads(text) for text in replay.read_text().splitlines()]
            found, ended_by_failure = pressure_rows(
                source.experiment,
                variant,
                lines,
                given.starts,
                given.runs_discarded,
                given.capacity_tokens,
            )
            rows += found
            title = (
                f"{source.experiment} {variant}: {len(found)} calls, "
                f"{given.capacity_tokens} KV tokens, {ended_by_failure} runs ended by a failed call"
            )
            printed += table_lines(title, bucket_stats(found, edges))
    pooled = bucket_stats(rows, edges)
    printed += table_lines(f"pooled: {len(rows)} calls", pooled)
    csv_path = results / f"{stem}.csv"
    graph = args.file.parent / "graphs" / f"{stem}.png"
    write_csv(csv_path, rows)
    render_pressure(
        [bucket.label for bucket in pooled],
        [bucket.calls for bucket in pooled],
        [bucket.ttft_p50_ms for bucket in pooled],
        [bucket.ttft_p90_ms for bucket in pooled],
        [bucket.cached_share for bucket in pooled],
        graph,
    )
    print("\n".join(printed))
    print(f"pressure: wrote {csv_path} and {graph}")


if __name__ == "__main__":
    main()
