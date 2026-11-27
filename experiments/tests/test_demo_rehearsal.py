"""The P5.4 demo rehearsal (docs/task-cards/78-P5.4-demo-rehearsal.md): its file, the faults it
reads on a page, how it judges a turn, a replay and a part, and both rounds as they walk the UI.
The page and the cluster are fakes with a clock of their own, so nothing here needs a browser or a
cluster; the browser layer is tested by the rehearsal's own run."""

import csv
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from demo_rehearsal import (
    Chat,
    Rehearsal,
    RehearsalError,
    Round,
    RoundFailed,
    Turn,
    Waiter,
    infrastructure_round,
    judge_replay,
    judge_turn,
    load_rehearsal,
    page_faults,
    part_green,
    practical_round,
    replaced,
    results_dir,
)
from go_no_go import FIXED_REPLY, Question
from task_suite import load_suite

EXPERIMENTS = Path(__file__).resolve().parents[1]
REPO = EXPERIMENTS.parent
REHEARSAL_PATH = EXPERIMENTS / "demo-rehearsal.yaml"
REHEARSAL = load_rehearsal(REHEARSAL_PATH)
SUITE = load_suite(EXPERIMENTS / "task-suite.yaml")
DEMO = yaml.safe_load((REPO / "config" / "demo.yaml").read_text())
REAL = yaml.safe_load((REPO / "config" / "real.yaml").read_text())
SPEC = yaml.safe_load(REHEARSAL_PATH.read_text())

# Texts the UI shows on a demo that goes right (its templates, P3.13, P4.6a), none of which may
# read as a fault.
NORMAL_TEXTS = [
    "Tenax",
    "Profile demo · Qwen3-1.7B-GPTQ-Int8",
    "What should the agent work on?",
    "Describe a task, or start from an example below. Each scenario runs on fixed data with",
    "Step 2 · 3 expected",
    "Finished in 3.1 s",
    "Steps 3 taken",
    "3 steps · 2.1 s · numbers computed by group_by, not by the model",
    "The answer appears when the last step finishes.",
    "The total revenue of the North region is 5,710.5.",
    "I can only do arithmetic and answer questions about attached CSV files, orders and contracts.",
    "Order 1043 has the status shipped.",
    "Running · waiting",
    "2 · 0",
    "\u2013 · \u2013",
    "Start 20 tasks",
    "Kill pod",
    "Killing vllm-6b8f7487d9-gt87b…",
    "Start replays recorded tasks through the gateway (make replay). Kill deletes the pod at once; "
    "its runs continue on another one.",
    "Replay running: 7 of 20 finished · make replay TRACE=experiments/results/traces/record-demo-"
    "e1044feb534c-c5cf306f32ee-e03c0981507c.jsonl PATTERN=steady PROFILE=real "
    "OUT=experiments/results/replays/demo-steady-20261127T181356.jsonl",
    "Replay done: 20 of 20 finished · make replay TRACE=x.jsonl PATTERN=steady PROFILE=real",
    "vllm-6b8f7487d9-gt87b killed at 18:14:19 UTC.",
    "retry 1",
    "99% cached",
    "Show steps",
    "Hide steps",
]
# One raw error or failure caption per pattern of the file, in its order.
FAULT_TEXTS = [
    "Traceback (most recent call last):",
    "TypeError: cannot read properties",
    "[object Object]",
    "Order undefined has the status shipped.",
    "The total is NaN.",
    "The answer is null",
    '{"detail": "Not Found"}',
    '{"message": "boom"}',
    "The app backend did not answer (HTTP 504).",
    "Request failed with status code 500",
    "Http failure response for /api/chats: 504 Gateway Timeout",
    "EngineCore encountered an issue",
    "The run did not start: the app backend did not answer.",
    "The chat could not be read.",
    "The run did not start: no files.",
    "The demo runner is not running: make demo-runner",
    "The demo runner refused the app backend's token: restart make demo-runner",
    "no other vLLM pod is Ready: vllm-a's runs would have nowhere to go",
    "Replay done: 20 of 20 finished, 1 call lost",
    "Replay failed (exit 2): 3 of 20 finished",
    "The run failed. agent.max_steps reached",
]


