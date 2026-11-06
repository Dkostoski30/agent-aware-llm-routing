"""config/sim.yaml's simulator values from the calibration table (P2.8b, card 31): the TTFT line,
the per-token time, the load factor, the start-up time and the units the profile writes them in.
Known inputs, then the committed table against the committed profile."""

from pathlib import Path

import pytest
import yaml
from calibrator.config import CalibrationError
from calibrator.mapping import (
    inter_token_ms,
    load_factor,
    prefill_line,
    read_rows,
    simulator,
    startup_s,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
# The files config/sim.yaml's comments name: the adapter on one pod alone (decided 2026-11-06).
TABLE = REPO_ROOT / "sim" / "calibration-5553d79b34d7-lora-1pod.csv"
COLD_STARTS = REPO_ROOT / "sim" / "cold-start-5553d79b34d7-lora.csv"


def _cell(
    concurrency: int, prompt: int, cached: int, ttft: float, per_token: float
) -> dict[str, str]:
    """A table row with the columns the mapping reads."""
    return {
        "concurrency": str(concurrency),
        "prompt_tokens": str(prompt),
        "cached_tokens_p50": str(cached),
        "ttft_ms_p50": str(ttft),
        "per_token_ms_p50": str(per_token),
    }


def _start(case: str, ready_s: float, first_ttft_ms: int) -> dict[str, str]:
    """A cold-start row with the columns the mapping reads."""
    return {"case": case, "created_to_ready_s": str(ready_s), "first_ttft_ms": str(first_ttft_ms)}


# On the line 100 ms + 0.2 ms per uncached token at concurrency 1; the concurrency-2 row is far
# off it.
CELLS = [
    _cell(1, 256, 0, 151.2, 60.0),
    _cell(1, 1024, 1008, 103.2, 61.0),
    _cell(1, 2048, 0, 509.6, 62.0),
    _cell(2, 2048, 0, 900.0, 63.0),
]
STARTS = [
    _start("alone", 120.0, 10_000),
    _start("alone", 126.0, 14_000),
    _start("alone", 130.0, 20_000),
    _start("both", 200.0, 30_000),
]


def test_the_ttft_line_is_fit_to_uncached_tokens_at_concurrency_one() -> None:
    overhead, per_token = prefill_line(CELLS)
    assert overhead == pytest.approx(100.0)
    assert per_token == pytest.approx(0.2)


def test_a_line_needs_two_uncached_lengths() -> None:
    with pytest.raises(CalibrationError, match="two"):
        prefill_line([_cell(1, 256, 0, 150.0, 60.0), _cell(1, 256, 0, 160.0, 60.0)])


def test_the_per_token_time_is_the_median_at_one_concurrency() -> None:
    assert inter_token_ms(CELLS, 1) == 61.0
    assert inter_token_ms(CELLS, 2) == 63.0


def test_a_concurrency_the_table_lacks_is_refused() -> None:
    with pytest.raises(CalibrationError, match="concurrency 4"):
        inter_token_ms(CELLS, 4)


def test_the_load_factor_is_the_per_token_time_at_max_num_seqs_over_one() -> None:
    assert load_factor(CELLS, 2) == pytest.approx(63.0 / 61.0)


def test_a_pod_never_gets_faster_under_load() -> None:
    """A ratio under 1 means the concurrency-1 cells ran slow, not that load speeds a pod up."""
    slower_alone = [_cell(1, 256, 0, 150.0, 190.0), _cell(2, 256, 0, 150.0, 146.0)]
    assert load_factor(slower_alone, 2) == 1.0


def test_start_up_adds_the_slow_first_call_to_ready_for_one_pod_alone() -> None:
    """Medians of the alone starts: Ready at 126 s, a first TTFT of 14 s against a warm 0.5 s."""
    assert startup_s(STARTS, 500.0) == pytest.approx(126.0 + 13.5)


def test_start_up_needs_a_pod_started_alone() -> None:
    with pytest.raises(CalibrationError, match="alone"):
        startup_s([_start("both", 200.0, 30_000)], 500.0)


def test_the_values_are_written_in_the_profiles_units() -> None:
    """A warm call is the line at the uncached warm-up prompt: 100 + 0.2 x 1000 = 300 ms."""
    assert simulator(CELLS, STARTS, max_num_seqs=2, warmup_prompt_tokens=1000) == {
        "prefill_overhead": "100ms",
        "prefill_time_per_token": "200us",
        "inter_token_latency": "61ms",
        "time_factor_under_load": 1.03,
        "startup_duration": "140s",
    }


def test_the_sim_profile_is_the_calibration_table() -> None:
    """An edit to either fails here until the other follows."""
    profile = yaml.safe_load((REPO_ROOT / "config" / "sim.yaml").read_text())["simulator"]
    calibration = yaml.safe_load((REPO_ROOT / "experiments" / "calibrate.yaml").read_text())
    expected = simulator(
        read_rows(TABLE),
        read_rows(COLD_STARTS),
        max_num_seqs=profile["max_num_seqs"],
        warmup_prompt_tokens=calibration["warmup"]["prompt_tokens"],
    )
    assert {key: profile[key] for key in expected} == expected
