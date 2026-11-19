"""make pod-removed (P3.4): the groups, the affected runs and the verdict, from known lines.

The live path (kubectl delete during a replay on the cluster) is what make pod-removed's
done-when proves; here the cluster is replaced by recorded calls of what it was asked.
"""

import io
import json
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from load_generator.pod_removed import (
    FINISHED_BEFORE,
    RUNNING_AT_DELETE,
    SENT_AFTER,
    SENT_DURING_SETTLE,
    Deletion,
    PodRemovedError,
    Removed,
    Tee,
    build_report,
    choose_pod,
    delete_pod,
    endpoint_pod,
    failure_reasons,
    group_of,
    live_pins,
    load_pod_removed,
    log_path,
    main,
    picker_lines,
    pinned_pods,
    report_lines,
    run_pod_removed,
)
from load_generator.record import REPO_ROOT

# The delete, on the wall clock in ms as ts_start, and the settle after it.
DELETED_MS = 100_000
SETTLE_MS = 5_000
DELETED = "vllm-a"
OTHER = "vllm-b"
TRACE = REPO_ROOT / "experiments/results/traces/convert-apb-ca5fc26bbb1d-cd81f326aece.jsonl"


def call(
    run: str, step: int, start: int, duration: int | None = 1_000, pod: str | None = None
) -> dict[str, Any]:
    """A trace.v1 line with only what the report reads. No duration is a call with no full reply,
    which has no pod either."""
    line: dict[str, Any] = {"run_id": run, "step": step, "ts_start": start}
    if duration is not None:
        line["duration_ms"] = duration
    if pod is not None:
        line["pod"] = pod
    return line


def picked(run: str, pod: str, reason: str, source: str | None = None) -> str:
    """A picker line as the plugin logs it, in llm-d-router's JSON (docs/otel-json-stdout.md). The
    pods are endpoint names, <pod>-rank-0, as on the cluster
    (experiments/results/pod-removed-07fcbf93ea76-picker.log)."""
    line = {"timestamp": "2026-11-11T12:00:00Z", "body": "Run call picked", "run-id": run}
    line.update({"step": "2", "pod": f"{pod}-rank-0", "reason": reason})
    if source is not None:
        line["from"] = f"{source}-rank-0"
    return json.dumps(line)


# A window that starts before the fixtures' lines, logged at 12:00:00.
BEFORE_FIXTURES = "2026-11-11T11:00:00+00:00"


def ended(run: str) -> str:
    return json.dumps({"timestamp": "2026-11-11T12:00:00Z", "body": "Run ended", "run-id": run})


@pytest.mark.parametrize(
    ("line", "group"),
    [
        (call("r", 1, DELETED_MS - 2_000, duration=1_000), FINISHED_BEFORE),
        (call("r", 1, DELETED_MS - 1_000, duration=1_000), FINISHED_BEFORE),
        (call("r", 1, DELETED_MS - 1_000, duration=1_001), RUNNING_AT_DELETE),
        # A failed call's end is not recorded, so one sent before the delete counts as running.
        (call("r", 1, DELETED_MS - 9_000, duration=None), RUNNING_AT_DELETE),
        (call("r", 1, DELETED_MS), SENT_DURING_SETTLE),
        (call("r", 1, DELETED_MS + SETTLE_MS - 1), SENT_DURING_SETTLE),
        (call("r", 1, DELETED_MS + SETTLE_MS), SENT_AFTER),
        (call("r", 1, DELETED_MS + SETTLE_MS, duration=None), SENT_AFTER),
    ],
)
def test_each_call_falls_in_one_group_by_when_it_was_sent(
    line: Mapping[str, Any], group: str
) -> None:
    assert group_of(line, DELETED_MS, SETTLE_MS) == group


def test_failure_reasons_are_read_from_the_replays_printed_lines() -> None:
    text = (
        "replay: 1/20 bfcl-1 bfcl: 3 calls, pods ['vllm-a']\n"
        "replay: bfcl-2 step 3: no full reply: HTTPStatusError('Server error 503')\n"
        "replay: bfcl-2 step 3: no full reply: TimeoutError()\n"
        "pod-removed: deleting vllm-a\n"
    )
    assert failure_reasons(text) == {
        ("bfcl-2", 3): ["HTTPStatusError('Server error 503')", "TimeoutError()"]
    }


