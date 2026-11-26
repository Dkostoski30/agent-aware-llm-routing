"""make autoscale-square (P5.2, card 76): the autoscalers' square, a seed per file.

P5.2's squares run autoscaled variants in the orders of a Williams square, each file another seed
(decided 2026-11-25), so a variant is held to the reference only within a file, as in P5.1's
squares. A series is one of these when its file has an autoscale section. make autoscale-compare
(P4.5) pairs files of one seed and so cannot read these; it stays for the results it made.

First square-compare's own report on the files, with its refusals: total run time judged by card
73's rule. Then each variant's autoscale numbers per file from its .meta.json: when remaining work,
the queue and the context passed their targets, when spec replicas rose, when the pods asked for
were Ready, time to capacity, the first scale-down, finished, failed and interrupted kept runs,
run completion p50, makespan, runs per minute, idle pod-minutes, the KEDA operator's restarts.

Then, for each variant against the reference, card 73's rule on two more numbers (fixed before the
runs: the user, 2026-11-25): the time spec replicas rose (earlier when the gap is over 0 in every
file and its mean exceeds its max-min over the files, later mirrored), and idle pod-minutes (fewer,
more, the same way). A file where a side has no rise, or where the reference idled 0, has no gap,
and the line says so instead of a verdict. A series may not judge idle pod-minutes: on real 1<->2
neither autoscaler can ask for fewer than 2 pods (P4.5), so only their means are printed. Failed
and interrupted kept runs are summed per variant, reported, not judged. A CSV of the per-file rows.

Usage: python -m load_generator.autoscale_square SERIES
"""

import argparse
import csv
import dataclasses
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from load_generator import autoscale_compare, square_compare
from load_generator.square_compare import (
    FASTER,
    SLOWER,
    Series,
    SquareFile,
    check_files,
    load_file,
    load_series,
    series_results,
    verdict,
)

# card 73's verdict read for a time and for a cost: square_compare's "faster" is the gap over 0.
EARLIER = "earlier"
LATER = "later"
FEWER = "fewer"
MORE = "more"
RISE_WORDS = {FASTER: EARLIER, SLOWER: LATER}
IDLE_WORDS = {FASTER: FEWER, SLOWER: MORE}


@dataclass(frozen=True)
class AutoscaleSeries:
    series: Series
    # False on a ceiling no variant can ask under (real 1<->2): idle time is printed, not judged.
    idle_judged: bool


def load_autoscale_series(path: Path) -> AutoscaleSeries | None:
    """None for a series without an autoscale section, which square-compare alone reads."""
    section = yaml.safe_load(path.read_text()).get("autoscale")
    if section is None:
        return None
    return AutoscaleSeries(load_series(path), idle_judged=section["idle_judged"])


@dataclass(frozen=True)
class AutoscaleRow:
    """One CSV row: an autoscaled variant in one file, over the kept runs."""

    experiment: str
    seed: int
    # 1 for the variant that ran first in its file.
    position: int
    variant: str
    autoscaler: str
    # Seconds since the replay's start; None if it never happened.
    remaining_seconds: float | None
    queue_seconds: float | None
    context_seconds: float | None
    scale_up_seconds: float | None
    scaled_before_replay: bool
    ready_seconds: float | None
    capacity_seconds: float | None
    scale_down_seconds: float | None
    runs: int
    finished_runs: int
    failed_runs: int
    interrupted_runs: int
    # Over the finished runs; None with none.
    completion_p50_ms: int | None
    makespan_ms: int | None
    runs_per_minute: float
    idle_pod_minutes: float
    keda_restarts: int | None
    # Against the reference in its file (relative_gap); None for the reference and without a gap.
    rise_gap: float | None
    idle_gap: float | None


CSV_FIELDS = tuple(field.name for field in dataclasses.fields(AutoscaleRow))


def relative_gap(reference: float | None, variant: float | None) -> float | None:
    """The share of the reference's number the variant saves, as square_compare.gap: over 0 is an
    earlier rise or fewer idle pod-minutes. None without both, or against a reference of 0."""
    if reference is None or variant is None or reference == 0:
        return None
    return (reference - variant) / reference