def test_the_file_loads_two_rounds_and_both_parts() -> None:
    assert REHEARSAL.rounds == 2
    assert REHEARSAL.ui_url == "http://localhost:4200"
    assert REHEARSAL.settle_seconds == 15
    assert REHEARSAL.practical.profile == "demo"
    assert [chat.pick for chat in REHEARSAL.practical.chats] == [
        "csv",
        "contracts",
        "support",
        "out_of_scope",
    ]
    assert [[turn.question.id for turn in chat.turns] for chat in REHEARSAL.practical.chats] == [
        ["group-total", "south-total-follow-up"],
        ["termination-diff"],
        ["status-1043"],
        ["poem"],
    ]
    assert REHEARSAL.infrastructure.profile == "real"
    assert REHEARSAL.infrastructure.pattern == "steady"
    assert REHEARSAL.infrastructure.kill_after_seconds == 60
    assert REHEARSAL.infrastructure.replay_timeout_seconds == 1200
    assert REHEARSAL.infrastructure.ready_timeout_seconds == 510
    assert REHEARSAL.infrastructure.pod_gone_seconds == 5


def test_the_hash_is_the_files_first_12_hex_digits() -> None:
    import hashlib

    assert REHEARSAL.hash == hashlib.sha256(REHEARSAL_PATH.read_bytes()).hexdigest()[:12]


def test_a_suite_turn_is_the_suites_question_word_for_word() -> None:
    """The questions P4.7 scored on the demo model, so the rehearsal asks nothing untested but its
    follow-up and its out-of-scope message."""
    by_id = {
        (scenario.name, question.id): question
        for scenario in SUITE.scenarios
        for question in scenario.questions
    }
    asked = [
        ("csv", "group-total"),
        ("contracts", "termination-diff"),
        ("support", "status-1043"),
    ]
    chats = REHEARSAL.practical.chats
    assert [chats[0].turns[0].question, chats[1].turns[0].question, chats[2].turns[0].question] == [
        by_id[key] for key in asked
    ]


def _sales_total(region: str) -> float:
    with (EXPERIMENTS / "data" / "sales.csv").open() as handle:
        return sum(
            float(row["revenue"]) for row in csv.DictReader(handle) if row["region"] == region
        )


def test_numbers_are_computed_from_the_chats_csv() -> None:
    csv_chat = REHEARSAL.practical.chats[0]
    assert csv_chat.turns[0].expected == pytest.approx(_sales_total("North"))
    assert csv_chat.turns[1].expected == pytest.approx(_sales_total("South"))
    assert _sales_total("South") != _sales_total("North")


def test_texts_and_fixed_replies_have_no_number() -> None:
    chats = REHEARSAL.practical.chats
    assert chats[1].turns[0].expected is None
    assert chats[3].turns[0].expected is None


def test_the_out_of_scope_reply_is_the_profiles_word_for_word() -> None:
    [turn] = REHEARSAL.practical.chats[3].turns
    assert turn.question.expected == {
        "kind": FIXED_REPLY,
        "reply": DEMO["agent"]["out_of_scope_reply"],
    }
    assert turn.question.expected_calls == []


def test_a_chat_attaches_its_scenarios_files_and_the_out_of_scope_chat_none() -> None:
    names = [[path.name for path in chat.files] for chat in REHEARSAL.practical.chats]
    assert names == [
        ["sales.csv"],
        ["contract-2025.txt", "contract-2026.txt"],
        ["orders.csv", "policies.txt"],
        [],
    ]
    assert all(path.is_file() for chat in REHEARSAL.practical.chats for path in chat.files)


def test_the_ui_gives_each_attached_file_the_suites_name_and_id() -> None:
    """The browser attaches a file from disk, so the UI names it by the file's basename and makes
    its id from the stem (ui/src/app/runs/run-files.ts): both must be what the suite's expected
    calls and answers were written against."""
    for chat in SPEC["practical"]["chats"]:
        if "files_of" not in chat:
            continue
        [scenario] = [s for s in SUITE.scenarios if s.name == chat["files_of"]]
        loaded = next(c for c in REHEARSAL.practical.chats if c.pick == chat["pick"])
        assert [path.name for path in loaded.files] == [file["name"] for file in scenario.files]
        assert [path.stem for path in loaded.files] == [file["id"] for file in scenario.files]


def test_the_run_timeout_is_the_agents_own_bound() -> None:
    assert REHEARSAL.practical.run_timeout == (
        DEMO["agent"]["max_steps"] * DEMO["agent"]["llm_timeout_seconds"]
    )
    assert REHEARSAL.practical.tolerance == DEMO["agent"]["answer_tolerance"]


