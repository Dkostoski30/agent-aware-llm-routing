"""make autoscale-compare (P4.5): the autoscalers' numbers across files of one real 1<->2 replay.

Reads only the .meta.json files make experiment wrote: each autoscaled variant's crossings and
scale-up windows (autoscale.py) and its summary over the kept runs. Files of differing replays, or
without the same variants, are refused. A variant's position in its file is by when it started.

Prints, per variant per file and per variant as a mean and min-max over the files: when remaining
work, the queue and the context passed their targets, when spec replicas rose, when the pods asked
for were Ready, finished and failed kept runs, run completion p50 (finished runs only), makespan,
idle pod-minutes and the KEDA operator's restarts; then per position the completion p50 and the
mean failed runs over whichever variants ran there, to show a drift over a sitting; then the
verdict. Writes the per-file rows to the CSV --out names.

The verdict (fixed before the runs, docs/task-cards/61-P4.5-real-autoscaling.md; the user,
2026-11-20: judge only what can exist at 1<->2) is "same ranking as on the simulator" when, for
remaining work against the queue, all three hold:
1. scale-up: remaining work never rose in no more files than the queue, and its mean spec-rise
   time over the files where it rose is no later; a file where a variant was scaled before its
   replay has no time for it and is counted apart;
2. completion: their mean completion p50s differ by no more than the larger of the two variants'
   own min-max spreads over the files;
3. failures: remaining work's failed kept runs, summed over the files, are no more than the queue's.
Idle pod-minutes are printed, not judged: at a ceiling of 2 pods no autoscaler asks for fewer, so
the simulator's lead cannot show, and whichever scales earlier pays more idle time.

Usage: python -m load_generator.autoscale_compare META [META ...] --out CSV
"""

import argparse
import csv
import dataclasses
import json
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The pair the simulator ranked (P3.10): remaining work against the queue.
CANDIDATE = "remaining"
REFERENCE = "queue"
# What makes two files one replay.
REPLAY_KEYS = ("trace", "pattern", "seed", "profile_sha256")


class CompareError(Exception):
    pass


@dataclass(frozen=True)
class VariantResult:
    # <name>-<hash>, the stem of the file's results.
    experiment: str
    # 1 for the variant that ran first in its file.
    position: int
    variant: str
    autoscaler: str
    # Seconds since the replay's start; None if it never happened.
    remaining_seconds: float | None
    queue_seconds: float | None
    context_seconds: float | None
    # Also None if the pods were scaled before the replay.
    scale_up_seconds: float | None
    scaled_before_replay: bool
    ready_seconds: float | None
    # Kept runs.
    runs: int
    finished_runs: int
    failed_runs: int
    # Over the finished runs; None with none.
    completion_p50_ms: int | None
    makespan_ms: int | None
    idle_pod_minutes: float
    # None with no sample of the count.
    keda_restarts: int | None


CSV_FIELDS = tuple(field.name for field in dataclasses.fields(VariantResult))


@dataclass(frozen=True)
class Spread:
    mean: float
    low: float
    high: float
    # How many files had the number.
    count: int


def variant_results(meta: Mapping[str, Any]) -> list[VariantResult]:
    """A missing key raises KeyError."""
    experiment = f"{meta['experiment']}-{meta['hash']}"
    ran = sorted(meta["variants"].items(), key=lambda item: item[1]["started"])
    results = []
    for position, (name, variant) in enumerate(ran, start=1):
        autoscale = variant["autoscale"]
        if autoscale is None:
            raise CompareError(f"{experiment}: variant {name} has no autoscaler")
        crossings = autoscale["crossings"]
        windows = autoscale["scale_up_windows"]
        summary = variant["autoscale_summary"]
        results.append(
            VariantResult(
                experiment=experiment,
                position=position,
                variant=name,
                autoscaler=autoscale["autoscaler"],
                remaining_seconds=crossings["remaining_seconds"],
                queue_seconds=crossings["queue_seconds"],
                context_seconds=windows["context_seconds"],
                scale_up_seconds=crossings["scale_up_seconds"],
                scaled_before_replay=crossings["scaled_before_replay"],
                ready_seconds=windows["ready_seconds"],
                runs=summary["runs"],
                finished_runs=summary["finished_runs"],
                failed_runs=summary["failed_runs"],
                completion_p50_ms=summary["completion_p50_ms"],
                makespan_ms=summary["makespan_ms"],
                idle_pod_minutes=summary["idle_pod_minutes"],
                keda_restarts=windows["restarts"],
            )
        )
    return results


