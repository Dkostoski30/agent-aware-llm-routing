"""make square-compare (P5.0, card 73): a square's variants across its files, a seed per file.

The files are built here, a CSV and a .meta.json each, so the numbers are known: a run is one call
whose duration is its run time, and its gap is 0.
"""

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from load_generator.results import CallRow, write_csv
from load_generator.square_compare import (
    FASTER,
    NO_DIFFERENCE,
    SLOWER,
    SquareError,
    compare,
    gap,
    load_series,
    picks_by_reason,
    verdict,
)

REFERENCE = "default"
SOURCE = {"commit": "abc", "uncommitted": [], "system_sha256": "same"}


def call(variant: str, run: str, duration: int | None, ttft: int = 100) -> CallRow:
    return CallRow(
        variant=variant,
        run=run,
        task_type="hotpotqa",
        step=1,
        pod="pod-a",
        ttft_ms=None if duration is None else ttft,
        duration_ms=duration,
        gap_before_ms=0,
        prompt_tokens=100,
        completion_tokens=10,
        cached_tokens=None if duration is None else 40,
        moved=None,
        tokens_on_pod=None if duration is None else 110,
        over_kv_limit=None if duration is None else False,
    )


def variant_meta(**changes: Any) -> dict[str, Any]:
    return {
        "late_starts": {"tolerance_ms": 50, "calls": 2, "total_ms": 300, "max_ms": 200},
        "waiting": {
            "csv": "w.csv",
            "window_seconds": 60,
            "peaks": [
                {"burst_seconds": 120.0, "most_waiting": {"pod-a": 6.0, "pod-b": 7.0}},
                {"burst_seconds": 240.0, "most_waiting": {"pod-a": 5.0, "pod-b": 2.0}},
            ],
        },
        "picker_log": None,
        **changes,
    }


def square_file(
    directory: Path,
    name: str,
    seed: int,
    durations: Mapping[str, Sequence[int | None]],
    variants: Mapping[str, Mapping[str, Any]] | None = None,
    **changes: Any,
) -> Path:
    """durations are each variant's runs, in the order the variants ran; the runs are r1, r2, ..."""
    csv_path = directory / f"{name}.csv"
    rows = [
        call(variant, f"r{index}", duration)
        for variant, times in durations.items()
        for index, duration in enumerate(times, start=1)
    ]
    write_csv(csv_path, rows)
    meta = {
        "experiment": name,
        "trace": "trace.jsonl",
        "pattern": "burst-fan-out-heavy",
        "profile_sha256": "profile",
        "seed": seed,
        "source": SOURCE,
        "variants": {
            variant: (variants or {}).get(variant, variant_meta()) for variant in durations
        },
        **changes,
    }
    csv_path.with_suffix(".meta.json").write_text(json.dumps(meta))
    return csv_path


def two_files(directory: Path) -> list[Path]:
    """A 2x2 square: default and plugin in opposite orders, plugin faster by 10% and 20%."""
    return [
        square_file(directory, "a", 6, {"default": [1000, 1000], "plugin": [900, 900]}),
        square_file(directory, "b", 9, {"plugin": [400, 400], "default": [500, 500]}),
    ]


def test_a_gap_is_the_share_of_the_references_total_the_variant_saves() -> None:
    assert gap(reference_ms=2000, variant_ms=1800) == pytest.approx(0.1)
    assert gap(reference_ms=2000, variant_ms=2200) == pytest.approx(-0.1)


@pytest.mark.parametrize(
    ("gaps", "outcome"),
    [
        ([0.10, 0.20], FASTER),
        ([0.10, 0.12, 0.08, 0.11], FASTER),
        ([-0.10, -0.12, -0.08, -0.11], SLOWER),
        # Lower in every file, but the files disagree by more than the mean.
        ([0.02, 0.30, 0.05, 0.03], NO_DIFFERENCE),
        # Lower in 3 of 4.
        ([0.10, 0.12, -0.01, 0.11], NO_DIFFERENCE),
        # The mean equal to the spread is not over it; both are 0.5 exactly in binary.
        ([0.25, 0.75], NO_DIFFERENCE),
        ([0.0, 0.0], NO_DIFFERENCE),
    ],
)
def test_the_verdict_needs_every_file_and_a_mean_gap_over_the_gaps_spread(
    gaps: list[float], outcome: str
) -> None:
    assert verdict(gaps) == outcome


