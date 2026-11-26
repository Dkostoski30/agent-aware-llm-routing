"""make autoscale-square (P5.2, card 76): the autoscalers' square, a seed per file, held to queue.

The files are built here, a CSV and a .meta.json each, so the numbers are known: a run is one call
whose duration is its run time, and each variant's autoscale numbers are given whole.
"""

import csv
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from load_generator import autoscale_square
from load_generator.autoscale_square import (
    EARLIER,
    FEWER,
    LATER,
    MORE,
    AutoscaleSeries,
    compare,
    load_autoscale_series,
    relative_gap,
)
from load_generator.results import CallRow, write_csv
from load_generator.square_compare import NO_DIFFERENCE, SquareError, load_series

REFERENCE = "queue"
SOURCE = {"commit": "abc", "uncommitted": [], "system_sha256": "same"}


def call(variant: str, run: str, duration: int) -> CallRow:
    return CallRow(
        variant=variant,
        run=run,
        task_type="hotpotqa",
        step=1,
        pod="pod-a",
        ttft_ms=100,
        duration_ms=duration,
        gap_before_ms=0,
        prompt_tokens=100,
        completion_tokens=10,
        cached_tokens=40,
        moved=None,
        tokens_on_pod=110,
        over_kv_limit=False,
    )


def scaled(
    started: float,
    rise: float | None,
    idle: float,
    failed: int = 0,
    interrupted: int = 0,
    before: bool = False,
    autoscaler: str | None = None,
) -> dict[str, Any]:
    """One autoscaled variant's .meta.json entry, as make experiment writes it."""
    return {
        "started": started,
        "late_starts": {"tolerance_ms": 50, "calls": 0, "total_ms": 0, "max_ms": None},
        "waiting": None,
        "picker_log": None,
        "autoscale": {
            "autoscaler": autoscaler,
            "crossings": {
                "remaining_seconds": 60.0,
                "queue_seconds": 90.0,
                "scale_up_seconds": rise,
                "scaled_before_replay": before,
            },
            "scale_up_windows": {
                "context_seconds": 70.0,
                "ready_seconds": None if rise is None else rise + 150.0,
                "scale_down_seconds": 900.0,
                "restarts": 0,
            },
        },
        "autoscale_summary": {
            "runs": 2,
            "finished_runs": 2 - failed,
            "failed_runs": failed,
            "interrupted_runs": interrupted,
            "completion_p50_ms": 20000,
            "makespan_ms": 600000,
            "runs_per_minute": 4.5,
            "time_to_capacity_seconds": None if rise is None else 150.0,
            "idle_pod_minutes": idle,
        },
    }


def square_file(
    directory: Path,
    name: str,
    seed: int,
    variants: Mapping[str, tuple[Sequence[int], Mapping[str, Any]]],
) -> Path:
    """variants: each variant's run times and meta entry, in the order they ran; runs r1, r2, ..."""
    csv_path = directory / f"{name}.csv"
    rows = [
        call(variant, f"r{index}", duration)
        for variant, (times, _) in variants.items()
        for index, duration in enumerate(times, start=1)
    ]
    write_csv(csv_path, rows)
    metas = {}
    for variant, (_, meta) in variants.items():
        meta["autoscale"]["autoscaler"] = meta["autoscale"]["autoscaler"] or variant
        metas[variant] = meta
    meta = {
        "experiment": name,
        "hash": f"{name}hash",
        "trace": "trace.jsonl",
        "pattern": "burst-fan-out-heavy",
        "profile_sha256": "profile",
        "seed": seed,
        "source": SOURCE,
        "variants": metas,
    }
    csv_path.with_suffix(".meta.json").write_text(json.dumps(meta))
    return csv_path


def two_files(directory: Path, **b_changes: Any) -> list[Path]:
    """remaining against queue in opposite orders: remaining 10% and 20% faster, its rise 10% and
    20% earlier, its idle time 50% and 40% lower."""
    b_remaining = {"rise": 64.0, "idle": 0.3, **b_changes}
    return [
        square_file(
            directory,
            "a",
            6,
            {
                "remaining": ([900, 900], scaled(1.0, rise=90.0, idle=0.5)),
                "queue": ([1000, 1000], scaled(2.0, rise=100.0, idle=1.0)),
            },
        ),
        square_file(
            directory,
            "b",
            9,
            {
                "queue": ([500, 500], scaled(1.0, rise=80.0, idle=0.5)),
                "remaining": ([400, 400], scaled(2.0, **b_remaining)),
            },
        ),
    ]


