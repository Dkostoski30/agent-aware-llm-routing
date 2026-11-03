"""x-expected-steps per task type, from recorded runs (P2.4, D1).

The median, not the mean: a few very long runs (one of the dataset's takes 94 calls) would otherwise
speak for the type. Rounded up, since the header is a whole number and a remaining-work figure that
is too low starts a scale-up late.

Every run of the trace counts, failed ones too, so a trace needs no run status: a failed run uses
the GPU like any other, and remaining work estimates that load. On AgentProcessBench the medians of
all runs and of the runs labelled good differ by at most 1 step (experiments/log.md). Runs dropped
from a trace do not count, since a replay never sends them: that lowers tau2_airline, tau2_retail
and bfcl by 1 step each against the whole dataset.
"""

import math
import statistics
from collections.abc import Iterable


def expected_steps(runs: Iterable[tuple[str, int]]) -> dict[str, int]:
    """Each task type's value, from (task type, the run's calls) pairs."""
    steps_by_type: dict[str, list[int]] = {}
    for task_type, steps in runs:
        steps_by_type.setdefault(task_type, []).append(steps)
    return {
        task_type: math.ceil(statistics.median(steps)) for task_type, steps in steps_by_type.items()
    }
