"""Scores the P1.7 held-out set through the agent runtime in k3s (P1.7 step 2,
docs/task-cards/19-P1.7-fine-tune-tool-calls.md).

Sends every held-out question of make finetune-data once, with its seed and its file (if any), in
each D10 mode, and scores each run with the go/no-go's scorer (go_no_go.is_correct). The mode is
switched the way go_no_go.py switches it, and the profile from config/ is put back at the end, also
after a failure or Ctrl+C. Writes one row per run to
results/finetune-heldout-<data hash>-<agent image tag>[-<adapter>].csv as each run ends, and prints
the score per mode and kind. With --baseline (P1.7 step 3) it also prints the baseline's score next
to it, paired by question, with McNemar's exact p-value per mode.

A run cut short (sleep, crash, Ctrl+C) keeps every finished row, and the next run asks only the
questions that have none: the file's name ties its rows to the data and the agent code, so after a
change to config/ delete the file to start again. A run whose LLM call got a 5xx or no connection
stops the whole run unsaved: that is the cluster failing (vLLM not ready, a 503 from the gateway),
not a wrong answer, and a resume asks it again (cluster_failed).

Usage: heldout.py EXPERIMENT_YAML [--baseline CSV]   (experiments/finetune-data.yaml)
"""

import argparse
import csv
import io
import json
import math
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from finetune_data import KINDS, data_dir
from go_no_go import (
    AGENT_DEPLOYMENT,
    AGENT_PODS,
    AGENT_PORT,
    FIXED_REPLY,
    REPO_ROOT,
    REQUEST_TIMEOUT_SECONDS,
    Question,
    adapter_id,
    apply_profile,
    expected_value,
    is_correct,
    kubectl,
    ready_pod_ip,
    run_question,
    served_differs,
    served_mode,
    with_mode,
)

# The go/no-go's columns, with each question's kind.
COLUMNS = (
    "mode",
    "kind",
    "question",
    "seed",
    "correct",
    "steps",
    "invalid_calls",
    "duration_s",
    "expected",
    "answer",
    "status",
    "error",
)
# How the agent runtime starts the error of a run whose LLM call failed with a 5xx from the gateway
# or vLLM (httpx words it "Server error"; 503 while no vLLM pod is ready) or with no connection:
# "LLM call failed: <httpx error type>: <message>" (agent_runtime/loop.py, run_task).
CLUSTER_FAILURES = (
    "LLM call failed: HTTPStatusError: Server error",
    "LLM call failed: ConnectError:",
)
# The kind whose fixed reply only the training data has, not the system prompt (P1.7): its gain is
# by construction, so the comparison also shows every other kind without it.
OFF_TOPIC = "off-topic"


@dataclass(frozen=True)
class Heldout:
    question: Question
    kind: str
    # The file as run-request.v1 carries it, or None for a question asked without one.
    file: dict[str, str] | None
    seed: int


def load_heldout(path: Path) -> list[Heldout]:
    """The held-out questions of heldout.jsonl, in order."""
    questions = []
    for line in path.read_text().splitlines():
        item = json.loads(line)
        question = Question(
            id=item["id"],
            text=item["text"],
            expected_calls=item["expected_calls"],
            expected=item["expected"],
            wrong_if_matches=item["wrong_if_matches"],
        )
        questions.append(Heldout(question, item["kind"], item["file"], item["seed"]))
    return questions


def expected_of(heldout: Heldout) -> float | None:
    """The answer the scorer looks for, computed from the file's rows; None for a question
    answered with a fixed sentence."""
    spec = heldout.question.expected
    if spec["kind"] == FIXED_REPLY:
        return None
    content = "" if heldout.file is None else heldout.file["content"]
    return expected_value(list(csv.DictReader(io.StringIO(content))), spec)


def score(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], tuple[int, int]]:
    """(correct, runs) per (mode, kind)."""
    counts: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        correct, runs = counts.get((row["mode"], row["kind"]), (0, 0))
        counts[row["mode"], row["kind"]] = (correct + bool(row["correct"]), runs + 1)
    return counts


def result_path(
    experiments: Path, data_hash: str, agent_image: str, adapter: str | None = None
) -> Path:
    """results/finetune-heldout-<data hash>-<agent image tag>[-<adapter>].csv: tied to the
    generated data, the agent code and the served adapter (D11, as go_no_go.result_path)."""
    tag = agent_image.rsplit(":", 1)[1]
    suffix = "" if adapter is None else f"-{adapter}"
    return experiments / "results" / f"finetune-heldout-{data_hash}-{tag}{suffix}.csv"


def csv_value(expected: float | None) -> str:
    """An expected value as the result CSV holds it: csv writes a float with str() and None as an
    empty field."""
    return "" if expected is None else str(expected)


