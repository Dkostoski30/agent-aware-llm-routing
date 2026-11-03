"""make experiment-compare (P2.5): numbers across CSVs of one load, from the CSVs alone.

A variant's medians say little about where a difference came from, so this gives sums, the runs
paired one by one, and the slow first tokens. Over files, the same runs averaged per variant cancel
a steady drift when the files run the variants in opposite orders. The same variant between files
says how large a difference can be with no routing change at all.

Usage: python -m load_generator.experiment_compare --stall-seconds N CSV [CSV ...]
"""

import argparse
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from load_generator.report_config import load_report_config
from load_generator.results import (
    CallRow,
    read_csv,
    run_times_ms,
    slow_first_tokens,
    variant_rows,
    variant_totals,
)

RUNS_LEFT_OUT = load_report_config().runs_left_out
# Not a setting: every number here is one side against the other.
VARIANTS_COMPARED = 2


@dataclass(frozen=True)
class RunDifferences:
    """Over first minus second, so a negative difference means first was faster."""

    first_faster: int
    second_faster: int
    # None with no runs to pair.
    median_ms: float | None
    total_ms: float


def _seconds(milliseconds: float) -> str:
    return f"{milliseconds / 1000:.1f} s"


def paired_differences(
    first: Mapping[str, int | None], second: Mapping[str, int | None]
) -> dict[str, int]:
    """First minus second, per run that finished on both sides."""
    differences = {}
    for run, time in first.items():
        other = second[run]
        if time is not None and other is not None:
            differences[run] = time - other
    return differences


def run_differences_summary(differences: Mapping[str, float]) -> RunDifferences:
    values = list(differences.values())
    return RunDifferences(
        first_faster=sum(value < 0 for value in values),
        second_faster=sum(value > 0 for value in values),
        median_ms=statistics.median(values) if values else None,
        total_ms=sum(values),
    )


def without_largest(
    first: Mapping[str, float], second: Mapping[str, float], count: int
) -> tuple[float, float]:
    """Both totals without the count runs that most favour the side the totals favour."""
    direction = 1 if sum(first.values()) >= sum(second.values()) else -1
    ordered = sorted(first, key=lambda run: (first[run] - second[run]) * direction, reverse=True)
    kept = ordered[count:]
    return sum(first[run] for run in kept), sum(second[run] for run in kept)


def _load(csvs: Sequence[Path]) -> dict[Path, dict[str, list[CallRow]]]:
    if len(set(csvs)) != len(csvs):
        raise ValueError(f"a file is named twice: {[str(path) for path in csvs]}")
    files = {path: variant_rows(read_csv(path)) for path in csvs}
    names = sorted(files[csvs[0]])
    if len(names) != VARIANTS_COMPARED:
        raise ValueError(f"{csvs[0]} has {names}, a comparison needs {VARIANTS_COMPARED} variants")
    runs = {row.run for row in files[csvs[0]][names[0]]}
    for path, variants in files.items():
        if sorted(variants) != names:
            raise ValueError(f"{path} has other variants than {csvs[0]}: {sorted(variants)}")
        for variant, rows in variants.items():
            if {row.run for row in rows} != runs:
                raise ValueError(f"{path}, {variant}: other runs than {csvs[0]}'s, so another load")
    return files


def _labels(csvs: Sequence[Path]) -> dict[Path, str]:
    """Names, or whole paths if two share one: the first run and its rerun do."""
    names = [path.name for path in csvs]
    unique = len(set(names)) == len(names)
    return {path: path.name if unique else str(path) for path in csvs}


def _difference_line(first: str, second: str, summary: RunDifferences, runs: int) -> str:
    median = "-" if summary.median_ms is None else f"{summary.median_ms / 1000:.2f} s"
    return (
        f"{first} faster in {summary.first_faster} of {runs} runs, {second} in "
        f"{summary.second_faster}; {first} minus {second} per run: median {median}"
    )


