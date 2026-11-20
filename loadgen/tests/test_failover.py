"""make failover-check (P4.4): the completion rate, the calls on the killed pod and the verdict,
from known lines.

The live path (a pod killed during a replay on the cluster) is what make failover-check's
done-when proves; here the cluster is replaced by recorded calls of what it was asked.
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
from load_generator.failover import (
    FailoverError,
    Kill,
    build_failover_report,
    check_pool,
    kill_pod,
    load_failover,
    main,
    report_lines,
    retry_starts,
    run_failover,
    variant_profile,
)
from load_generator.record import REPO_ROOT

# The kill, on the wall clock in ms as ts_start, and the settle after it.
KILLED_MS = 100_000
SETTLE_MS = 5_000
KILLED = "sim-a"
KEPT = "sim-b"
TRACE = REPO_ROOT / "experiments/results/traces/convert-apb-ca5fc26bbb1d-cd81f326aece.jsonl"
DEADLINE = "TimeoutError()"


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


def retried(run: str, step: int, attempt: int, sent: int, reason: str = "ConnectError()") -> str:
    """The line replay.py prints before it sends a call again."""
    return f"replay: {run} step {step}: retry {attempt}, attempt sent at {sent}: {reason}"


def picked(
    run: str, pod: str, reason: str, source: str | None = None, at: str = "2026-11-19T12:00:00Z"
) -> str:
    """A picker line as the plugin logs it; the pods are endpoint names, <pod>-rank-0."""
    line = {"timestamp": at, "body": "Run call picked", "run-id": run}
    line.update({"step": "2", "pod": f"{pod}-rank-0", "reason": reason})
    if source is not None:
        line["from"] = f"{source}-rank-0"
    return json.dumps(line)


# Run a is pinned to the killed pod; its step 2 was running there at the kill, was sent again
# 5 s after the kill and answered on the kept pod. Run b stays on the kept pod.
SAVED = [
    call("a", 1, KILLED_MS - 9_000, 2_000, KILLED),
    call("a", 2, KILLED_MS + 5_000, 3_000, KEPT),
    call("a", 3, KILLED_MS + 8_100, 2_000, KEPT),
    call("b", 1, KILLED_MS - 8_000, 2_000, KEPT),
    call("b", 2, KILLED_MS - 1_000, 3_000, KEPT),
    call("b", 3, KILLED_MS + 2_100, 2_000, KEPT),
]
SAVED_LOG = retried("a", 2, 1, KILLED_MS - 3_000)
# The same without retry: run a's step 2 died with the pod and ended its run.
LOST = [line for line in SAVED if line["run_id"] == "b"] + [
    call("a", 1, KILLED_MS - 9_000, 2_000, KILLED),
    call("a", 2, KILLED_MS - 3_000, None),
]
LOST_LOG = failed("a", 2, "RemoteProtocolError('peer closed connection')")
REMAPPED_PICKER = picked("a", KEPT, "remapped", source=KILLED)


def report(
    lines: list[dict[str, Any]], log: str, picker: str = REMAPPED_PICKER, retry: bool = True
) -> Any:
    return build_failover_report(lines, log, picker, KILLED, KILLED_MS, SETTLE_MS, retry)


def test_a_call_the_killed_pod_lost_and_the_retry_saved_passes() -> None:
    result = report(SAVED, SAVED_LOG)
    assert (result.runs, result.done) == (2, 2)
    assert result.completion_rate == 1.0
    assert result.on_killed == [f"a step 2: retried 1, answered by {KEPT}"]
    assert result.retries == 1
    assert result.failed_by_kill == []
    assert (result.affected, result.remapped) == (["a"], ["a"])
    assert not result.void
    assert result.passed


def test_without_retry_the_lost_call_ends_its_run() -> None:
    result = report(LOST, LOST_LOG, retry=False)
    assert (result.runs, result.done, result.completion_rate) == (2, 1, 0.5)
    assert result.on_killed == ["a step 2: failed, RemoteProtocolError('peer closed connection')"]
    assert result.failed_by_kill == ["a"]
    assert not result.passed


def test_a_retry_that_ran_out_fails_the_run_by_the_kill() -> None:
    lines = [
        *LOST[:3],
        call("a", 1, KILLED_MS - 9_000, 2_000, KILLED),
        call("a", 2, KILLED_MS + 10_000, None),
    ]
    log = "\n".join(
        [
            retried("a", 2, 1, KILLED_MS - 3_000),
            retried("a", 2, 2, KILLED_MS + 5_000),
            failed("a", 2, "HTTPStatusError('503')"),
        ]
    )
    result = report(lines, log)
    assert result.on_killed == ["a step 2: retried 2, failed, HTTPStatusError('503')"]
    assert result.retries == 2
    assert result.failed_by_kill == ["a"]
    assert not result.passed


def test_a_call_the_killed_pod_answered_before_it_died_counts_on_it() -> None:
    lines = [
        call("a", 1, KILLED_MS - 9_000, 2_000, KILLED),
        call("a", 2, KILLED_MS - 3_000, 3_500, KILLED),
        call("a", 3, KILLED_MS + 6_000, 2_000, KEPT),
    ]
    result = report(lines, "")
    assert result.on_killed == [f"a step 2: answered by {KILLED}"]
    assert result.passed


def test_a_run_failed_on_its_deadline_is_counted_apart() -> None:
    lines = [*SAVED, call("c", 1, KILLED_MS - 50_000, None)]
    result = report(lines, "\n".join([SAVED_LOG, failed("c", 1, DEADLINE)]))
    assert (result.runs, result.done) == (3, 2)
    assert result.deadline_runs == ["c"]
    assert result.failed_by_kill == []
    assert result.passed


def test_a_deadline_inside_the_window_does_not_count_as_the_kills() -> None:
    lines = [*SAVED, call("c", 1, KILLED_MS - 500, None)]
    result = report(lines, "\n".join([SAVED_LOG, failed("c", 1, DEADLINE)]))
    assert result.failed_by_kill == []
    assert result.deadline_runs == ["c"]


def test_a_failure_with_no_printed_reason_in_the_window_counts_as_the_kills() -> None:
    lines = [*SAVED, call("c", 1, KILLED_MS + 1_000, None)]
    result = report(lines, SAVED_LOG)
    assert result.failed_by_kill == ["c"]
    assert not result.passed


def test_a_kill_with_no_call_on_the_killed_pod_is_void_not_passed() -> None:
    lines = [line for line in SAVED if line["run_id"] == "b"]
    result = report(lines, "")
    assert result.on_killed == []
    assert result.void
    assert not result.passed


def test_the_control_is_counted_not_judged() -> None:
    result = report(LOST, LOST_LOG, retry=False)
    text = report_lines("retry-off", result)
    assert text[-1] == (
        "retry-off: control, completion 1 of 2 runs (50.0%), 1 failed by the kill ['a']"
    )


def test_the_report_says_whether_the_retry_passed_failed_or_was_void() -> None:
    assert report_lines("retry-on", report(SAVED, SAVED_LOG))[-1] == (
        "retry-on: passed, completion 2 of 2 runs (100.0%)"
    )
    lost = report(LOST, LOST_LOG)
    assert report_lines("retry-on", lost)[-1] == (
        "retry-on: failed, completion 1 of 2 runs (50.0%), 1 failed by the kill ['a']"
    )
    void = report([line for line in SAVED if line["run_id"] == "b"], "")
    assert report_lines("retry-on", void)[-1] == (
        "retry-on: void, no call was running on the killed pod at the kill"
    )


def test_retry_starts_are_each_calls_attempts_by_run_and_step() -> None:
    log = "\n".join(
        [
            retried("a", 2, 1, 10),
            "replay: 1/2 b csv: 2 calls, pods ['sim-b']",
            retried("a", 2, 2, 20),
            retried("b", 1, 1, 30),
        ]
    )
    assert retry_starts(log) == {("a", 2): [10, 20], ("b", 1): [30]}


def test_a_variant_profile_switches_the_replays_retry_alone() -> None:
    profile = {"replay": {"retry": True, "seed": 1}, "agent": {"llm_retries": 2}}
    off = variant_profile(profile, retry=False)
    assert off == {"replay": {"retry": False, "seed": 1}, "agent": {"llm_retries": 2}}
    assert profile["replay"]["retry"] is True


SIM_MODEL = {"name": "Qwen/Qwen3-0.6B-GPTQ-Int8", "scheduling_policy": "fcfs"}
REAL_MODEL = {**SIM_MODEL, "scheduling_policy": "priority", "lora": {"name": "thesis-adapter"}}


def test_the_pods_must_be_the_files_profiles() -> None:
    """make sim-deploy installs the sim chart, make vllm-deploy the vllm one, each with the
    profile's model section among its values (Makefile); the agent, whose ConfigMap holds the
    profile, does not run on sim."""
    check_pool("sim", {"model": SIM_MODEL}, "sim-0.1.0", {"model": SIM_MODEL})
    check_pool("real", {"model": REAL_MODEL}, "vllm-0.1.0", {"model": REAL_MODEL})


@pytest.mark.parametrize(
    ("name", "model", "chart", "values"),
    [
        ("sim", SIM_MODEL, "vllm-0.1.0", {"model": SIM_MODEL}),
        ("real", REAL_MODEL, "sim-0.1.0", {"model": REAL_MODEL}),
        ("real", REAL_MODEL, "vllm-0.1.0", {"model": SIM_MODEL}),
        ("sim", SIM_MODEL, "sim-0.1.0", {}),
    ],
    ids=["sim on vllm's chart", "real on the sim's chart", "another model", "no model"],
)
def test_pods_of_another_profile_are_refused(
    name: str, model: dict[str, Any], chart: str, values: dict[str, Any]
) -> None:
    with pytest.raises(FailoverError, match=f"make up PROFILE={name}"):
        check_pool(name, {"model": model}, chart, values)


def experiment_file(tmp_path: Path, **changes: Any) -> Path:
    spec: dict[str, Any] = {
        "profile": "sim",
        "routing": {"plugin": True, "affinity": True},
        "trace": str(TRACE),
        "pattern": "burst",
        "seed": 6,
        "kill_after_seconds": 125,
        "wait_for_calls_seconds": 60,
        "settle_seconds": 5,
        "variants": [{"name": "retry-off", "retry": False}, {"name": "retry-on", "retry": True}],
    }
    spec.update(changes)
    path = tmp_path / "failover.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_the_experiment_file_is_read_whole(tmp_path: Path) -> None:
    experiment = load_failover(experiment_file(tmp_path))
    assert experiment.name == "failover"
    assert experiment.profile == "sim"
    assert experiment.routing == {"plugin": True, "affinity": True}
    assert experiment.trace == TRACE
    assert (experiment.pattern, experiment.seed) == ("burst", 6)
    assert experiment.kill_after_seconds == 125
    assert experiment.wait_for_calls_seconds == 60
    assert experiment.settle_seconds == 5
    assert [(v.name, v.retry) for v in experiment.variants] == [
        ("retry-off", False),
        ("retry-on", True),
    ]
    assert len(experiment.hash) == 12


@pytest.mark.parametrize(
    "changes",
    [
        {"routing": {"plugin": True, "affinity": False}},
        {"routing": {"plugin": False, "affinity": True}},
        {"kill_after_seconds": 0},
        {"wait_for_calls_seconds": -1},
        {"settle_seconds": -1},
        {"trace": "missing.jsonl"},
        {"variants": []},
        {"variants": [{"name": "a", "retry": True}, {"name": "a", "retry": False}]},
        {"variants": [{"name": "a", "retry": "on"}]},
    ],
)
def test_an_experiment_file_that_could_not_show_a_failover_is_refused(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(FailoverError):
        load_failover(experiment_file(tmp_path, **changes))


def picker_with_pins() -> str:
    """Two runs pinned to the killed pod and one to the kept, logged now: inside a replay's window,
    which starts at the run's own clock."""
    at = datetime.now(UTC).isoformat()
    runs = [("r1", KILLED), ("r2", KILLED), ("r3", KEPT)]
    return "\n".join(picked(run, pod, "first", at=at) for run, pod in runs)