def check_baseline(
    baseline: Sequence[Mapping[str, Any]],
    questions: Sequence[Heldout],
    expected: Mapping[str, float | None],
    modes: Sequence[str],
) -> None:
    """Raises ValueError unless the baseline has one row per mode and question of this held-out
    set, each with the same expected value: otherwise its pairs would compare other questions."""
    for mode in modes:
        got: dict[str, str] = {}
        for row in baseline:
            if row["mode"] != mode:
                continue
            if row["question"] in got:
                raise ValueError(f"the baseline has two {mode} rows for {row['question']}")
            got[row["question"]] = row["expected"]
        want = {h.question.id: csv_value(expected[h.question.id]) for h in questions}
        if got != want:
            differ = sorted(q for q in got.keys() | want.keys() if got.get(q) != want.get(q))
            raise ValueError(
                f"the baseline's {mode} rows are not this held-out set's: {', '.join(differ)} "
                "missing, extra or with another expected value"
            )


def paired(
    baseline: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    mode: str,
    kinds: Sequence[str],
) -> tuple[int, int, int, int]:
    """The questions of the given kinds in one mode, paired with their baseline row: (both right,
    only the baseline right, only this run right, both wrong)."""
    before = {row["question"]: row["correct"] for row in baseline if row["mode"] == mode}
    pairs = Counter(
        (before[row["question"]], row["correct"])
        for row in rows
        if row["mode"] == mode and row["kind"] in kinds
    )
    return pairs[True, True], pairs[True, False], pairs[False, True], pairs[False, False]


def mcnemar_p(only_baseline: int, only_adapter: int) -> float:
    """McNemar's exact two-sided p-value. Only the questions one side got right and the other
    wrong count; if neither side is better, each goes either way with probability 1/2, so the
    smaller count is binomial(n, 1/2). Twice its lower tail, at most 1."""
    n = only_baseline + only_adapter
    tail = sum(math.comb(n, k) for k in range(min(only_baseline, only_adapter) + 1))
    return min(1.0, 2 * tail / 2.0**n)


def cluster_failed(error: str | None) -> bool:
    """Whether a run's error is the cluster failing, not the model. Another failed LLM call follows
    from what the model wrote and is a wrong run: a 4xx (the model looped until its history was
    longer than model.max_model_len, P1.7), a read timeout (a reply too long for
    agent.llm_timeout_seconds) or a body that is no chat completion."""
    return error is not None and error.startswith(CLUSTER_FAILURES)


def earlier_rows(path: Path) -> list[dict[str, Any]]:
    """The rows an earlier run left in the result CSV, correct read back as a bool; none when
    there is no file."""
    if not path.exists():
        return []
    with path.open(newline="") as rows:
        reader = csv.DictReader(rows)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(
                f"{path} has the columns {reader.fieldnames}, not {list(COLUMNS)}: delete it to "
                "start again"
            )
        return [{**row, "correct": row["correct"] == str(True)} for row in reader]


def remaining(
    questions: Sequence[Heldout], rows: Iterable[Mapping[str, Any]], mode: str
) -> list[Heldout]:
    """The questions with no row in mode yet, in order."""
    scored = {row["question"] for row in rows if row["mode"] == mode}
    return [heldout for heldout in questions if heldout.question.id not in scored]