def report(directory: Path, paths: list[Path], idle_judged: bool = True) -> str:
    out, square_out = directory / "autoscale.csv", directory / "square.csv"
    return "\n".join(compare(paths, REFERENCE, idle_judged, out, square_out))


def test_a_relative_gap_is_the_share_of_the_references_number_the_variant_saves() -> None:
    assert relative_gap(100.0, 90.0) == pytest.approx(0.1)
    assert relative_gap(100.0, 110.0) == pytest.approx(-0.1)


@pytest.mark.parametrize(
    ("reference", "variant"),
    [(None, 90.0), (100.0, None), (0.0, 0.0)],
    ids=["no reference", "no variant", "reference 0"],
)
def test_a_relative_gap_without_both_numbers_or_of_a_zero_reference_is_none(
    reference: float | None, variant: float | None
) -> None:
    assert relative_gap(reference, variant) is None


def test_the_report_holds_the_run_times_square_verdict_first(tmp_path: Path) -> None:
    """square-compare's own report, so total run time is judged by card 73's rule as in P5.1."""
    text = report(tmp_path, two_files(tmp_path))
    assert "remaining against queue: +10.0% in a, +20.0% in b; mean +15.0%" in text
    assert text.index("-> faster") < text.index("spec rose")


def test_an_earlier_rise_in_every_file_by_more_than_its_spread_is_earlier(tmp_path: Path) -> None:
    text = report(tmp_path, two_files(tmp_path))
    assert (
        "remaining against queue, spec rose: 90.0 s against 100.0 s in a (+10.0%), 64.0 s against "
        f"80.0 s in b (+20.0%); mean +15.0%, spread 10.0% -> {EARLIER}"
    ) in text


def test_a_later_rise_in_every_file_is_later(tmp_path: Path) -> None:
    paths = [
        square_file(
            tmp_path,
            "a",
            6,
            {
                "remaining": ([900], scaled(1.0, rise=110.0, idle=0.5)),
                "queue": ([1000], scaled(2.0, rise=100.0, idle=1.0)),
            },
        ),
        square_file(
            tmp_path,
            "b",
            9,
            {
                "queue": ([500], scaled(1.0, rise=80.0, idle=0.5)),
                "remaining": ([400], scaled(2.0, rise=96.0, idle=0.3)),
            },
        ),
    ]
    assert f"spread 10.0% -> {LATER}" in report(tmp_path, paths)


def test_a_rise_that_changes_sign_between_files_shows_no_difference(tmp_path: Path) -> None:
    text = report(tmp_path, two_files(tmp_path, rise=96.0))
    assert f"(-20.0%); mean -5.0%, spread 30.0% -> {NO_DIFFERENCE}" in text


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"rise": None}, "remaining never rose in b"),
        ({"rise": None, "before": True}, "remaining was scaled before its replay in b"),
    ],
    ids=["never rose", "scaled before"],
)
def test_a_file_without_a_rise_on_a_side_gives_no_rise_verdict_and_says_why(
    tmp_path: Path, changes: Mapping[str, Any], why: str
) -> None:
    text = report(tmp_path, two_files(tmp_path, **changes))
    assert f"remaining against queue, spec rose: no verdict, {why}" in text


def test_fewer_idle_pod_minutes_in_every_file_by_more_than_their_spread_is_fewer(
    tmp_path: Path,
) -> None:
    text = report(tmp_path, two_files(tmp_path))
    assert (
        "remaining against queue, idle pod-minutes: 0.50 against 1.00 in a (+50.0%), 0.30 against "
        f"0.50 in b (+40.0%); mean +45.0%, spread 10.0% -> {FEWER}"
    ) in text


def test_more_idle_pod_minutes_in_every_file_is_more(tmp_path: Path) -> None:
    paths = [
        square_file(
            tmp_path,
            "a",
            6,
            {
                "remaining": ([900], scaled(1.0, rise=90.0, idle=1.2)),
                "queue": ([1000], scaled(2.0, rise=100.0, idle=1.0)),
            },
        ),
        square_file(
            tmp_path,
            "b",
            9,
            {
                "queue": ([500], scaled(1.0, rise=80.0, idle=0.5)),
                "remaining": ([400], scaled(2.0, rise=64.0, idle=0.65)),
            },
        ),
    ]
    assert f"spread 10.0% -> {MORE}" in report(tmp_path, paths)


def test_a_reference_that_idled_no_pod_minute_gives_no_idle_verdict(tmp_path: Path) -> None:
    paths = two_files(tmp_path)
    meta_path = paths[1].with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["variants"]["queue"]["autoscale_summary"]["idle_pod_minutes"] = 0.0
    meta_path.write_text(json.dumps(meta))
    text = report(tmp_path, paths)
    assert "remaining against queue, idle pod-minutes: no verdict, queue idled 0 in b" in text


