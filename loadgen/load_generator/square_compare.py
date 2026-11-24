"""make square-compare (P5.0, card 73): a square's variants across its files, a seed per file.

P5.1's and P5.2's real comparisons are Williams squares: every file runs the same variants in its
own order, and each file a different seed (Phase 5's decision 3). Runs differ between files, so a
variant is held to the reference only within a file, by total run time over the kept runs, and the
files are then read side by side. make experiment-compare pairs runs across files of one load and
so cannot read these; it stays for the results it made.

Per file, in the order its variants ran: each variant's total run time, run-completion p50 and p95,
first-token and generation time apart (PLAN.md's real-pod comparisons), hit rate, moved steps,
failed calls, the replayer's late starts, the most calls waiting per pod after each burst, and for a
plugin variant its picks by reason from its picker log. Each file's commit and uncommitted paths.
Then each variant's gap against the reference per file, the verdict, and the mean of each position
in the files' orders, to show a drift over a sitting. A CSV of the per-file rows beside it.

The verdict (fixed before the runs: the user, 2026-11-24, card 73). In each file, gap = (the
reference's total run time - the variant's) / the reference's, so files of different loads compare.
Faster when the gap is over 0 in every file and its mean over the files is more than its spread,
max - min: the files' own disagreement is the noise. Slower is the same with the signs turned.
Anything else is no difference shown. No verdict when a kept run did not finish on either side.

Refused: under 2 files, a file without the source make experiment records since P5.0, files of
another trace, pattern or profile, two files of one seed, other variants, other runs within a file,
and files run on other system code (experiment.SYSTEM_PATHS' hash): a fix between two files means
the earlier ones run again (Phase 5's decision 1).

Usage: python -m load_generator.square_compare SERIES
"""

import argparse
import csv
import dataclasses
import json
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from load_generator.experiment import file_outputs
from load_generator.record import REPO_ROOT
from load_generator.results import (
    CallRow,
    percentile,
    read_csv,
    run_times_ms,
    summarise,
    variant_rows,
    variant_totals,
)

FASTER = "faster"
SLOWER = "slower"
NO_DIFFERENCE = "no difference shown"
# Not a setting: a comparison of one file has no spread to read.
FEWEST_FILES = 2
# What makes two files one replay but for the seed.
REPLAY_KEYS = ("trace", "pattern", "profile_sha256")
# The run-completion shares reported, as make experiment's report and P3.7's verdict read them.
MEDIAN_SHARE = 0.5
P95_SHARE = 0.95
# Where make experiment writes its results, which a later file of a series finds uncommitted.
RESULT_DIRECTORIES = ("experiments/results/", "experiments/graphs/")
# thesis-run-router's log line for each call it placed, with the rule that placed it (P3.5, P3.6).
PICKED = "Run call picked"


class SquareError(Exception):
    """The files are not one square, so a comparison of them would not mean what it says."""


@dataclass(frozen=True)
class Series:
    name: str
    # The experiment files, in the order they run.
    files: list[Path]
    # The variant every other is held to.
    reference: str