def test_the_infrastructure_part_takes_the_patterns_runs_and_the_profiles_pods() -> None:
    assert REHEARSAL.infrastructure.runs == REAL["replay"]["patterns"]["steady"]["runs"] == 20
    assert REHEARSAL.infrastructure.replicas == REAL["replicas"] == 2


def _write(tmp_path: Path, edit: Callable[[dict[str, Any]], None]) -> Path:
    spec = yaml.safe_load(REHEARSAL_PATH.read_text())
    spec["practical"]["suite"] = str(EXPERIMENTS / "task-suite.yaml")
    edit(spec)
    path = tmp_path / "rehearsal.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def _set(keys: Sequence[str | int], value: Any) -> Callable[[dict[str, Any]], None]:
    def edit(spec: dict[str, Any]) -> None:
        target: Any = spec
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return edit


def _drop(keys: Sequence[str | int]) -> Callable[[dict[str, Any]], None]:
    def edit(spec: dict[str, Any]) -> None:
        target: Any = spec
        for key in keys[:-1]:
            target = target[key]
        del target[keys[-1]]

    return edit


def test_a_copy_of_the_file_with_the_suite_by_path_loads(tmp_path: Path) -> None:
    loaded = load_rehearsal(_write(tmp_path, lambda spec: None))
    assert loaded.practical == REHEARSAL.practical
    assert loaded.infrastructure == REHEARSAL.infrastructure


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (_set(["rounds"], 0), "rounds must be 1 or more"),
        (_set(["practical", "chats", 0, "turns", 0, "suite"], "nope"), "csv has no question nope"),
        (_set(["practical", "chats", 0, "files_of"], "nope"), "no scenario nope"),
        (
            _drop(["practical", "chats", 0, "files_of"]),
            "group-total: a suite question needs its chat's files_of",
        ),
        (
            _set(["practical", "chats", 0, "turns", 0], {}),
            "a turn is one of suite or question",
        ),
        (_set(["practical", "chats", 0, "turns"], []), "a chat needs a turn"),
        (_set(["infrastructure", "pattern"], "burst-fan-out"), "not in real's demo patterns"),
        (_set(["infrastructure", "profile"], "demo"), "demo has no demo buttons"),
        (_set(["raw_errors", 0], "("), "raw_errors: bad pattern"),
        (_set(["settle_seconds"], 0), "settle_seconds must be over 0"),
    ],
    ids=[
        "no rounds",
        "unknown question",
        "unknown scenario",
        "suite turn without files",
        "empty turn",
        "chat without turns",
        "pattern not offered",
        "profile without buttons",
        "bad regex",
        "no settle",
    ],
)
def test_a_broken_file_is_refused_before_anything_runs(
    tmp_path: Path, edit: Callable[[dict[str, Any]], None], message: str
) -> None:
    with pytest.raises(RehearsalError, match=re.escape(message)):
        load_rehearsal(_write(tmp_path, edit))


def test_a_fixed_reply_question_in_a_profile_without_one_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in ("demo", "real"):
        (config / f"{name}.yaml").write_text((REPO / "config" / f"{name}.yaml").read_text())
    profile = yaml.safe_load((config / "demo.yaml").read_text())
    del profile["agent"]["out_of_scope_reply"]
    (config / "demo.yaml").write_text(yaml.safe_dump(profile))
    with pytest.raises(
        RehearsalError, match=re.escape("poem: demo has no agent.out_of_scope_reply")
    ):
        load_rehearsal(_write(tmp_path, lambda spec: None), config)


# Faults on a page


def test_each_raw_error_pattern_catches_its_text() -> None:
    assert len(FAULT_TEXTS) == len(REHEARSAL.raw_errors)
    for pattern, text in zip(REHEARSAL.raw_errors, FAULT_TEXTS, strict=True):
        assert pattern.search(text), (pattern.pattern, text)


def test_every_fault_text_is_a_fault_on_a_page() -> None:
    for text in FAULT_TEXTS:
        assert page_faults(["Tenax", text], 0, 0, REHEARSAL.raw_errors) != [], text


def test_no_text_of_a_demo_that_goes_right_is_a_fault() -> None:
    assert page_faults(NORMAL_TEXTS, 0, 0, REHEARSAL.raw_errors) == []