def test_a_runs_pin_at_the_delete_is_its_last_answered_call_before_it() -> None:
    lines = [
        call("r1", 1, 10_000, pod=OTHER),
        call("r1", 2, 20_000, pod=DELETED),
        # Running at the delete and answered: its pod is the pin's.
        call("r2", 1, DELETED_MS - 500, duration=3_000, pod=DELETED),
        # A call with no reply names no pod, so the pin is the call before.
        call("r3", 1, 10_000, pod=OTHER),
        call("r3", 2, DELETED_MS - 100, duration=None),
        # Sent after the delete only: no pin at the delete.
        call("r4", 1, DELETED_MS + 1, pod=OTHER),
        call("r1", 3, DELETED_MS + 10, pod=OTHER),
    ]
    assert pinned_pods(lines, DELETED_MS) == {"r1": DELETED, "r2": DELETED, "r3": OTHER}


@pytest.mark.parametrize(
    ("endpoint", "pod"),
    [
        ("vllm-66c49d9b9b-2mn9p-rank-0", "vllm-66c49d9b9b-2mn9p"),
        ("vllm-66c49d9b9b-2mn9p-rank-12", "vllm-66c49d9b9b-2mn9p"),
        ("vllm-66c49d9b9b-2mn9p", "vllm-66c49d9b9b-2mn9p"),
    ],
)
def test_an_endpoint_name_gives_its_pod(endpoint: str, pod: str) -> None:
    assert endpoint_pod(endpoint) == pod


def test_live_pins_are_each_runs_last_pick_until_its_end() -> None:
    text = "\n".join(
        [
            picked("r1", DELETED, "first"),
            picked("r2", OTHER, "first"),
            picked("r1", OTHER, "remapped", DELETED),
            picked("r3", DELETED, "first"),
            ended("r2"),
            ended("never-picked"),
        ]
    )
    assert live_pins(picker_lines(text), BEFORE_FIXTURES) == {"r1": OTHER, "r3": DELETED}


def test_live_pins_leave_out_what_the_picker_logged_before_the_window() -> None:
    """pod-removed-346f89bc516e: kubectl's --since-time returned set-routing's probe, logged before
    the window, and it was counted as a third live pin on the pod deleted."""
    probe = json.dumps(
        {
            "timestamp": "2026-11-11T11:09:51.413582724Z",
            "body": "Run call picked",
            "run-id": "set-routing-probe",
            "pod": f"{DELETED}-rank-0",
            "reason": "first",
        }
    )
    run = json.dumps(
        {
            "timestamp": "2026-11-11T11:11:16.522562767Z",
            "body": "Run call picked",
            "run-id": "hotpotqa-124-4",
            "pod": f"{OTHER}-rank-0",
            "reason": "first",
        }
    )
    since = "2026-11-11T11:09:51.500000+00:00"
    assert live_pins(picker_lines("\n".join([probe, run])), since) == {"hotpotqa-124-4": OTHER}


@pytest.mark.parametrize(
    ("pins", "pod", "counts"),
    [
        ({"r1": OTHER, "r2": OTHER, "r3": DELETED}, OTHER, {OTHER: 2, DELETED: 1}),
        ({"r1": DELETED}, DELETED, {OTHER: 0, DELETED: 1}),
        # A tie goes by name, so the choice is the same on every run of one log.
        ({"r1": OTHER, "r2": DELETED}, DELETED, {OTHER: 1, DELETED: 1}),
        ({}, DELETED, {OTHER: 0, DELETED: 0}),
        # A pin on a pod that is not Ready is no reason to delete another.
        ({"r1": "vllm-gone", "r2": OTHER}, OTHER, {OTHER: 1, DELETED: 0}),
    ],
)
def test_the_pod_to_delete_holds_the_most_live_pins(
    pins: dict[str, str], pod: str, counts: dict[str, int]
) -> None:
    assert choose_pod(pins, [OTHER, DELETED]) == (pod, counts)


