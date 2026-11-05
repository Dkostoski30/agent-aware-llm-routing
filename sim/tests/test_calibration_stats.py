"""make calibrate's arithmetic (P2.8a): percentiles, per-token time, the rows it writes, when a pod
counts as warm, the host check, the clock's drift and cold-start times. Known inputs only."""

import pytest
from calibrator.config import CalibrationError, Cell, Host, Warmup
from calibrator.stats import (
    CELL_COLUMNS,
    COLD_START_COLUMNS,
    ClockReading,
    Measured,
    cell_row,
    check_host,
    cold_start_row,
    cold_start_times,
    drift,
    nearest_rank,
    per_token_ms,
    settled,
)

PERFORMANCE = "27fa6203-3987-4dcc-918d-748559d549ec"
HOST = Host(power_scheme=PERFORMANCE, sleep_settings=("STANDBYIDLE", "HIBERNATEIDLE"))
# powercfg.exe /getactivescheme, as Windows prints it (no newline at the end).
SCHEME = f"Power Scheme GUID: {PERFORMANCE}  (Performance)"


def _setting(alias: str, ac: str) -> str:
    """powercfg.exe /query SCHEME_CURRENT SUB_SLEEP <alias>, as Windows prints it, CRLF included."""
    lines = [
        f"Power Scheme GUID: {PERFORMANCE}  (Performance)",
        "  Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)",
        "    GUID Alias: SUB_SLEEP",
        "    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)",
        f"      GUID Alias: {alias}",
        "      Minimum Possible Setting: 0x00000000",
        "      Maximum Possible Setting: 0xffffffff",
        "      Possible Settings increment: 0x00000001",
        "      Possible Settings units: Seconds",
        f"    Current AC Power Setting Index: {ac}",
        "    Current DC Power Setting Index: 0x000000f0",
        "",
    ]
    return "\r\n".join(lines)