def test_a_fault_names_the_pattern_and_the_line_once() -> None:
    lines = ["Tenax", "The chat could not be read.", "The chat could not be read."]
    assert page_faults(lines, 0, 0, REHEARSAL.raw_errors) == [
        "raw error 'could not be read': The chat could not be read."
    ]


def test_an_alert_and_a_failure_notice_are_faults_without_any_text() -> None:
    assert page_faults([], 1, 0, []) == ["1 alert shown"]
    assert page_faults([], 0, 1, []) == ["1 failure notice shown"]
    assert page_faults([], 0, 2, []) == ["2 failure notices shown"]
    assert page_faults([], 0, 0, []) == []


# A turn


def _run(
    answer: str,
    pick: str = "csv",
    calls: Sequence[tuple[str, dict[str, Any]]] = (("group_by", {}),),
    status: str = "done",
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [{"content": pick, "tool_calls": []}]
    steps += [
        {"content": None, "tool_calls": [{"name": n, "arguments": a, "result": {"value": 1}}]}
        for n, a in calls
    ]
    return {"status": status, "answer": answer, "steps": steps, "invalid_calls": 0}


NORTH = REHEARSAL.practical.chats[0].turns[0]
NORTH_ANSWER = f"The total revenue of the North region is {NORTH.expected}."
TOLERANCE = REHEARSAL.practical.tolerance


def test_a_right_turn_has_no_failure() -> None:
    assert judge_turn(NORTH, "csv", _run(NORTH_ANSWER), NORTH_ANSWER, TOLERANCE) == []


def test_the_page_may_wrap_the_answer_differently() -> None:
    shown = NORTH_ANSWER.replace(" the North", "\n the North")
    assert judge_turn(NORTH, "csv", _run(NORTH_ANSWER), shown, TOLERANCE) == []


def test_a_wrong_pick_fails_the_turn() -> None:
    run = _run(NORTH_ANSWER, pick="support")
    assert judge_turn(NORTH, "csv", run, NORTH_ANSWER, TOLERANCE) == [
        "group-total: step 1 picked support, not csv"
    ]


def test_a_wrong_answer_fails_the_turn() -> None:
    wrong = "The total revenue of the North region is 12."
    assert judge_turn(NORTH, "csv", _run(wrong), wrong, TOLERANCE) == [
        f"group-total: wrong answer (expected {NORTH.expected}): {wrong!r}"
    ]


def test_a_failed_run_fails_the_turn() -> None:
    failures = judge_turn(NORTH, "csv", _run(NORTH_ANSWER, status="failed"), "", TOLERANCE)
    assert "group-total: the run ended failed" in failures


def test_an_answer_the_page_does_not_show_fails_the_turn() -> None:
    assert judge_turn(NORTH, "csv", _run(NORTH_ANSWER), "Something else.", TOLERANCE) == [
        f"group-total: the page shows 'Something else.', not the run's answer {NORTH_ANSWER!r}"
    ]


def test_a_run_with_no_step_picked_nothing() -> None:
    run = {"status": "failed", "answer": "", "steps": [], "invalid_calls": 0}
    failures = judge_turn(NORTH, "csv", run, "", TOLERANCE)
    assert failures[0] == "group-total: step 1 picked None, not csv"


# A replay


def _demo(
    state: str = "done",
    finished: int = 20,
    total: int = 20,
    lost: int = 0,
    killed: str | None = "vllm-a",
    started_at: str = "2026-11-27T18:00:00Z",
) -> dict[str, Any]:
    return {
        "enabled": True,
        "poll_seconds": 5,
        "patterns": [{"name": "steady", "runs": 20}, {"name": "burst", "runs": 30}],
        "runner": {
            "replay": {
                "pattern": "steady",
                "command": "make replay",
                "started_at": started_at,
                "state": state,
                "runs_total": total,
                "runs_finished": finished,
                "calls_failed": lost,
                "exit_code": 0 if state == "done" else None,
            },
            "last_kill": None if killed is None else {"pod": killed, "at": "2026-11-27T18:01:00Z"},
        },
        "runner_error": None,
    }


INFRA = REHEARSAL.infrastructure


def test_a_replay_done_with_every_run_and_the_kill_taken_passes() -> None:
    assert judge_replay(_demo(), INFRA, "vllm-a") == []


@pytest.mark.parametrize(
    ("demo", "failure"),
    [
        (_demo(state="failed"), "the replay ended failed (exit None)"),
        (_demo(finished=19), "19 of 20 runs finished"),
        (_demo(total=30, finished=30), "the replay had 30 runs, steady has 20"),
        (_demo(lost=1), "1 call lost"),
        (_demo(killed=None), "no kill of vllm-a taken"),
        (_demo(killed="vllm-b"), "no kill of vllm-a taken"),
    ],
    ids=["failed", "a run unfinished", "another pattern", "a call lost", "no kill", "other pod"],
)
def test_each_way_a_replay_goes_wrong_fails_it(demo: dict[str, Any], failure: str) -> None:
    assert failure in judge_replay(demo, INFRA, "vllm-a")


def test_a_replacement_is_a_full_set_of_ready_pods_without_the_killed_one() -> None:
    assert replaced(["vllm-b", "vllm-c"], "vllm-a", 2)
    assert not replaced(["vllm-a", "vllm-b"], "vllm-a", 2)
    assert not replaced(["vllm-b"], "vllm-a", 2)


# The part


def test_a_part_is_green_only_when_every_round_ran_and_passed() -> None:
    passed = Round("practical", 1)
    failed = Round("practical", 2, failures=["x"])
    assert part_green([passed, replace(passed, number=2)], 2)
    assert not part_green([passed], 2)
    assert not part_green([passed, failed], 2)
    assert not part_green([], 2)


def test_the_results_go_in_one_folder_per_part_stamped_with_the_hash() -> None:
    now = datetime(2026, 11, 27, 18, 13, 56, tzinfo=UTC)
    assert results_dir(REHEARSAL, "practical", now) == (
        EXPERIMENTS / "results" / f"demo-rehearsal-{REHEARSAL.hash}-practical-20261127T181356"
    )


# Waiting on the page


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Quiet:
    """A page with nothing on it but what a test sets."""

    def __init__(self) -> None:
        self.shown_faults: list[str] = []
        self.horses = 0

    def faults(self) -> list[str]:
        return self.shown_faults

    def working(self) -> int:
        return self.horses


def test_a_wait_returns_what_it_waited_for() -> None:
    clock = Clock()
    waiter = Waiter(Quiet(), clock, clock.sleep, 1.0)
    assert waiter.until(lambda: "ok" if clock.now >= 3 else None, 10, "three seconds") == "ok"
    assert clock.now == 3


def test_a_wait_fails_on_a_fault_shown_before_it_ends() -> None:
    page = Quiet()
    page.shown_faults = ["1 alert shown"]
    clock = Clock()
    with pytest.raises(RoundFailed, match="the page shows: 1 alert shown"):
        Waiter(page, clock, clock.sleep, 1.0).until(lambda: "ok", 10, "anything")


def test_a_wait_fails_after_its_timeout_naming_what_it_waited_for() -> None:
    clock = Clock()
    with pytest.raises(RoundFailed, match="the answer to x not shown after 5 s"):
        Waiter(Quiet(), clock, clock.sleep, 1.0).until(lambda: None, 5, "the answer to x")
    assert clock.now == 5


def test_a_horse_still_working_after_the_settle_is_a_stuck_spinner() -> None:
    page = Quiet()
    page.horses = 1
    clock = Clock()
    with pytest.raises(RoundFailed, match="stuck spinner: 1 working horse 15 s after the answer"):
        Waiter(page, clock, clock.sleep, 1.0).settle(15, "the answer")


def test_a_horse_that_stops_within_the_settle_is_fine() -> None:
    page = Quiet()
    page.horses = 1
    clock = Clock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        page.horses = 0 if clock.now >= 4 else 1

    Waiter(page, clock, sleep, 1.0).settle(15, "the answer")
    assert clock.now == 4


# The practical round, on a fake UI


class FakeChatPage(Quiet):
    """The UI for the practical part: each message's run answers answer_after seconds after it is
    sent, with the run body the test gives for that message."""

    def __init__(self, clock: Clock, runs: Sequence[dict[str, Any]], answer_after: float = 3):
        super().__init__()
        self.clock = clock
        self.runs = list(runs)
        self.answer_after = answer_after
        self.events: list[str] = []
        self.sent: list[tuple[str, list[str]]] = []
        self.chat: list[tuple[int, float]] = []
        self.chat_number = 0
        self.shown_instead: dict[int, str] = {}

    def register(self, email: str, password: str) -> None:
        self.events.append("register")

    def new_chat(self) -> None:
        self.events.append("new chat")
        self.chat = []

    def send(self, text: str, files: Sequence[Path]) -> None:
        self.events.append(f"send {text}")
        self.sent.append((text, [path.name for path in files]))
        if not self.chat:
            self.chat_number += 1
        self.chat.append((len(self.sent) - 1, self.clock.now))

    def answers(self) -> list[str]:
        shown = []
        for index, sent_at in self.chat:
            if self.clock.now - sent_at < self.answer_after:
                break
            shown.append(self.shown_instead.get(index, self.runs[index]["answer"]))
        return shown

    def chat_id(self) -> str | None:
        return str(self.chat_number) if self.chat else None

    def fetch(self, path: str) -> Any:
        if path == f"/api/chats/{self.chat_number}":
            messages = []
            for index, _ in self.chat:
                messages.append({"role": "user", "run_id": None})
                messages.append({"role": "assistant", "run_id": f"run-{index}"})
            return {"messages": messages}
        match = re.fullmatch(r"/api/runs/run-(\d+)", path)
        assert match, path
        return self.runs[int(match.group(1))]

    def screenshot(self, name: str) -> None:
        self.events.append(f"screenshot {name}")


def _right_runs() -> list[dict[str, Any]]:
    chats = REHEARSAL.practical.chats
    south = chats[0].turns[1].expected
    reply = DEMO["agent"]["out_of_scope_reply"]
    return [
        _run(NORTH_ANSWER),
        _run(f"The South region made {south} in total."),
        _run(
            "The termination notice went from 30 days to 60 days.",
            pick="contracts",
            calls=[("diff_clauses", {"clause": "termination"})],
        ),
        _run(
            "Order 1043 is shipped.",
            pick="support",
            calls=[("get_order", {"order_id": "1043"})],
        ),
        _run(reply, pick="out_of_scope", calls=[]),
    ]


def _practical(page: FakeChatPage, clock: Clock) -> Round:
    waiter = Waiter(page, clock, clock.sleep, 1.0)
    return practical_round(page, REHEARSAL, waiter, 1, "a@demo-rehearsal.invalid", "pw")


def test_a_practical_round_that_goes_right_passes_with_every_turn_recorded() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs())
    result = _practical(page, clock)
    assert result.failures == []
    assert result.passed
    assert [step["question"] for step in result.steps] == [
        "group-total",
        "south-total-follow-up",
        "termination-diff",
        "status-1043",
        "poem",
    ]
    assert [step["run_id"] for step in result.steps] == [f"run-{i}" for i in range(5)]
    assert [step["picked"] for step in result.steps] == [
        "csv",
        "csv",
        "contracts",
        "support",
        "out_of_scope",
    ]
    assert all(step["seconds"] == 3 for step in result.steps)