def test_picker_lines_skip_what_is_not_a_json_object() -> None:
    text = "\n".join(["I1111 klog text", "[1, 2]", ended("r1"), "", picked("r1", OTHER, "first")])
    assert [line["body"] for line in picker_lines(text)] == ["Run ended", "Run call picked"]


def passing_lines() -> list[dict[str, Any]]:
    """r1 is pinned to the deleted pod and goes on on the other; r2 was on the other pod all along;
    r3's call was running on the deleted pod and failed."""
    return [
        call("r1", 1, DELETED_MS - 3_000, duration=1_000, pod=DELETED),
        call("r1", 2, DELETED_MS - 1_000, duration=2_000, pod=DELETED),
        call("r1", 3, DELETED_MS + SETTLE_MS + 1_000, pod=OTHER),
        call("r2", 1, DELETED_MS - 3_000, pod=OTHER),
        call("r2", 2, DELETED_MS + SETTLE_MS + 2_000, pod=OTHER),
        call("r3", 1, DELETED_MS - 3_000, duration=1_000, pod=DELETED),
        call("r3", 2, DELETED_MS - 500, duration=None),
    ]


PASSING_PICKER = "\n".join(
    [picked("r1", OTHER, "remapped", DELETED), ended("r1"), ended("r2"), ended("r3")]
)
PASSING_LOG = "replay: r3 step 2: no full reply: RemoteProtocolError('closed')\n"


def report(
    lines: list[dict[str, Any]], log: str = PASSING_LOG, picker: str = PASSING_PICKER
) -> Any:
    return build_report(lines, log, picker, DELETED, DELETED_MS, SETTLE_MS)


def test_runs_moved_off_the_deleted_pod_with_no_failure_after_the_settle_pass() -> None:
    result = report(passing_lines())
    assert result.passed
    assert result.affected == ["r1", "r3"]
    assert result.tested == ["r1"]
    assert result.groups[FINISHED_BEFORE].calls == 3
    assert result.groups[RUNNING_AT_DELETE].calls == 2
    assert result.groups[RUNNING_AT_DELETE].failed == 1
    assert result.groups[RUNNING_AT_DELETE].reasons == ["r3 step 2: RemoteProtocolError('closed')"]
    assert result.groups[SENT_AFTER].calls == 2
    assert result.groups[SENT_AFTER].failed == 0
    assert (result.runs, result.ended) == (3, 3)


def test_a_failure_sent_after_the_settle_fails_and_says_why() -> None:
    lines = [*passing_lines(), call("r2", 3, DELETED_MS + SETTLE_MS + 9_000, duration=None)]
    result = report(lines, log=PASSING_LOG + "replay: r2 step 3: no full reply: TimeoutError()\n")
    assert not result.passed
    assert result.groups[SENT_AFTER].reasons == ["r2 step 3: TimeoutError()"]
    assert any("sent after" in line and "1 failed" in line for line in report_lines(result))


def test_a_failure_during_the_settle_is_counted_and_does_not_decide() -> None:
    lines = [*passing_lines(), call("r4", 1, DELETED_MS + 100, duration=None)]
    result = report(lines, log=PASSING_LOG + "replay: r4 step 1: no full reply: 503\n")
    assert result.passed
    assert result.groups[SENT_DURING_SETTLE].failed == 1


def test_a_call_after_the_settle_on_the_deleted_pod_fails() -> None:
    lines = [*passing_lines(), call("r2", 3, DELETED_MS + SETTLE_MS + 9_000, pod=DELETED)]
    result = report(lines)
    assert not result.passed
    assert result.sent_after_on_deleted == 1


@pytest.mark.parametrize(
    "picker",
    [
        # No remapped line for r1.
        "\n".join([picked("r1", OTHER, "pinned")]),
        # Remapped from the pod that stayed.
        "\n".join([picked("r1", OTHER, "remapped", OTHER)]),
        # Twice: the run moved again after its remap.
        "\n".join(
            [picked("r1", OTHER, "remapped", DELETED), picked("r1", DELETED, "remapped", OTHER)]
        ),
    ],
)
def test_a_tested_run_needs_exactly_one_remap_from_the_deleted_pod(picker: str) -> None:
    result = report(passing_lines(), picker=picker)
    assert not result.passed
    assert result.remap_misses == ["r1"]


