"""config/report.yaml (P2.5): what the reports show. The modules using it read it on import."""

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from load_generator.record import REPO_ROOT

REPORT_FILE = REPO_ROOT / "config" / "report.yaml"
# The speed divides by the tokens after the first.
FEWEST_POSSIBLE_TOKENS = 2


class ReportConfigError(Exception):
    """A setting that would make a report wrong rather than merely different."""


@dataclass(frozen=True)
class GraphLayout:
    bar_height: float
    value_gap: float
    hit_rate_axis_end: float
    variant_spacing: float
    band_margin: float


@dataclass(frozen=True)
class ReportConfig:
    fewest_tokens_for_speed: int
    runs_left_out: tuple[int, ...]
    pressure_bucket_edges: tuple[float, ...]
    burst_waiting_window_seconds: int
    late_start_ms: int
    graph: GraphLayout


def _is_count(value: Any, least: int) -> bool:
    return isinstance(value, int) and value >= least


def _rising_over_zero(values: Any) -> bool:
    if not isinstance(values, list) or not values:
        return False
    if not all(isinstance(value, int | float) and value > 0 for value in values):
        return False
    return all(low < high for low, high in pairwise(values))


def load_report_config(path: Path = REPORT_FILE) -> ReportConfig:
    """A missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    fewest = spec["fewest_tokens_for_speed"]
    if not _is_count(fewest, FEWEST_POSSIBLE_TOKENS):
        raise ReportConfigError(
            f"fewest_tokens_for_speed is {fewest}, it must be a whole number, at least "
            f"{FEWEST_POSSIBLE_TOKENS}"
        )
    left_out = spec["runs_left_out"]
    if not isinstance(left_out, list) or not all(_is_count(count, 1) for count in left_out):
        raise ReportConfigError(f"runs_left_out is {left_out}, it must list whole numbers over 0")
    edges = spec["pressure_bucket_edges"]
    if not _rising_over_zero(edges):
        raise ReportConfigError(
            f"pressure_bucket_edges is {edges}, it must list numbers over 0, each above the one "
            "before"
        )
    window = spec["burst_waiting_window_seconds"]
    if not _is_count(window, 1):
        raise ReportConfigError(
            f"burst_waiting_window_seconds is {window}, it must be a whole number, at least 1"
        )
    late = spec["late_start_ms"]
    if not _is_count(late, 0):
        raise ReportConfigError(f"late_start_ms is {late}, it must be a whole number, at least 0")
    return ReportConfig(
        fewest,
        tuple(left_out),
        tuple(float(edge) for edge in edges),
        window,
        late,
        GraphLayout(**spec["graph"]),
    )