def test_a_chat_attaches_its_files_with_its_first_message_only() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs())
    _practical(page, clock)
    assert [files for _, files in page.sent] == [
        ["sales.csv"],
        [],
        ["contract-2025.txt", "contract-2026.txt"],
        ["orders.csv", "policies.txt"],
        [],
    ]


def test_each_chat_starts_from_new_chat_and_every_turn_is_photographed() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs())
    _practical(page, clock)
    assert page.events[0] == "register"
    assert [e for e in page.events if not e.startswith("send")] == [
        "register",
        "new chat",
        "screenshot 1-1-group-total",
        "screenshot 1-2-south-total-follow-up",
        "new chat",
        "screenshot 2-1-termination-diff",
        "new chat",
        "screenshot 3-1-status-1043",
        "new chat",
        "screenshot 4-1-poem",
    ]


def test_the_follow_up_is_judged_on_its_own_run() -> None:
    runs = _right_runs()
    runs[1] = _run("The South region made 1 in total.")
    clock = Clock()
    page = FakeChatPage(clock, runs)
    result = _practical(page, clock)
    assert result.failures == [
        f"south-total-follow-up: wrong answer (expected "
        f"{REHEARSAL.practical.chats[0].turns[1].expected}): 'The South region made 1 in total.'"
    ]