@contextmanager
def result_csv(path: Path) -> Iterator[Callable[[dict[str, Any]], None]]:
    """Opens the result CSV and yields a function that writes one row and flushes it: a pass
    takes ~40 min (P1.7), so a crash or Ctrl+C must not lose the rows already scored. Rows go after
    the ones an earlier run left (earlier_rows). A new file's header is flushed at once, so a run
    killed before its first row leaves a file the next run reads as no rows."""
    path.parent.mkdir(exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
            out.flush()

        def save(row: dict[str, Any]) -> None:
            writer.writerow(row)
            out.flush()

        yield save


def run_mode(
    client: httpx.Client,
    questions: Sequence[Heldout],
    mode: str,
    expected: Mapping[str, float | None],
    tolerance: float,
    run_timeout: float,
    save: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """Every question once, one after the other, on an agent already running in mode; each row is
    saved as its run ends. Raises RuntimeError, before saving, on a run the cluster failed
    (cluster_failed)."""
    rows: list[dict[str, Any]] = []
    for heldout in questions:
        question = heldout.question
        files = [] if heldout.file is None else [heldout.file]
        run, duration = run_question(client, files, question.text, heldout.seed, run_timeout)
        served = served_mode(run)
        if served not in (None, mode):
            raise RuntimeError(
                f"{run['run_id']} was answered in {served} mode, not {mode}: an agent pod "
                "with the other profile served it"
            )
        if cluster_failed(run["error"]):
            raise RuntimeError(
                f"{run['run_id']} ({question.id}): {run['error']}: the cluster failed, not the "
                "model; it is not saved, and make heldout asks it again"
            )
        correct = is_correct(run, question, expected[question.id], tolerance)
        row = {
            "mode": mode,
            "kind": heldout.kind,
            "question": question.id,
            "seed": heldout.seed,
            "correct": correct,
            "steps": len(run["steps"]),
            "invalid_calls": run["invalid_calls"],
            "duration_s": round(duration, 1),
            "expected": expected[question.id],
            "answer": run["answer"],
            "status": run["status"],
            "error": run["error"],
        }
        rows.append(row)
        save(row)
        verdict = "correct" if correct else "wrong"
        print(
            f"heldout: {mode} {question.id}: {verdict} ({run['status']}, {row['steps']} steps, "
            f"{row['invalid_calls']} invalid, {row['duration_s']} s): {run['answer']!r}",
            flush=True,
        )
    return rows


def report(rows: Sequence[Mapping[str, Any]], modes: Sequence[str], results: Path) -> None:
    """Prints the score per mode and kind."""
    print(f"heldout: {len(rows)} rows in {results}")
    counts = score(rows)
    for mode in modes:
        for kind in KINDS:
            correct, runs = counts.get((mode, kind), (0, 0))
            print(f"{mode} {kind}: {correct}/{runs}")
        total = [counts[key] for key in counts if key[0] == mode]
        print(f"{mode}: {sum(c for c, _ in total)}/{sum(n for _, n in total)} correct")


def compare(
    baseline: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    modes: Sequence[str],
    path: Path,
) -> None:
    """Prints the baseline's score next to this run's per mode and kind, then per mode the paired
    counts and McNemar's p-value, over every kind and without off-topic (OFF_TOPIC)."""
    print(f"heldout: against the baseline {path}")
    before, after = score(baseline), score(rows)
    groups = (("all kinds", KINDS), (f"without {OFF_TOPIC}", [k for k in KINDS if k != OFF_TOPIC]))
    for mode in modes:
        for kind in KINDS:
            was, runs = before.get((mode, kind), (0, 0))
            now, _ = after.get((mode, kind), (0, 0))
            print(f"{mode} {kind}: {was}/{runs} -> {now}/{runs}")
        for label, kinds in groups:
            both, only_baseline, only_adapter, neither = paired(baseline, rows, mode, kinds)
            runs = both + only_baseline + only_adapter + neither
            print(
                f"{mode} {label}: {both + only_baseline}/{runs} -> {both + only_adapter}/{runs}, "
                f"{only_adapter} fixed, {only_baseline} broken, "
                f"McNemar p = {mcnemar_p(only_baseline, only_adapter):.3g}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="P1.7: score the held-out set in k3s")
    parser.add_argument("experiment", type=Path, help="experiments/finetune-data.yaml")
    parser.add_argument(
        "--baseline", type=Path, help="a result CSV of the same held-out set to compare with"
    )
    args = parser.parse_args()
    spec_path: Path = args.experiment
    spec = yaml.safe_load(spec_path.read_text())
    data = data_dir(spec_path)
    questions = load_heldout(data / "heldout.jsonl")
    profile_path = REPO_ROOT / "config" / f"{spec['profile']}.yaml"
    profile_text = profile_path.read_text()
    profile = yaml.safe_load(profile_text)
    tolerance: float = profile["agent"]["answer_tolerance"]
    # As go_no_go.py: a run still going after max_steps calls of the longest wait is stuck.
    run_timeout = profile["agent"]["max_steps"] * profile["agent"]["llm_timeout_seconds"]
    # Before the cluster is touched, so a broken question fails first.
    expected = {heldout.question.id: expected_of(heldout) for heldout in questions}
    baseline: list[dict[str, Any]] | None = None
    if args.baseline is not None:
        if not args.baseline.exists():
            sys.exit(f"heldout: the baseline {args.baseline} does not exist")
        baseline = earlier_rows(args.baseline)
        check_baseline(baseline, questions, expected, spec["modes"])

    running = yaml.safe_load(
        kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    )
    differs = served_differs(running["model"], profile["model"])
    if differs is not None:
        sys.exit(f"heldout: {differs}: run make up PROFILE={spec['profile']} first")
    agent_image = kubectl(
        "get",
        "deployment",
        AGENT_DEPLOYMENT,
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    )

    results = result_path(spec_path.parent, data.name, agent_image, adapter_id(profile["model"]))
    print(f"heldout: {len(questions)} questions x {len(spec['modes'])} modes, rows in {results}")
    rows = earlier_rows(results)
    if rows:
        print(f"heldout: {len(rows)} rows from an earlier run kept, asking the rest", flush=True)
    try:
        with result_csv(results) as save:
            for mode in spec["modes"]:
                left = remaining(questions, rows, mode)
                if not left:
                    print(f"heldout: {mode}: every question already has a row", flush=True)
                    continue
                print(f"heldout: agent.tool_choice {mode}, restarting the agent", flush=True)
                apply_profile(with_mode(profile_text, mode))
                pods = json.loads(kubectl("get", "pods", "--selector", AGENT_PODS, "-o", "json"))
                # A new client per mode, so no pooled connection leads to the pod of the last mode.
                with httpx.Client(
                    base_url=f"http://{ready_pod_ip(pods)}:{AGENT_PORT}",
                    timeout=REQUEST_TIMEOUT_SECONDS,
                ) as client:
                    rows += run_mode(client, left, mode, expected, tolerance, run_timeout, save)
    finally:
        print(f"heldout: putting back {profile_path}", flush=True)
        apply_profile(profile_text)
    report(rows, spec["modes"], results)
    if baseline is not None:
        compare(baseline, rows, spec["modes"], args.baseline)


if __name__ == "__main__":
    main()
