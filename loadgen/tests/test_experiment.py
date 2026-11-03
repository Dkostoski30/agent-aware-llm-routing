"""make experiment (P2.5): the experiment file, the order of the steps, and what a run writes.

The cluster is a fake that records what it is asked, so these tests say what must happen around a
replay and in which order. The live path (helm, kubectl, the gateway) is what make experiment's
done-when proves.
"""

import asyncio
import csv
import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from load_generator.experiment import (
    Experiment,
    ExperimentError,
    Warmup,
    check_journal_readable,
    count_clock_jumps,
    load_experiment,
    main,
    outputs,
    pods_to_warm,
    refuse_leftovers,
    report_lines,
    run_experiment,
    served_config_file,
    warm_pod,
    warmed_ttft_ms,
    write_results,
)
from load_generator.plan import (
    Pattern,
    choose_runs,
    load_replay_config,
    load_trace,
    start_times,
    warmup_run,
)
from load_generator.record import experiment_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUND_ROBIN_VALUES = REPO_ROOT / "infra" / "helm" / "router-round-robin-values.yaml"
BASELINE = REPO_ROOT / "experiments" / "baseline.yaml"
BASELINE_CHECK = REPO_ROOT / "experiments" / "baseline-check.yaml"
BASELINE_REVERSED = REPO_ROOT / "experiments" / "baseline-reversed.yaml"
APB_TRACE = "convert-apb-ca5fc26bbb1d-cd81f326aece"
TRACE_DIR = REPO_ROOT / "experiments" / "results" / "traces"


