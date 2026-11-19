"""make warmup-report (P4.2): where runs' first calls went while a pod was new, from known lines.

The live path (KEDA adding simulated pods during a burst replay) is what make experiment
NAME=warmup proves; here the replay's lines and the picker's log are written by hand.
"""

import csv
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from load_generator.warmup import (
    PodRow,
    VariantRow,
    WarmupError,
    main,
    no_room_runs,
    ready_times,
    report,
    settings,
    write_reports,
)

# The replay's start, in Unix seconds, as the experiment's .meta.json holds it.
STARTED = 1_795_000_000.0
OLD = "vllm-6cbf6478d6-2pj68"
NEW = "vllm-6cbf6478d6-lvnd9"
NEWER = "vllm-6cbf6478d6-m947d"


def at(seconds: float) -> str:
    """The picker's readyAt for a pod Ready this many seconds after the replay's start."""
    return datetime.fromtimestamp(STARTED + seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def loaded(warmup: bool, window: int = 120) -> dict[str, Any]:
    return {
        "body": "Plugin loaded",
        "type": "thesis-run-router",
        "warmup": warmup,
        "warmupSeconds": window,
    }


def ready(pod: str, seconds: float) -> dict[str, Any]:
    return {"body": "Pod ready", "pod": pod, "readyAt": at(seconds)}


def picked(run_id: str, step: str, reason: str) -> dict[str, Any]:
    return {"body": "Run call picked", "run-id": run_id, "step": step, "reason": reason}


def call(run_id: str, step: int, seconds: float, pod: str | None) -> dict[str, Any]:
    """A replay line sent this many seconds after the start; a failed call names no pod."""
    line: dict[str, Any] = {
        "run_id": run_id,
        "step": step,
        "ts_start": int((STARTED + seconds) * 1000),
    }
    if pod is not None:
        line["pod"] = pod
    return line


def test_settings_are_the_plugins_as_it_loaded() -> None:
    assert settings([ready(OLD, -500), loaded(True, 90)]) == (True, 90)


def test_a_log_without_the_plugin_is_refused() -> None:
    with pytest.raises(WarmupError, match="Plugin loaded"):
        settings([ready(OLD, -500)])


def test_ready_times_are_the_vllm_pods_only() -> None:
    lines = [
        ready(OLD, -500),
        ready("vllm-pool-epp-7d9f8c6b5-x2x4q", -600),
        ready("agent-5c8d7f9b6-q8z2w", -600),
        {"body": "Run call picked", "pod": NEW},
        ready(NEW, 100),
    ]
    assert ready_times(lines) == [(OLD, STARTED - 500), (NEW, STARTED + 100)]


def test_no_room_runs_are_the_runs_whose_first_call_found_no_new_pod_with_room() -> None:
    lines = [
        picked("run-1", "1", "warmup-full"),
        picked("run-2", "1", "warmup"),
        picked("run-3", "2", "warmup-full"),
        picked("run-4", "1", "first"),
    ]
    assert no_room_runs(lines) == {"run-1"}


# A replay where KEDA added NEW at 100 s: its window is 100-220 s.
PICKER = [loaded(True), ready(OLD, -500), ready(NEW, 100), picked("run-3", "1", "warmup-full")]
CALLS = [
    call("run-0", 1, 50, OLD),
    call("run-1", 1, 102, OLD),
    call("run-0", 2, 104, NEW),
    call("run-2", 1, 105, NEW),
    call("run-3", 1, 110, OLD),
    call("run-4", 1, 150, OLD),
    call("run-5", 1, 200, None),
    call("run-6", 1, 221, OLD),
]


def test_first_calls_while_a_pod_was_new_are_counted_by_where_they_went() -> None:
    variant, _ = report("on", STARTED, CALLS, PICKER)
    assert variant == VariantRow(
        variant="on",
        warmup=True,
        window_seconds=120,
        new_pods=1,
        first_calls_while_new=5,
        to_new=1,
        to_old=3,
        to_old_no_room=1,
        failed=1,
    )


def test_each_new_pod_has_its_ready_time_and_first_calls() -> None:
    _, pods = report("on", STARTED, CALLS, PICKER)
    assert pods == [
        PodRow(
            variant="on",
            pod=NEW,
            ready_s=100.0,
            first_call_s=104.0,
            first_step1_s=105.0,
            first_calls_elsewhere_before=1,
            first_calls_in_window=1,
        )
    ]


def test_a_new_pod_no_first_call_reached_counts_every_one_elsewhere() -> None:
    picker = [loaded(False), ready(OLD, -500), ready(NEW, 100), ready(NEWER, 130)]
    calls = [call("run-1", 1, 120, OLD), call("run-2", 1, 140, NEWER), call("run-3", 1, 160, OLD)]
    variant, pods = report("off", STARTED, calls, picker)
    assert (variant.to_new, variant.to_old) == (1, 2)
    assert [(pod.pod, pod.first_step1_s, pod.first_calls_elsewhere_before) for pod in pods] == [
        (NEW, None, 3),
        (NEWER, 140.0, 0),
    ]


def test_a_pod_ready_before_the_replay_is_old() -> None:
    variant, pods = report(
        "on", STARTED, [call("run-1", 1, 10, OLD)], [loaded(True), ready(OLD, -1)]
    )
    assert pods == []
    assert variant.new_pods == 0 and variant.first_calls_while_new == 0


def result(tmp_path: Path, variants: Mapping[str, Sequence[Mapping[str, Any]] | None]) -> Path:
    """An experiment's files: the meta, and each variant's replay lines and picker log; None is a
    variant on the default picker, which keeps no picker log."""
    stem = tmp_path / "warmup-0123456789ab"
    meta: dict[str, Any] = {"experiment": "warmup", "variants": {}}
    for name, picker in variants.items():
        stem.with_name(f"{stem.name}-{name}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in CALLS)
        )
        log = None
        if picker is not None:
            log = f"{stem.name}-{name}-picker.log"
            stem.with_name(log).write_text(
                "not json\n" + "".join(json.dumps(line) + "\n" for line in picker)
            )
        meta["variants"][name] = {"started": STARTED, "picker_log": log}
    stem.with_name(f"{stem.name}.meta.json").write_text(json.dumps(meta))
    return stem


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_report_writes_one_row_per_variant_and_per_new_pod(tmp_path: Path) -> None:
    stem = result(tmp_path, {"off": [loaded(False), *PICKER[1:]], "on": PICKER})
    write_reports(stem)
    variants = rows(stem.with_name(f"{stem.name}-warmup.csv"))
    assert [(row["variant"], row["warmup"], row["to_new"], row["to_old"]) for row in variants] == [
        ("off", "False", "1", "3"),
        ("on", "True", "1", "3"),
    ]
    pods = rows(stem.with_name(f"{stem.name}-warmup-pods.csv"))
    assert [(row["variant"], row["pod"], row["first_step1_s"]) for row in pods] == [
        ("off", NEW, "105.0"),
        ("on", NEW, "105.0"),
    ]


def test_a_variant_on_the_default_picker_is_refused(tmp_path: Path) -> None:
    stem = result(tmp_path, {"default": None})
    with pytest.raises(WarmupError, match="default"):
        write_reports(stem)


def test_main_prints_each_variant(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stem = result(tmp_path, {"on": PICKER})
    main([str(stem)])
    out = capsys.readouterr().out
    assert "on: warm-up on, window 120 s, 1 pod turned Ready" in out
    assert "5 first calls while a pod was new: 1 to a new pod, 3 to an old one" in out


def test_a_pods_window_holds_its_ready_time_and_its_end_as_the_plugins_does() -> None:
    """The plugin counts a pod new while its age is at most the window, both ends included; a
    failed call went to no pod, so it is not one that went elsewhere."""
    picker = [loaded(True), ready(OLD, -500), ready(NEW, 100)]
    calls = [
        call("run-1", 1, 100, OLD),
        call("run-2", 1, 101, None),
        call("run-3", 1, 220, OLD),
        call("run-4", 1, 220.5, OLD),
        call("run-5", 1, 230, NEW),
    ]
    variant, pods = report("on", STARTED, calls, picker)
    assert (variant.first_calls_while_new, variant.to_old, variant.failed) == (3, 2, 1)
    [pod] = pods
    assert (pod.first_step1_s, pod.first_calls_elsewhere_before, pod.first_calls_in_window) == (
        230.0,
        3,
        0,
    )