def _rows(file: SquareFile, reference: str) -> list[AutoscaleRow]:
    """A missing key raises KeyError; a variant without an autoscaler is autoscale_compare's
    CompareError."""
    found = autoscale_compare.variant_results(file.meta)
    by_variant = {result.variant: result for result in found}
    rows = []
    for result in found:
        extra = file.meta["variants"][result.variant]
        summary = extra["autoscale_summary"]
        other = by_variant[reference]
        own = result.variant == reference
        rows.append(
            AutoscaleRow(
                experiment=result.experiment,
                seed=file.meta["seed"],
                position=result.position,
                variant=result.variant,
                autoscaler=result.autoscaler,
                remaining_seconds=result.remaining_seconds,
                queue_seconds=result.queue_seconds,
                context_seconds=result.context_seconds,
                scale_up_seconds=result.scale_up_seconds,
                scaled_before_replay=result.scaled_before_replay,
                ready_seconds=result.ready_seconds,
                capacity_seconds=summary["time_to_capacity_seconds"],
                scale_down_seconds=extra["autoscale"]["scale_up_windows"]["scale_down_seconds"],
                runs=result.runs,
                finished_runs=result.finished_runs,
                failed_runs=result.failed_runs,
                interrupted_runs=summary["interrupted_runs"],
                completion_p50_ms=result.completion_p50_ms,
                makespan_ms=result.makespan_ms,
                runs_per_minute=summary["runs_per_minute"],
                idle_pod_minutes=result.idle_pod_minutes,
                keda_restarts=result.keda_restarts,
                rise_gap=None
                if own
                else relative_gap(other.scale_up_seconds, result.scale_up_seconds),
                idle_gap=None
                if own
                else relative_gap(other.idle_pod_minutes, result.idle_pod_minutes),
            )
        )
    return rows


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} s"


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value / 1000:.1f} s"


def _percent(share: float) -> str:
    return f"{100 * share:+.1f}%"


def _row_line(row: AutoscaleRow) -> str:
    rose = "before the replay" if row.scaled_before_replay else _seconds(row.scale_up_seconds)
    restarts = "-" if row.keda_restarts is None else str(row.keda_restarts)
    return (
        f"  {row.position} {row.variant}: remaining work passed {_seconds(row.remaining_seconds)}, "
        f"queue {_seconds(row.queue_seconds)}, context {_seconds(row.context_seconds)}; spec rose "
        f"{rose}, Ready {_seconds(row.ready_seconds)}, capacity in "
        f"{_seconds(row.capacity_seconds)}, first scale-down {_seconds(row.scale_down_seconds)}; "
        f"runs {row.finished_runs}/{row.runs} finished, {row.failed_runs} failed, "
        f"{row.interrupted_runs} interrupted; p50 {_ms(row.completion_p50_ms)}, makespan "
        f"{_ms(row.makespan_ms)}, {row.runs_per_minute:.2f} runs/min; idle "
        f"{row.idle_pod_minutes:.2f} pod-min; KEDA restarts {restarts}"
    )


def _missing_rise(row: AutoscaleRow) -> str | None:
    if row.scaled_before_replay:
        return f"{row.variant} was scaled before its replay"
    if row.scale_up_seconds is None:
        return f"{row.variant} never rose"
    return None


def _judged_line(
    title: str,
    pairs: Sequence[tuple[AutoscaleRow, AutoscaleRow, float | None]],
    show: Callable[[AutoscaleRow], str],
    missing: Callable[[AutoscaleRow, AutoscaleRow], str],
    words: Mapping[str, str],
) -> str:
    """pairs: per file the variant's row, the reference's and the gap between them."""
    for row, other, value in pairs:
        if value is None:
            return f"{title}: no verdict, {missing(row, other)} in {_file(row)}"
    gaps = [value for _, _, value in pairs if value is not None]
    each = ", ".join(
        f"{show(row)} against {show(other)} in {_file(row)} ({_percent(value)})"
        for row, other, value in pairs
        if value is not None
    )
    spread = 100 * (max(gaps) - min(gaps))
    found = verdict(gaps)
    return (
        f"{title}: {each}; mean {_percent(statistics.mean(gaps))}, spread {spread:.1f}% -> "
        f"{words.get(found, found)}"
    )