def load_series(path: Path) -> Series:
    """experiments/series/<name>.yaml names experiment files of experiments/ by their stem. A
    missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    reference = spec["reference"]
    names = spec["files"]
    if len(names) < FEWEST_FILES:
        raise SquareError(f"{path}: a square needs at least {FEWEST_FILES} files, got {names}")
    for name in names:
        if names.count(name) > 1:
            raise SquareError(f"{path}: names {name} twice")
    files = [path.parent.parent / f"{name}.yaml" for name in names]
    for name, file in zip(names, files, strict=True):
        if not file.exists():
            raise SquareError(f"{path}: no experiment file {name} at {file}")
    return Series(name=path.stem, files=files, reference=reference)


@dataclass(frozen=True)
class SquareFile:
    # The CSV's stem, <name>-<hash>.
    label: str
    directory: Path
    meta: Mapping[str, Any]
    # Each variant's rows, in the order the variants ran.
    variants: dict[str, list[CallRow]]


def load_file(path: Path) -> SquareFile:
    meta_path = path.with_suffix(".meta.json")
    if not path.exists() or not meta_path.exists():
        raise SquareError(f"no results at {path}: make experiment-series runs the file first")
    meta = json.loads(meta_path.read_text())
    rows = variant_rows(read_csv(path))
    variants = {variant: rows[variant] for variant in meta["variants"]}
    return SquareFile(label=path.stem, directory=path.parent, meta=meta, variants=variants)


def check_files(files: Sequence[SquareFile], reference: str) -> None:
    if len(files) < FEWEST_FILES:
        raise SquareError(f"a square needs at least {FEWEST_FILES} files, got {len(files)}")
    first = files[0]
    if reference not in first.variants:
        raise SquareError(f"no variant {reference} in {first.label}, it has {list(first.variants)}")
    seeds: dict[int, str] = {}
    for file in files:
        if file.meta.get("source") is None:
            raise SquareError(
                f"{file.label} has no source: it ran before P5.0 (card 73), so what system code "
                "it ran on is unknown"
            )
        for key in REPLAY_KEYS:
            if file.meta[key] != first.meta[key]:
                raise SquareError(
                    f"{file.label}'s {key} is {file.meta[key]}, {first.label}'s "
                    f"{first.meta[key]}: another replay"
                )
        seed = file.meta["seed"]
        if seed in seeds:
            raise SquareError(
                f"{seeds[seed]} and {file.label} both ran seed {seed}: a square runs a seed per "
                "file"
            )
        seeds[seed] = file.label
        if sorted(file.variants) != sorted(first.variants):
            raise SquareError(
                f"{file.label} has other variants than {first.label}: {list(file.variants)}"
            )
        runs = {variant: {row.run for row in rows} for variant, rows in file.variants.items()}
        first_variant = next(iter(runs))
        for variant, these in runs.items():
            if these != runs[first_variant]:
                raise SquareError(
                    f"{file.label}: {variant} has other runs than {first_variant}, so another load"
                )
        if file.meta["source"]["system_sha256"] != first.meta["source"]["system_sha256"]:
            raise SquareError(
                f"{file.label} ran on other system code than {first.label} (commits "
                f"{file.meta['source']['commit']} and {first.meta['source']['commit']}): every "
                "file before a fix runs again"
            )


def gap(reference_ms: int, variant_ms: int) -> float:
    """The share of the reference's total run time the variant saves; under 0 when slower."""
    return (reference_ms - variant_ms) / reference_ms


def verdict(gaps: Sequence[float]) -> str:
    mean = statistics.mean(gaps)
    spread = max(gaps) - min(gaps)
    if all(value > 0 for value in gaps) and mean > spread:
        return FASTER
    if all(value < 0 for value in gaps) and -mean > spread:
        return SLOWER
    return NO_DIFFERENCE


def picks_by_reason(log: str, runs: set[str]) -> dict[str, int]:
    """The picker's placements of the given runs' calls, by the rule that placed each. Runs left
    out are set-routing's probe and those the warm-up dropped."""
    reasons: Counter[str] = Counter()
    for text in log.splitlines():
        try:
            line = json.loads(text)
        except json.JSONDecodeError:
            # The picker's own start-up lines are not JSON, and place no call.
            continue
        if line.get("body") == PICKED and line.get("run-id") in runs:
            reasons[line["reason"]] += 1
    return dict(sorted(reasons.items()))


@dataclass(frozen=True)
class VariantResult:
    """One CSV row: a variant in one file, over the kept runs."""

    experiment: str
    seed: int
    # 1 for the variant that ran first in its file.
    position: int
    variant: str
    runs: int
    finished_runs: int
    failed_calls: int
    # Over the finished runs, as the p50 and p95; None with none finished.
    total_run_ms: int
    run_p50_ms: int | None
    run_p95_ms: int | None
    first_token_ms: int
    generation_ms: int
    hit_rate: float | None
    moved: int
    later_steps: int
    late_starts: int
    late_ms: int
    # The fewest, over the bursts, of each burst's most waiting on a pod; None without bursts or
    # with a burst that has no reading.
    weakest_burst_waiting: float | None
    # Against the reference in its file; None for the reference and when a side has a run that did
    # not finish.
    gap: float | None