def test_idle_pod_minutes_not_judged_are_reported_as_means_only(tmp_path: Path) -> None:
    """On real 1<->2 neither side can ask for fewer than 2 pods (P4.5, decided 2026-11-25)."""
    text = report(tmp_path, two_files(tmp_path), idle_judged=False)
    assert (
        "remaining against queue, idle pod-minutes: not judged at this ceiling; remaining 0.40 "
        "(0.30-0.50), queue 0.75 (0.50-1.00)"
    ) in text
    assert f"-> {FEWER}" not in text


def test_failed_and_interrupted_kept_runs_are_summed_per_variant(tmp_path: Path) -> None:
    text = report(tmp_path, two_files(tmp_path, failed=2, interrupted=1))
    assert "remaining: failed kept runs 2 of 4, interrupted 1" in text
    assert "queue: failed kept runs 0 of 4, interrupted 0" in text


def test_each_variants_autoscale_numbers_are_reported_in_its_file(tmp_path: Path) -> None:
    text = report(tmp_path, two_files(tmp_path))
    assert (
        "  1 remaining: remaining work passed 60.0 s, queue 90.0 s, context 70.0 s; spec rose "
        "90.0 s, Ready 240.0 s, capacity in 150.0 s, first scale-down 900.0 s; runs 2/2 finished, "
        "0 failed, 0 interrupted; p50 20.0 s, makespan 600.0 s, 4.50 runs/min; idle 0.50 pod-min; "
        "KEDA restarts 0"
    ) in text


def test_the_csv_holds_each_variant_per_file_with_its_gaps_and_is_the_same_twice(
    tmp_path: Path,
) -> None:
    paths = two_files(tmp_path)
    report(tmp_path, paths)
    first = (tmp_path / "autoscale.csv").read_bytes()
    report(tmp_path, paths)
    assert (tmp_path / "autoscale.csv").read_bytes() == first
    rows = list(csv.DictReader((tmp_path / "autoscale.csv").open()))
    assert [(row["experiment"], row["seed"], row["position"], row["variant"]) for row in rows] == [
        ("a-ahash", "6", "1", "remaining"),
        ("a-ahash", "6", "2", "queue"),
        ("b-bhash", "9", "1", "queue"),
        ("b-bhash", "9", "2", "remaining"),
    ]
    assert float(rows[0]["rise_gap"]) == pytest.approx(0.1)
    assert float(rows[3]["idle_gap"]) == pytest.approx(0.4)
    assert rows[1]["rise_gap"] == rows[1]["idle_gap"] == ""


def test_the_square_refusals_hold(tmp_path: Path) -> None:
    """square-compare's checks, so two files of one seed are refused here too."""
    paths = two_files(tmp_path)
    meta_path = paths[1].with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["seed"] = 6
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(SquareError, match="both ran seed 6"):
        report(tmp_path, paths)


def series_file(directory: Path, text: str) -> Path:
    (directory / "series").mkdir()
    for name in ("x-a", "x-b"):
        (directory / f"{name}.yaml").write_text("profile: real\n")
    path = directory / "series" / "x.yaml"
    path.write_text(text)
    return path


def test_a_series_with_an_autoscale_section_is_an_autoscale_series(tmp_path: Path) -> None:
    path = series_file(
        tmp_path, "reference: queue\nfiles: [x-a, x-b]\nautoscale:\n  idle_judged: false\n"
    )
    assert load_autoscale_series(path) == AutoscaleSeries(load_series(path), idle_judged=False)


def test_a_series_without_an_autoscale_section_is_not_one(tmp_path: Path) -> None:
    path = series_file(tmp_path, "reference: default\nfiles: [x-a, x-b]\n")
    assert load_autoscale_series(path) is None


def test_python_m_runs_main_and_refuses_a_series_without_an_autoscale_section(
    tmp_path: Path,
) -> None:
    """python -m, as the Makefile runs it: the module itself calls main (P4.5's compare once did
    not). The refusal shows main ran without results to read."""
    path = series_file(tmp_path, "reference: default\nfiles: [x-a, x-b]\n")
    # loadgen/ and agent/, which pytest puts on its own path but a child process does not inherit.
    loadgen = Path(autoscale_square.__file__).resolve().parents[1]
    paths = os.pathsep.join([str(loadgen), str(loadgen.parent / "agent")])
    finished = subprocess.run(
        [sys.executable, "-m", "load_generator.autoscale_square", str(path)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": paths},
    )
    assert finished.returncode == 1
    assert "has no autoscale section: make square-compare reads it" in finished.stderr
