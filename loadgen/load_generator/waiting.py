"""The calls waiting on each vLLM pod after each burst (P5.0, card 73).

Card 72 judged whether a load overloaded the pods by vllm:num_requests_waiting in Prometheus within
a window after each burst, from a dump saved by hand outside the repo. make experiment now asks the
same Prometheus as its stall check for each replay, keeps the series beside the results, and puts
each burst's most waiting per pod into the .meta.json, so a square's comparison can say for each
file whether its load overloaded the pods. Printed, not judged.
"""

import csv
import dataclasses
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Per pod, the calls waiting at each sample.
WAITING_QUERY = "sum by (pod) (vllm:num_requests_waiting)"


@dataclass(frozen=True)
class WaitingRow:
    # Since the replay's start.
    seconds: float
    pod: str
    waiting: float


CSV_FIELDS = tuple(field.name for field in dataclasses.fields(WaitingRow))


def rows_from(result: Sequence[Mapping[str, Any]], started: float) -> list[WaitingRow]:
    """WAITING_QUERY's range answer, in time order. Not a number is no reading."""
    rows = [
        WaitingRow(float(at) - started, series["metric"]["pod"], float(value))
        for series in result
        for at, value in series["values"]
        if not math.isnan(float(value))
    ]
    return sorted(rows, key=lambda row: (row.seconds, row.pod))


def write_waiting_csv(path: Path, rows: Iterable[WaitingRow]) -> None:
    """Mode x: a result is never written over."""
    with path.open("x", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(dataclasses.asdict(row) for row in rows)


def burst_peaks(
    rows: Sequence[WaitingRow], bursts: Sequence[float], window: int
) -> list[dict[str, float]]:
    """For each burst, the most calls waiting on each pod from the burst to window seconds after it,
    both ends included. A pod with no reading in the window is left out."""
    peaks = []
    for burst in bursts:
        most: dict[str, float] = {}
        for row in rows:
            if burst <= row.seconds <= burst + window:
                most[row.pod] = max(most.get(row.pod, row.waiting), row.waiting)
        peaks.append(dict(sorted(most.items())))
    return peaks