def test_the_round_stops_at_the_first_failing_turn() -> None:
    runs = _right_runs()
    runs[2] = _run("I do not know.", pick="contracts", calls=[])
    clock = Clock()
    page = FakeChatPage(clock, runs)
    result = _practical(page, clock)
    assert not result.passed
    assert len(page.sent) == 3
    assert [step["question"] for step in result.steps] == [
        "group-total",
        "south-total-follow-up",
        "termination-diff",
    ]
    assert page.events[-1] == "screenshot failed"


def test_an_answer_never_shown_fails_the_round_at_the_agents_bound() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs(), answer_after=10_000)
    result = _practical(page, clock)
    timeout = REHEARSAL.practical.run_timeout
    assert result.failures == [f"the answer to group-total not shown after {timeout:.0f} s"]


def test_a_fault_on_the_page_fails_the_round() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs())
    page.shown_faults = ["raw error 'did not start': The run did not start: x."]
    result = _practical(page, clock)
    assert result.failures == [
        "the page shows: raw error 'did not start': The run did not start: x."
    ]


def test_a_stuck_horse_after_an_answer_fails_the_round() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs())
    page.horses = 1
    result = _practical(page, clock)
    assert result.failures == [
        "stuck spinner: 1 working horse 15 s after the answer to group-total"
    ]


