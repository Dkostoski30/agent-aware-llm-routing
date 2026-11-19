"""make drain-check (P4.3): the pod chosen, the calls judged and the verdict, from known lines.

The live path (a scale-down during a replay on the cluster) is what make drain-check's done-when
proves; here the cluster is replaced by recorded calls of what it was asked.
"""

import json
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from load_generator.drain import (
    DEADLINE_REASON,
    DrainError,
    ScaleDown,
    build_drain_report,
    busiest_pod,
    load_drain,
    main,
    picker_restarts,
    pool_command,
    removed_pods,
    report_lines,
    run_drain,
    scale_down,
)
from load_generator.record import REPO_ROOT

# The scale-down, on the wall clock in ms as ts_start, and the settle after it.
SCALED_MS = 100_000
SETTLE_MS = 5_000
REMOVED = "vllm-a"
KEPT = "vllm-b"
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


def failed(run: str, step: int, reason: str) -> str:
    """The line replay.py prints for a call with no full reply."""
    return f"replay: {run} step {step}: no full reply: {reason}"


def picked(
    run: str, pod: str, reason: str, source: str | None = None, at: str = "2026-11-19T12:00:00Z"
) -> str:
    """A picker line as the plugin logs it; the pods are endpoint names, <pod>-rank-0."""
    line = {"timestamp": at, "body": "Run call picked", "run-id": run}
    line.update({"step": "2", "pod": f"{pod}-rank-0", "reason": reason})
    if source is not None:
        line["from"] = f"{source}-rank-0"
    return json.dumps(line)


def drained_lines() -> list[dict[str, Any]]:
    """Run a is pinned to the removed pod and its step 2 runs across the scale-down, then it moves;
    run b stays on the kept pod."""
    return [
        call("a", 1, SCALED_MS - 9_000, 2_000, REMOVED),
        call("a", 2, SCALED_MS - 3_000, 20_000, REMOVED),
        call("a", 3, SCALED_MS + 17_100, 2_000, KEPT),
        call("b", 1, SCALED_MS - 8_000, 2_000, KEPT),
        call("b", 2, SCALED_MS - 1_000, 3_000, KEPT),
        call("b", 3, SCALED_MS + 2_100, 2_000, KEPT),
    ]


REMAPPED_PICKER = picked("a", KEPT, "remapped", source=REMOVED)


def drained_with(changes: dict[int, dict[str, Any] | None]) -> list[dict[str, Any]]:
    """drained_lines with the lines at the given places replaced, or left out where None: a run
    whose call failed sends no later step, since a replay never retries (P2.3)."""
    kept = []
    for place, line in enumerate(drained_lines()):
        changed = changes.get(place, line)
        if changed is not None:
            kept.append(changed)
    return kept


CUT = call("a", 2, SCALED_MS - 3_000, duration=None)


def report(
    lines: list[dict[str, Any]], log: str = "", picker: str = REMAPPED_PICKER, drain: bool = True
) -> Any:
    return build_drain_report(lines, log, picker, REMOVED, SCALED_MS, SETTLE_MS, drain)


def test_a_call_that_ran_across_the_scale_down_and_ended_passes() -> None:
    result = report(drained_lines())
    assert result.running_on_removed == 1
    assert result.running_on_removed_failed == 0
    assert result.interrupted == []
    assert result.answered_after_on_removed == 0
    assert result.affected == ["a"]
    assert result.remapped == ["a"]
    assert not result.void
    assert result.passed


def test_a_call_cut_on_the_removed_pod_interrupts_its_run() -> None:
    lines = drained_with({1: CUT, 2: None})
    log = failed("a", 2, "RemoteProtocolError('peer closed connection')")
    result = report(lines, log)
    # A failed call names no pod: its run's pin at the scale-down stands for it.
    assert result.running_on_removed == 1
    assert result.running_on_removed_failed == 1
    assert result.interrupted == ["a"]
    assert result.window_failures == ["a step 2: RemoteProtocolError('peer closed connection')"]
    assert not result.passed


def test_a_failure_without_a_printed_reason_interrupts_its_run() -> None:
    lines = drained_with({1: CUT, 2: None})
    assert report(lines).interrupted == ["a"]


def test_a_call_failing_on_its_deadline_is_counted_apart() -> None:
    lines = drained_with({5: call("b", 3, SCALED_MS + 2_100, duration=None)})
    result = report(lines, failed("b", 3, DEADLINE_REASON))
    assert result.interrupted == []
    assert result.deadline_failures == 1
    assert result.passed