PROFILE: dict[str, Any] = {
    "replicas": 2,
    "routing": {"plugin": False, "affinity": False},
    "replay": {"retry": True},
    "agent": {"llm_retries": 2, "llm_retry_backoff_seconds": 5},
    "model": SIM_MODEL,
}


class FakeCluster:
    """The module's cluster calls, recorded in order. kubectl's replies follow its first word."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        replay_seconds: float = 5,
        picker: str | None = None,
        failing: tuple[str, ...] = (),
        fail_replay: bool = False,
        pool_model: dict[str, Any] = SIM_MODEL,
    ):
        self.calls: list[Any] = []
        # Kills taken so far, and how many there should be once this variant's has come.
        self.killed = threading.Condition()
        self.kills = 0
        self.wanted = 1
        self.killed_ms = KILLED_MS
        self.replay_seconds = replay_seconds
        self.picker = picker
        self.failing = failing
        self.fail_replay = fail_replay
        self.pool_model = pool_model
        # The profile each replay was given.
        self.profiles: list[dict[str, Any]] = []
        target = "load_generator.failover"
        monkeypatch.setattr(f"{target}.set_routing", self.set_routing)
        monkeypatch.setattr(f"{target}.kubectl", self.kubectl)
        monkeypatch.setattr(f"{target}.helm", self.helm)
        monkeypatch.setattr(f"{target}.replay_to_file", self.replay)
        monkeypatch.setattr(f"{target}.run_command", self.run_command)
        monkeypatch.setattr(f"{target}.PICKER_POLL_SECONDS", 0.01)

    def set_routing(self, profile: str, values: Any, routing: Any) -> None:
        self.calls.append(("set_routing", profile, routing))

    def run_command(self, command: list[str]) -> None:
        self.calls.append(("run", *command[:2]))
        self.wanted = self.kills + 1

    def kubectl(self, *args: str) -> str:
        self.calls.append(("kubectl", *args[:2]))
        if args[0] in self.failing:
            raise subprocess.CalledProcessError(1, ["kubectl", *args], stderr="kubectl failed")
        if args[0] == "delete":
            with self.killed:
                self.kills += 1
                self.killed_ms = round(time.time() * 1000)
                self.killed.notify_all()
        if args[:2] == ("get", "deployment"):
            return json.dumps({"spec": {"selector": {"matchLabels": {"app": "epp"}}}})
        if args[:2] == ("get", "pods") and args[3] == "app=epp":
            return json.dumps({"items": [pod_item("epp-1", restarts=(0,))]})
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": [pod_item(KEPT), pod_item(KILLED)]})
        if args[0] == "logs":
            return picker_with_pins() if self.picker is None else self.picker
        return ""

    def helm(self, *args: str) -> str:
        self.calls.append(("helm", *args[:2]))
        if args[0] == "list":
            return json.dumps([{"name": "vllm", "chart": "sim-0.1.0"}])
        return json.dumps({"replicas": 2, "model": self.pool_model})

    def replay(self, profile: dict[str, Any], *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("replay",))
        self.profiles.append(profile)
        with self.killed:
            self.killed.wait_for(lambda: self.kills >= self.wanted, self.replay_seconds)
        if self.fail_replay:
            raise RuntimeError("replay died")
        print(retried("a", 2, 1, self.killed_ms - 3_000))
        shift = self.killed_ms - KILLED_MS
        return [{**line, "ts_start": line["ts_start"] + shift} for line in SAVED]


def pod_item(name: str, restarts: tuple[int, ...] = ()) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "status": {
            "podIP": "10.0.0.1",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"restartCount": count} for count in restarts],
        },
    }


def test_kill_force_deletes_the_busiest_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = FakeCluster(monkeypatch)
    state = Kill(ready=[KEPT, KILLED], since="2026-11-19T11:00:00+00:00")
    kill_pod(state, wait_for_calls_seconds=1)
    assert state.pod == KILLED
    assert state.pins == {KEPT: 1, KILLED: 2}
    assert state.killed_ms is not None
    assert ("kubectl", "delete", "pod") in cluster.calls


def test_the_kill_is_a_failure_not_a_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain delete runs the pod's preStop drain (P4.3); grace 0 with --force skips it: the
    kubelet then gives ~1 s of preStop and SIGKILL 2 s after SIGTERM (Kubernetes v1.36.5)."""
    asked: list[tuple[str, ...]] = []
    cluster = FakeCluster(monkeypatch)

    def kubectl(*args: str) -> str:
        asked.append(args)
        return cluster.kubectl(*args)

    monkeypatch.setattr("load_generator.failover.kubectl", kubectl)
    kill_pod(Kill(ready=[KEPT, KILLED], since="2026-11-19T11:00:00+00:00"), 1)
    (delete,) = [args for args in asked if args[0] == "delete"]
    assert delete[:3] == ("delete", "pod", KILLED)
    assert {"--grace-period=0", "--force", "--wait=false"} <= set(delete)


