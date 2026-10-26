"""Summary math of the P0.4 concurrency check: percentiles, peaks and parsing of tool output."""

import pytest
from concurrency_check import (
    RequestResult,
    parse_docker_mem,
    parse_nvidia_smi_mib,
    peak_per_key,
    percentile,
    summarize,
)


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        # Nearest rank: the smallest value with at least q% of the values at or below it.
        ([5.0], 50, 5.0),
        ([5.0], 95, 5.0),
        ([1.0, 2.0, 3.0, 4.0], 50, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 95, 4.0),
        ([float(v) for v in range(1, 21)], 95, 19.0),
        ([float(v) for v in range(1, 101)], 50, 50.0),
        ([float(v) for v in range(1, 101)], 95, 95.0),
        ([float(v) for v in range(1, 101)], 100, 100.0),
        # Order of the input does not matter.
        ([4.0, 1.0, 3.0, 2.0], 50, 2.0),
    ],
)
def test_percentile_nearest_rank(values: list[float], q: float, expected: float) -> None:
    assert percentile(values, q) == expected


def test_percentile_of_no_values_is_an_error() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


@pytest.mark.parametrize("q", [0, -1, 101])
def test_percentile_outside_range_is_an_error(q: float) -> None:
    with pytest.raises(ValueError, match="percentile"):
        percentile([1.0], q)


def test_summarize_counts_and_latency_per_port() -> None:
    results = [
        RequestResult(port=8001, ok=True, seconds=1.0),
        RequestResult(port=8001, ok=True, seconds=3.0),
        RequestResult(port=8001, ok=False, seconds=9.0),
        RequestResult(port=8002, ok=True, seconds=2.0),
    ]
    summary = summarize(results, ports=[8001, 8002])
    assert summary[8001].requests == 3
    assert summary[8001].errors == 1
    # Failed requests are counted but left out of the latency:
    # a timeout says nothing about serving speed.
    assert summary[8001].p50_seconds == 1.0
    assert summary[8001].p95_seconds == 3.0
    assert summary[8002].requests == 1
    assert summary[8002].errors == 0
    assert summary[8002].p50_seconds == 2.0


def test_summarize_port_without_successes_has_no_latency() -> None:
    results = [RequestResult(port=8001, ok=False, seconds=5.0)]
    summary = summarize(results, ports=[8001, 8002])
    assert summary[8001].requests == 1
    assert summary[8001].errors == 1
    assert summary[8001].p50_seconds is None
    assert summary[8002].requests == 0
    assert summary[8002].p95_seconds is None


def test_peak_per_key_takes_the_maximum_of_each_key() -> None:
    samples = [{"vllm-1": 100, "vllm-2": 300}, {"vllm-1": 250, "vllm-2": 200}, {"vllm-1": 50}]
    assert peak_per_key(samples) == {"vllm-1": 250, "vllm-2": 300}


def test_peak_per_key_of_no_samples_is_empty() -> None:
    assert peak_per_key([]) == {}


@pytest.mark.parametrize(
    ("usage", "expected_bytes"),
    [
        # docker stats prints "<used> / <limit>" with binary units (KiB, MiB, GiB) or plain B.
        ("2.5GiB / 9.7GiB", int(2.5 * 1024**3)),
        ("812.4MiB / 9.7GiB", int(812.4 * 1024**2)),
        ("64KiB / 9.7GiB", 64 * 1024),
        ("0B / 0B", 0),
    ],
)
def test_parse_docker_mem(usage: str, expected_bytes: int) -> None:
    assert parse_docker_mem(usage) == expected_bytes


def test_parse_docker_mem_rejects_unknown_unit() -> None:
    with pytest.raises(ValueError, match="memory usage"):
        parse_docker_mem("2.5GB / 9.7GB")


def test_parse_nvidia_smi_mib_reads_the_first_gpu() -> None:
    assert parse_nvidia_smi_mib("3712\n") == 3712


def test_parse_nvidia_smi_mib_rejects_non_numbers() -> None:
    with pytest.raises(ValueError, match="nvidia-smi"):
        parse_nvidia_smi_mib("[N/A]\n")
