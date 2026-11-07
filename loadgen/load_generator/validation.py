"""P2.9: an experiment's simulated variants against its real ones, metric by metric.

Each pair is the same replay on real vLLM and on simulated pods. The relative error is
|sim - real| / real, and a metric is within the tolerance when that is at most the tolerance. A
value one side lacks (no answered call, no finished run, no cached-token count) is a miss with no
error: there is nothing to compare.

make experiment writes the table at the end of the run. This module's main writes it again from
the experiment's CSV alone, with no cluster, once the earlier table is deleted.

Usage: python -m load_generator.validation EXPERIMENT CSV
"""

import argparse
import csv
from collections.abc import Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from load_generator.record import REPO_ROOT, experiment_hash
from load_generator.results import (
    CallRow,
    VariantSummary,
    percentile,
    read_csv,
    summarise,
    variant_rows,
)

# Card 32's metrics, in its order.
METRICS = (
    "ttft_p50_ms",
    "ttft_p95_ms",
    "duration_p50_ms",
    "duration_p95_ms",
    "run_completion_p50_ms",
    "run_completion_p95_ms",
    "hit_rate",
)


class ValidationError(Exception):
    """The comparison would not mean what it says."""


@dataclass(frozen=True)
class Pair:
    real: str
    sim: str


@dataclass(frozen=True)
class Validation:
    tolerance: float
    pairs: list[Pair]


@dataclass(frozen=True)
class TableRow:
    real_variant: str
    sim_variant: str
    metric: str
    real: float | None
    sim: float | None
    relative_error: float | None
    tolerance: float
    within: bool


def load_validation(section: Mapping[str, Any], variants: Sequence[str]) -> Validation:
    """An experiment file's validation section; a missing key raises KeyError."""
    tolerance = section["tolerance"]
    if tolerance <= 0:
        raise ValidationError(f"tolerance {tolerance}: it must be above 0")
    pairs = [Pair(real=pair["real"], sim=pair["sim"]) for pair in section["pairs"]]
    if not pairs:
        raise ValidationError("no pairs: nothing to compare")
    for pair in pairs:
        for name in (pair.real, pair.sim):
            if name not in variants:
                raise ValidationError(f"pair names {name}, the file's variants are {variants}")
        if pair.real == pair.sim:
            raise ValidationError(f"{pair.real} is paired with itself")
    return Validation(tolerance=tolerance, pairs=pairs)


def _percentiles(values: Sequence[int], prefix: str) -> dict[str, float | None]:
    return {
        f"{prefix}_p50_ms": percentile(values, 0.5) if values else None,
        f"{prefix}_p95_ms": percentile(values, 0.95) if values else None,
    }


def metrics(rows: Sequence[CallRow], summary: VariantSummary) -> dict[str, float | None]:
    """One variant's rows, warm-up runs already left out, and their summary. TTFT and duration over
    answered calls, run completion over finished runs."""
    ttft = [row.ttft_ms for row in rows if row.ttft_ms is not None]
    durations = [row.duration_ms for row in rows if row.duration_ms is not None]
    return {
        **_percentiles(ttft, "ttft"),
        **_percentiles(durations, "duration"),
        **_percentiles(summary.completions_ms, "run_completion"),
        "hit_rate": summary.hit_rate,
    }


def relative_error(real: float, sim: float) -> float:
    if real == 0:
        raise ValidationError("the real value is 0, so no error is relative to it")
    return abs(sim - real) / real


def _table_row(
    pair: Pair, metric: str, real: float | None, sim: float | None, tolerance: float
) -> TableRow:
    error = None if real is None or sim is None else relative_error(real, sim)
    within = error is not None and error <= tolerance
    return TableRow(pair.real, pair.sim, metric, real, sim, error, tolerance, within)


def table(rows: Sequence[CallRow], validation: Validation) -> list[TableRow]:
    """One row per pair and metric, in the pairs' and METRICS' order."""
    by_variant = variant_rows(rows)
    summaries = summarise(rows)
    found = []
    for pair in validation.pairs:
        for name in (pair.real, pair.sim):
            if name not in by_variant:
                raise ValidationError(f"no rows of {name}")
        real = metrics(by_variant[pair.real], summaries[pair.real])
        sim = metrics(by_variant[pair.sim], summaries[pair.sim])
        found += [
            _table_row(pair, metric, real[metric], sim[metric], validation.tolerance)
            for metric in METRICS
        ]
    return found


def table_path(root: Path, experiment_hash: str) -> Path:
    return root / "sim" / f"validation-{experiment_hash}.csv"


def write_table(path: Path, entries: Sequence[TableRow]) -> None:
    """Never over an earlier table: a rerun of the same file must not erase the first result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(field.name for field in fields(TableRow))
        for entry in entries:
            writer.writerow("" if value is None else value for value in astuple(entry))


def _value_text(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def report_lines(entries: Sequence[TableRow]) -> list[str]:
    lines = []
    pairs = list(dict.fromkeys((entry.real_variant, entry.sim_variant) for entry in entries))
    for real, sim in pairs:
        pair_entries = [e for e in entries if (e.real_variant, e.sim_variant) == (real, sim)]
        within = sum(entry.within for entry in pair_entries)
        lines.append(
            f"validation {real} vs {sim}: {within} of {len(pair_entries)} metrics within "
            f"{pair_entries[0].tolerance:g}"
        )
        for entry in pair_entries:
            error = "-" if entry.relative_error is None else f"{entry.relative_error:.3f}"
            lines.append(
                f"  {entry.metric}: real {_value_text(entry.real)}, sim {_value_text(entry.sim)}, "
                f"error {error}, {'within' if entry.within else 'miss'}"
            )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="P2.9: the validation table from an experiment's CSV."
    )
    parser.add_argument("experiment", type=Path)
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()

    spec = yaml.safe_load(args.experiment.read_text())
    validation = load_validation(
        spec["validation"], [variant["name"] for variant in spec["variants"]]
    )
    entries = table(read_csv(args.csv), validation)
    path = table_path(REPO_ROOT, experiment_hash(args.experiment))
    write_table(path, entries)
    for line in report_lines(entries):
        print(line)
    print(f"validation: wrote {path}")


if __name__ == "__main__":
    main()