def _file(row: AutoscaleRow) -> str:
    """The file's name without the hash make experiment adds to its results."""
    return row.experiment.rsplit("-", 1)[0]


def _rise_missing(row: AutoscaleRow, other: AutoscaleRow) -> str:
    why = _missing_rise(other) or _missing_rise(row)
    return why or f"{other.variant} rose at 0 s"


def _idle_missing(row: AutoscaleRow, other: AutoscaleRow) -> str:
    return f"{other.variant} idled 0"


def _pod_minutes(row: AutoscaleRow) -> str:
    return f"{row.idle_pod_minutes:.2f}"


def _spread_text(values: Sequence[float]) -> str:
    return f"{statistics.mean(values):.2f} ({min(values):.2f}-{max(values):.2f})"


def verdict_lines(rows: Sequence[AutoscaleRow], reference: str, idle_judged: bool) -> list[str]:
    lines = []
    files = list(dict.fromkeys(row.experiment for row in rows))
    references = {row.experiment: row for row in rows if row.variant == reference}
    for variant in dict.fromkeys(row.variant for row in rows):
        if variant == reference:
            continue
        own = {row.experiment: row for row in rows if row.variant == variant}
        title = f"{variant} against {reference}"
        rises = [(own[name], references[name], own[name].rise_gap) for name in files]
        lines.append(
            _judged_line(
                f"{title}, spec rose",
                rises,
                lambda row: _seconds(row.scale_up_seconds),
                _rise_missing,
                RISE_WORDS,
            )
        )
        if idle_judged:
            idles = [(own[name], references[name], own[name].idle_gap) for name in files]
            lines.append(
                _judged_line(
                    f"{title}, idle pod-minutes", idles, _pod_minutes, _idle_missing, IDLE_WORDS
                )
            )
        else:
            lines.append(
                f"{title}, idle pod-minutes: not judged at this ceiling; {variant} "
                f"{_spread_text([row.idle_pod_minutes for row in own.values()])}, {reference} "
                f"{_spread_text([row.idle_pod_minutes for row in references.values()])}"
            )
    for variant in dict.fromkeys(row.variant for row in rows):
        own_rows = [row for row in rows if row.variant == variant]
        lines.append(
            f"{variant}: failed kept runs {sum(row.failed_runs for row in own_rows)} of "
            f"{sum(row.runs for row in own_rows)}, interrupted "
            f"{sum(row.interrupted_runs for row in own_rows)}"
        )
    return lines


def write_autoscale_csv(path: Path, rows: Iterable[AutoscaleRow]) -> None:
    """Written over: it is made from the files alone, the same each time."""
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(dataclasses.asdict(row) for row in rows)


def compare(
    csvs: Sequence[Path], reference: str, idle_judged: bool, out: Path, square_out: Path
) -> list[str]:
    """The report's lines; the per-file rows go to out, square-compare's to square_out."""
    lines = square_compare.compare(csvs, reference, square_out)
    files = [load_file(path) for path in csvs]
    check_files(files, reference)
    rows = []
    for file in files:
        own = _rows(file, reference)
        lines.append(f"{file.label} (seed {file.meta['seed']}), autoscaling:")
        lines += [_row_line(row) for row in own]
        rows += own
    lines += verdict_lines(rows, reference, idle_judged)
    write_autoscale_csv(out, rows)
    return lines


def print_comparison(autoscaled: AutoscaleSeries) -> None:
    """The series' autoscale CSV beside square-compare's."""
    csvs, square_out = series_results(autoscaled.series)
    out = square_out.with_name(f"{autoscaled.series.name}-autoscale.csv")
    series = autoscaled.series
    for line in compare(csvs, series.reference, autoscaled.idle_judged, out, square_out):
        print(line)
    print(f"autoscale-square: wrote {out} and {square_out}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="P5.2: an autoscalers' square across its files.")
    parser.add_argument("series", type=Path)
    path = parser.parse_args(argv).series
    autoscaled = load_autoscale_series(path)
    if autoscaled is None:
        raise SystemExit(f"{path} has no autoscale section: make square-compare reads it")
    print_comparison(autoscaled)


if __name__ == "__main__":
    main()