CSV_FIELDS = tuple(field.name for field in dataclasses.fields(VariantResult))


def _unfinished(rows: Iterable[CallRow]) -> int:
    return sum(time is None for time in run_times_ms(rows).values())


def _weakest(waiting: Mapping[str, Any] | None) -> float | None:
    if waiting is None:
        return None
    most: list[float | None] = [
        max(peak["most_waiting"].values(), default=None) for peak in waiting["peaks"]
    ]
    read = [value for value in most if value is not None]
    if not read or len(read) < len(most):
        return None
    return min(read)


def _result(file: SquareFile, position: int, variant: str, reference: str) -> VariantResult:
    rows = file.variants[variant]
    summary = summarise(rows)[variant]
    totals = variant_totals(rows)
    times = summary.completions_ms
    extra = file.meta["variants"][variant]
    reference_rows = file.variants[reference]
    gap_value = None
    if variant != reference and not _unfinished(rows) and not _unfinished(reference_rows):
        gap_value = gap(variant_totals(reference_rows).run_ms, totals.run_ms)
    return VariantResult(
        experiment=file.label,
        seed=file.meta["seed"],
        position=position,
        variant=variant,
        runs=summary.runs,
        finished_runs=totals.finished_runs,
        failed_calls=summary.failed_calls,
        total_run_ms=totals.run_ms,
        run_p50_ms=percentile(times, MEDIAN_SHARE) if times else None,
        run_p95_ms=percentile(times, P95_SHARE) if times else None,
        first_token_ms=totals.ttft_ms,
        generation_ms=totals.generation_ms,
        hit_rate=summary.hit_rate,
        moved=summary.moved,
        later_steps=summary.later_steps,
        late_starts=extra["late_starts"]["calls"],
        late_ms=extra["late_starts"]["total_ms"],
        weakest_burst_waiting=_weakest(extra["waiting"]),
        gap=gap_value,
    )


