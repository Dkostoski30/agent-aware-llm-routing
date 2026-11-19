"""make warmup-report (P4.2): where runs' first calls went while a pod was new.

Reads an experiment's files, each variant's replay lines and the picker's log, so it needs no
cluster. The picker's thesis-run-router logs each pod's Ready time ("Pod ready", from Kubernetes'
Ready condition) whatever its rules, and its warm-up window as it loaded ("Plugin loaded"). A pod
is new from its Ready time for the window; only vLLM pods that turned Ready after the replay
started count, the ones KEDA added during it.

For each variant, the runs' first calls (step 1) sent while some pod was new: how many went to a
new pod, how many to an old one, and of those how many the picker had judged with no new pod with
room (warmup-full; a variant with warm-up off judges none), and how many failed. For each new pod:
its Ready time, its first call and first step-1 call, how many step-1 calls went elsewhere between
its Ready time and its first one, and how many it got in its window. Times are seconds from the
replay's start; a call's time is when the replayer sent it, by the VM's clock, as the Ready times
of pods on the same machine.

Writes <RESULT>-warmup.csv (one row per variant) and <RESULT>-warmup-pods.csv (one per new pod).

Usage: python -m load_generator.warmup RESULT
"""

import argparse
import csv
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from load_generator.pod_removed import picker_lines
from load_generator.results import Line

# The vLLM Deployment's pods, <deployment>-<replicaset hash>-<pod hash>; the picker's own
# vllm-pool-epp-<hash>-<hash> has more parts. As the cluster panel's query (P3.11).
VLLM_POD = re.compile(r"vllm-[a-z0-9]+-[a-z0-9]+")


class WarmupError(Exception):
    pass


@dataclass(frozen=True)
class VariantRow:
    variant: str
    warmup: bool
    window_seconds: int
    new_pods: int
    first_calls_while_new: int
    to_new: int
    to_old: int
    to_old_no_room: int
    failed: int


@dataclass(frozen=True)
class PodRow:
    variant: str
    pod: str
    ready_s: float
    first_call_s: float | None
    first_step1_s: float | None
    first_calls_elsewhere_before: int
    first_calls_in_window: int


def settings(picker: Iterable[Mapping[str, Any]]) -> tuple[bool, int]:
    """Warm-up's flag and window as the plugin loaded them."""
    for line in picker:
        if line.get("body") == "Plugin loaded":
            return bool(line["warmup"]), int(line["warmupSeconds"])
    raise WarmupError("no Plugin loaded line in the picker's log")


def ready_times(picker: Iterable[Mapping[str, Any]]) -> list[tuple[str, float]]:
    """Each vLLM pod's Ready times, in Unix seconds, in the log's order."""
    return [
        (line["pod"], datetime.fromisoformat(line["readyAt"]).timestamp())
        for line in picker
        if line.get("body") == "Pod ready" and VLLM_POD.fullmatch(line["pod"])
    ]


def no_room_runs(picker: Iterable[Mapping[str, Any]]) -> set[str]:
    """The runs whose first call warm-up judged with no new pod with room."""
    return {
        line["run-id"]
        for line in picker
        if line.get("body") == "Run call picked"
        and line.get("step") == "1"
        and line.get("reason") == "warmup-full"
    }


