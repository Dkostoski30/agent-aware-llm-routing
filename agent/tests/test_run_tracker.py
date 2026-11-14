"""The run tracker (D2, P3.8): the remaining work of the active runs, each run counting
max(expected-steps - step, 1), and its Prometheus text."""

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


@pytest.mark.parametrize("update", ["stepped", "expected_changed"])
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
    )


def test_exposition_with_no_runs_reads_zero() -> None:
    text = RunTracker().exposition()
    assert "thesis_remaining_work 0\n" in text
    assert "thesis_active_runs 0\n" in text


def test_content_type_is_the_text_format_version() -> None:
    assert METRICS_CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"
