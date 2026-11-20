"""The run tracker (D2, P3.8): the remaining work of the active runs, each run counting
max(expected-steps - step, 1), the context they hold (P4.10), and its Prometheus text."""

import threading
from collections.abc import Callable

import pytest
from agent_runtime.run_tracker import METRICS_CONTENT_TYPE, RunTracker


def test_remaining_work_sums_each_active_run() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.started("b", 3)
    tracker.started("c", 2)
    tracker.stepped("a", 2)
    tracker.stepped("b", 1)
    # a: 5 - 2 = 3, b: 3 - 1 = 2, c: 2 - 0 = 2, before its first call.
    assert tracker.remaining_work() == 7
    assert tracker.active_runs() == 3


def test_a_run_past_its_expected_steps_counts_one() -> None:
    tracker = RunTracker()
    tracker.started("over", 2)
    tracker.started("at", 3)
    tracker.started("before", 4)
    tracker.stepped("over", 5)
    tracker.stepped("at", 3)
    tracker.stepped("before", 1)
    assert tracker.remaining_work() == 1 + 1 + 3


def test_a_later_step_lowers_the_run_s_work() -> None:
    tracker = RunTracker()
    tracker.started("a", 4)
    tracker.stepped("a", 1)
    tracker.stepped("a", 2)
    assert tracker.remaining_work() == 2


def test_ended_runs_drop_out() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.started("b", 3)
    tracker.stepped("a", 1)
    tracker.ended("a")
    assert tracker.remaining_work() == 3
    assert tracker.active_runs() == 1
    tracker.ended("b")
    assert tracker.remaining_work() == 0
    assert tracker.active_runs() == 0


def test_expected_changed_after_the_first_step_changes_the_sum() -> None:
    # D13: step 1 sends the default, and the run's expected steps change once the scenario is known.
    tracker = RunTracker()
    tracker.started("a", 2)
    tracker.stepped("a", 1)
    assert tracker.remaining_work() == 1
    tracker.expected_changed("a", 6)
    assert tracker.remaining_work() == 5
    tracker.expected_changed("a", 1)
    assert tracker.remaining_work() == 1


def test_ended_on_an_unknown_run_does_nothing() -> None:
    tracker = RunTracker()
    tracker.started("a", 3)
    tracker.ended("unknown")
    tracker.ended("a")
    tracker.ended("a")
    assert tracker.remaining_work() == 0
    assert tracker.active_runs() == 0


def test_a_run_started_twice_is_refused() -> None:
    tracker = RunTracker()
    tracker.started("a", 3)
    with pytest.raises(ValueError, match="run a started twice"):
        tracker.started("a", 4)
    assert tracker.remaining_work() == 3


@pytest.mark.parametrize("update", ["stepped", "expected_changed", "context"])
def test_an_update_on_an_unknown_run_is_refused(update: str) -> None:
    tracker = RunTracker()
    with pytest.raises(KeyError, match="run unknown was not started"):
        getattr(tracker, update)("unknown", 2)
    assert tracker.active_runs() == 0


def test_an_ended_run_can_start_again() -> None:
    tracker = RunTracker()
    tracker.started("a", 3)
    tracker.stepped("a", 2)
    tracker.ended("a")
    tracker.started("a", 3)
    assert tracker.remaining_work() == 3


def test_exposition_is_prometheus_text() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.started("b", 2)
    tracker.stepped("a", 1)
    assert tracker.exposition() == (
        "# HELP thesis_remaining_work Sum over active runs of max(expected-steps - step, 1) (D2).\n"
        "# TYPE thesis_remaining_work gauge\n"
        "thesis_remaining_work 6\n"
        "# HELP thesis_active_runs Runs started and not yet ended.\n"
        "# TYPE thesis_active_runs gauge\n"
        "thesis_active_runs 2\n"
        "# HELP thesis_context_tokens Sum over active runs of the prompt tokens of their latest"
        " call.\n"
        "# TYPE thesis_context_tokens gauge\n"
        "thesis_context_tokens 0\n"
    )


def test_exposition_with_no_runs_reads_zero() -> None:
    text = RunTracker().exposition()
    assert "thesis_remaining_work 0\n" in text
    assert "thesis_active_runs 0\n" in text
    assert "thesis_context_tokens 0\n" in text


# The context the active runs hold (P4.10): each run's latest call's prompt tokens.


def test_context_tokens_sums_each_active_run_s_latest_count() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.started("b", 3)
    tracker.context("a", 400)
    tracker.context("b", 250)
    assert tracker.context_tokens() == 650


def test_a_started_run_with_no_count_adds_zero() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.started("b", 3)
    tracker.context("a", 400)
    assert tracker.context_tokens() == 400


def test_a_later_count_replaces_the_run_s_count() -> None:
    """The latest call's prompt holds the earlier ones, so counts are not added up."""
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.context("a", 400)
    tracker.context("a", 520)
    assert tracker.context_tokens() == 520


def test_an_ended_run_s_context_drops_out() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.started("b", 3)
    tracker.context("a", 400)
    tracker.context("b", 250)
    tracker.ended("a")
    assert tracker.context_tokens() == 250
    tracker.ended("b")
    assert tracker.context_tokens() == 0


def test_a_run_started_again_holds_no_context() -> None:
    tracker = RunTracker()
    tracker.started("a", 3)
    tracker.context("a", 400)
    tracker.ended("a")
    tracker.started("a", 3)
    assert tracker.context_tokens() == 0


def test_a_count_on_an_ended_run_is_refused() -> None:
    tracker = RunTracker()
    tracker.started("a", 3)
    tracker.ended("a")
    with pytest.raises(KeyError, match="run a was not started"):
        tracker.context("a", 400)
    assert tracker.context_tokens() == 0


def test_steps_and_counts_are_kept_apart() -> None:
    tracker = RunTracker()
    tracker.started("a", 5)
    tracker.context("a", 400)
    tracker.stepped("a", 2)
    tracker.expected_changed("a", 6)
    assert (tracker.remaining_work(), tracker.context_tokens()) == (4, 400)


class InterleavingLock:
    """A lock that, once armed, runs arrive right after its next release: what another thread may
    do between two locked reads (the replayer serves /metrics from a thread of its own)."""

    def __init__(self, arrive: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._arrive = arrive
        self.armed = False

    def __enter__(self) -> None:
        self._lock.acquire()

    def __exit__(self, *_: object) -> None:
        self._lock.release()
        if self.armed:
            self.armed = False
            self._arrive()


def test_exposition_reads_the_three_gauges_under_one_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that arrives with its count as soon as exposition lets go of the lock is in none of
    the three gauges or in all of them, never in the context alone."""
    tracker = RunTracker()

    def arrive() -> None:
        tracker.started("late", 3)
        tracker.context("late", 100)

    lock = InterleavingLock(arrive)
    monkeypatch.setattr(tracker, "_lock", lock)
    lock.armed = True
    assert gauges(tracker.exposition()) == (0, 0, 0)
    assert gauges(tracker.exposition()) == (3, 1, 100)


def gauges(text: str) -> tuple[int, int, int]:
    """(remaining work, active runs, context tokens) from the exposition's samples."""
    samples = dict(
        line.split(" ") for line in text.splitlines() if line and not line.startswith("#")
    )
    return (
        int(samples["thesis_remaining_work"]),
        int(samples["thesis_active_runs"]),
        int(samples["thesis_context_tokens"]),
    )


def test_content_type_is_the_text_format_version() -> None:
    assert METRICS_CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"