def test_a_run_that_finished_on_the_deleted_pod_before_the_delete_is_not_affected() -> None:
    lines = [*passing_lines(), call("r0", 1, DELETED_MS - 9_000, duration=1_000, pod=DELETED)]
    result = report(lines)
    assert result.affected == ["r1", "r3"]


def test_no_affected_run_with_a_call_after_the_settle_tests_nothing() -> None:
    lines = [line for line in passing_lines() if line["run_id"] != "r1"]
    result = report(lines)
    assert result.tested == []
    assert not result.passed
    assert any("nothing tested" in line for line in report_lines(result))


def test_run_ended_counts_each_replayed_run_once() -> None:
    picker = "\n".join(
        [PASSING_PICKER, ended("r1"), ended("set-routing-probe"), picked("r1", OTHER, "pinned")]
    )
    result = report(passing_lines(), picker=picker)
    assert (result.runs, result.ended) == (3, 3)


def experiment_file(tmp_path: Path, **changes: Any) -> Path:
    spec: dict[str, Any] = {
        "profile": "sim",
        "routing": {"plugin": True, "affinity": True},
        "trace": str(TRACE),
        "pattern": "steady-long",
        "seed": 6,
        "delete_after_seconds": 120,
        "settle_seconds": 5,
    }
    spec.update(changes)
    path = tmp_path / "pod-removed.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_the_experiment_file_is_read_whole(tmp_path: Path) -> None:
    experiment = load_pod_removed(experiment_file(tmp_path))
    assert experiment.name == "pod-removed"
    assert len(experiment.hash) == 12
    assert experiment.routing == {"plugin": True, "affinity": True}
    assert experiment.trace == TRACE
    assert (experiment.delete_after_seconds, experiment.settle_seconds) == (120, 5)


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"routing": {"plugin": False, "affinity": True}}, "plugin"),
        ({"routing": {"plugin": True, "affinity": False}}, "affinity"),
        ({"routing": {"plugin": True, "affinity": "yes"}}, "routing"),
        ({"delete_after_seconds": 0}, "delete_after_seconds"),
        ({"settle_seconds": -1}, "settle_seconds"),
        ({"trace": "no-such-trace.jsonl"}, "trace"),
    ],
)
def test_an_experiment_file_that_could_not_show_a_remap_is_refused(
    tmp_path: Path, changes: dict[str, Any], says: str
) -> None:
    with pytest.raises(PodRemovedError, match=says):
        load_pod_removed(experiment_file(tmp_path, **changes))


def test_tee_writes_to_every_stream() -> None:
    first, second = io.StringIO(), io.StringIO()
    tee = Tee(first, second)
    tee.write("replay: a line\n")
    tee.flush()
    assert first.getvalue() == second.getvalue() == "replay: a line\n"


SINCE = "2026-11-11T09:21:35.146281+00:00"


def test_delete_pod_deletes_the_pod_with_the_most_live_pins_then_waits_until_it_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, ...]] = []
    picker = "\n".join([picked("r1", OTHER, "first"), picked("r2", DELETED, "first"), ended("r2")])

    def kubectl(*args: str) -> str:
        asked.append(args)
        return picker if args[0] == "logs" else ""

    monkeypatch.setattr("load_generator.pod_removed.kubectl", kubectl)
    deletion = Deletion([DELETED, OTHER], SINCE)
    delete_pod(deletion)
    assert asked[0] == ("logs", "deployment/vllm-pool-epp", f"--since-time={SINCE}")
    assert asked[1] == ("delete", "pod", OTHER, "--wait=false")
    assert asked[2][:3] == ("wait", f"pod/{OTHER}", "--for=delete")
    assert deletion.pod == OTHER
    assert deletion.pins == {DELETED: 0, OTHER: 1}
    assert deletion.requested_ms is not None and deletion.gone_ms is not None
    assert deletion.requested_ms <= deletion.gone_ms
    assert deletion.error is None


def test_a_failed_delete_is_kept_for_after_the_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    def kubectl(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["kubectl", *args])

    monkeypatch.setattr("load_generator.pod_removed.kubectl", kubectl)
    deletion = Deletion([DELETED, OTHER], SINCE)
    delete_pod(deletion)
    assert deletion.error is not None
    assert deletion.gone_ms is None