def test_kill_waits_for_a_pin_and_gives_up_after_its_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = FakeCluster(monkeypatch, picker="")
    state = Kill(ready=[KEPT, KILLED], since="2026-11-19T11:00:00+00:00")
    kill_pod(state, wait_for_calls_seconds=0.05)
    assert state.pod is None
    assert ("kubectl", "delete", "pod") not in cluster.calls
    assert cluster.calls.count(("kubectl", "logs", "deployment/vllm-pool-epp")) > 1


def test_kill_keeps_a_failed_kubectl_for_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCluster(monkeypatch, failing=("delete",))
    state = Kill(ready=[KEPT, KILLED], since="2026-11-19T11:00:00+00:00")
    kill_pod(state, wait_for_calls_seconds=1)
    assert state.error == "kubectl delete failed: kubectl failed"
    assert state.killed_ms is None


def test_every_variant_gets_its_retry_and_the_routing_comes_back_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch)
    experiment = load_failover(experiment_file(tmp_path, kill_after_seconds=0.01))
    results = run_failover(experiment, PROFILE, tmp_path)
    assert [result.variant.name for result in results] == ["retry-off", "retry-on"]
    assert all(result.kill.pod == KILLED for result in results)
    assert [p["replay"]["retry"] for p in cluster.profiles] == [False, True]
    runs = [call for call in cluster.calls if call[0] == "run"]
    restart = ("run", "bash", str(REPO_ROOT / "infra" / "restart-vllm.sh"))
    assert runs == [restart, restart]
    rules = {"plugin": True, "affinity": True}
    assert cluster.calls.count(("set_routing", "sim", rules)) == 2
    assert cluster.calls[-1] == ("set_routing", "sim", None)