def test_nearest_rank_returns_a_measured_value() -> None:
    assert nearest_rank([5.0, 1.0, 4.0, 2.0, 3.0], 0.5) == 3.0
    assert nearest_rank([5.0, 1.0, 4.0, 2.0, 3.0], 0.95) == 5.0
    assert nearest_rank([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert nearest_rank([float(value) for value in range(1, 21)], 0.95) == 19.0
    with pytest.raises(CalibrationError):
        nearest_rank([], 0.5)


def test_per_token_time_is_the_gaps_after_the_first_token() -> None:
    assert per_token_ms(ttft_ms=400, duration_ms=9850, completion_tokens=64) == 150.0
    with pytest.raises(CalibrationError, match="1 token"):
        per_token_ms(ttft_ms=400, duration_ms=400, completion_tokens=1)


def _measured(call: int, in_flight_at_end: int = 2) -> Measured:
    """Call k: TTFT 300 + 10k ms, then 63 gaps of 140 + k ms, cached 16k tokens."""
    ttft = 300 + 10 * call
    return Measured(
        pod="vllm-a",
        ttft_ms=ttft,
        duration_ms=ttft + 63 * (140 + call),
        completion_tokens=64,
        cached_tokens=16 * call,
        in_flight_at_end=in_flight_at_end,
    )


def test_cell_row_holds_the_cell_and_its_percentiles() -> None:
    cell = Cell(concurrency=2, prompt_tokens=1024, cache="partial")
    measured = [_measured(call, in_flight_at_end=1 if call >= 18 else 2) for call in range(1, 21)]
    row = cell_row("vllm-a", cell, measured, clock_jumps=3)
    assert list(row) == list(CELL_COLUMNS)
    assert row == {
        "pod": "vllm-a",
        "concurrency": 2,
        "prompt_tokens": 1024,
        "cache": "partial",
        "calls": 20,
        "calls_below_concurrency": 3,
        "cached_tokens_p50": 160,
        "ttft_ms_p50": 400,
        "ttft_ms_p95": 490,
        "per_token_ms_p50": 150.0,
        "per_token_ms_p95": 159.0,
        "clock_jumps": 3,
    }


def test_cell_row_takes_only_its_pods_calls() -> None:
    cell = Cell(concurrency=1, prompt_tokens=256, cache="uncached")
    other = Measured("vllm-b", 9999, 99999, 64, 0, 1)
    with pytest.raises(CalibrationError, match="vllm-b"):
        cell_row("vllm-a", cell, [_measured(1), other], clock_jumps=0)


WARMUP = Warmup(prompt_tokens=1024, settled_ttft_ms=1000, max_calls=3)


def test_a_pod_is_warm_once_a_call_is_under_the_threshold() -> None:
    assert not settled([25000], WARMUP)
    assert not settled([25000, 14900], WARMUP)
    assert settled([25000, 14900, 900], WARMUP)
    assert not settled([1000], WARMUP)


def test_a_pod_still_slow_after_max_calls_stops_the_calibration() -> None:
    with pytest.raises(CalibrationError, match="3 warm-up calls"):
        settled([25000, 14900, 4600], WARMUP)


def test_host_check_passes_performance_with_sleep_never() -> None:
    settings = {alias: _setting(alias, "0x00000000") for alias in HOST.sleep_settings}
    check_host(SCHEME, settings, HOST)


def test_host_check_refuses_another_power_plan() -> None:
    balanced = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)"
    settings = {alias: _setting(alias, "0x00000000") for alias in HOST.sleep_settings}
    with pytest.raises(CalibrationError, match="381b4222-f694-41f0-9685-ff5bb260df2e"):
        check_host(balanced, settings, HOST)


def test_host_check_refuses_sleep_on_ac_and_names_the_setting() -> None:
    settings = {
        "STANDBYIDLE": _setting("STANDBYIDLE", "0x00000258"),
        "HIBERNATEIDLE": _setting("HIBERNATEIDLE", "0x00000000"),
    }
    with pytest.raises(CalibrationError, match=r"STANDBYIDLE.*600"):
        check_host(SCHEME, settings, HOST)


def test_host_check_refuses_output_it_cannot_read() -> None:
    settings = {alias: _setting(alias, "0x00000000") for alias in HOST.sleep_settings}
    settings["HIBERNATEIDLE"] = "The parameter is incorrect."
    with pytest.raises(CalibrationError, match="HIBERNATEIDLE"):
        check_host(SCHEME, settings, HOST)
    with pytest.raises(CalibrationError, match="power plan"):
        check_host("Access denied.", settings, HOST)


def test_drift_is_the_monotonic_clock_against_windows() -> None:
    start = ClockReading(monotonic_before=100.0, windows_unix_ms=1_000_000, monotonic_after=100.4)
    end = ClockReading(monotonic_before=700.0, windows_unix_ms=1_612_000, monotonic_after=700.4)
    measured = drift(start, end)
    assert measured.share == pytest.approx(-12 / 612)
    assert measured.uncertainty == pytest.approx(0.4 / 612)


def _pod(created: str, ready: str | None) -> dict[str, object]:
    conditions = [{"type": "PodScheduled", "status": "True", "lastTransitionTime": created}]
    if ready is not None:
        conditions.append({"type": "Ready", "status": "True", "lastTransitionTime": ready})
    return {
        "metadata": {"name": "vllm-abc", "creationTimestamp": created},
        "status": {"conditions": conditions},
    }


def test_cold_start_times_come_from_the_pods_timestamps() -> None:
    pod = _pod("2026-11-05T21:20:00Z", "2026-11-05T21:22:05Z")
    ready_unix = 1793913725.0  # 2026-11-05T21:22:05Z
    created_to_ready, ready_to_answer = cold_start_times(pod, first_answer_unix=ready_unix + 31.5)
    assert created_to_ready == 125.0
    assert ready_to_answer == 31.5


def test_cold_start_refuses_a_pod_that_is_not_ready() -> None:
    with pytest.raises(CalibrationError, match="vllm-abc"):
        cold_start_times(_pod("2026-11-05T21:20:00Z", None), first_answer_unix=1793913725.0)


def test_cold_start_row_has_the_columns_in_order() -> None:
    row = cold_start_row(
        restart=2,
        case="alone",
        pod="vllm-abc",
        times=(125.0, 31.5),
        warmup_ttft_ms=[26000, 14900, 900],
    )
    assert list(row) == list(COLD_START_COLUMNS)
    assert row == {
        "restart": 2,
        "case": "alone",
        "pod": "vllm-abc",
        "created_to_ready_s": 125.0,
        "ready_to_first_answer_s": 31.5,
        "first_ttft_ms": 26000,
        "warmup_calls": 3,
    }