def trace_file(directory: Path) -> Path:
    path = directory / "trace.jsonl"
    lines = [
        {
            "schema_version": 1,
            "run_id": run,
            "task_type": "csv",
            "step": step,
            "ts_start": 0,
            "gap_before_ms": 0 if step == 1 else 2,
            "expected_steps": 2,
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "duration_ms": 100,
        }
        for run in ("a", "b", "c")
        for step in (1, 2)
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def experiment_file(directory: Path, **changes: Any) -> Path:
    values = directory / "round-robin-values.yaml"
    values.write_text("router: {}\n")
    trace_file(directory)
    spec: dict[str, Any] = {
        "profile": "real",
        "trace": "trace.jsonl",
        "pattern": "baseline",
        "seed": 6,
        "kv_capacity_tokens_per_pod": 4384,
        "warmup": {"runs_discarded": 1, "calls_per_pod": 2, "words": 10, "completion_tokens": 3},
        "variants": [
            {
                "name": "round-robin",
                "values": "round-robin-values.yaml",
                "epp_config": "round-robin.yaml",
            },
            {"name": "default", "values": None, "epp_config": "default-plugins.yaml"},
        ],
    }
    spec.update(changes)
    path = directory / "exp.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def replay_lines(pod: str = "pod-a") -> list[dict[str, Any]]:
    """Runs 1 and 2 of a plan, one call each, and run 2 on the given pod."""
    return [
        {
            "schema_version": 1,
            "run_id": f"a-{position}",
            "task_type": "csv",
            "step": 1,
            "ts_start": 0,
            "gap_before_ms": 1,
            "expected_steps": 2,
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "duration_ms": 100,
            "ttft_ms": 10,
            "cached_tokens": 16,
            "pod": "pod-a" if position == 1 else pod,
        }
        for position in (1, 2)
    ]


class FakeCluster:
    def __init__(
        self,
        replays: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        running_config: Callable[[Path | None], str] | None = None,
        fail_replay_of: str | None = None,
        fail_restart_number: int | None = None,
        fail_switch_number: int | None = None,
    ) -> None:
        self.asked: list[str] = []
        self.replays = replays
        self.fail_replay_of = fail_replay_of
        self.fail_restart_number = fail_restart_number
        self.fail_switch_number = fail_switch_number
        self.values: Path | None = None
        self.running_config = running_config or (
            lambda values: "default-plugins.yaml" if values is None else "round-robin.yaml"
        )

    def check_ready(self) -> None:
        self.asked.append("check")

    def restart_pods(self) -> None:
        self.asked.append("restart")
        if self.asked.count("restart") == self.fail_restart_number:
            raise RuntimeError("the restart broke")

    def set_routing(self, values: Path | None) -> None:
        name = f"routing {values.name if values else 'default'}"
        self.asked.append(name)
        switches = sum(step.startswith("routing") for step in self.asked)
        if switches == self.fail_switch_number:
            raise RuntimeError("the switch broke")
        self.values = values

    def epp_config_file(self) -> str:
        self.asked.append("config")
        return self.running_config(self.values)

    def warm_pods(self) -> dict[str, list[int]]:
        self.asked.append("warm")
        return {"pod-a": [9000, 100], "pod-b": [8000, 90]}

    def replay(self, out: Path) -> list[dict[str, Any]]:
        self.asked.append(f"replay {out.name}")
        if self.fail_replay_of and self.fail_replay_of in out.name:
            raise RuntimeError("the replay broke")
        variant = out.stem.rsplit("-", 1)[-1]
        lines = (self.replays or {}).get(variant, replay_lines())
        out.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return [dict(line) for line in lines]

    def clock_jumps(self, since: float, until: float) -> int:
        self.asked.append(f"jumps {since:.0f} {until:.0f}")
        return 3


def ticking_clock() -> Callable[[], float]:
    now = [100.0]

    def clock() -> float:
        now[0] += 1
        return now[0]

    return clock


def loaded(directory: Path, **changes: Any) -> Experiment:
    return load_experiment(experiment_file(directory, **changes), {"baseline": PATTERN})


PATTERN = Pattern(runs=2, rate_per_second=0.02, burst=None, fan_out=None)


def test_the_file_gives_the_experiment(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    assert experiment.name == "exp"
    assert experiment.profile == "real"
    assert experiment.trace == tmp_path / "trace.jsonl"
    assert experiment.pattern_name == "baseline"
    assert experiment.pattern == PATTERN
    assert experiment.seed == 6
    assert experiment.kv_capacity_tokens_per_pod == 4384
    assert experiment.warmup == Warmup(
        runs_discarded=1, calls_per_pod=2, words=10, completion_tokens=3
    )
    assert [variant.name for variant in experiment.variants] == ["round-robin", "default"]
    assert experiment.variants[0].values == tmp_path / "round-robin-values.yaml"
    assert experiment.variants[0].epp_config == "round-robin.yaml"
    assert experiment.variants[1].values is None


def test_the_hash_is_of_the_file_so_any_edit_changes_the_stamp(tmp_path: Path) -> None:
    path = experiment_file(tmp_path)
    before = load_experiment(path, {"baseline": PATTERN}).hash
    assert before == experiment_hash(path)
    path.write_text(path.read_text() + "# an edit\n")
    assert load_experiment(path, {"baseline": PATTERN}).hash != before


def test_a_pattern_the_profile_lacks_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExperimentError, match="no-such"):
        load_experiment(experiment_file(tmp_path, pattern="no-such"), {"baseline": PATTERN})


def test_discarding_every_run_is_refused(tmp_path: Path) -> None:
    warmup = {"runs_discarded": 2, "calls_per_pod": 2, "words": 10, "completion_tokens": 3}
    with pytest.raises(ExperimentError, match="runs_discarded"):
        loaded(tmp_path, warmup=warmup)


def test_two_variants_of_one_name_are_refused(tmp_path: Path) -> None:
    variant = {"name": "same", "values": None, "epp_config": "default-plugins.yaml"}
    with pytest.raises(ExperimentError, match="same"):
        loaded(tmp_path, variants=[variant, variant])


def test_a_values_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    variant = {"name": "gone", "values": "gone.yaml", "epp_config": "x.yaml"}
    with pytest.raises(ExperimentError, match=r"gone\.yaml"):
        loaded(tmp_path, variants=[variant])


def test_a_trace_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExperimentError, match=r"missing\.jsonl"):
        loaded(tmp_path, trace="missing.jsonl")