def _variant_lines(variant: str, rows: Sequence[CallRow], stall_ms: int) -> list[str]:
    totals = variant_totals(rows)
    slow = slow_first_tokens(rows, stall_ms)
    no_count = totals.calls_without_cached_count
    lines = [
        f"  {variant}: runs {_seconds(totals.run_ms)} over {totals.finished_runs} of {totals.runs} "
        f"finished, first tokens {_seconds(totals.ttft_ms)}, generation "
        f"{_seconds(totals.generation_ms)}, uncached tokens {totals.uncached_tokens}"
        + (f", and {no_count} calls with no count" if no_count else ""),
        f"    under the KV limit: {totals.under_limit_calls} calls, first tokens "
        f"{_seconds(totals.under_limit_ttft_ms)}, uncached tokens "
        f"{totals.under_limit_uncached_tokens}",
        f"    over the KV limit: {totals.over_limit_calls} calls, first tokens "
        f"{_seconds(totals.over_limit_ttft_ms)}",
        f"    {variant}: {len(slow)} calls of {_seconds(stall_ms)} or more to first token, "
        f"{_seconds(sum(call.ttft_ms or 0 for call in slow))} in all",
    ]
    lines += [
        f"      {call.run} step {call.step} on {call.pod}: {_seconds(call.ttft_ms or 0)}"
        for call in slow
    ]
    return lines


def _average_lines(
    first: str, second: str, times: Mapping[str, Sequence[Mapping[str, int | None]]]
) -> list[str]:
    """Over the runs that finished in every file, on both sides."""
    every_run = list(times[first][0])
    finished = [
        run
        for run in every_run
        if all(file[run] is not None for variant in times.values() for file in variant)
    ]
    files = len(times[first])
    if not finished:
        return [f"over {files} files: no run finished in every file on both sides"]

    def averaged(variant: str) -> dict[str, float]:
        return {run: sum(file[run] or 0 for file in times[variant]) / files for run in finished}

    one, other = averaged(first), averaged(second)
    one_total, other_total = sum(one.values()), sum(other.values())
    lines = [
        f"over {files} files: {first} {_seconds(one_total)}, {second} {_seconds(other_total)}; "
        f"{first} minus {second} {_seconds(one_total - other_total)}, "
        f"{100 * (one_total - other_total) / other_total:.1f}% of {second}"
    ]
    summary = run_differences_summary({run: one[run] - other[run] for run in finished})
    lines.append(f"  {_difference_line(first, second, summary, len(finished))}")
    for count in RUNS_LEFT_OUT:
        if count < len(finished):
            kept_one, kept_other = without_largest(one, other, count)
            lines.append(
                f"  without the {count} runs that favour that side most: "
                f"{100 * (kept_one - kept_other) / kept_other:.1f}% of {second}"
            )
    if len(finished) < len(every_run):
        lines.append(f"  left out {len(every_run) - len(finished)} runs that failed in a file")
    return lines


def _between_files_lines(
    labels: Sequence[str], variant: str, files: Sequence[Mapping[str, int | None]]
) -> list[str]:
    """Each later file against the first, for one variant. Total keeps its sign, and the median is
    of each run's size, since a drift in one direction would otherwise hide a wide spread."""
    lines = []
    for label, times in zip(labels[1:], files[1:], strict=True):
        differences = paired_differences(times, files[0])
        sizes = [abs(difference) for difference in differences.values()]
        if not sizes:
            lines.append(f"{variant} between {label} and {labels[0]}: no run finished in both")
            continue
        lines.append(
            f"{variant} between {label} and {labels[0]}: total "
            f"{_seconds(sum(differences.values()))}, median size per run "
            f"{statistics.median(sizes) / 1000:.2f} s"
        )
    return lines


def report_lines(csvs: Sequence[Path], stall_ms: int) -> list[str]:
    files = _load(csvs)
    first, second = sorted(files[csvs[0]])
    labels = _labels(csvs)
    lines = []
    for path, variants in files.items():
        lines.append(f"{labels[path]}, in order {', '.join(variants)}")
        for variant, rows in variants.items():
            lines += _variant_lines(variant, rows, stall_ms)
        differences = paired_differences(
            run_times_ms(variants[first]), run_times_ms(variants[second])
        )
        summary = run_differences_summary(differences)
        lines.append(f"  {_difference_line(first, second, summary, len(differences))}")
    times = {
        variant: [run_times_ms(files[path][variant]) for path in csvs]
        for variant in (first, second)
    }
    lines += _average_lines(first, second, times)
    for variant in (first, second):
        lines += _between_files_lines(list(labels.values()), variant, times[variant])
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.5: numbers across CSVs of one load.")
    parser.add_argument("csvs", type=Path, nargs="+")
    parser.add_argument("--stall-seconds", type=float, required=True)
    args = parser.parse_args()
    for line in report_lines(args.csvs, round(args.stall_seconds * 1000)):
        print(line)


if __name__ == "__main__":
    main()