def _ordinal(position: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(position, "th")
    return f"{position}{suffix}"


def _seconds(milliseconds: float) -> str:
    return f"{milliseconds / 1000:.1f} s"


def _two_places(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else f"{milliseconds / 1000:.2f} s"


def _percent(share: float) -> str:
    return f"{100 * share:+.1f}%"


def _variant_lines(file: SquareFile, result: VariantResult) -> list[str]:
    extra = file.meta["variants"][result.variant]
    hit_rate = "-" if result.hit_rate is None else f"{result.hit_rate:.3f}"
    late = extra["late_starts"]
    largest = "-" if late["max_ms"] is None else late["max_ms"]
    lines = [
        f"  {result.variant}, {_ordinal(result.position)}: total run time "
        f"{_seconds(result.total_run_ms)} over {result.finished_runs} of {result.runs} runs, p50 "
        f"{_two_places(result.run_p50_ms)}, p95 {_two_places(result.run_p95_ms)}; first tokens "
        f"{_seconds(result.first_token_ms)}, generation {_seconds(result.generation_ms)}; hit rate "
        f"{hit_rate}, moved {result.moved} of {result.later_steps} later steps; "
        f"{result.failed_calls} failed calls",
        f"    late starts: {late['calls']} calls, {_seconds(late['total_ms'])} in all, at most "
        f"{largest} ms",
    ]
    waiting = extra["waiting"]
    if waiting is not None:
        peaks = "; ".join(
            f"{peak['burst_seconds']:g} s "
            + (", ".join(f"{most:g}" for most in peak["most_waiting"].values()) or "no reading")
            for peak in waiting["peaks"]
        )
        lines.append(
            f"    most waiting per pod within {waiting['window_seconds']} s after each burst: "
            f"{peaks}"
        )
    if extra["picker_log"] is not None:
        log = (file.directory / extra["picker_log"]).read_text()
        runs = {row.run for row in file.variants[result.variant]}
        picks = ", ".join(
            f"{reason} {count}" for reason, count in picks_by_reason(log, runs).items()
        )
        lines.append(f"    picks by reason: {picks or 'none'}")
    return lines


def _source_line(file: SquareFile) -> str:
    """The results make experiment writes are counted, not named: in a series each file finds
    those of the files before it uncommitted, and they change no replay. The .meta.json keeps
    every path."""
    source = file.meta["source"]
    line = f"{file.label}: commit {source['commit']}"
    results = [path for path in source["uncommitted"] if path.startswith(RESULT_DIRECTORIES)]
    named = [path for path in source["uncommitted"] if path not in results]
    parts = []
    if named:
        parts.append(", ".join(named))
    if results:
        parts.append(f"{len(results)} files of results")
    if parts:
        line += f", uncommitted {' and '.join(parts)}"
    return line


def _verdict_lines(
    files: Sequence[SquareFile], results: Sequence[VariantResult], reference: str
) -> list[str]:
    lines = []
    for variant in files[0].variants:
        if variant == reference:
            continue
        unfinished = [
            (side, file.label, _unfinished(file.variants[side]))
            for file in files
            for side in (variant, reference)
            if _unfinished(file.variants[side])
        ]
        if unfinished:
            side, label, count = unfinished[0]
            lines.append(
                f"{variant} against {reference}: no verdict, {side} did not finish {count} of its "
                f"runs in {label}"
            )
            continue
        gaps = {
            result.experiment: result.gap
            for result in results
            if result.variant == variant and result.gap is not None
        }
        values = list(gaps.values())
        each = ", ".join(f"{_percent(value)} in {label}" for label, value in gaps.items())
        lines.append(
            f"{variant} against {reference}: {each}; mean {_percent(statistics.mean(values))}, "
            f"spread {100 * (max(values) - min(values)):.1f}% -> {verdict(values)}"
        )
    return lines


def _position_line(files: Sequence[SquareFile], results: Sequence[VariantResult]) -> str:
    """Each total over its file's mean of all its variants' totals, averaged per position: in a
    square each position holds every file once, so a load's size cancels and a drift shows."""
    if any(_unfinished(rows) for file in files for rows in file.variants.values()):
        return "by position: no means, a run did not finish"
    shares: dict[int, list[float]] = {}
    for file in files:
        own = [result for result in results if result.experiment == file.label]
        mean = statistics.mean(result.total_run_ms for result in own)
        for result in own:
            shares.setdefault(result.position, []).append(result.total_run_ms / mean)
    each = ", ".join(
        f"{_ordinal(position)} {statistics.mean(values):.3f}"
        for position, values in sorted(shares.items())
    )
    return f"by position, total over its file's mean: {each}"


def write_square_csv(path: Path, results: Iterable[VariantResult]) -> None:
    """Written over: it is made from the files alone, the same each time."""
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(dataclasses.asdict(result) for result in results)


def compare(csvs: Sequence[Path], reference: str, out: Path) -> list[str]:
    """The report's lines; the per-file rows go to out."""
    files = [load_file(path) for path in csvs]
    check_files(files, reference)
    results = [
        _result(file, position, variant, reference)
        for file in files
        for position, variant in enumerate(file.variants, start=1)
    ]
    lines = []
    for file in files:
        lines.append(
            f"{file.label} (seed {file.meta['seed']}), in order {', '.join(file.variants)}"
        )
        for result in results:
            if result.experiment == file.label:
                lines += _variant_lines(file, result)
    lines += [_source_line(file) for file in files]
    lines += _verdict_lines(files, results, reference)
    lines.append(_position_line(files, results))
    write_square_csv(out, results)
    return lines


def series_results(series: Series) -> tuple[list[Path], Path]:
    """The CSV each file of the series writes, and the square's own CSV beside them."""
    csvs = [file_outputs(path, REPO_ROOT).csv for path in series.files]
    return csvs, REPO_ROOT / "experiments" / "results" / f"{series.name}-square.csv"


def print_comparison(series: Series) -> None:
    csvs, out = series_results(series)
    for line in compare(csvs, series.reference, out):
        print(line)
    print(f"square-compare: wrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="P5.0: a square's variants across its files.")
    parser.add_argument("series", type=Path)
    print_comparison(load_series(parser.parse_args().series))


if __name__ == "__main__":
    main()
