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
    GroupCost,
    Kill,
    build_failover_report,
    check_pool,
    completion_over_seeds,
    kill_pod,
    load_failover,
    load_failover_seeds,
    main,
    recovery_cost,
    report_lines,
    retry_lost_ms,
    retry_starts,
    run_failover,
    run_times_ms,
    summary_row,
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


# P5.3 (card 77): one file over several seeds, each in its own order, with a no-kill variant that
# the kill variants' runs are paired with for their recovery cost.

SEEDS_VARIANTS = [
    {"name": "retry-off", "retry": False},
    {"name": "retry-on", "retry": True},
    {"name": "no-kill", "retry": True, "kill": False},
]
SEEDS = [
    {"seed": 6, "order": ["retry-off", "retry-on", "no-kill"]},
    {"seed": 9, "order": ["no-kill", "retry-off", "retry-on"]},
]


def seeds_file(tmp_path: Path, **changes: Any) -> Path:
    """experiment_file's file with SEEDS in place of its one seed, then the changes."""
    path = experiment_file(tmp_path)
    spec = yaml.safe_load(path.read_text())
    del spec["seed"]
    spec.update(variants=SEEDS_VARIANTS, seeds=SEEDS)
    spec.update(changes)
    path.write_text(yaml.safe_dump(spec))
    return path


def test_a_file_of_seeds_is_one_experiment_per_seed_in_its_order(tmp_path: Path) -> None:
    experiments = load_failover_seeds(seeds_file(tmp_path))
    assert [experiment.seed for experiment in experiments] == [6, 9]
    assert [[v.name for v in experiment.variants] for experiment in experiments] == [
        ["retry-off", "retry-on", "no-kill"],
        ["no-kill", "retry-off", "retry-on"],
    ]
    digest = experiments[0].hash
    assert [experiment.stem for experiment in experiments] == [
        f"failover-{digest}-s6",
        f"failover-{digest}-s9",
    ]
    no_kill = next(v for v in experiments[0].variants if v.name == "no-kill")
    assert (no_kill.retry, no_kill.kill) == (True, False)
    assert all(v.kill for v in experiments[0].variants if v.name != "no-kill")


def test_a_file_of_one_seed_keeps_its_results_name(tmp_path: Path) -> None:
    """P4.4's form, whose results are named without the seed (failover-aa3c44493359-*)."""
    (experiment,) = load_failover_seeds(experiment_file(tmp_path))
    assert experiment.stem == f"failover-{experiment.hash}"
    assert experiment == load_failover(experiment_file(tmp_path))