def test_every_output_carries_the_name_and_hash(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    stem = f"exp-{experiment.hash}"
    assert out.csv == tmp_path / "experiments" / "results" / f"{stem}.csv"
    assert out.meta == tmp_path / "experiments" / "results" / f"{stem}.meta.json"
    assert out.graph == tmp_path / "experiments" / "graphs" / f"{stem}.png"
    assert out.replay("default") == tmp_path / "experiments" / "results" / f"{stem}-default.jsonl"


@pytest.mark.parametrize(
    "leftover",
    ["exp-{hash}.csv", "exp-{hash}.meta.json", "exp-{hash}-default.jsonl",
     "exp-{hash}-default.jsonl.partial"],
)  # fmt: skip
def test_an_earlier_result_stops_the_experiment_before_it_touches_the_cluster(
    tmp_path: Path, leftover: str
) -> None:
    experiment = loaded(tmp_path)
    results = tmp_path / "experiments" / "results"
    results.mkdir(parents=True)
    (results / leftover.format(hash=experiment.hash)).write_text("kept")
    with pytest.raises(ExperimentError, match=experiment.hash):
        refuse_leftovers(experiment, outputs(experiment, tmp_path))


def test_a_graph_left_behind_stops_it_too(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    graphs = tmp_path / "experiments" / "graphs"
    graphs.mkdir(parents=True)
    (graphs / f"exp-{experiment.hash}.png").write_text("kept")
    with pytest.raises(ExperimentError, match=experiment.hash):
        refuse_leftovers(experiment, outputs(experiment, tmp_path))


def test_nothing_left_behind_passes(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    refuse_leftovers(experiment, outputs(experiment, tmp_path))


def test_each_variant_starts_from_restarted_pods_and_warm_ones_in_this_order(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    stem = f"exp-{experiment.hash}"
    assert cluster.asked == [
        "check",
        "restart",
        "routing round-robin-values.yaml",
        "config",
        "warm",
        f"replay {stem}-round-robin.jsonl",
        "jumps 101 102",
        "restart",
        "routing default",
        "config",
        "warm",
        f"replay {stem}-default.jsonl",
        "jumps 103 104",
    ]


def test_ending_on_the_default_does_not_switch_routing_again(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked.count("routing default") == 1


def test_ending_on_another_variant_puts_the_default_back(tmp_path: Path) -> None:
    variant = {
        "name": "round-robin",
        "values": "round-robin-values.yaml",
        "epp_config": "round-robin.yaml",
    }
    experiment = loaded(tmp_path, variants=[variant])
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked[-1] == "routing default"
    assert cluster.values is None


def test_a_replay_that_breaks_still_puts_the_default_back(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_replay_of="round-robin")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-1] == "routing default"
    assert cluster.values is None


def test_a_restart_that_breaks_after_another_variant_still_puts_the_default_back(
    tmp_path: Path,
) -> None:
    """The next variant is the default, but its switch has not happened yet."""
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_restart_number=2)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="restart broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-1] == "routing default"
    assert cluster.values is None


def test_a_switch_back_that_breaks_is_tried_again(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_switch_number=2)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="switch broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked.count("routing default") == 2
    assert cluster.values is None


def test_a_first_switch_that_breaks_halfway_is_put_back_too(tmp_path: Path) -> None:
    """helm may have changed the picker before it failed."""
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_switch_number=1)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="switch broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-2:] == ["routing round-robin-values.yaml", "routing default"]
    assert "warm" not in cluster.asked


def test_a_restart_that_breaks_before_any_switch_leaves_routing_alone(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_restart_number=1)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="restart broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert not any(step.startswith("routing") for step in cluster.asked)


def test_a_failed_switch_back_does_not_lose_a_finished_experiment(tmp_path: Path) -> None:
    """baseline-reversed ends on another variant, so its last helm call comes after a 1.5 h run."""
    variant = {
        "name": "round-robin",
        "values": "round-robin-values.yaml",
        "epp_config": "round-robin.yaml",
    }
    experiment = loaded(tmp_path, variants=[variant])
    cluster = FakeCluster(fail_switch_number=2)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="switch broke"):
        run_experiment(
            experiment,
            out,
            cluster,
            ticking_clock(),
            lambda rows, meta: write_results(out, rows, meta),
        )
    assert out.csv.exists()
    assert json.loads(out.meta.read_text())["hash"] == experiment.hash
    assert out.graph.exists()


def test_main_prints_the_report_before_the_default_is_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the switch back fails, the log's numbers are still on the screen."""
    variants = [
        {"name": "default", "values": None, "epp_config": "default-plugins.yaml"},
        {
            "name": "round-robin",
            "values": "round-robin-values.yaml",
            "epp_config": "round-robin.yaml",
        },
    ]
    path = experiment_file(tmp_path, variants=variants)
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    profile["replay"]["patterns"]["baseline"] = {"runs": 2, "rate_per_second": 0.02}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "real.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "load_generator.experiment.LiveCluster",
        lambda _experiment, _profile: FakeCluster(fail_switch_number=3),
    )
    monkeypatch.setattr("sys.argv", ["experiment", str(path)])
    with pytest.raises(RuntimeError, match="switch broke"):
        main()
    assert "runs finished" in capsys.readouterr().out
    experiment = load_experiment(path, {"baseline": PATTERN})
    assert outputs(experiment, tmp_path).csv.exists()


