"""make experiment-graph (P2.5): an experiment's graph, from its CSV alone.

Three panels, one per question: how long runs took, how much of the prompts the cache served, and
how long a first token took with a pod under and over its KV limit (P2.4). Each variant keeps its
colour and line style in every panel, so it reads in greyscale too.

Usage: python -m load_generator.graph CSV [--out PNG]
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

from matplotlib.axes import Axes
from matplotlib.figure import Figure

from load_generator.record import REPO_ROOT
from load_generator.report_config import load_report_config
from load_generator.results import TtftStats, read_csv, summarise, ttft_stats, variant_rows

# Four hues that stay apart on this surface; LINE_STYLES separates them in greyscale. A variant's
# colour comes from its place in name order, so it does not change between a baseline and its
# reversed run.
COLOURS = ("#2a78d6", "#eb6834", "#1baf7a", "#6250d6")
LINE_STYLES = ("-", "--", "-.", ":")
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e4e3df"
# Thesis pages are about 16 cm wide, and this keeps the text readable at that width.
FIGURE_INCHES = (13.0, 4.2)
DPI = 200
# A first token at 0 ms cannot sit on a log axis, so the axis starts at one millisecond.
SMALLEST_SECONDS = 0.001
LAYOUT = load_report_config().graph


@dataclass(frozen=True)
class VariantFigure:
    name: str
    finished_runs: int
    total_runs: int
    # Ascending, with ecdf the share of finished runs at or below each.
    completions_seconds: list[float]
    ecdf: list[float]
    hit_rate: float | None
    under: TtftStats
    over: TtftStats


def figure_data(csv: Path) -> list[VariantFigure]:
    rows = read_csv(csv)
    summaries = summarise(rows)
    figures = []
    for name, calls in sorted(variant_rows(rows).items()):
        summary = summaries[name]
        finished = len(summary.completions_ms)
        figures.append(
            VariantFigure(
                name=name,
                finished_runs=finished,
                total_runs=summary.runs,
                completions_seconds=[time / 1000 for time in summary.completions_ms],
                ecdf=[(rank + 1) / finished for rank in range(finished)],
                hit_rate=summary.hit_rate,
                under=ttft_stats(calls, over_limit=False),
                over=ttft_stats(calls, over_limit=True),
            )
        )
    return figures


def _style(axes: Axes) -> None:
    axes.set_facecolor(SURFACE)
    axes.spines[["top", "right"]].set_visible(False)
    axes.spines[["left", "bottom"]].set_color(MUTED)
    axes.tick_params(colors=MUTED, labelsize=9)
    axes.grid(True, color=GRID, linewidth=0.8)
    axes.set_axisbelow(True)


def _completion_panel(axes: Axes, data: list[VariantFigure]) -> None:
    for index, variant in enumerate(data):
        axes.step(
            variant.completions_seconds,
            variant.ecdf,
            where="post",
            color=COLOURS[index],
            linestyle=LINE_STYLES[index],
            linewidth=1.8,
            label=f"{variant.name}: {variant.finished_runs} of {variant.total_runs} runs finished",
        )
    axes.set_xscale("log")
    axes.set_ylim(0, 1.02)
    axes.set_xlabel("Run completion time (s)", color=MUTED, fontsize=10)
    axes.set_ylabel("Share of finished runs", color=MUTED, fontsize=10)
    axes.set_title("Run completion time", color=TEXT, fontsize=11, loc="left")


def _hit_rate_panel(axes: Axes, data: list[VariantFigure]) -> None:
    positions = list(range(len(data)))
    rates = [variant.hit_rate or 0.0 for variant in data]
    axes.barh(
        positions,
        rates,
        height=LAYOUT.bar_height,
        color=[COLOURS[index] for index in positions],
    )
    for position, variant, rate in zip(positions, data, rates, strict=True):
        text = "no counts" if variant.hit_rate is None else f"{rate:.3f}"
        axes.text(rate + LAYOUT.value_gap, position, text, va="center", color=TEXT, fontsize=10)
    axes.set_yticks(positions, [variant.name for variant in data], color=TEXT)
    axes.invert_yaxis()
    axes.set_xlim(0, LAYOUT.hit_rate_axis_end)
    axes.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    axes.grid(False, axis="y")
    axes.set_xlabel("Cached share of prompt tokens", color=MUTED, fontsize=10)
    axes.set_title("Prefix-cache hit rate", color=TEXT, fontsize=11, loc="left")


def _seconds(milliseconds: int) -> float:
    return max(milliseconds / 1000, SMALLEST_SECONDS)


def _ttft_panel(axes: Axes, data: list[VariantFigure]) -> None:
    """The median as a dot, a line on to the 95th percentile, and the number of calls beside it:
    the calls over the limit may be few, and P2.4 warns against reading much into few."""
    count = len(data)
    for index, variant in enumerate(data):
        # Variants side by side within each band, around its centre.
        offset = (index - (count - 1) / 2) * LAYOUT.variant_spacing
        for band, stats in enumerate((variant.under, variant.over)):
            if stats.p50_ms is None or stats.p95_ms is None:
                continue
            middle, far = _seconds(stats.p50_ms), _seconds(stats.p95_ms)
            axes.plot(
                [middle, far],
                [band + offset, band + offset],
                color=COLOURS[index],
                linestyle=LINE_STYLES[index],
                linewidth=1.8,
            )
            axes.plot(middle, band + offset, "o", color=COLOURS[index], markersize=6)
            axes.annotate(
                f"n={stats.calls}",
                (far, band + offset),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                color=MUTED,
                fontsize=8,
            )
    axes.set_xscale("log")
    axes.set_yticks([0, 1], ["Under KV limit", "Over KV limit"], color=TEXT)
    axes.set_ylim(1 + LAYOUT.band_margin, -LAYOUT.band_margin)
    axes.margins(x=0.2)
    axes.grid(False, axis="y")
    axes.set_xlabel("Time to first token (s): median to p95", color=MUTED, fontsize=10)
    axes.set_title("First token and the pod's capacity", color=TEXT, fontsize=11, loc="left")


def render(csv: Path, out: Path) -> None:
    data = figure_data(csv)
    if not data:
        raise ValueError(f"{csv} has no rows")
    if len(data) > len(COLOURS):
        raise ValueError(f"{csv} has {len(data)} variants, the graph has {len(COLOURS)} colours")
    figure = Figure(figsize=FIGURE_INCHES, layout="constrained", facecolor=SURFACE)
    completion, hit_rate, ttft = figure.subplots(1, 3)
    for axes in (completion, hit_rate, ttft):
        _style(axes)
    _completion_panel(completion, data)
    _hit_rate_panel(hit_rate, data)
    _ttft_panel(ttft, data)
    figure.legend(
        loc="outside upper center", ncols=len(data), frameon=False, labelcolor=TEXT, fontsize=10
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, dpi=DPI)


def graph_path(csv: Path) -> Path:
    return REPO_ROOT / "experiments" / "graphs" / f"{csv.stem}.png"


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.5: an experiment's graph from its CSV.")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--out", type=Path, help="default: experiments/graphs/<csv name>.png")
    args = parser.parse_args()
    out = args.out or graph_path(args.csv)
    render(args.csv, out)
    print(f"graph: wrote {out}")


if __name__ == "__main__":
    main()