class FakeCluster:
    """The module's cluster calls, recorded in order."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        replay_seconds: float,
        fail: bool = False,
        failing: tuple[str, ...] = (),
        picker: str = PASSING_PICKER,
        routing_fails: bool = False,
    ):
        self.calls: list[Any] = []
        self.deleted = threading.Event()
        self.replay_seconds = replay_seconds
        self.fail = fail
        # kubectl verbs that exit non-zero, and the picker's log they read.
        self.failing = failing
        self.picker = picker
        self.routing_fails = routing_fails
        target = "load_generator.pod_removed"
        monkeypatch.setattr(f"{target}.set_routing", self.set_routing)
        monkeypatch.setattr(f"{target}.kubectl", self.kubectl)
        monkeypatch.setattr(f"{target}.replay_to_file", self.replay)

    def set_routing(self, profile: str, values: Any, routing: Any) -> None:
        self.calls.append(("set_routing", profile, values, routing))
        if self.routing_fails and routing is not None:
            raise subprocess.CalledProcessError(1, ["bash", "set-routing.sh"])

    def kubectl(self, *args: str) -> str:
        self.calls.append(("kubectl", args[0]))
        if args[0] == "get":
            ready = [{"type": "Ready", "status": "True"}]
            pods = [
                {"metadata": {"name": name}, "status": {"podIP": "10.0.0.1", "conditions": ready}}
                for name in (OTHER, DELETED)
            ]
            return json.dumps({"items": pods})
        if args[0] == "delete":
            self.deleted.set()
        if args[0] in self.failing:
            raise subprocess.CalledProcessError(1, ["kubectl", *args])
        if args[0] == "logs":
            return self.picker
        return ""

    def replay(self, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("replay",))
        self.deleted.wait(self.replay_seconds)
        if self.fail:
            raise RuntimeError("replay died")
        return passing_lines()


PROFILE = {"replicas": 2, "routing": {"plugin": False, "affinity": False, "run_ttl_seconds": 300}}


def test_the_picker_runs_the_rules_for_the_replay_and_the_profiles_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=5)
    experiment = load_pod_removed(experiment_file(tmp_path, delete_after_seconds=0.01))
    deletion, _, _ = run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    # PASSING_PICKER ends every run, so no pod holds a pin and the tie goes by name.
    assert deletion.pod == DELETED
    assert cluster.calls[0] == ("kubectl", "get")
    assert cluster.calls[1] == ("set_routing", "sim", None, {"plugin": True, "affinity": True})
    # The timer's thread reads the picker's log while the replay runs, so only the order around
    # the replay is fixed.
    assert cluster.calls.index(("replay",)) > 1
    assert cluster.calls.index(("kubectl", "rollout")) > cluster.calls.index(("replay",))
    assert ("kubectl", "logs") in cluster.calls
    assert cluster.calls[-1] == ("set_routing", "sim", None, None)


def test_a_replay_that_dies_puts_the_profiles_routing_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=5, fail=True)
    experiment = load_pod_removed(experiment_file(tmp_path, delete_after_seconds=0.01))
    with pytest.raises(RuntimeError, match="replay died"):
        run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    assert cluster.calls[-1] == ("set_routing", "sim", None, None)


def test_a_replay_that_ends_before_the_delete_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=0)
    experiment = load_pod_removed(experiment_file(tmp_path, delete_after_seconds=60))
    with pytest.raises(PodRemovedError, match="before the delete"):
        run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    assert ("kubectl", "delete") not in cluster.calls
    assert cluster.calls[-1] == ("set_routing", "sim", None, None)


def test_a_failed_delete_is_raised_after_the_replay_and_the_routing_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=5, failing=("delete",))
    experiment = load_pod_removed(experiment_file(tmp_path, delete_after_seconds=0.01))
    with pytest.raises(subprocess.CalledProcessError):
        run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    assert ("kubectl", "rollout") not in cluster.calls
    assert cluster.calls[-1] == ("set_routing", "sim", None, None)


def test_a_pod_that_outlives_the_wait_still_gives_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=5, failing=("wait",))
    experiment = load_pod_removed(experiment_file(tmp_path, delete_after_seconds=0.01))
    deletion, lines, _ = run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    assert deletion.gone_ms is None
    assert lines == passing_lines()
    assert ("kubectl", "rollout") in cluster.calls


def test_a_delete_thread_that_dies_is_named_and_not_taken_for_a_short_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pick with no timestamp: live_pins raises KeyError in the timer's thread.
    broken = json.dumps({"body": "Run call picked", "run-id": "r1", "pod": f"{OTHER}-rank-0"})
    died: list[type[BaseException]] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: died.append(args.exc_type))
    cluster = FakeCluster(monkeypatch, replay_seconds=0.5, picker=broken)
    experiment = load_pod_removed(experiment_file(tmp_path, delete_after_seconds=0.01))
    with pytest.raises(PodRemovedError, match="delete thread stopped"):
        run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    assert died == [KeyError]
    assert ("kubectl", "delete") not in cluster.calls
    assert cluster.calls[-1] == ("set_routing", "sim", None, None)


def test_a_switch_that_fails_halfway_puts_the_profiles_routing_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=0, routing_fails=True)
    experiment = load_pod_removed(experiment_file(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        run_pod_removed(experiment, PROFILE, tmp_path / "out.jsonl")
    assert ("replay",) not in cluster.calls
    assert cluster.calls[-1] == ("set_routing", "sim", None, None)


def test_the_running_calls_on_the_deleted_pod_are_counted_apart() -> None:
    lines = [
        *passing_lines(),
        # Running at the delete on the pod that stayed.
        call("r2", 9, DELETED_MS - 100, duration=5_000, pod=OTHER),
    ]
    result = report(lines)
    assert result.groups[RUNNING_AT_DELETE].calls == 3
    # r1's step 2 answered from the deleted pod; r3's step 2 got no reply, and r3 was pinned there.
    assert (result.running_on_deleted, result.running_on_deleted_failed) == (2, 1)
    assert any(f"2 on {DELETED}, 1 failed" in line for line in report_lines(result))


def main_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lines: list[dict[str, Any]]
) -> Path:
    """main on a repo root in tmp_path, with run_pod_removed replaced by its result."""
    (tmp_path / "config").mkdir()
    # Tracked in the repo, so it is there before a run.
    (tmp_path / "experiments" / "results").mkdir(parents=True)
    (tmp_path / "config" / "sim.yaml").write_text(yaml.safe_dump(PROFILE))
    path = experiment_file(tmp_path)

    def run(experiment: Any, profile: Any, out: Path) -> tuple[Removed, list[Any], str]:
        assert profile == PROFILE
        log_path(out).write_text(PASSING_LOG)
        removed = Removed(DELETED, {DELETED: 2, OTHER: 0}, SINCE, DELETED_MS, DELETED_MS + 900)
        return removed, lines, PASSING_PICKER

    monkeypatch.setattr("load_generator.pod_removed.REPO_ROOT", tmp_path)
    monkeypatch.setattr("load_generator.pod_removed.run_pod_removed", run)
    monkeypatch.setattr("sys.argv", ["pod_removed", str(path)])
    return path


def test_main_writes_the_meta_and_refuses_a_second_run_of_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = main_setup(tmp_path, monkeypatch, passing_lines())
    main()
    stem = f"pod-removed-{load_pod_removed(path).hash}"
    results = tmp_path / "experiments" / "results"
    meta = json.loads((results / f"{stem}.meta.json").read_text())
    assert meta["passed"] is True
    assert meta["deleted_pod"] == DELETED
    assert meta["live_pins_at_delete"] == {DELETED: 2, OTHER: 0}
    assert meta["groups"][SENT_AFTER] == {"calls": 2, "failed": 0}
    assert (results / f"{stem}-picker.log").read_text() == PASSING_PICKER
    with pytest.raises(PodRemovedError, match="results of this file exist"):
        main()


def test_main_exits_1_when_the_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = [*passing_lines(), call("r2", 3, DELETED_MS + SETTLE_MS + 9_000, duration=None)]
    main_setup(tmp_path, monkeypatch, failing)
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