def report(
    variant: str, started: float, calls: Sequence[Line], picker: Sequence[Mapping[str, Any]]
) -> tuple[VariantRow, list[PodRow]]:
    """One variant's row and its new pods' rows, from its replay lines and its picker's log."""
    warmup, window = settings(picker)
    windows = [(pod, at - started) for pod, at in ready_times(picker) if at >= started]
    sent = sorted(
        ((line["ts_start"] / 1000 - started, line.get("pod"), line) for line in calls),
        key=lambda entry: entry[0],
    )
    firsts = [(seconds, pod, line["run_id"]) for seconds, pod, line in sent if line["step"] == 1]
    no_room = no_room_runs(picker)

    def new_at(seconds: float) -> set[str]:
        return {pod for pod, ready in windows if ready <= seconds <= ready + window}

    counts = {"while": 0, "new": 0, "old": 0, "no_room": 0, "failed": 0}
    for seconds, pod, run_id in firsts:
        new = new_at(seconds)
        if not new:
            continue
        counts["while"] += 1
        if pod is None:
            counts["failed"] += 1
        elif pod in new:
            counts["new"] += 1
        else:
            counts["old"] += 1
            counts["no_room"] += run_id in no_room
    pods = []
    for pod, ready in windows:
        on_pod = [seconds for seconds, on, _ in sent if on == pod]
        first_step1 = next((seconds for seconds, on, _ in firsts if on == pod), None)
        until = float("inf") if first_step1 is None else first_step1
        pods.append(
            PodRow(
                variant=variant,
                pod=pod,
                ready_s=round(ready, 1),
                first_call_s=round(on_pod[0], 1) if on_pod else None,
                first_step1_s=None if first_step1 is None else round(first_step1, 1),
                first_calls_elsewhere_before=sum(
                    ready <= seconds < until and on is not None and on != pod
                    for seconds, on, _ in firsts
                ),
                first_calls_in_window=sum(
                    on == pod and ready <= seconds <= ready + window for seconds, on, _ in firsts
                ),
            )
        )
    row = VariantRow(
        variant=variant,
        warmup=warmup,
        window_seconds=window,
        new_pods=len(windows),
        first_calls_while_new=counts["while"],
        to_new=counts["new"],
        to_old=counts["old"],
        to_old_no_room=counts["no_room"],
        failed=counts["failed"],
    )
    return row, pods


def _write(path: Path, kind: type[VariantRow] | type[PodRow], rows: Iterable[Any]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(field.name for field in fields(kind))
        writer.writerows(astuple(row) for row in rows)


def write_reports(stem: Path) -> tuple[list[VariantRow], list[PodRow]]:
    """Every variant of the experiment whose files start with stem, into the two CSVs beside
    them."""
    meta = json.loads(stem.with_name(f"{stem.name}.meta.json").read_text())
    variants: list[VariantRow] = []
    pods: list[PodRow] = []
    for name, variant in meta["variants"].items():
        if variant["picker_log"] is None:
            raise WarmupError(
                f"variant {name} ran the default picker, which logs no Ready times: "
                "every variant needs the plugin"
            )
        lines = stem.with_name(f"{stem.name}-{name}.jsonl").read_text().splitlines()
        calls = [json.loads(line) for line in lines if line]
        picker = picker_lines(stem.with_name(variant["picker_log"]).read_text())
        row, new_pods = report(name, variant["started"], calls, picker)
        variants.append(row)
        pods.extend(new_pods)
    _write(stem.with_name(f"{stem.name}-warmup.csv"), VariantRow, variants)
    _write(stem.with_name(f"{stem.name}-warmup-pods.csv"), PodRow, pods)
    return variants, pods


def report_lines(variants: Sequence[VariantRow], pods: Sequence[PodRow]) -> list[str]:
    out = []
    for row in variants:
        state = "on" if row.warmup else "off"
        pods_word = "pod" if row.new_pods == 1 else "pods"
        out.append(
            f"{row.variant}: warm-up {state}, window {row.window_seconds} s, "
            f"{row.new_pods} {pods_word} turned Ready during the replay"
        )
        out.append(
            f"  {row.first_calls_while_new} first calls while a pod was new: {row.to_new} to a new "
            f"pod, {row.to_old} to an old one ({row.to_old_no_room} with no new pod with room), "
            f"{row.failed} failed"
        )
        for pod in pods:
            if pod.variant == row.variant:
                out.append(
                    f"  {pod.pod}: Ready at {pod.ready_s} s, first call {pod.first_call_s} s, "
                    f"first step-1 call {pod.first_step1_s} s, "
                    f"{pod.first_calls_elsewhere_before} step-1 calls elsewhere before it, "
                    f"{pod.first_calls_in_window} in its window"
                )
    return out


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "result", type=Path, help="experiments/results/<name>-<hash>, without an extension"
    )
    args = parser.parse_args(argv)
    variants, pods = write_reports(args.result)
    print("\n".join(report_lines(variants, pods)))


if __name__ == "__main__":
    main()
