"""make calibrate's arithmetic (P2.8a): pure, so every number in the table comes from code with
tests. Percentiles are nearest rank, as load_generator.results has them: a value that was measured.
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from calibrator.config import CalibrationError, Cell, Host, Warmup

CELL_COLUMNS = (
    "pod",
    "concurrency",
    "prompt_tokens",
    "cache",
    "calls",
    # Calls that ended with fewer than concurrency in flight, themselves included: the last few of a
    # cell, once some streams are done.
    "calls_below_concurrency",
    "cached_tokens_p50",
    "ttft_ms_p50",
    "ttft_ms_p95",
    "per_token_ms_p50",
    "per_token_ms_p95",
    # The journal's clock steps while the cell ran (P2.5): a cell with some may read off.
    "clock_jumps",
)
COLD_START_COLUMNS = (
    "restart",
    # alone: one pod started beside a running one; both: every pod started at once.
    "case",
    "pod",
    # From Kubernetes' timestamps, whole seconds of the wall clock.
    "created_to_ready_s",
    "ready_to_first_answer_s",
    "first_ttft_ms",
    "warmup_calls",
)
SCHEME_LINE = re.compile(r"Power Scheme GUID: ([0-9a-f-]{36})")
AC_LINE = re.compile(r"Current AC Power Setting Index: 0x([0-9a-f]+)")


@dataclass(frozen=True)
class Measured:
    pod: str
    ttft_ms: int
    duration_ms: int
    completion_tokens: int
    cached_tokens: int
    # Calls of the cell in flight when this one ended, itself included.
    in_flight_at_end: int


@dataclass(frozen=True)
class ClockReading:
    """Windows' clock read between two readings of the VM's monotonic clock: powershell.exe takes
    a few hundred ms to start, so the reading lies somewhere between them."""

    monotonic_before: float
    windows_unix_ms: int
    monotonic_after: float


@dataclass(frozen=True)
class Drift:
    # How much more the monotonic clock advanced than Windows' clock, as a share of Windows' time:
    # P2.5 measured about -0.02, so durations read 2% low.
    share: float
    # The most the two readings' brackets can move share either way.
    uncertainty: float


def nearest_rank(values: Sequence[float], share: float) -> float:
    if not values:
        raise CalibrationError("a percentile of no values")
    ordered = sorted(values)
    return ordered[math.ceil(share * len(ordered)) - 1]


def per_token_ms(ttft_ms: int, duration_ms: int, completion_tokens: int) -> float:
    """The mean gap between tokens after the first, the simulator's inter-token latency."""
    if completion_tokens < 2:
        raise CalibrationError("a reply of 1 token has no time between tokens")
    return (duration_ms - ttft_ms) / (completion_tokens - 1)


def cell_row(
    pod: str, cell: Cell, measured: Sequence[Measured], clock_jumps: int
) -> dict[str, Any]:
    others = sorted({call.pod for call in measured} - {pod})
    if others:
        raise CalibrationError(f"calls of {others} in the row of {pod}")
    ttft = [float(call.ttft_ms) for call in measured]
    per_token = [
        per_token_ms(call.ttft_ms, call.duration_ms, call.completion_tokens) for call in measured
    ]
    return {
        "pod": pod,
        "concurrency": cell.concurrency,
        "prompt_tokens": cell.prompt_tokens,
        "cache": cell.cache,
        "calls": len(measured),
        "calls_below_concurrency": sum(
            call.in_flight_at_end < cell.concurrency for call in measured
        ),
        "cached_tokens_p50": int(nearest_rank([call.cached_tokens for call in measured], 0.5)),
        "ttft_ms_p50": int(nearest_rank(ttft, 0.5)),
        "ttft_ms_p95": int(nearest_rank(ttft, 0.95)),
        "per_token_ms_p50": round(nearest_rank(per_token, 0.5), 2),
        "per_token_ms_p95": round(nearest_rank(per_token, 0.95), 2),
        "clock_jumps": clock_jumps,
    }


def settled(ttft_ms: Sequence[int], warmup: Warmup) -> bool:
    """Whether the warm-up calls so far end it: the last was under the threshold. A pod still slow
    after max_calls stops the calibration, as a cold pod's numbers would go into the table."""
    if ttft_ms and ttft_ms[-1] < warmup.settled_ttft_ms:
        return True
    if len(ttft_ms) >= warmup.max_calls:
        raise CalibrationError(
            f"still {ttft_ms[-1]} ms to the first token after {len(ttft_ms)} warm-up calls, "
            f"warmup.settled_ttft_ms is {warmup.settled_ttft_ms}"
        )
    return False


def check_host(scheme_output: str, setting_outputs: Mapping[str, str], host: Host) -> None:
    """scheme_output is powercfg.exe /getactivescheme; setting_outputs, per alias,
    powercfg.exe /query SCHEME_CURRENT SUB_SLEEP <alias>."""
    schemes = set(SCHEME_LINE.findall(scheme_output))
    if not schemes:
        raise CalibrationError(f"no power plan in powercfg's output: {scheme_output!r}")
    if schemes != {host.power_scheme}:
        raise CalibrationError(
            f"power plan {sorted(schemes)}, host.power_scheme is {host.power_scheme}: "
            "choose Performance in Windows and Armoury Crate"
        )
    for alias in host.sleep_settings:
        found = AC_LINE.findall(setting_outputs[alias])
        if len(found) != 1:
            raise CalibrationError(f"no AC value of {alias} in powercfg's output")
        seconds = int(found[0], 16)
        if seconds != 0:
            raise CalibrationError(
                f"{alias} on AC is {seconds} s, not never: the run would stop when Windows "
                "sleeps (powercfg.exe /change standby-timeout-ac 0, hibernate-timeout-ac 0)"
            )


def drift(start: ClockReading, end: ClockReading) -> Drift:
    def middle(reading: ClockReading) -> float:
        return (reading.monotonic_before + reading.monotonic_after) / 2

    def half_width(reading: ClockReading) -> float:
        return (reading.monotonic_after - reading.monotonic_before) / 2

    windows = (end.windows_unix_ms - start.windows_unix_ms) / 1000
    monotonic = middle(end) - middle(start)
    return Drift(
        share=(monotonic - windows) / windows,
        uncertainty=(half_width(start) + half_width(end)) / windows,
    )


def _unix(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp).timestamp()


def cold_start_times(pod: Mapping[str, Any], first_answer_unix: float) -> tuple[float, float]:
    """Created to Ready and Ready to the first answered call, from the pod's JSON (kubectl get -o
    json). Both ends are wall clock, Kubernetes' in whole seconds."""
    ready = [
        condition["lastTransitionTime"]
        for condition in pod["status"]["conditions"]
        if condition["type"] == "Ready" and condition["status"] == "True"
    ]
    if not ready:
        raise CalibrationError(f"pod {pod['metadata']['name']} is not Ready")
    ready_unix = _unix(ready[0])
    return ready_unix - _unix(pod["metadata"]["creationTimestamp"]), first_answer_unix - ready_unix


def cold_start_row(
    restart: int, case: str, pod: str, times: tuple[float, float], warmup_ttft_ms: Sequence[int]
) -> dict[str, Any]:
    return {
        "restart": restart,
        "case": case,
        "pod": pod,
        "created_to_ready_s": times[0],
        "ready_to_first_answer_s": round(times[1], 1),
        "first_ttft_ms": warmup_ttft_ms[0],
        "warmup_calls": len(warmup_ttft_ms),
    }
