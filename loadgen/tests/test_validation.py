"""P2.9: a simulated variant against a real one, metric by metric, and the table that records it."""

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from load_generator.results import CallRow, summarise
from load_generator.validation import (
    METRICS,
    Pair,
    TableRow,
    Validation,
    ValidationError,
    load_validation,
    metrics,
    relative_error,
    report_lines,
    table,
    table_path,
    write_table,
)


def row(variant: str, run: str, step: int, **changes: Any) -> CallRow:
    values: dict[str, Any] = {
        "variant": variant,
        "run": run,
        "task_type": "csv",
        "step": step,
        "pod": "pod-a",
        "ttft_ms": 100,
        "duration_ms": 1000,
        "gap_before_ms": 0,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_tokens": 50,
        "moved": None if step == 1 else False,
        "tokens_on_pod": 110,
        "over_kv_limit": False,
    }
    values.update(changes)
    return CallRow(**values)


def one_variant(rows: Sequence[CallRow]) -> dict[str, float | None]:
    return metrics(rows, summarise(rows)[rows[0].variant])


def test_the_metrics_are_the_cards_seven() -> None:
    assert METRICS == (
        "ttft_p50_ms",
        "ttft_p95_ms",
        "duration_p50_ms",
        "duration_p95_ms",
        "run_completion_p50_ms",
        "run_completion_p95_ms",
        "hit_rate",
    )


def test_percentiles_are_nearest_rank_over_answered_calls() -> None:
    """20 calls with TTFTs 10..200 ms: p50 is the 10th (100), p95 the 19th (190). The failed call
    has no times, so it moves neither."""
    rows = [
        row("real", f"r{index}", 1, ttft_ms=10 * index, duration_ms=1000 + 10 * index)
        for index in range(1, 21)
    ]
    rows.append(row("real", "failed", 1, ttft_ms=None, duration_ms=None, cached_tokens=None))
    found = one_variant(rows)
    assert found["ttft_p50_ms"] == 100
    assert found["ttft_p95_ms"] == 190
    assert found["duration_p50_ms"] == 1100
    assert found["duration_p95_ms"] == 1190


def test_run_completion_is_over_finished_runs_from_their_steps() -> None:
    """Run a: 1000 + (5 + 1000) ms. Run b: one call of 3000 ms. Run c failed and is left out."""
    rows = [
        row("real", "a", 1),
        row("real", "a", 2, gap_before_ms=5),
        row("real", "b", 1, duration_ms=3000),
        row("real", "c", 1, ttft_ms=None, duration_ms=None),
    ]
    found = one_variant(rows)
    assert found["run_completion_p50_ms"] == 2005
    assert found["run_completion_p95_ms"] == 3000


def test_the_hit_rate_is_cached_over_prompt_tokens() -> None:
    rows = [row("real", "a", 1, cached_tokens=0), row("real", "a", 2, cached_tokens=80)]
    assert one_variant(rows)["hit_rate"] == pytest.approx(0.4)


def test_a_variant_with_no_answered_call_has_no_values() -> None:
    rows = [row("sim", "a", 1, ttft_ms=None, duration_ms=None, cached_tokens=None)]
    assert one_variant(rows) == dict.fromkeys(METRICS)


def test_the_error_is_relative_to_real_either_way() -> None:
    assert relative_error(real=200, sim=230) == pytest.approx(0.15)
    assert relative_error(real=200, sim=170) == pytest.approx(0.15)


def test_a_real_value_of_zero_has_no_relative_error() -> None:
    with pytest.raises(ValidationError, match="0"):
        relative_error(real=0, sim=5)


def validation(tolerance: float = 0.15) -> Validation:
    return Validation(tolerance=tolerance, pairs=[Pair(real="real-1pod", sim="sim-1pod")])


def test_the_table_has_a_row_per_pair_and_metric_in_the_cards_order() -> None:
    rows = [row("real-1pod", "a", 1), row("sim-1pod", "a", 1)]
    found = table(rows, validation())
    assert [(entry.real_variant, entry.sim_variant, entry.metric) for entry in found] == [
        ("real-1pod", "sim-1pod", metric) for metric in METRICS
    ]
    assert all(entry.relative_error == 0 and entry.within for entry in found)


