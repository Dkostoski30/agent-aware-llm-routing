"""make experiment-series (P5.0, card 73): a series' experiment files one after another, then their
square's comparison (make square-compare).

A series is experiments/series/<name>.yaml (square_compare.load_series): the files of one square in
the order they run, and the variant the others are held to. Each file runs as make experiment runs
it, so each variant still starts from restarted pods. A file whose CSV, .meta.json and graph are all
written finished before and is skipped, so the same command picks up a night that stopped. Any
other file runs, and make experiment's own check refuses the leftovers of one that stopped halfway.
The first failure stops the series: a file after it would run on whatever the failure left.

Usage: python -m load_generator.series SERIES
"""

import argparse
from collections.abc import Callable
from pathlib import Path

from load_generator.experiment import Outputs, file_outputs, run_file
from load_generator.record import REPO_ROOT
from load_generator.square_compare import Series, load_series, print_comparison


def finished(out: Outputs) -> bool:
    """make experiment writes the graph after the CSV and the .meta.json (write_results)."""
    return out.csv.exists() and out.meta.exists() and out.graph.exists()


def run_series(
    series: Series,
    outputs_of: Callable[[Path], Outputs],
    run: Callable[[Path], None],
    say: Callable[[str], None],
) -> None:
    for number, path in enumerate(series.files, start=1):
        if finished(outputs_of(path)):
            say(f"series: {path.name} finished before, skipped")
            continue
        say(f"series: running {path.name}, {number} of {len(series.files)}")
        run(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="P5.0: a series' experiment files, then its square."
    )
    parser.add_argument("series", type=Path)
    series = load_series(parser.parse_args().series)
    run_series(series, lambda path: file_outputs(path, REPO_ROOT), run_file, print)
    print_comparison(series)


if __name__ == "__main__":
    main()