def check_same_replay(metas: Sequence[Mapping[str, Any]]) -> None:
    first = metas[0]
    for other in metas[1:]:
        for key in REPLAY_KEYS:
            if other[key] != first[key]:
                raise CompareError(
                    f"{other['experiment']} replays another {key} than {first['experiment']}: "
                    f"{other[key]} against {first[key]}"
                )
        missing = set(first["variants"]) ^ set(other["variants"])
        if missing:
            raise CompareError(
                f"{first['experiment']} and {other['experiment']} differ in variants: "
                f"{', '.join(sorted(missing))}"
            )


def spread(values: Iterable[float | None]) -> Spread | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return Spread(statistics.fmean(present), min(present), max(present), len(present))


def _rises(results: Sequence[VariantResult]) -> Spread | None:
    return spread(result.scale_up_seconds for result in results)


def _never_rose(results: Sequence[VariantResult]) -> int:
    return sum(
        result.scale_up_seconds is None and not result.scaled_before_replay for result in results
    )


def scale_up_holds(candidate: Sequence[VariantResult], reference: Sequence[VariantResult]) -> bool:
    """With no rise time at all on one side, it holds only if that side is the reference's."""
    if _never_rose(candidate) > _never_rose(reference):
        return False
    rises, other = _rises(candidate), _rises(reference)
    if rises is None or other is None:
        return other is None
    return rises.mean <= other.mean


def completion_holds(
    candidate: Sequence[VariantResult], reference: Sequence[VariantResult]
) -> bool:
    """With no finished run on one side there is nothing to compare, so it does not hold."""
    times = spread(result.completion_p50_ms for result in candidate)
    other = spread(result.completion_p50_ms for result in reference)
    if times is None or other is None:
        return False
    allowed = max(times.high - times.low, other.high - other.low)
    return abs(times.mean - other.mean) <= allowed


def failures_hold(candidate: Sequence[VariantResult], reference: Sequence[VariantResult]) -> bool:
    return sum(result.failed_runs for result in candidate) <= sum(
        result.failed_runs for result in reference
    )


def _of(results: Sequence[VariantResult], autoscaler: str) -> list[VariantResult]:
    found = [result for result in results if result.autoscaler == autoscaler]
    if not found:
        raise CompareError(f"the verdict compares {CANDIDATE} with {REFERENCE}: no {autoscaler}")
    return found


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} s"


def _spread_text(found: Spread | None, show: Callable[[float], str]) -> str:
    if found is None:
        return "-"
    return f"{show(found.mean)} ({show(found.low)}-{show(found.high)}, {found.count} files)"


def _ms_as_seconds(value: float) -> str:
    return f"{value / 1000:.1f} s"


def _plain_seconds(value: float) -> str:
    return f"{value:.1f} s"


def _pod_minutes(value: float) -> str:
    return f"{value:.2f}"


def verdict_lines(results: Sequence[VariantResult]) -> list[str]:
    candidate, reference = _of(results, CANDIDATE), _of(results, REFERENCE)
    checks = {
        "scale-up": scale_up_holds(candidate, reference),
        "completion": completion_holds(candidate, reference),
        "failures": failures_hold(candidate, reference),
    }
    lines = []
    for name, found in ((CANDIDATE, candidate), (REFERENCE, reference)):
        before = sum(result.scaled_before_replay for result in found)
        lines.append(
            f"{name}: spec rose {_spread_text(_rises(found), _plain_seconds)}; completion p50 "
            f"{_spread_text(spread(r.completion_p50_ms for r in found), _ms_as_seconds)}; "
            f"failed kept runs {sum(result.failed_runs for result in found)}"
        )
        lines.append(
            f"{name} scaled before its replay in {before} file(s), never rose in "
            f"{_never_rose(found)}"
        )
    for name, held in checks.items():
        lines.append(f"{name}: {'holds' if held else 'fails'}")
    failed = [name for name, held in checks.items() if not held]
    if failed:
        lines.append(f"verdict: different (failed: {', '.join(failed)})")
    else:
        lines.append("verdict: same ranking as on the simulator")
    return lines