def test_a_call_sent_during_the_settle_that_fails_otherwise_interrupts_its_run() -> None:
    lines = drained_with({5: call("b", 3, SCALED_MS + 2_100, duration=None)})
    result = report(lines, failed("b", 3, "ReplyError('HTTP 503')"))
    assert result.interrupted == ["b"]
    assert not result.passed


def test_a_failure_after_the_settle_is_reported_and_does_not_decide() -> None:
    lines = drained_with({2: call("a", 3, SCALED_MS + 17_100, duration=None)})
    result = report(lines, failed("a", 3, "ReplyError('HTTP 500')"))
    assert result.after_failures == ["a step 3: ReplyError('HTTP 500')"]
    assert result.interrupted == []
    assert result.passed


def test_a_call_the_removed_pod_answers_after_the_settle_fails_the_drain() -> None:
    lines = drained_with({2: call("a", 3, SCALED_MS + 17_100, 2_000, REMOVED)})
    result = report(lines)
    assert result.answered_after_on_removed == 1
    assert not result.passed


def test_a_scale_down_with_no_call_on_the_removed_pod_is_void_not_passed() -> None:
    lines = [call("b", 1, SCALED_MS - 8_000, 2_000, KEPT), call("b", 2, SCALED_MS + 9_000)]
    result = report(lines, picker="")
    assert result.running_on_removed == 0
    assert result.void
    assert not result.passed


def test_an_affected_run_without_its_remapped_line_is_listed_apart() -> None:
    result = report(drained_lines(), picker="")
    assert result.affected == ["a"]
    assert result.remapped == []
    # The remap is the cost the drain cannot save, counted; it does not decide.
    assert result.passed


def test_a_remap_off_another_pod_is_not_the_removed_pods() -> None:
    result = report(drained_lines(), picker=picked("a", REMOVED, "remapped", source=KEPT))
    assert result.remapped == []


def test_a_fanned_out_step_with_one_call_cut_interrupts_its_run() -> None:
    # Two calls of one step, one failing on its deadline and one cut: the step is not deadline-only.
    cut_b = call("b", 3, SCALED_MS + 2_100, duration=None)
    lines = [*drained_lines()[:5], cut_b, dict(cut_b)]
    log = "\n".join([failed("b", 3, DEADLINE_REASON), failed("b", 3, "ReplyError('HTTP 503')")])
    result = report(lines, log)
    assert result.interrupted == ["b"]
    assert result.deadline_failures == 0


def test_the_report_says_which_drain_passed_failed_or_was_void() -> None:
    passed = report_lines("drain-on", report(drained_lines()), 12.5)
    assert passed[-1] == "drain-on: passed"
    assert "  removed pod vllm-a, gone 12.5 s after the scale-down" in passed
    lines = [drained_lines()[0], CUT]
    cut = report(lines, failed("a", 2, "RemoteProtocolError('x')"))
    assert report_lines("drain-on", cut, 0.4)[-1] == "drain-on: failed, 1 interrupted runs ['a']"
    void = report([call("b", 1, SCALED_MS - 8_000, 2_000, KEPT)], picker="")
    assert report_lines("drain-on", void, 1.0)[-1] == (
        "drain-on: void, no call was running on the removed pod at the scale-down"
    )


def test_the_control_is_counted_not_judged() -> None:
    lines = [drained_lines()[0], CUT]
    cut = report(lines, failed("a", 2, "RemoteProtocolError('x')"), drain=False)
    assert not cut.passed
    assert report_lines("drain-off", cut, 0.4)[-1] == "drain-off: control, 1 interrupted runs ['a']"


def test_a_pod_that_outlives_the_wait_is_said_so() -> None:
    lines = report_lines("drain-on", report(drained_lines()), None)
    assert "  removed pod vllm-a, not gone by the end" in lines


@pytest.mark.parametrize(
    ("pins", "pod"),
    [
        ({"r1": KEPT, "r2": REMOVED, "r3": REMOVED}, REMOVED),
        ({"r1": KEPT}, KEPT),
        # A tie goes by name.
        ({"r1": KEPT, "r2": REMOVED}, REMOVED),
        # Pins on a pod that is not Ready do not count.
        ({"r1": "vllm-gone"}, None),
        ({}, None),
    ],
)
def test_the_pod_removed_holds_the_most_live_pins(pins: dict[str, str], pod: str | None) -> None:
    assert busiest_pod(pins, [KEPT, REMOVED]) == pod