def test_the_report_gives_each_files_gap_and_the_verdict(tmp_path: Path) -> None:
    report = "\n".join(compare(two_files(tmp_path), REFERENCE, tmp_path / "square.csv"))
    assert (
        "plugin against default: +10.0% in a, +20.0% in b; mean +15.0%, spread 10.0% -> faster"
    ) in report


def test_each_variants_numbers_are_reported_in_its_file_in_the_order_it_ran(tmp_path: Path) -> None:
    lines = compare(two_files(tmp_path), REFERENCE, tmp_path / "square.csv")
    report = "\n".join(lines)
    assert "a (seed 6), in order default, plugin" in report
    assert "b (seed 9), in order plugin, default" in report
    assert (
        "  plugin, 2nd: total run time 1.8 s over 2 of 2 runs, p50 0.90 s, p95 0.90 s; first "
        "tokens 0.2 s, generation 1.6 s; hit rate 0.400, moved 0 of 0 later steps; 0 failed calls"
    ) in report
    assert "    late starts: 2 calls, 0.3 s in all, at most 200 ms" in report
    assert "    most waiting per pod within 60 s after each burst: 120 s 6, 7; 240 s 5, 2" in report


def test_the_position_means_show_a_drift_over_a_sitting(tmp_path: Path) -> None:
    """Each total over its file's mean of the variants' totals, averaged over the files per
    position: a has 2000 and 1800 (mean 1900), b 800 and 1000 (mean 900)."""
    report = "\n".join(compare(two_files(tmp_path), REFERENCE, tmp_path / "square.csv"))
    first = (2000 / 1900 + 800 / 900) / 2
    second = (1800 / 1900 + 1000 / 900) / 2
    assert f"by position, total over its file's mean: 1st {first:.3f}, 2nd {second:.3f}" in report


def test_a_run_that_did_not_finish_gives_no_verdict(tmp_path: Path) -> None:
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000, 1000], "plugin": [900, None]}),
        square_file(tmp_path, "b", 9, {"plugin": [400, 400], "default": [500, 500]}),
    ]
    report = "\n".join(compare(files, REFERENCE, tmp_path / "square.csv"))
    assert "plugin against default: no verdict, plugin did not finish 1 of its runs in a" in report


def test_the_csv_has_a_row_per_variant_per_file_and_is_the_same_twice(tmp_path: Path) -> None:
    out = tmp_path / "square.csv"
    files = two_files(tmp_path)
    compare(files, REFERENCE, out)
    first = out.read_text()
    compare(files, REFERENCE, out)
    assert out.read_text() == first
    with out.open() as file:
        rows = list(csv.DictReader(file))
    assert [(row["experiment"], row["position"], row["variant"]) for row in rows] == [
        ("a", "1", "default"),
        ("a", "2", "plugin"),
        ("b", "1", "plugin"),
        ("b", "2", "default"),
    ]
    plugin = rows[1]
    assert plugin["total_run_ms"] == "1800"
    assert float(plugin["gap"]) == pytest.approx(0.1)
    assert rows[0]["gap"] == ""
    assert plugin["late_starts"] == "2"
    # The fewest, over the bursts, of each burst's most waiting on a pod: 7 then 5.
    assert plugin["weakest_burst_waiting"] == "5.0"


def test_the_picks_are_counted_by_reason_over_the_files_runs_only() -> None:
    lines = [
        {"body": "Run call picked", "run-id": "r1", "reason": "first"},
        {"body": "Run call picked", "run-id": "r1", "reason": "pinned"},
        {"body": "Run call picked", "run-id": "r2", "reason": "spill"},
        # set-routing's probe, and a run the warm-up dropped.
        {"body": "Run call picked", "run-id": "set-routing-probe", "reason": "first"},
        {"body": "Run call picked", "run-id": "r0", "reason": "first"},
        {"body": "Run ended", "run-id": "r1"},
    ]
    log = "".join(json.dumps(line) + "\n" for line in lines) + "not json, a startup line\n"
    assert picks_by_reason(log, {"r1", "r2"}) == {"first": 1, "pinned": 1, "spill": 1}


def test_a_plugin_variants_picks_are_reported(tmp_path: Path) -> None:
    log = tmp_path / "a-plugin-picker.log"
    log.write_text(
        json.dumps({"body": "Run call picked", "run-id": "r1", "reason": "first"}) + "\n"
    )
    files = [
        square_file(
            tmp_path,
            "a",
            6,
            {"default": [1000, 1000], "plugin": [900, 900]},
            variants={"plugin": variant_meta(picker_log=log.name)},
        ),
        square_file(tmp_path, "b", 9, {"plugin": [400, 400], "default": [500, 500]}),
    ]
    report = "\n".join(compare(files, REFERENCE, tmp_path / "square.csv"))
    assert "    picks by reason: first 1" in report