def test_results_are_handed_over_before_the_default_is_put_back(tmp_path: Path) -> None:
    variant = {
        "name": "round-robin",
        "values": "round-robin-values.yaml",
        "epp_config": "round-robin.yaml",
    }
    experiment = loaded(tmp_path, variants=[variant])
    cluster = FakeCluster()
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    switches_when_handed: list[int] = []
    run_experiment(
        experiment,
        out,
        cluster,
        ticking_clock(),
        lambda _rows, _meta: switches_when_handed.append(cluster.asked.count("routing default")),
    )
    assert switches_when_handed == [0]
    assert cluster.asked[-1] == "routing default"


def test_a_failed_variant_hands_over_no_results(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    handed: list[object] = []
    with pytest.raises(RuntimeError, match="broke"):
        run_experiment(
            experiment,
            out,
            FakeCluster(fail_replay_of="default"),
            ticking_clock(),
            lambda rows, _meta: handed.append(rows),
        )
    assert handed == []


def test_the_stamp_is_of_the_profile_at_the_start_not_after_the_long_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit during the 1.5 h run must not stamp the results with a profile they did not use."""
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)
    profile = tmp_path / "config" / "real.yaml"
    profile.parent.mkdir()
    profile.write_text("as loaded\n")

    class EditedDuringTheRun(FakeCluster):
        def replay(self, out: Path) -> list[dict[str, Any]]:
            profile.write_text("edited later\n")
            return super().replay(out)

    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    _, meta = run_experiment(experiment, out, EditedDuringTheRun(), ticking_clock())
    assert meta["profile_sha256"] == hashlib.sha256(b"as loaded\n").hexdigest()
    assert meta["trace"] == "trace.jsonl"


def test_a_picker_on_another_config_stops_before_any_call_is_sent(tmp_path: Path) -> None:
    """Both variants on one picker would look like a result: no difference."""
    experiment = loaded(tmp_path)
    cluster = FakeCluster(running_config=lambda _: "default-plugins.yaml")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(ExperimentError, match=r"round-robin\.yaml"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert not any(step.startswith(("warm", "replay")) for step in cluster.asked)
    assert cluster.asked[-1] == "routing default"


def test_the_results_have_every_variants_rows_without_the_discarded_run(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    rows, _ = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    assert [(row.variant, row.run) for row in rows] == [("round-robin", "a-2"), ("default", "a-2")]


def test_the_rows_split_by_the_files_kv_capacity(tmp_path: Path) -> None:
    """The fake's calls last 100 ms and start seconds apart, so each is alone on its pod with its 55
    tokens, which is over the first capacity and under the second."""
    over = loaded(tmp_path, kv_capacity_tokens_per_pod=54)
    under = loaded(tmp_path, kv_capacity_tokens_per_pod=1000)
    rows_over, _ = run_experiment(
        over, outputs(over, tmp_path / "over"), FakeCluster(), ticking_clock()
    )
    rows_under, _ = run_experiment(
        under, outputs(under, tmp_path / "under"), FakeCluster(), ticking_clock()
    )
    assert {row.over_kv_limit for row in rows_over} == {True}
    assert {row.over_kv_limit for row in rows_under} == {False}


def test_a_failed_call_does_not_stop_the_experiment_and_is_a_row(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    lines = replay_lines()
    del lines[1]["duration_ms"], lines[1]["ttft_ms"], lines[1]["pod"], lines[1]["cached_tokens"]
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    rows, meta = run_experiment(
        experiment, out, FakeCluster(replays={"robin": lines}), ticking_clock()
    )
    assert [row.duration_ms for row in rows] == [None, 100]
    assert meta["variants"]["round-robin"]["failed_calls"] == 1
    assert meta["variants"]["default"]["failed_calls"] == 0


def test_the_meta_says_what_the_csv_cannot(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    _, meta = run_experiment(
        experiment, out, FakeCluster(replays={"robin": replay_lines("pod-b")}), ticking_clock()
    )
    assert meta["hash"] == experiment.hash
    assert meta["profile"] == "real"
    assert meta["pattern"] == "baseline"
    assert meta["seed"] == 6
    robin = meta["variants"]["round-robin"]
    assert robin["epp_config"] == "round-robin.yaml"
    assert robin["warmup_ttft_ms"] == {"pod-a": [9000, 100], "pod-b": [8000, 90]}
    assert robin["clock_jumps"] == 3
    assert robin["calls_per_pod"] == {"pod-a": 1, "pod-b": 1}
    assert robin["pod_switch_share"] == 1.0
    assert meta["variants"]["default"]["pod_switch_share"] == 0.0
    # The trace's first step has gap 0; the fake's lines have gap 1.
    assert robin["max_gap_deviation_ms"] == 1


def test_results_are_written_as_csv_meta_and_a_graph_from_the_csv(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    write_results(out, rows, meta)
    with out.csv.open() as file:
        assert len(list(csv.DictReader(file))) == 2
    assert json.loads(out.meta.read_text())["hash"] == experiment.hash
    assert out.graph.read_bytes().startswith(b"\x89PNG")


def test_two_runs_of_one_file_give_the_same_row_count(tmp_path: Path) -> None:
    """The done-when's check, when no call fails."""
    experiment = loaded(tmp_path)
    first = outputs(experiment, tmp_path / "first")
    second = outputs(experiment, tmp_path / "second")
    for out in (first, second):
        out.csv.parent.mkdir(parents=True)
    one, _ = run_experiment(experiment, first, FakeCluster(), ticking_clock())
    other, _ = run_experiment(experiment, second, FakeCluster(), ticking_clock())
    assert len(one) == len(other)


def test_pods_to_warm_are_the_ready_ones() -> None:
    pods = {
        "items": [
            {
                "metadata": {"name": "ready"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.1",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
            {
                "metadata": {"name": "loading"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.2",
                    "conditions": [{"type": "Ready", "status": "False"}],
                },
            },
            {
                "metadata": {"name": "left-failed"},
                "status": {"phase": "Failed", "conditions": []},
            },
        ]
    }
    assert pods_to_warm(pods, replicas=1) == {"ready": "10.0.0.1"}


def test_fewer_ready_pods_than_the_profile_has_are_refused() -> None:
    with pytest.raises(ExperimentError, match="2"):
        pods_to_warm({"items": []}, replicas=2)


def test_a_warm_up_call_goes_to_that_pod_and_its_times_come_back() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        chunks = [
            {"choices": [{"delta": {"content": "x"}}]},
            {
                "choices": [],
                "system_fingerprint": "the-pod",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": body["max_tokens"],
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        ]
        text = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=text.encode())

    async def warm() -> list[int]:
        async with httpx.AsyncClient(
            base_url="http://10.0.0.1:8000", transport=httpx.MockTransport(handler)
        ) as client:
            return await warm_pod(client, "the-model", warmup_run(1, 3, 10, 3), 10.0)

    times = asyncio.run(warm())
    assert len(times) == 3
    assert all(time >= 0 for time in times)
    assert {request.url.host for request in seen} == {"10.0.0.1"}
    contents = [json.loads(request.content)["messages"][0]["content"] for request in seen]
    assert len(set(contents)) == 3


def test_a_warm_up_call_that_fails_stops_the_experiment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def warm() -> list[int]:
        async with httpx.AsyncClient(
            base_url="http://10.0.0.1:8000", transport=httpx.MockTransport(handler)
        ) as client:
            return await warm_pod(client, "the-model", warmup_run(1, 2, 10, 3), 10.0)

    with pytest.raises(ExperimentError, match="warm"):
        asyncio.run(warm())


def completed(code: int, out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["journalctl"], code, stdout=out)


def test_clock_jumps_are_the_lines_the_journal_finds(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        asked.append(command)
        return completed(0, "one Clock change detected\ntwo Clock change detected\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert count_clock_jumps(1000.4, 2000.6) == 2
    assert "@1000" in asked[0]
    assert "@2001" in asked[0]


def test_no_clock_jumps_is_journalctls_exit_1_and_counts_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(1, ""))
    assert count_clock_jumps(1000, 2000) == 0


def test_a_journal_that_cannot_be_read_is_an_error_not_zero_jumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(2, ""))
    with pytest.raises(ExperimentError, match="journalctl"):
        count_clock_jumps(1000, 2000)


def test_a_journal_that_shows_nothing_stops_the_experiment_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the rights to read, journalctl prints nothing, which would read as no clock jumps."""
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(0, ""))
    with pytest.raises(ExperimentError, match="journalctl"):
        check_journal_readable()


def test_a_journal_with_lines_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(0, "a line\n"))
    check_journal_readable()


def test_the_report_gives_each_variants_numbers_and_the_kv_split(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    report = "\n".join(report_lines(rows, meta))
    assert "round-robin: 1 of 1 runs finished, 1 calls, 0 failed" in report
    assert "default: 1 of 1 runs finished" in report
    assert "hit rate 0.320" in report
    assert "clock jumps 3" in report
    assert "largest gap deviation 1 ms" in report
    assert "under the KV limit, all: 1 calls, TTFT p50 0.01 s" in report
    assert "over the KV limit, moved: 0 calls" in report


def test_warmed_time_leaves_out_each_pods_first_call() -> None:
    """A pod's first call is slow whatever the host's speed (P2.4), so it would hide that speed."""
    assert warmed_ttft_ms({"a": [17_000, 300, 500], "b": [4000, 400]}) == pytest.approx(400.0)


def test_with_only_first_calls_there_is_no_warmed_time() -> None:
    assert warmed_ttft_ms({"a": [17_000]}) is None


def test_the_report_shows_the_hosts_speed(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    report = "\n".join(report_lines(rows, meta))
    # (100 - 10) / 4 tokens; the fake's warm-up calls after each pod's first are 100 and 90 ms.
    assert "generation 22.5 ms per token, warm-up calls 95.0 ms to first token" in report


def test_the_running_config_file_is_read_from_the_pickers_arguments() -> None:
    args = ["--pool-name", "p", "--config-file", "/config/round-robin.yaml", "--v=3"]
    assert served_config_file(json.dumps(args)) == "round-robin.yaml"


def test_a_picker_with_no_config_argument_is_refused() -> None:
    with pytest.raises(ExperimentError, match="config-file"):
        served_config_file('["--pool-name", "p"]')


def test_a_warm_up_run_has_the_calls_asked_for_each_with_its_own_text() -> None:
    run = warmup_run(2, 3, 10, 4)
    assert run.run_id == "warmup-2"
    assert run.start_seconds == 0
    assert [step.step for step in run.steps] == [1, 2, 3]
    assert all(step.completion_tokens == 4 for step in run.steps)
    assert all(len(step.content.split()) == 10 for step in run.steps)
    assert len({step.content for step in run.steps}) == 3


def test_the_pods_get_different_warm_up_text() -> None:
    one, other = warmup_run(1, 1, 10, 4), warmup_run(2, 1, 10, 4)
    assert one.steps[0].content != other.steps[0].content


def committed(path: Path) -> Experiment:
    spec = yaml.safe_load(path.read_text())
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{spec['profile']}.yaml").read_text())
    return load_experiment(path, load_replay_config(profile["replay"]).patterns)


@pytest.mark.parametrize("path", [BASELINE, BASELINE_CHECK], ids=lambda path: path.stem)
def test_the_committed_experiment_files_load(path: Path) -> None:
    experiment = committed(path)
    assert [variant.name for variant in experiment.variants] == ["round-robin", "default"]
    assert experiment.variants[0].values == ROUND_ROBIN_VALUES.resolve()
    assert experiment.variants[1].values is None
    load_trace(experiment.trace)


def test_the_baseline_runs_the_decided_load() -> None:
    experiment = committed(BASELINE)
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert experiment.pattern == Pattern(40, 0.02, None, None)
    assert experiment.seed == 6
    assert experiment.warmup.runs_discarded == 5


def test_the_reversed_baseline_differs_from_the_baseline_only_in_the_order() -> None:
    """Otherwise a difference between the two could come from something other than the order."""
    baseline = yaml.safe_load(BASELINE.read_text())
    reversed_baseline = yaml.safe_load(BASELINE_REVERSED.read_text())
    assert reversed_baseline["variants"] == list(reversed(baseline["variants"]))
    del baseline["variants"], reversed_baseline["variants"]
    assert reversed_baseline == baseline


def test_the_reversed_baseline_loads_and_keeps_its_own_results() -> None:
    experiment = committed(BASELINE_REVERSED)
    assert [variant.name for variant in experiment.variants] == ["default", "round-robin"]
    assert outputs(experiment, REPO_ROOT).csv != outputs(committed(BASELINE), REPO_ROOT).csv


def test_the_baselines_first_20_runs_are_p24s_replay() -> None:
    """The profiles' comment says the load is known because of this."""
    experiment = committed(BASELINE)
    runs = load_trace(experiment.trace)
    measured = Pattern(20, 0.02, None, None)
    assert start_times(experiment.pattern, 6)[:20] == start_times(measured, 6)
    first = [run.run_id for run in choose_runs(runs, 40, 6)][:20]
    assert first == [run.run_id for run in choose_runs(runs, 20, 6)]


def test_the_kv_capacity_is_what_the_real_profiles_cache_size_gives() -> None:
    """vLLM logs 4,384 tokens for 480 MiB of KV cache on this model (P2.4). A new size in the
    profile must be measured again, since the split over and under the limit rests on it."""
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    assert profile["model"]["kv_cache_memory_bytes"] == 503316480
    assert committed(BASELINE).kv_capacity_tokens_per_pod == 4384


def test_the_round_robin_variant_is_the_default_without_its_scorers() -> None:
    """Keeps the metrics scrape, so the picker's other load does not change with the variant."""
    router = yaml.safe_load(ROUND_ROBIN_VALUES.read_text())["router"]["epp"]
    assert router["pluginsConfigFile"] == "round-robin.yaml"
    config = yaml.safe_load(router["pluginsCustomConfig"]["round-robin.yaml"])
    assert {plugin["type"] for plugin in config["plugins"]} == {
        "metrics-data-source",
        "core-metrics-extractor",
        "max-score-picker",
    }
    assert [
        plugin["pluginRef"] for profile in config["schedulingProfiles"]
        for plugin in profile["plugins"]
    ] == ["max-score-picker"]  # fmt: skip


def test_the_trace_the_experiment_names_is_committed() -> None:
    assert (TRACE_DIR / f"{APB_TRACE}.jsonl").exists()