def pod(name: str, deleting: bool = False, restarts: tuple[int, ...] = ()) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-19T12:00:00Z"
    statuses = [{"restartCount": count} for count in restarts]
    return {"metadata": metadata, "status": {"containerStatuses": statuses}}


def test_the_pods_being_removed_are_those_with_a_deletion_time() -> None:
    pods = {"items": [pod(KEPT), pod(REMOVED, deleting=True)]}
    assert removed_pods(pods) == [REMOVED]


def test_picker_restarts_sum_every_container_of_every_pod() -> None:
    pods = {"items": [pod("epp-1", restarts=(1, 0)), pod("epp-2", restarts=(2,)), pod("epp-3")]}
    assert picker_restarts(pods) == 3


@pytest.mark.parametrize(
    ("profile", "on", "chart", "value"),
    [
        ("sim", True, "infra/charts/sim", "drain.enabled=true"),
        ("sim", False, "infra/charts/sim", "drain.enabled=false"),
        ("real", False, "infra/charts/vllm", "drain.enabled=false"),
    ],
)
def test_a_variant_sets_the_pools_drain_and_keeps_every_other_value(
    profile: str, on: bool, chart: str, value: str
) -> None:
    assert pool_command(profile, on) == [
        "helm",
        "upgrade",
        "vllm",
        str(REPO_ROOT / chart),
        "--reuse-values",
        "--force-conflicts",
        "--set",
        value,
    ]


def experiment_file(tmp_path: Path, **changes: Any) -> Path:
    spec: dict[str, Any] = {
        "profile": "sim",
        "routing": {"plugin": True, "affinity": True},
        "trace": str(TRACE),
        "pattern": "burst",
        "seed": 6,
        "scale_down_after_seconds": 125,
        "wait_for_calls_seconds": 60,
        "settle_seconds": 5,
        "variants": [{"name": "drain-off", "drain": False}, {"name": "drain-on", "drain": True}],
    }
    spec.update(changes)
    path = tmp_path / "drain.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_the_experiment_file_is_read_whole(tmp_path: Path) -> None:
    experiment = load_drain(experiment_file(tmp_path))
    assert experiment.name == "drain"
    assert experiment.profile == "sim"
    assert experiment.routing == {"plugin": True, "affinity": True}
    assert experiment.trace == TRACE
    assert (experiment.pattern, experiment.seed) == ("burst", 6)
    assert experiment.scale_down_after_seconds == 125
    assert experiment.wait_for_calls_seconds == 60
    assert experiment.settle_seconds == 5
    assert [(v.name, v.drain) for v in experiment.variants] == [
        ("drain-off", False),
        ("drain-on", True),
    ]
    assert len(experiment.hash) == 12