def refusal(directory: Path, files: list[Path], match: str, reference: str = REFERENCE) -> None:
    with pytest.raises(SquareError, match=match):
        compare(files, reference, directory / "square.csv")


def test_one_file_is_no_square(tmp_path: Path) -> None:
    refusal(tmp_path, two_files(tmp_path)[:1], "at least 2 files")


def test_a_file_run_before_p5_0_is_refused(tmp_path: Path) -> None:
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000], "plugin": [900]}, source=None),
        square_file(tmp_path, "b", 9, {"plugin": [400], "default": [500]}),
    ]
    refusal(tmp_path, files, "no source")


@pytest.mark.parametrize("key", ["trace", "pattern", "profile_sha256"])
def test_files_of_another_replay_are_refused(tmp_path: Path, key: str) -> None:
    other: dict[str, Any] = {key: "other"}
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000], "plugin": [900]}),
        square_file(tmp_path, "b", 9, {"plugin": [400], "default": [500]}, **other),
    ]
    refusal(tmp_path, files, key)


def test_two_files_of_one_seed_are_refused(tmp_path: Path) -> None:
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000], "plugin": [900]}),
        square_file(tmp_path, "b", 6, {"plugin": [400], "default": [500]}),
    ]
    refusal(tmp_path, files, "seed 6")


def test_files_of_other_variants_are_refused(tmp_path: Path) -> None:
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000], "plugin": [900]}),
        square_file(tmp_path, "b", 9, {"round-robin": [400], "default": [500]}),
    ]
    refusal(tmp_path, files, "other variants")


def test_a_reference_the_files_lack_is_refused(tmp_path: Path) -> None:
    refusal(tmp_path, two_files(tmp_path), "no variant session-affinity", "session-affinity")


def test_variants_of_other_runs_in_one_file_are_refused(tmp_path: Path) -> None:
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000, 1000], "plugin": [900]}),
        square_file(tmp_path, "b", 9, {"plugin": [400], "default": [500]}),
    ]
    refusal(tmp_path, files, "other runs")


def test_files_run_on_other_system_code_are_refused(tmp_path: Path) -> None:
    """A fix between two files means the earlier ones run again (Phase 5's decision 1)."""
    other = {**SOURCE, "commit": "def", "system_sha256": "changed"}
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000], "plugin": [900]}),
        square_file(tmp_path, "b", 9, {"plugin": [400], "default": [500]}, source=other),
    ]
    refusal(tmp_path, files, "system code")


def test_files_of_other_commits_on_the_same_system_code_are_compared(tmp_path: Path) -> None:
    """A commit of results or docs between two files changes no replay."""
    other = {**SOURCE, "commit": "def", "uncommitted": ["experiments/b.yaml"]}
    files = [
        square_file(tmp_path, "a", 6, {"default": [1000], "plugin": [900]}),
        square_file(tmp_path, "b", 9, {"plugin": [400], "default": [500]}, source=other),
    ]
    report = "\n".join(compare(files, REFERENCE, tmp_path / "square.csv"))
    assert "a: commit abc" in report
    assert "b: commit def, uncommitted experiments/b.yaml" in report


def series_file(directory: Path, text: str) -> Path:
    (directory / "series").mkdir(exist_ok=True)
    for name in ("one", "two"):
        (directory / f"{name}.yaml").write_text(f"{name}\n")
    path = directory / "series" / "s.yaml"
    path.write_text(text)
    return path


def test_a_series_names_its_files_beside_it_and_the_reference(tmp_path: Path) -> None:
    series = load_series(series_file(tmp_path, "reference: default\nfiles: [one, two]\n"))
    assert series.name == "s"
    assert series.files == [tmp_path / "one.yaml", tmp_path / "two.yaml"]
    assert series.reference == "default"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("reference: default\nfiles: [one, three]\n", "no experiment file three"),
        ("reference: default\nfiles: []\n", "at least 2 files"),
        ("reference: default\nfiles: [one, one]\n", "one twice"),
        ("files: [one, two]\n", "reference"),
    ],
)
def test_a_series_that_could_not_be_a_square_is_refused(
    tmp_path: Path, text: str, match: str
) -> None:
    with pytest.raises((SquareError, KeyError), match=match):
        load_series(series_file(tmp_path, text))
