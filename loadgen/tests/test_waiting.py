"""The calls waiting on each pod after each burst (P5.0, card 73): Prometheus's answer to rows, the
rows to a CSV, and the most waiting per pod within a window after each burst."""

import csv
from pathlib import Path

from load_generator.waiting import WaitingRow, burst_peaks, rows_from, write_waiting_csv


def series(pod: str, values: dict[float, str]) -> dict[str, object]:
    return {"metric": {"pod": pod}, "values": [[at, value] for at, value in values.items()]}


def test_the_answer_becomes_rows_in_seconds_since_the_replays_start() -> None:
    answer = [
        series("pod-b", {1000.0: "2", 1005.0: "3"}),
        series("pod-a", {1000.0: "0"}),
    ]
    assert rows_from(answer, started=990.0) == [
        WaitingRow(10.0, "pod-a", 0.0),
        WaitingRow(10.0, "pod-b", 2.0),
        WaitingRow(15.0, "pod-b", 3.0),
    ]


def test_not_a_number_is_no_reading() -> None:
    assert rows_from([series("pod-a", {1000.0: "NaN"})], started=990.0) == []


def test_the_rows_are_written_as_a_csv(tmp_path: Path) -> None:
    path = tmp_path / "waiting.csv"
    write_waiting_csv(path, [WaitingRow(10.0, "pod-a", 2.0)])
    with path.open() as file:
        assert list(csv.DictReader(file)) == [{"seconds": "10.0", "pod": "pod-a", "waiting": "2.0"}]


def test_each_burst_gets_the_most_waiting_per_pod_within_the_window_after_it() -> None:
    rows = [
        WaitingRow(115.0, "pod-a", 9.0),
        WaitingRow(120.0, "pod-a", 1.0),
        WaitingRow(150.0, "pod-a", 6.0),
        WaitingRow(150.0, "pod-b", 7.0),
        WaitingRow(180.0, "pod-b", 8.0),
        WaitingRow(185.0, "pod-b", 12.0),
        WaitingRow(250.0, "pod-a", 2.0),
    ]
    # 115 s is before the first burst and 185 s past its 60 s; both window edges count.
    assert burst_peaks(rows, [120.0, 240.0], window=60) == [
        {"pod-a": 6.0, "pod-b": 8.0},
        {"pod-a": 2.0},
    ]


def test_a_burst_with_no_reading_after_it_has_no_pods() -> None:
    assert burst_peaks([WaitingRow(10.0, "pod-a", 3.0)], [120.0], window=60) == [{}]