def test_the_page_showing_another_answer_fails_the_round() -> None:
    clock = Clock()
    page = FakeChatPage(clock, _right_runs())
    page.shown_instead[0] = "Something else."
    result = _practical(page, clock)
    assert result.failures == [
        f"group-total: the page shows 'Something else.', not the run's answer {NORTH_ANSWER!r}"
    ]


# The infrastructure round, on a fake UI and cluster


class FakeCluster:
    def __init__(self, clock: Clock, pods: Sequence[str]) -> None:
        self.clock = clock
        self.pods = list(pods)
        # (when, the pods Ready from then on)
        self.changes: list[tuple[float, list[str]]] = []

    def ready_pods(self) -> list[str]:
        while self.changes and self.clock.now >= self.changes[0][0]:
            self.pods = self.changes.pop(0)[1]
        return list(self.pods)


class FakePanelPage(Quiet):
    """The cluster panel: Start begins a replay that ends replay_seconds later as the test says;
    Kill takes the first pod on offer, which the cluster replaces replace_seconds later."""

    def __init__(
        self,
        clock: Clock,
        cluster: FakeCluster,
        replay_seconds: float = 400,
        replace_seconds: float = 150,
        ending: dict[str, Any] | None = None,
        runner_error: str | None = None,
        panel_lag: float = 1,
    ) -> None:
        super().__init__()
        self.clock = clock
        self.cluster = cluster
        self.replay_seconds = replay_seconds
        self.replace_seconds = replace_seconds
        self.ending = ending if ending is not None else _demo(killed=None)
        self.runner_error = runner_error
        # How long the panel still lists a killed pod after the press.
        self.panel_lag = panel_lag
        self.events: list[str] = []
        self.started: float | None = None
        self.killed: tuple[str, float] | None = None

    def register(self, email: str, password: str) -> None:
        self.events.append("register")

    def fetch(self, path: str) -> Any:
        assert path == "/api/demo"
        if self.runner_error is not None:
            return {**_demo(), "runner": None, "runner_error": self.runner_error}
        if self.started is None:
            body = _demo(killed=None, started_at="2026-11-27T17:00:00Z")
        elif self.clock.now - self.started < self.replay_seconds:
            body = _demo(state="running", finished=3, killed=None)
        else:
            body = {**self.ending, "runner": dict(self.ending["runner"])}
        if self.killed is not None:
            body["runner"] = {**body["runner"], "last_kill": {"pod": self.killed[0], "at": "x"}}
        return body

    def start(self, pattern: str) -> None:
        self.events.append(f"start {pattern} at {self.clock.now:.0f}")
        self.started = self.clock.now

    def kill(self) -> str:
        pod = self.cluster.ready_pods()[0]
        self.events.append(f"kill {pod} at {self.clock.now:.0f}")
        self.killed = (pod, self.clock.now)
        rest = [p for p in self.cluster.pods if p != pod]
        self.cluster.pods = rest
        self.cluster.changes.append((self.clock.now + self.replace_seconds, [*rest, "vllm-new"]))
        return pod

    def pods_shown(self) -> list[str]:
        if self.killed is not None and self.clock.now - self.killed[1] < self.panel_lag:
            return [self.killed[0], *self.cluster.pods]
        return list(self.cluster.pods)

    def screenshot(self, name: str) -> None:
        self.events.append(f"screenshot {name}")


