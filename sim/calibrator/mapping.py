"""config/sim.yaml's simulator values from make calibrate's table (P2.8b, card 31; the reasons
are in sim/calibration.md). Pure, so every value in the profile comes from code with tests, and a
test holds the profile to the committed table.

llm-d-inference-sim v1.0.0's per-token calculator (pkg/simulator/latencies.go): TTFT = overhead +
uncached prompt tokens x time per token, then one gap per later token, all three times the same
load factor, which runs linearly from 1 at one running request to time_factor_under_load at
max_num_seqs.
"""

import csv
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from calibrator.config import CalibrationError

Row = Mapping[str, str]

# The cold starts that set startup_duration: one pod added beside a running one, as autoscaling adds
# one. "both" restarts two pods on one GPU and disk at once, which the simulator's independent pods
# cannot express.
STARTUP_CASE = "alone"


def read_rows(path: Path) -> list[dict[str, str]]:
    """A table or cold-start file as make calibrate wrote it."""
    with path.open(newline="") as table:
        return list(csv.DictReader(table))


def _at(cells: Sequence[Row], concurrency: int) -> list[Row]:
    found = [cell for cell in cells if int(cell["concurrency"]) == concurrency]
    if not found:
        raise CalibrationError(f"the table has no cells at concurrency {concurrency}")
    return found


def prefill_line(cells: Sequence[Row]) -> tuple[float, float]:
    """The least-squares line through the concurrency-1 cells' TTFT p50 against their uncached
    tokens (the prompt less the cell's cached_tokens p50), all cache modes together: (overhead ms,
    ms per uncached token). Concurrency 1 because the simulator applies its load factor on top."""
    alone = _at(cells, 1)
    uncached = [int(c["prompt_tokens"]) - int(c["cached_tokens_p50"]) for c in alone]
    if len(set(uncached)) < 2:
        raise CalibrationError("a TTFT line needs cells with at least two uncached lengths")
    ttft = [float(c["ttft_ms_p50"]) for c in alone]
    per_token, overhead = statistics.linear_regression(uncached, ttft)
    return overhead, per_token


def inter_token_ms(cells: Sequence[Row], concurrency: int) -> float:
    """The median of the per-token p50s of every cell at this concurrency."""
    return statistics.median(float(c["per_token_ms_p50"]) for c in _at(cells, concurrency))


def load_factor(cells: Sequence[Row], max_num_seqs: int) -> float:
    """The per-token time at max_num_seqs over that at 1, the simulator's factor at its limit. Never
    under 1: a smaller ratio says the concurrency-1 cells ran slow (P2.8a's lora file did, for its
    first 9 cells), not that more requests make a pod faster."""
    return max(1.0, inter_token_ms(cells, max_num_seqs) / inter_token_ms(cells, 1))


def startup_s(starts: Sequence[Row], warm_ttft_ms: float) -> float:
    """Median created-to-Ready of the pods started alone, plus how much longer their first call's
    TTFT was than a warm pod's: the simulator has no slow first call, so its pods are Ready when a
    real pod first answers at a warm pod's speed (decided 2026-11-06)."""
    alone = [start for start in starts if start["case"] == STARTUP_CASE]
    if not alone:
        raise CalibrationError(f"no pod was started {STARTUP_CASE}")
    ready = statistics.median(float(start["created_to_ready_s"]) for start in alone)
    first_ttft = statistics.median(int(start["first_ttft_ms"]) for start in alone)
    return ready + (first_ttft - warm_ttft_ms) / 1000


def simulator(
    cells: Sequence[Row], starts: Sequence[Row], max_num_seqs: int, warmup_prompt_tokens: int
) -> dict[str, Any]:
    """The simulator section's timed values, rounded to whole ms, µs per prompt token, whole s and
    a load factor of two decimals: finer steps than the table's cells differ by. The warm TTFT is
    the line's for make calibrate's uncached warm-up prompt, the call the first TTFT was timed
    on."""
    overhead, per_token = prefill_line(cells)
    warm_ttft = overhead + per_token * warmup_prompt_tokens
    return {
        "prefill_overhead": f"{round(overhead)}ms",
        "prefill_time_per_token": f"{round(per_token * 1000)}us",
        "inter_token_latency": f"{round(inter_token_ms(cells, 1))}ms",
        "time_factor_under_load": round(load_factor(cells, max_num_seqs), 2),
        "startup_duration": f"{round(startup_s(starts, warm_ttft))}s",
    }
