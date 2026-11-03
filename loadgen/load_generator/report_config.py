"""config/report.yaml (P2.5): what the reports show. The modules using it read it on import."""

from dataclasses import dataclass
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
    graph: GraphLayout


def _is_count(value: Any, least: int) -> bool:
    return isinstance(value, int) and value >= least


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
    return ReportConfig(fewest, tuple(left_out), GraphLayout(**spec["graph"]))