def test_load_failover_refuses_a_file_of_seeds(tmp_path: Path) -> None:
    with pytest.raises(FailoverError, match="seeds"):
        load_failover(seeds_file(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": 6},
        {"seeds": []},
        {"seeds": [{"seed": 6, "order": ["retry-off", "retry-on"]}]},
        {"seeds": [{"seed": 6, "order": ["retry-off", "retry-on", "no-kill", "no-kill"]}]},
        {"seeds": [{"seed": 6, "order": ["retry-off", "retry-on", "other"]}]},
        {"seeds": [SEEDS[0], SEEDS[0]]},
        {"variants": [*SEEDS_VARIANTS[:2], {"name": "no-kill", "retry": False, "kill": False}]},
        {
            "variants": [*SEEDS_VARIANTS, {"name": "no-kill-2", "retry": True, "kill": False}],
            "seeds": [{"seed": 6, "order": ["retry-off", "retry-on", "no-kill", "no-kill-2"]}],
        },
        {"variants": [SEEDS_VARIANTS[2]], "seeds": [{"seed": 6, "order": ["no-kill"]}]},
        {"variants": [*SEEDS_VARIANTS[:2], {"name": "no-kill", "retry": True, "kill": "no"}]},
    ],
    ids=[
        "seed and seeds",
        "no seeds",
        "an order without a variant",
        "a variant twice in an order",
        "an order naming no variant",
        "a seed twice",
        "a no-kill without the retry",
        "two no-kill variants",
        "nothing killed",
        "kill not true or false",
    ],
)
def test_a_file_of_seeds_that_could_not_show_a_failover_is_refused(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(FailoverError):
        load_failover_seeds(seeds_file(tmp_path, **changes))


def timed(run: str, step: int, start: int, gap: int, duration: int | None) -> dict[str, Any]:
    """A replay line with its gap after the step before, on the replay's monotonic clock: for a
    retried call, from that step's end to its last attempt (replay._call)."""
    return {**call(run, step, start, duration, KEPT if duration else None), "gap_before_ms": gap}


def test_a_runs_time_is_its_steps_slowest_calls_and_gaps() -> None:
    lines = [
        timed("a", 1, 0, 0, 1_000),
        timed("a", 2, 3_000, 2_000, 4_000),
        timed("a", 2, 3_500, 2_500, 1_000),
        timed("a", 3, 9_000, 1_000, 500),
        timed("b", 1, 0, 0, 2_000),
        timed("b", 2, 5_000, 3_000, None),
    ]
    assert run_times_ms(lines, "") == {"a": 1_000 + 6_000 + 1_500, "b": None}


def test_a_retried_first_calls_lost_time_is_added_and_a_later_ones_is_in_its_gap() -> None:
    """A first call's gap is 0 whatever happened, so its failed attempt and backoff are read from
    the retry line's sent time, on the wall clock as ts_start. A later call's gap already runs to
    its last attempt."""
    lines = [
        timed("a", 1, 10_000, 0, 1_000),
        timed("a", 2, 20_000, 9_000, 1_000),
    ]
    log = "\n".join([retried("a", 1, 1, 2_000), retried("a", 2, 1, 14_000)])
    assert run_times_ms(lines, log) == {"a": 8_000 + 1_000 + 9_000 + 1_000}


def test_each_retried_steps_lost_time_runs_from_its_first_attempt_to_its_last() -> None:
    """A fanned-out step's calls share a key: the lost time ends at the latest of their starts."""
    lines = [
        timed("a", 1, 10_000, 0, 1_000),
        timed("a", 2, 20_000, 9_000, 1_000),
        timed("a", 2, 13_000, 2_000, 1_000),
        timed("b", 1, 5_000, 0, 1_000),
    ]
    log = "\n".join(
        [retried("a", 1, 1, 2_000), retried("a", 2, 1, 12_000), retried("a", 2, 2, 16_000)]
    )
    assert retry_lost_ms(lines, log) == {("a", 1): 8_000, ("a", 2): 8_000}


def test_the_recovery_cost_pairs_each_run_with_its_no_kill_time_by_group() -> None:
    """Runs with a call on the killed pod at the kill, runs only pinned there, and the rest, each
    counted once; a run that did not finish on either side is named, not paired."""
    killed: dict[str, int | None] = {"hit": 30_000, "hit2": 22_000, "pinned": 15_000}
    killed.update({"other": 11_000, "lost": None, "lost-before": 9_000})
    reference: dict[str, int | None] = {"hit": 10_000, "hit2": 12_000, "pinned": 12_000}
    reference.update({"other": 10_000, "lost": 9_000, "lost-before": None})
    cost = recovery_cost(
        killed,
        reference,
        on_killed_runs=["hit", "hit2"],
        affected=["hit", "pinned"],
        lost={("hit", 2): 8_000, ("hit2", 1): 6_000},
    )
    assert cost.on_killed == GroupCost(runs=2, median_ms=15_000, max_ms=20_000)
    assert cost.pinned == GroupCost(runs=1, median_ms=3_000, max_ms=3_000)
    assert cost.others == GroupCost(runs=1, median_ms=1_000, max_ms=1_000)
    assert cost.unpaired == ["lost", "lost-before"]
    assert (cost.retried_steps, cost.lost_ms) == (2, 14_000)


def test_a_group_with_no_paired_run_has_no_figures() -> None:
    cost = recovery_cost({"a": 1_000}, {"a": 900}, [], [], {})
    assert cost.on_killed == GroupCost(runs=0, median_ms=None, max_ms=None)
    assert cost.others == GroupCost(runs=1, median_ms=100, max_ms=100)


def test_a_no_kill_variant_kills_nothing_and_waits_for_no_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = FakeCluster(monkeypatch, replay_seconds=0.2)
    experiment = load_failover_seeds(seeds_file(tmp_path, kill_after_seconds=0.01))[1]
    results = run_failover(experiment, PROFILE, tmp_path)
    assert [result.variant.name for result in results] == ["no-kill", "retry-off", "retry-on"]
    assert [result.kill.pod for result in results] == [None, KILLED, KILLED]
    assert [p["replay"]["retry"] for p in cluster.profiles] == [True, False, True]
    assert cluster.calls.count(("kubectl", "delete", "pod")) == 2
    assert cluster.calls.count(("kubectl", "rollout", "status")) == 2
    assert (tmp_path / f"{experiment.stem}-no-kill-picker.log").exists()


class SeedsCluster(FakeCluster):
    """FakeCluster's replay with each line's gap, which the recovery cost reads."""

    def replay(self, profile: dict[str, Any], *args: Any) -> list[dict[str, Any]]:
        lines = super().replay(profile, *args)
        return [{**line, "gap_before_ms": 0 if line["step"] == 1 else 1_000} for line in lines]


def seeds_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main on a file of seeds 6 and 9, in place of main_setup's file of one."""
    path = main_setup(tmp_path, monkeypatch)
    assert seeds_file(tmp_path, kill_after_seconds=0.01) == path
    monkeypatch.setattr(
        "load_generator.failover.source_state",
        lambda root: {"commit": "c0ffee", "uncommitted": [], "system_sha256": "5157e3"},
    )
    return path


def test_main_writes_each_seeds_results_with_its_recovery_and_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SeedsCluster(monkeypatch, replay_seconds=0.2)
    path = seeds_main(tmp_path, monkeypatch)
    main()
    digest = load_failover_seeds(path)[0].hash
    results = tmp_path / "experiments" / "results"
    for seed in (6, 9):
        meta = json.loads((results / f"failover-{digest}-s{seed}.meta.json").read_text())
        assert meta["seed"] == seed
        assert meta["source"]["system_sha256"] == "5157e3"
        by_name = {variant["name"]: variant for variant in meta["variants"]}
        assert by_name["no-kill"]["kill"] is False
        assert by_name["no-kill"]["verdict"] == "reference"
        assert by_name["retry-on"]["verdict"] == "passed"
        assert by_name["retry-off"]["verdict"] == "control"
        assert by_name["retry-on"]["recovery"]["on_killed"]["runs"] == 1
        assert "recovery" not in by_name["no-kill"]
        for variant in ("retry-off", "retry-on", "no-kill"):
            assert (results / f"failover-{digest}-s{seed}-{variant}.jsonl.log").exists()
            assert (results / f"failover-{digest}-s{seed}-{variant}-picker.log").exists()
    summary = (results / f"failover-{digest}-summary.csv").read_text().splitlines()
    assert summary[0].startswith("seed,variant,retry,kill,verdict,runs,done,completion_rate")
    assert len(summary) == 1 + 2 * 3


def test_main_skips_a_finished_seed_and_runs_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster = SeedsCluster(monkeypatch, replay_seconds=0.2)
    path = seeds_main(tmp_path, monkeypatch)
    main()
    digest = load_failover_seeds(path)[0].hash
    results = tmp_path / "experiments" / "results"
    for name in results.glob(f"failover-{digest}-s9*"):
        name.unlink()
    replays = cluster.calls.count(("replay",))
    main()
    assert cluster.calls.count(("replay",)) - replays == 3
    assert "failover: seed 6 finished before, skipped" in capsys.readouterr().out
    assert (results / f"failover-{digest}-s9.meta.json").exists()


def test_main_refuses_a_seed_left_halfway_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = SeedsCluster(monkeypatch, replay_seconds=0.2)
    path = seeds_main(tmp_path, monkeypatch)
    digest = load_failover_seeds(path)[0].hash
    results = tmp_path / "experiments" / "results"
    (results / f"failover-{digest}-s9-no-kill.jsonl.log").write_text("")
    with pytest.raises(FailoverError, match="seed 9"):
        main()
    assert cluster.calls == []


def test_the_summary_gives_each_variants_completion_over_the_seeds() -> None:
    """The mean, then the lowest and the highest, of each variant's completion rate."""

    def variant(name: str, done: int) -> dict[str, Any]:
        return {"name": name, "runs": 50, "done": done, "completion_rate": done / 50}

    metas = [
        {"seed": 6, "variants": [variant("retry-off", 45), variant("retry-on", 50)]},
        {"seed": 9, "variants": [variant("retry-off", 48), variant("retry-on", 49)]},
    ]
    assert completion_over_seeds(metas) == {
        "retry-off": pytest.approx((0.93, 0.9, 0.96)),
        "retry-on": pytest.approx((0.99, 0.98, 1.0)),
    }


def test_a_void_variant_has_no_completion_to_count() -> None:
    """No pod was killed, so no report counted its runs."""
    metas = [{"seed": 6, "variants": [{"name": "retry-on", "killed_pod": None}]}]
    assert completion_over_seeds(metas) == {}


# P5.3b (card 79): a kill that stops the pod's containers at once, before the force delete, and a
# verdict that needs a call the kill cut.


def test_a_file_may_stop_the_container_and_require_a_cut(tmp_path: Path) -> None:
    experiment = load_failover(experiment_file(tmp_path, stop_container=True, require_cut=True))
    assert (experiment.stop_container, experiment.require_cut) == (True, True)
    plain = load_failover(experiment_file(tmp_path))
    assert (plain.stop_container, plain.require_cut) == (False, False)


@pytest.mark.parametrize("changes", [{"stop_container": "yes"}, {"require_cut": 1}])
def test_stop_container_and_require_cut_must_be_true_or_false(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(FailoverError, match="true or false"):
        load_failover(experiment_file(tmp_path, **changes))


class FakeCrictl:
    """sudo -n k3s crictl's replies, its calls recorded in the cluster's list so their order with
    kubectl's shows."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        calls: list[Any],
        failing: tuple[str, ...] = (),
        sandboxes: str = "sandbox-1\n",
    ):
        self.calls = calls
        self.failing = failing
        self.sandboxes = sandboxes
        monkeypatch.setattr("load_generator.failover.crictl", self)

    def __call__(self, *args: str) -> str:
        self.calls.append(("crictl", *args))
        if args[0] in self.failing:
            command = ["sudo", "-n", "k3s", "crictl", *args]
            raise subprocess.CalledProcessError(1, command, stderr="crictl failed")
        if args[0] == "pods":
            return self.sandboxes
        if args[0] == "ps":
            return "container-1\ncontainer-2\n"
        return ""


def stopping_kill() -> Kill:
    return Kill(ready=[KEPT, KILLED], since="2026-11-19T11:00:00+00:00", stop_container=True)


def test_kill_stops_the_pods_containers_at_once_before_the_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """crictl stop with no timeout sends SIGKILL with no SIGTERM first, so every stream on the pod
    is cut at the kill; the force delete after it lets the ReplicaSet replace the pod."""
    cluster = FakeCluster(monkeypatch)
    FakeCrictl(monkeypatch, cluster.calls)
    state = stopping_kill()
    kill_pod(state, wait_for_calls_seconds=1)
    assert state.pod == KILLED
    asked = [call for call in cluster.calls if call[0] == "crictl" or call[1] == "delete"]
    assert asked == [
        ("crictl", "pods", "--name", f"^{KILLED}$", "--state", "ready", "-q"),
        ("crictl", "ps", "--pod", "sandbox-1", "--state", "running", "-q"),
        ("crictl", "stop", "--timeout", "0", "container-1", "container-2"),
        ("kubectl", "delete", "pod"),
    ]


def test_without_stop_container_the_kill_runs_no_crictl(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = FakeCluster(monkeypatch)
    FakeCrictl(monkeypatch, cluster.calls)
    kill_pod(Kill(ready=[KEPT, KILLED], since="2026-11-19T11:00:00+00:00"), 1)
    assert [call for call in cluster.calls if call[0] == "crictl"] == []
    assert ("kubectl", "delete", "pod") in cluster.calls


def test_a_failed_crictl_is_kept_for_the_report_and_nothing_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = FakeCluster(monkeypatch)
    FakeCrictl(monkeypatch, cluster.calls, failing=("stop",))
    state = stopping_kill()
    kill_pod(state, wait_for_calls_seconds=1)
    assert state.error == "crictl stop failed: crictl failed"
    assert (state.pod, state.killed_ms) == (None, None)
    assert ("kubectl", "delete", "pod") not in cluster.calls


@pytest.mark.parametrize("sandboxes", ["", "sandbox-1\nsandbox-2\n"], ids=["none", "two"])
def test_a_pod_without_exactly_one_ready_sandbox_is_not_killed(
    monkeypatch: pytest.MonkeyPatch, sandboxes: str
) -> None:
    cluster = FakeCluster(monkeypatch)
    FakeCrictl(monkeypatch, cluster.calls, sandboxes=sandboxes)
    state = stopping_kill()
    kill_pod(state, wait_for_calls_seconds=1)
    assert state.error is not None and "sandbox" in state.error
    assert state.pod is None
    assert ("kubectl", "delete", "pod") not in cluster.calls


def cut_report(lines: list[dict[str, Any]], log: str, retry: bool = True) -> Any:
    return build_failover_report(
        lines, log, REMAPPED_PICKER, KILLED, KILLED_MS, SETTLE_MS, retry, require_cut=True
    )


# Run a's step 2 ran on the killed pod at the kill and was answered by it before it died.
ANSWERED = [
    call("a", 1, KILLED_MS - 9_000, 2_000, KILLED),
    call("a", 2, KILLED_MS - 3_000, 3_500, KILLED),
    call("a", 3, KILLED_MS + 6_000, 2_000, KEPT),
]


def test_with_require_cut_a_kill_whose_calls_all_finished_is_void() -> None:
    """P5.3's seeds: every call on the killed pod answered by it, so the retry was not tested."""
    result = cut_report(ANSWERED, "")
    assert result.on_killed == [f"a step 2: answered by {KILLED}"]
    assert result.cut == []
    assert result.void
    assert result.verdict == "void"
    assert report_lines("retry-on", result)[-1] == (
        "retry-on: void, no call on the killed pod was cut by the kill"
    )


def test_without_require_cut_the_same_kill_passes_as_in_p4_4() -> None:
    result = report(ANSWERED, "")
    assert result.cut == []
    assert result.verdict == "passed"


def test_with_require_cut_a_cut_call_sent_again_passes() -> None:
    result = cut_report(SAVED, SAVED_LOG)
    assert result.cut == [f"a step 2: retried 1, answered by {KEPT}"]
    assert result.verdict == "passed"
    assert "  calls on it cut by the kill: 1" in report_lines("retry-on", result)


def test_a_cut_call_whose_run_failed_fails_the_retry() -> None:
    result = cut_report(LOST, LOST_LOG)
    assert result.cut == ["a step 2: failed, RemoteProtocolError('peer closed connection')"]
    assert result.verdict == "failed"


def test_a_call_sent_to_the_killed_pod_during_the_settle_and_cut_counts() -> None:
    """Run a is pinned to the killed pod; its step 2, sent 200 ms after the kill before the picker
    learnt of it, was refused, sent again and answered on the kept pod. It was not running at the
    kill, so it is cut but not on_killed."""
    lines = [
        call("a", 1, KILLED_MS - 9_000, 8_000, KILLED),
        call("a", 2, KILLED_MS + 5_200, 1_000, KEPT),
    ]
    result = cut_report(lines, retried("a", 2, 1, KILLED_MS + 200, "HTTPStatusError('503')"))
    assert result.on_killed == []
    assert result.cut == [f"a step 2: retried 1, answered by {KEPT}"]
    assert result.verdict == "passed"


def test_a_call_on_the_killed_pod_that_hit_its_deadline_was_not_cut() -> None:
    lines = [
        call("a", 1, KILLED_MS - 9_000, 2_000, KILLED),
        call("a", 2, KILLED_MS - 3_000, None),
    ]
    result = cut_report(lines, failed("a", 2, DEADLINE))
    assert len(result.on_killed) == 1
    assert result.cut == []
    assert result.void


def test_a_summary_row_counts_the_cut_calls() -> None:
    variant = {
        "name": "retry-on",
        "retry": True,
        "kill": True,
        "verdict": "passed",
        "failed_by_kill": [],
        "on_killed": ["a step 2: answered by sim-a", "b step 3: retried 1, answered by sim-b"],
        "cut": ["b step 3: retried 1, answered by sim-b"],
    }
    row = summary_row(6, variant)
    assert (row["calls_on_killed_pod"], row["cut_calls"]) == (2, 1)
    # P5.3's .meta.json files have no cut list.
    p5_3 = {key: value for key, value in variant.items() if key != "cut"}
    assert "cut_calls" not in summary_row(6, p5_3)


def test_main_stops_the_container_and_judges_by_the_cut_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = SeedsCluster(monkeypatch, replay_seconds=0.2)
    FakeCrictl(monkeypatch, cluster.calls)
    path = seeds_main(tmp_path, monkeypatch)
    seeds_file(tmp_path, kill_after_seconds=0.01, stop_container=True, require_cut=True)
    main()
    digest = load_failover_seeds(path)[0].hash
    results = tmp_path / "experiments" / "results"
    meta = json.loads((results / f"failover-{digest}-s6.meta.json").read_text())
    assert (meta["stop_container"], meta["require_cut"]) == (True, True)
    by_name = {variant["name"]: variant for variant in meta["variants"]}
    assert by_name["retry-on"]["cut"] == [f"a step 2: retried 1, answered by {KEPT}"]
    assert by_name["retry-on"]["verdict"] == "passed"
    # The check before anything runs, then three crictl calls for each of 4 kills.
    assert cluster.calls[0] == ("crictl", "version")
    assert sum(call[:2] == ("crictl", "stop") for call in cluster.calls) == 4
    summary = (results / f"failover-{digest}-summary.csv").read_text().splitlines()
    header = summary[0].split(",")
    assert summary[1].split(",")[header.index("cut_calls")] == "1"


def test_main_refuses_to_stop_containers_without_sudo_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = SeedsCluster(monkeypatch, replay_seconds=0.2)
    FakeCrictl(monkeypatch, cluster.calls, failing=("version",))
    seeds_main(tmp_path, monkeypatch)
    seeds_file(tmp_path, kill_after_seconds=0.01, stop_container=True)
    with pytest.raises(FailoverError, match="sudo -v"):
        main()
    assert cluster.calls == [("crictl", "version")]


def test_main_without_stop_container_asks_no_sudo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = SeedsCluster(monkeypatch, replay_seconds=0.2)
    FakeCrictl(monkeypatch, cluster.calls)
    seeds_main(tmp_path, monkeypatch)
    main()
    assert [call for call in cluster.calls if call[0] == "crictl"] == []


def test_a_call_retried_on_the_kept_pod_was_not_cut_by_the_kill() -> None:
    """Run b stays on the kept pod; its step 2, running there at the kill, got a 503 and was sent
    again. Nothing on the killed pod was cut."""
    lines = [
        *ANSWERED,
        call("b", 1, KILLED_MS - 8_000, 2_000, KEPT),
        call("b", 2, KILLED_MS + 4_000, 1_000, KEPT),
    ]
    result = cut_report(lines, retried("b", 2, 1, KILLED_MS - 1_000, "HTTPStatusError('503')"))
    assert result.cut == []
    assert result.void


class AnsweredCluster(FakeCluster):
    """Every call on the killed pod answered by it before it died, as at P5.3's seeds."""

    def replay(self, profile: dict[str, Any], *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("replay",))
        self.profiles.append(profile)
        with self.killed:
            self.killed.wait_for(lambda: self.kills >= self.wanted, self.replay_seconds)
        shift = self.killed_ms - KILLED_MS
        return [
            {
                **line,
                "ts_start": line["ts_start"] + shift,
                "gap_before_ms": 0 if line["step"] == 1 else 1_000,
            }
            for line in ANSWERED
        ]


def test_main_judges_a_kill_that_cut_nothing_void_when_the_file_requires_a_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = AnsweredCluster(monkeypatch, replay_seconds=0.2)
    FakeCrictl(monkeypatch, cluster.calls)
    path = seeds_main(tmp_path, monkeypatch)
    seeds_file(tmp_path, kill_after_seconds=0.01, stop_container=True, require_cut=True)
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    digest = load_failover_seeds(path)[0].hash
    results = tmp_path / "experiments" / "results"
    meta = json.loads((results / f"failover-{digest}-s6.meta.json").read_text())
    by_name = {variant["name"]: variant for variant in meta["variants"]}
    assert len(by_name["retry-on"]["on_killed"]) == 1
    assert (by_name["retry-on"]["cut"], by_name["retry-on"]["verdict"]) == ([], "void")