def test_the_pickers_log_is_kept_before_the_replacement_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3.4's review: a wait that fails after the replay must not lose the picker's log, which the
    routing switch back restarts away."""
    cluster = FakeCluster(monkeypatch, failing=("rollout",))
    experiment = load_failover(experiment_file(tmp_path, kill_after_seconds=0.01))
    results = run_failover(experiment, PROFILE, tmp_path)
    verbs = [call[1:3] for call in cluster.calls if call[0] == "kubectl"]
    first_rollout = verbs.index(("rollout", "status"))
    assert ("logs", "deployment/vllm-pool-epp") in verbs[:first_rollout]
    assert results[0].picker_path.exists()
    assert results[0].replacement_error == "kubectl rollout failed: kubectl failed"


def test_a_replay_that_dies_puts_the_routing_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, fail_replay=True)
    experiment = load_failover(experiment_file(tmp_path, kill_after_seconds=0.01))
    with pytest.raises(RuntimeError, match="replay died"):
        run_failover(experiment, PROFILE, tmp_path)
    assert cluster.calls[-1] == ("set_routing", "sim", None)


def test_a_cluster_on_another_profile_is_refused_before_anything_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, pool_model=REAL_MODEL)
    experiment = load_failover(experiment_file(tmp_path))
    with pytest.raises(FailoverError, match="make up PROFILE=sim"):
        run_failover(experiment, PROFILE, tmp_path)
    assert [call[0] for call in cluster.calls] == ["helm", "helm"]


def test_a_profile_with_one_pod_is_refused_before_the_cluster_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch)
    experiment = load_failover(experiment_file(tmp_path))
    with pytest.raises(FailoverError, match="at least 2"):
        run_failover(experiment, {**PROFILE, "replicas": 1}, tmp_path)
    assert cluster.calls == []


def test_a_kill_threads_traceback_goes_to_the_kept_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeCluster(monkeypatch, replay_seconds=0.2)
    # Python's own hook, as outside pytest, which installs one that keeps the exception for itself.
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)

    def broken(state: Kill, wait: float) -> None:
        state.started = True
        raise KeyError("a bug in the kill")

    monkeypatch.setattr("load_generator.failover.kill_pod", broken)
    experiment = load_failover(experiment_file(tmp_path, kill_after_seconds=0.01))
    results = run_failover(experiment, PROFILE, tmp_path)
    log = (tmp_path / f"failover-{experiment.hash}-retry-off.jsonl.log").read_text()
    assert "a bug in the kill" in log
    assert results[0].kill.pod is None


def main_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sim.yaml").write_text(yaml.safe_dump(PROFILE))
    (tmp_path / "experiments" / "results").mkdir(parents=True)
    path = experiment_file(tmp_path, kill_after_seconds=0.01, **changes)
    monkeypatch.setattr("load_generator.failover.REPO_ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["failover", str(path)])
    return path


def test_main_writes_each_variants_files_and_refuses_to_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeCluster(monkeypatch)
    path = main_setup(tmp_path, monkeypatch)
    main()
    experiment = load_failover(path)
    results = tmp_path / "experiments" / "results"
    stem = f"failover-{experiment.hash}"
    meta = json.loads((results / f"{stem}.meta.json").read_text())
    assert [variant["name"] for variant in meta["variants"]] == ["retry-off", "retry-on"]
    on = meta["variants"][1]
    assert (on["passed"], on["killed_pod"], on["completion_rate"]) == (True, KILLED, 1.0)
    for variant in ("retry-off", "retry-on"):
        assert (results / f"{stem}-{variant}-picker.log").exists()
        assert (results / f"{stem}-{variant}.jsonl.log").exists()
    with pytest.raises(FailoverError, match="exist"):
        main()


def test_main_fails_when_the_retry_variant_had_no_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeCluster(monkeypatch, picker="", replay_seconds=0.5)
    path = main_setup(tmp_path, monkeypatch, wait_for_calls_seconds=0.05)
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    experiment = load_failover(path)
    results = tmp_path / "experiments" / "results"
    meta = json.loads((results / f"failover-{experiment.hash}.meta.json").read_text())
    assert [variant["killed_pod"] for variant in meta["variants"]] == [None, None]
    assert "retry-on: void, no pod held a live pin, so none was killed" in capsys.readouterr().out


def test_a_control_is_never_passed_even_when_nothing_failed() -> None:
    """Without the retry the variant is counted, not judged."""
    assert not report(SAVED, SAVED_LOG, retry=False).passed


def test_only_a_remapped_pick_counts_as_moved_off_the_killed_pod() -> None:
    pinned_line = picked("a", KEPT, "pinned", source=KILLED)
    assert report(SAVED, SAVED_LOG, picker=pinned_line).remapped == []


def at_ms(ms: int) -> str:
    """A picker timestamp for a wall-clock time in ms, as ts_start counts it."""
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def first_call_picked(run: str, pod: str, ms: int) -> str:
    """The plugin's pick of a run's first call, logged at ms."""
    line = json.loads(picked(run, pod, "first", at=at_ms(ms)))
    return json.dumps({**line, "step": "1"})


def test_a_failed_first_call_counts_on_the_pod_the_picker_sent_it_to() -> None:
    """It has no pin to stand for its pod (failover-576b813b6b4c's tau2_airline runs)."""
    lines = [*SAVED, call("c", 1, KILLED_MS - 2_000, None)]
    log = "\n".join([SAVED_LOG, failed("c", 1, "RemoteProtocolError('cut')")])
    picker = "\n".join([REMAPPED_PICKER, first_call_picked("c", KILLED, KILLED_MS - 2_000)])
    result = report(lines, log, picker)
    assert "c step 1: failed, RemoteProtocolError('cut')" in result.on_killed
    assert result.failed_by_kill == ["c"]


def test_a_pick_after_the_kill_is_not_where_the_call_was_at_it() -> None:
    lines = [*SAVED, call("c", 1, KILLED_MS - 2_000, None)]
    log = "\n".join([SAVED_LOG, failed("c", 1, "RemoteProtocolError('cut')")])
    picker = "\n".join([REMAPPED_PICKER, first_call_picked("c", KILLED, KILLED_MS + 1_000)])
    assert not any(text.startswith("c ") for text in report(lines, log, picker).on_killed)


def test_a_retried_call_is_placed_by_its_pick_not_its_runs_pin() -> None:
    """Its first attempt went to the kept pod (a spill, P3.5), so it did not run on the killed one,
    though the run was pinned there."""
    spilled = json.loads(picked("a", KEPT, "spilled", at=at_ms(KILLED_MS - 3_000)))
    picker = "\n".join([REMAPPED_PICKER, json.dumps(spilled)])
    assert report(SAVED, SAVED_LOG, picker).on_killed == []
