"""The run tracker (D2, P3.8): the active runs, their remaining work and the context they hold, as
Prometheus text.

The agent runtime and the replayer each keep one and serve its text on /metrics; Prometheus scrapes
both, so a query sums them: sum(thesis_remaining_work). A run counts from when it arrives until it
ends, finished or failed, and adds max(expected-steps - step, 1): step is the x-step of the last
call sent (0 before the first), and a run still going has at least one more call coming, so the sum
never reads 0 while runs are active, even when half of a task type's runs overrun its median (P2.4).

The context (P4.10) is the prompt tokens of each active run's latest call, summed: the numerator of
P4.9's cache pressure, live. A run holds 0 until a count arrives, and a later count replaces the
earlier, since the latest prompt holds the earlier ones. The replayer counts a step as it sends it,
from the plan; the agent runtime only from the reply's usage, so its runs trail by one call until
P4.8 counts prompts before sending (docs/task-cards/54-P4.10-context-tokens-gauge.md).

Standard library only, so the replayer imports it through PYTHONPATH=agent, and the text is written
by hand: three gauges do not justify prometheus-client (rule 5). No run-id label, since the number
of runs has no bound. The replayer serves /metrics from another thread, so a lock guards the runs.
"""

import threading
from dataclasses import dataclass

# Prometheus's text exposition format, version 0.0.4.
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@dataclass
class _Progress:
    expected: int
    step: int
    context: int = 0


class RunTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, _Progress] = {}

    def started(self, run_id: str, expected: int) -> None:
        """A run has arrived; it counts its expected steps until its first call. Starting a run
        that is active already is a caller's bug and raises ValueError."""
        with self._lock:
            if run_id in self._runs:
                raise ValueError(f"run {run_id} started twice")
            self._runs[run_id] = _Progress(expected=expected, step=0)

    def stepped(self, run_id: str, step: int) -> None:
        """The run sent the call with x-step step."""
        with self._lock:
            self._progress(run_id).step = step

    def expected_changed(self, run_id: str, expected: int) -> None:
        """The run's x-expected-steps changed, as when step 1 has picked the scenario (D13)."""
        with self._lock:
            self._progress(run_id).expected = expected

    def context(self, run_id: str, prompt_tokens: int) -> None:
        """The run's latest call has prompt_tokens prompt tokens."""
        with self._lock:
            self._progress(run_id).context = prompt_tokens

    def ended(self, run_id: str) -> None:
        """The run stopped, finished or failed. An unknown or already ended run is left alone, so a
        caller can end a run on every way out."""
        with self._lock:
            self._runs.pop(run_id, None)

    def remaining_work(self) -> int:
        with self._lock:
            return self._work()

    def active_runs(self) -> int:
        with self._lock:
            return len(self._runs)

    def context_tokens(self) -> int:
        with self._lock:
            return self._context()

    def exposition(self) -> str:
        """The three gauges in Prometheus's text format, read under one lock so they agree."""
        with self._lock:
            work = self._work()
            active = len(self._runs)
            context = self._context()
        return (
            "# HELP thesis_remaining_work Sum over active runs of max(expected-steps - step, 1)"
            " (D2).\n"
            "# TYPE thesis_remaining_work gauge\n"
            f"thesis_remaining_work {work}\n"
            "# HELP thesis_active_runs Runs started and not yet ended.\n"
            "# TYPE thesis_active_runs gauge\n"
            f"thesis_active_runs {active}\n"
            "# HELP thesis_context_tokens Sum over active runs of the prompt tokens of their latest"
            " call.\n"
            "# TYPE thesis_context_tokens gauge\n"
            f"thesis_context_tokens {context}\n"
        )

    def _work(self) -> int:
        """Called with the lock held."""
        return sum(max(run.expected - run.step, 1) for run in self._runs.values())

    def _context(self) -> int:
        """Called with the lock held."""
        return sum(run.context for run in self._runs.values())

    def _progress(self, run_id: str) -> _Progress:
        """An update on a run that was never started, or has ended, is a caller's bug."""
        if run_id not in self._runs:
            raise KeyError(f"run {run_id} was not started")
        return self._runs[run_id]