def test_an_error_at_the_tolerance_is_within_and_above_it_is_not() -> None:
    """TTFT 100 against 115 is 0.15; duration 1000 against 1151 is 0.151. 115 and 1151 are
    whole milliseconds, as the CSV holds them."""
    rows = [
        row("real-1pod", "a", 1),
        row("sim-1pod", "a", 1, ttft_ms=115, duration_ms=1151),
    ]
    found = {entry.metric: entry for entry in table(rows, validation())}
    assert found["ttft_p50_ms"].relative_error == pytest.approx(0.15)
    assert found["ttft_p50_ms"].within
    assert found["duration_p50_ms"].relative_error == pytest.approx(0.151)
    assert not found["duration_p50_ms"].within


def test_a_value_the_simulator_did_not_give_is_a_miss_with_no_error() -> None:
    rows = [
        row("real-1pod", "a", 1),
        row("sim-1pod", "a", 1, ttft_ms=None, duration_ms=None, cached_tokens=None),
    ]
    found = {entry.metric: entry for entry in table(rows, validation())}
    assert found["ttft_p50_ms"].real == 100
    assert found["ttft_p50_ms"].sim is None
    assert found["ttft_p50_ms"].relative_error is None
    assert not found["ttft_p50_ms"].within


def test_a_pair_whose_variant_has_no_rows_is_refused() -> None:
    with pytest.raises(ValidationError, match="sim-1pod"):
        table([row("real-1pod", "a", 1)], validation())


def test_the_section_gives_the_tolerance_and_pairs() -> None:
    section = {"tolerance": 0.15, "pairs": [{"real": "real-1pod", "sim": "sim-1pod"}]}
    assert load_validation(section, ["real-1pod", "sim-1pod"]) == validation()


@pytest.mark.parametrize("tolerance", [0, -0.1])
def test_a_tolerance_of_zero_or_less_is_refused(tolerance: float) -> None:
    section = {"tolerance": tolerance, "pairs": [{"real": "real-1pod", "sim": "sim-1pod"}]}
    with pytest.raises(ValidationError, match="tolerance"):
        load_validation(section, ["real-1pod", "sim-1pod"])


def test_a_pair_naming_a_variant_the_file_lacks_is_refused() -> None:
    section = {"tolerance": 0.15, "pairs": [{"real": "real-1pod", "sim": "sim-2pods"}]}
    with pytest.raises(ValidationError, match="sim-2pods"):
        load_validation(section, ["real-1pod", "sim-1pod"])


def test_a_variant_paired_with_itself_is_refused() -> None:
    section = {"tolerance": 0.15, "pairs": [{"real": "real-1pod", "sim": "real-1pod"}]}
    with pytest.raises(ValidationError, match="itself"):
        load_validation(section, ["real-1pod"])


def test_no_pairs_is_refused() -> None:
    with pytest.raises(ValidationError, match="pairs"):
        load_validation({"tolerance": 0.15, "pairs": []}, ["real-1pod"])


def test_the_table_is_in_sim_named_by_the_experiments_hash(tmp_path: Path) -> None:
    assert table_path(tmp_path, "abc123") == tmp_path / "sim" / "validation-abc123.csv"


def entries() -> list[TableRow]:
    return [
        TableRow("real-1pod", "sim-1pod", "ttft_p50_ms", 100, 115, 0.15, 0.15, True),
        TableRow("real-1pod", "sim-1pod", "hit_rate", 0.5, None, None, 0.15, False),
    ]


def test_the_table_file_has_a_line_per_row_and_blanks_for_none(tmp_path: Path) -> None:
    path = tmp_path / "sim" / "validation-abc.csv"
    write_table(path, entries())
    with path.open() as file:
        lines = list(csv.DictReader(file))
    assert lines[0] == {
        "real_variant": "real-1pod",
        "sim_variant": "sim-1pod",
        "metric": "ttft_p50_ms",
        "real": "100",
        "sim": "115",
        "relative_error": "0.15",
        "tolerance": "0.15",
        "within": "True",
    }
    assert lines[1]["sim"] == ""
    assert lines[1]["relative_error"] == ""


def test_a_table_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "validation-abc.csv"
    path.write_text("kept")
    with pytest.raises(FileExistsError):
        write_table(path, entries())
    assert path.read_text() == "kept"


def test_the_report_counts_what_is_within_per_pair_and_names_the_misses() -> None:
    lines = report_lines(entries())
    assert lines[0] == "validation real-1pod vs sim-1pod: 1 of 2 metrics within 0.15"
    assert "hit_rate" in lines[2]
    assert "miss" in lines[2]