@pytest.mark.parametrize(
    "changes",
    [
        {"routing": {"plugin": True, "affinity": False}},
        {"routing": {"plugin": False, "affinity": True}},
        {"routing": {"plugin": True, "affinity": "yes"}},
        {"scale_down_after_seconds": 0},
        {"wait_for_calls_seconds": -1},
        {"settle_seconds": -1},
        {"trace": "missing.jsonl"},
        {"variants": []},
        {"variants": [{"name": "a", "drain": True}, {"name": "a", "drain": False}]},
        {"variants": [{"name": "a", "drain": "on"}]},
        {"variants": [{"name": "a", "drain": None}]},
    ],
)
def test_an_experiment_file_that_could_not_show_a_drain_is_refused(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(DrainError):
        load_drain(experiment_file(tmp_path, **changes))


def picker_with_pins() -> str:
    """Two runs pinned to the removed pod and one to the kept, logged now: inside a replay's window,
    which starts at the run's own clock."""
    at = datetime.now(UTC).isoformat()
    runs = [("r1", REMOVED), ("r2", REMOVED), ("r3", KEPT)]
    return "\n".join(picked(run, pod, "first", at=at) for run, pod in runs)


class FakeCluster:
    """The module's cluster calls, recorded in order. kubectl's replies follow its first word."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        replay_seconds: float = 5,
        picker: str | None = None,
        failing: tuple[str, ...] = (),
        removes: str = REMOVED,
        fail_replay: bool = False,
    ):
        self.calls: list[Any] = []
        # Scale-downs taken so far, and how many there should be once this variant's has come: a
        # variant's replay lasts until its own scale-down, not an earlier variant's.
        self.scaled = threading.Condition()
        self.scales = 0
        self.wanted = 1
        # The last scale-down's wall-clock time in ms, which the replay's lines are placed around.
        self.scaled_ms = SCALED_MS
        self.replay_seconds = replay_seconds
        self.picker = picker
        self.failing = failing
        self.removes = removes
        self.fail_replay = fail_replay
        target = "load_generator.drain"
        monkeypatch.setattr(f"{target}.set_routing", self.set_routing)
        monkeypatch.setattr(f"{target}.kubectl", self.kubectl)
        monkeypatch.setattr(f"{target}.replay_to_file", self.replay)
        monkeypatch.setattr(f"{target}.run_command", self.run_command)
        monkeypatch.setattr(f"{target}.PICKER_POLL_SECONDS", 0.01)

    def set_routing(self, profile: str, values: Any, routing: Any) -> None:
        self.calls.append(("set_routing", profile, routing))

    def run_command(self, command: list[str]) -> None:
        self.calls.append(("run", *command[:2]))
        if command[:2] == ["helm", "upgrade"]:
            self.wanted = self.scales + 1

    def kubectl(self, *args: str) -> str:
        self.calls.append(("kubectl", *args[:2]))
        if args[0] in self.failing:
            raise subprocess.CalledProcessError(1, ["kubectl", *args])
        if args[0] == "scale":
            with self.scaled:
                self.scales += 1
                self.scaled_ms = round(time.time() * 1000)
                self.scaled.notify_all()
        if args[:2] == ("get", "deployment"):
            return json.dumps({"spec": {"selector": {"matchLabels": {"app": "epp"}}}})
        if args[:2] == ("get", "pods") and args[3] == "app=epp":
            return json.dumps({"items": [pod("epp-1", restarts=(0,))]})
        if args[:2] == ("get", "pods"):
            ready = [{"type": "Ready", "status": "True"}]
            items = []
            for name in (KEPT, REMOVED):
                item = pod(name, deleting=self.scales >= self.wanted and name == self.removes)
                item["status"].update(podIP="10.0.0.1", conditions=ready)
                items.append(item)
            return json.dumps({"items": items})
        if args[0] == "logs":
            return picker_with_pins() if self.picker is None else self.picker
        return ""

    def replay(self, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("replay",))
        with self.scaled:
            self.scaled.wait_for(lambda: self.scales >= self.wanted, self.replay_seconds)
        if self.fail_replay:
            raise RuntimeError("replay died")
        shift = self.scaled_ms - SCALED_MS
        return [{**line, "ts_start": line["ts_start"] + shift} for line in drained_lines()]


PROFILE = {
    "replicas": 2,
    "routing": {"plugin": False, "affinity": False, "run_ttl_seconds": 300},
    "drain": {"enabled": True, "timeout_seconds": 120, "quiet_seconds": 5},
}


def test_scale_down_marks_the_busiest_pod_then_scales_and_waits_until_it_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = FakeCluster(monkeypatch)
    state = ScaleDown(ready=[KEPT, REMOVED], since="2026-11-19T11:00:00+00:00", replicas=2)
    scale_down(state, wait_for_calls_seconds=1)
    assert state.chosen == REMOVED
    assert state.pins == {KEPT: 1, REMOVED: 2}
    assert state.removed == REMOVED
    assert state.requested_ms is not None and state.gone_ms is not None
    verbs = [call[1:] for call in cluster.calls]
    annotate = verbs.index(("annotate", "pod"))
    assert verbs[annotate + 1] == ("scale", "deployment/vllm")
    assert verbs[-1] == ("wait", f"pod/{REMOVED}")


def test_scale_down_waits_for_a_pin_and_gives_up_after_its_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = FakeCluster(monkeypatch, picker="")
    state = ScaleDown(ready=[KEPT, REMOVED], since="2026-11-19T11:00:00+00:00", replicas=2)
    scale_down(state, wait_for_calls_seconds=0.05)
    assert state.chosen is None
    assert not state.scaled
    assert ("kubectl", "scale", "deployment/vllm") not in cluster.calls
    # Read more than once: the wait polls.
    assert cluster.calls.count(("kubectl", "logs", "deployment/vllm-pool-epp")) > 1


def test_scale_down_keeps_a_failed_kubectl_for_after_the_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCluster(monkeypatch, failing=("annotate",))
    state = ScaleDown(ready=[KEPT, REMOVED], since="2026-11-19T11:00:00+00:00", replicas=2)
    scale_down(state, wait_for_calls_seconds=1)
    assert isinstance(state.error, subprocess.CalledProcessError)
    assert not state.scaled


def test_a_ready_set_removing_another_pod_is_kept_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCluster(monkeypatch, removes=KEPT)
    state = ScaleDown(ready=[KEPT, REMOVED], since="2026-11-19T11:00:00+00:00", replicas=2)
    scale_down(state, wait_for_calls_seconds=1)
    assert isinstance(state.error, DrainError)
    assert state.removed is None


def test_every_variant_sets_its_drain_and_the_profile_comes_back_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch)
    experiment = load_drain(experiment_file(tmp_path, scale_down_after_seconds=0.01))
    results = run_drain(experiment, PROFILE, tmp_path)
    assert [result.variant.name for result in results] == ["drain-off", "drain-on"]
    assert all(result.state.removed == REMOVED for result in results)
    runs = [call for call in cluster.calls if call[0] == "run"]
    assert runs == [
        ("run", "helm", "upgrade"),
        ("run", "bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")),
        ("run", "helm", "upgrade"),
        ("run", "bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")),
        ("run", "make", "sim-deploy"),
    ]
    rules = {"plugin": True, "affinity": True}
    assert cluster.calls.count(("set_routing", "sim", rules)) == 2
    assert cluster.calls[-1] == ("set_routing", "sim", None)


def test_a_replay_that_dies_puts_the_profiles_pool_and_routing_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, fail_replay=True)
    experiment = load_drain(experiment_file(tmp_path, scale_down_after_seconds=0.01))
    with pytest.raises(RuntimeError, match="replay died"):
        run_drain(experiment, PROFILE, tmp_path)
    assert ("run", "make", "sim-deploy") in cluster.calls
    assert cluster.calls[-1] == ("set_routing", "sim", None)


def test_a_replay_that_ends_before_the_scale_down_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeCluster(monkeypatch, replay_seconds=0)
    experiment = load_drain(experiment_file(tmp_path, scale_down_after_seconds=30))
    with pytest.raises(DrainError, match="ended before the scale-down"):
        run_drain(experiment, PROFILE, tmp_path)


def test_a_failed_scale_down_is_raised_after_the_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, failing=("scale",))
    experiment = load_drain(experiment_file(tmp_path, scale_down_after_seconds=0.01))
    with pytest.raises(subprocess.CalledProcessError):
        run_drain(experiment, PROFILE, tmp_path)
    assert cluster.calls[-1] == ("set_routing", "sim", None)


def test_a_profile_with_one_pod_is_refused_before_the_cluster_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch)
    experiment = load_drain(experiment_file(tmp_path))
    with pytest.raises(DrainError, match="at least 2"):
        run_drain(experiment, {**PROFILE, "replicas": 1}, tmp_path)
    assert cluster.calls == []


def main_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sim.yaml").write_text(yaml.safe_dump(PROFILE))
    (tmp_path / "experiments" / "results").mkdir(parents=True)
    path = experiment_file(tmp_path, scale_down_after_seconds=0.01, **changes)
    monkeypatch.setattr("load_generator.drain.REPO_ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["drain", str(path)])
    return path


def test_main_writes_each_variants_files_and_refuses_to_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeCluster(monkeypatch)
    path = main_setup(tmp_path, monkeypatch)
    main()
    experiment = load_drain(path)
    results = tmp_path / "experiments" / "results"
    stem = f"drain-{experiment.hash}"
    meta = json.loads((results / f"{stem}.meta.json").read_text())
    assert [variant["name"] for variant in meta["variants"]] == ["drain-off", "drain-on"]
    assert meta["variants"][1]["passed"] is True
    assert meta["variants"][1]["removed_pod"] == REMOVED
    for variant in ("drain-off", "drain-on"):
        assert (results / f"{stem}-{variant}-picker.log").exists()
        assert (results / f"{stem}-{variant}.jsonl.log").exists()
    with pytest.raises(DrainError, match="exist"):
        main()


def test_main_fails_when_a_drained_variant_was_not_scaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No pod holds a pin, so neither variant is scaled: the control is counted, the drain fails.
    FakeCluster(monkeypatch, picker="", replay_seconds=0.5)
    path = main_setup(tmp_path, monkeypatch, wait_for_calls_seconds=0.05)
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    experiment = load_drain(path)
    results = tmp_path / "experiments" / "results"
    meta = json.loads((results / f"drain-{experiment.hash}.meta.json").read_text())
    assert [variant["removed_pod"] for variant in meta["variants"]] == [None, None]