def position_means(results: Sequence[VariantResult]) -> dict[int, tuple[Spread | None, float]]:
    """Per position: the completion p50s over whichever variants ran there, and their mean failed
    kept runs."""
    positions = sorted({result.position for result in results})
    means = {}
    for position in positions:
        there = [result for result in results if result.position == position]
        means[position] = (
            spread(result.completion_p50_ms for result in there),
            statistics.fmean(result.failed_runs for result in there),
        )
    return means


def write_csv(path: Path, results: Iterable[VariantResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_FIELDS)
        for result in results:
            writer.writerow(
                ["" if value is None else value for value in dataclasses.astuple(result)]
            )


def _row_line(result: VariantResult) -> str:
    p50 = None if result.completion_p50_ms is None else result.completion_p50_ms / 1000
    makespan = None if result.makespan_ms is None else result.makespan_ms / 1000
    restarts = "-" if result.keda_restarts is None else str(result.keda_restarts)
    rose = "before the replay" if result.scaled_before_replay else _seconds(result.scale_up_seconds)
    return (
        f"  {result.position} {result.variant:<10} remaining {_seconds(result.remaining_seconds)}, "
        f"queue {_seconds(result.queue_seconds)}, context {_seconds(result.context_seconds)}; "
        f"spec rose {rose}, Ready {_seconds(result.ready_seconds)}; "
        f"runs {result.finished_runs}/{result.runs} finished, "
        f"{result.failed_runs} failed; p50 {_seconds(p50)}, makespan {_seconds(makespan)}; "
        f"idle {result.idle_pod_minutes:.2f} pod-min; KEDA restarts {restarts}"
    )


def _variant_lines(results: Sequence[VariantResult]) -> list[str]:
    lines = []
    for name in dict.fromkeys(result.variant for result in results):
        found = [result for result in results if result.variant == name]
        lines.append(
            f"  {name:<10} spec rose {_spread_text(_rises(found), _plain_seconds)}; "
            f"Ready {_spread_text(spread(r.ready_seconds for r in found), _plain_seconds)}; "
            f"failed {sum(result.failed_runs for result in found)} of "
            f"{sum(result.runs for result in found)}; p50 "
            f"{_spread_text(spread(r.completion_p50_ms for r in found), _ms_as_seconds)}; idle "
            f"{_spread_text(spread(r.idle_pod_minutes for r in found), _pod_minutes)} pod-min"
        )
    return lines


def report_lines(results: Sequence[VariantResult]) -> list[str]:
    lines = []
    for experiment in dict.fromkeys(result.experiment for result in results):
        lines.append(experiment)
        lines += [_row_line(result) for result in results if result.experiment == experiment]
    lines.append("per variant, mean (min-max) over the files:")
    lines += _variant_lines(results)
    lines.append("per position, over whichever variant ran there:")
    for position, (times, failed) in position_means(results).items():
        lines.append(
            f"  {position}: completion p50 {_spread_text(times, _ms_as_seconds)}, "
            f"failed kept runs {failed:.2f} per file"
        )
    return lines + verdict_lines(results)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("metas", type=Path, nargs="+", help="the files' .meta.json")
    parser.add_argument("--out", type=Path, required=True, help="the CSV to write")
    args = parser.parse_args(argv)
    metas = [json.loads(path.read_text()) for path in args.metas]
    check_same_replay(metas)
    results = [result for meta in metas for result in variant_results(meta)]
    print("\n".join(report_lines(results)))
    write_csv(args.out, results)
    print(f"autoscale-compare: wrote {args.out}")


if __name__ == "__main__":
    main()