def _infrastructure(
    page: FakePanelPage, cluster: FakeCluster, clock: Clock, rehearsal: Rehearsal = REHEARSAL
) -> Round:
    waiter = Waiter(page, clock, clock.sleep, 1.0)
    return infrastructure_round(
        page, cluster, rehearsal, waiter, 1, "a@demo-rehearsal.invalid", "pw"
    )


def test_an_infrastructure_round_that_goes_right_passes() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster)
    result = _infrastructure(page, cluster, clock)
    assert result.failures == []
    [step] = result.steps
    assert step["killed"] == "vllm-a"
    assert step["kill_at_s"] == 60
    assert step["replaced_after_s"] == 150
    assert step["gone_from_panel_after_s"] == 1
    assert step["replay"]["runs_finished"] == 20
    assert cluster.pods == ["vllm-b", "vllm-new"]


def test_kill_is_pressed_kill_after_seconds_after_start() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster)
    _infrastructure(page, cluster, clock)
    assert [e for e in page.events if not e.startswith("screenshot")] == [
        "register",
        "start steady at 0",
        "kill vllm-a at 60",
    ]
    assert [e for e in page.events if e.startswith("screenshot")] == [
        "screenshot started",
        "screenshot killed",
        "screenshot done",
    ]


def test_the_round_waits_for_every_pod_ready_before_it_starts() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-b"])
    cluster.changes.append((100, ["vllm-b", "vllm-c"]))
    page = FakePanelPage(clock, cluster)
    result = _infrastructure(page, cluster, clock)
    assert result.passed
    assert "start steady at 100" in page.events


def test_pods_not_ready_in_time_fail_the_round_before_anything_is_pressed() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-b"])
    page = FakePanelPage(clock, cluster)
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["2 vLLM pods Ready before the round not shown after 510 s"]
    assert page.events == ["screenshot failed"]


def test_a_runner_that_does_not_answer_fails_the_round_with_its_caption() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster, runner_error="The demo runner is not running")
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["the demo buttons cannot be used: The demo runner is not running"]


def test_a_killed_pod_the_panel_still_lists_after_pod_gone_seconds_fails_the_round() -> None:
    """What the user saw on 2026-11-27 (card 80): the panel trailing a kill by up to ~10 s."""
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster, panel_lag=10)
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["the panel without vllm-a not shown after 5 s"]


def test_a_killed_pod_gone_from_the_panel_within_pod_gone_seconds_passes() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster, panel_lag=5)
    result = _infrastructure(page, cluster, clock)
    assert result.passed
    assert result.steps[0]["gone_from_panel_after_s"] == 5


def test_a_replay_that_never_ends_fails_at_its_timeout() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster, replay_seconds=10_000)
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["the replay's end not shown after 1200 s"]
    assert clock.now == 1200


def test_a_replacement_never_ready_fails_the_round() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster, replace_seconds=10_000)
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["vllm-a's replacement Ready not shown after 510 s"]


def test_a_replay_with_a_lost_call_fails_the_round() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster, ending=_demo(lost=1, killed=None))
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["1 call lost"]


def test_a_replay_that_never_started_fails_the_round() -> None:
    """The runner keeps its last replay, so the round's is told by a new start time."""
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster)
    page.start = lambda pattern: None  # type: ignore[method-assign]
    result = _infrastructure(page, cluster, clock)
    assert result.failures == [
        f"the replay started not shown after {REHEARSAL.settle_seconds:.0f} s"
    ]


def test_a_stuck_horse_after_the_replay_fails_the_round() -> None:
    clock = Clock()
    cluster = FakeCluster(clock, ["vllm-a", "vllm-b"])
    page = FakePanelPage(clock, cluster)
    page.horses = 2
    result = _infrastructure(page, cluster, clock)
    assert result.failures == ["stuck spinner: 2 working horses 15 s after the replay's end"]


def test_a_round_record_holds_its_failures_and_steps() -> None:
    result = Round("practical", 1, failures=["x"], steps=[{"question": "q"}])
    assert result.record() == {
        "part": "practical",
        "round": 1,
        "passed": False,
        "failures": ["x"],
        "steps": [{"question": "q"}],
    }


def test_chat_and_turn_are_plain_values() -> None:
    question = Question("q", "text?", [], {"kind": FIXED_REPLY, "reply": "r"})
    assert Chat(files=[], pick="out_of_scope", turns=[Turn(question, None)]) == Chat(
        files=[], pick="out_of_scope", turns=[Turn(question, None)]
    )
