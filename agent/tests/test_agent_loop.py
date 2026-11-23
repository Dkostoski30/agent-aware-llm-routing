"""The agent loop against a mocked LLM (P1.3a-1): both D10 modes, D1 headers, caps, history, the
record of every LLM call (P2.2a), and step 1's scenario pick (D13, P4.8).

The LLM is httpx.MockTransport answering each request with the next scripted assistant message, in
the shape vLLM's /v1/chat/completions returns it. Step 1's request (the one with structured
outputs) is answered with the test's scenario instead, and kept apart in ScriptedLLM.picks, so
requests and bodies() index the steps after it.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_runtime.config import AgentConfig, Sampling, Scenario, ToolChoice
from agent_runtime.csv_tools import Table, group_by, parse_file
from agent_runtime.documents import Document, parse_document, read_document
from agent_runtime.history import CountError
from agent_runtime.llm import LLMClient
from agent_runtime.loop import (
    REPEATED_CALL,
    RunResult,
    SavedRun,
    StepCallback,
    Tracking,
    run_task,
)
from agent_runtime.retry import NO_RETRY, RetryPolicy
from agent_runtime.run_tracker import RunTracker
from agent_runtime.store import CallRecord
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.anyio

HEADERS_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "headers.v1.json"
MODES: list[ToolChoice] = ["auto", "required"]
RUN_ID = "run-0123456789ab"
TASK = "What is 7 times 8 plus 3?"
SYSTEM_PROMPT = "You are a calculator agent."
# The pick plus four: the repeated-call test needs five calls (P4.8).
MAX_STEPS = 5
MAX_INVALID_CALLS = 2
MAX_RESULT_ROWS = 20
MAX_RESULT_CHARS = 560
RESULT_DECIMALS = 2
SCENARIO_PROMPT = "Which scenario is the question above?"
OUT_OF_SCOPE_REPLY = "I can only help with arithmetic, CSV files, orders and contracts."
# Every scenario's own value, unlike the default's 3, so a test sees which one a call carried.
SCENARIO_EXPECTED_STEPS: dict[Scenario, int] = {
    "csv": 4,
    "contracts": 5,
    "support": 6,
    "arithmetic": 2,
}
SALES = parse_file(
    "sales", "sales.csv", (Path(__file__).parent / "fixtures" / "sales.csv").read_text()
)
FIXTURES = Path(__file__).parent / "fixtures"
CONTRACTS = [
    parse_document(
        f"contract-{year}",
        f"contract-{year}.txt",
        (FIXTURES / f"contract-{year}.txt").read_text(),
    )
    for year in (2025, 2026)
]


def make_config(mode: ToolChoice) -> AgentConfig:
    return AgentConfig(
        model_name="test-model",
        tool_choice=mode,
        max_steps=MAX_STEPS,
        max_invalid_calls=MAX_INVALID_CALLS,
        expected_steps_default=3,
        llm_timeout_seconds=5,
        retry=NO_RETRY,
        history_ttl_seconds=60,
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20),
        system_prompt=SYSTEM_PROMPT,
        max_file_bytes=1000,
        max_files=2,
        max_result_rows=MAX_RESULT_ROWS,
        max_result_chars=MAX_RESULT_CHARS,
        result_decimals=RESULT_DECIMALS,
        max_clause_chars=1600,
        max_diff_changes=20,
        diff_context_words=3,
        events_poll_seconds=0.01,
        end_of_run=False,
        scenario_prompt=SCENARIO_PROMPT,
        out_of_scope_reply=OUT_OF_SCOPE_REPLY,
        scenario_expected_steps=SCENARIO_EXPECTED_STEPS,
        history_budget_tokens=1000,
    )


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def calc(expression: str, call_id: str = "call-1") -> dict[str, Any]:
    return tool_call("calculator", {"expression": expression}, call_id)


def calls(*tool_calls: dict[str, Any]) -> dict[str, Any]:
    # vLLM also returns fields the history must not keep.
    return {"role": "assistant", "content": None, "tool_calls": list(tool_calls), "refusal": None}


def text(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tool_calls": [], "reasoning_content": None}


def final(mode: ToolChoice, answer: str) -> dict[str, Any]:
    """The reply that ends a run in this mode (D10)."""
    if mode == "auto":
        return text(answer)
    return calls(tool_call("final_answer", {"answer": answer}))


class ScriptedLLM:
    """Answers each chat request with the next scripted message and records the requests.

    finish_reason is what vLLM sets unless one is given; failure replaces every answer with that
    response, or raises that error. usage and fingerprint (system_fingerprint), when given, are in
    every answer; without them the answer has neither. scenario is step 1's pick; a failure fails
    step 1 too, which then counts in requests.
    """

    def __init__(
        self,
        replies: list[dict[str, Any]],
        finish_reason: str | None = None,
        failure: httpx.Response | httpx.HTTPError | None = None,
        usage: dict[str, Any] | None = None,
        fingerprint: str | None = None,
        scenario: str = "arithmetic",
    ) -> None:
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.failure = failure
        self.usage = usage
        self.fingerprint = fingerprint
        self.scenario = scenario
        self.requests: list[httpx.Request] = []
        self.picks: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if isinstance(self.failure, httpx.HTTPError):
            self.requests.append(request)
            raise self.failure
        if self.failure is not None:
            self.requests.append(request)
            return self.failure
        if "structured_outputs" in json.loads(request.content):
            self.picks.append(request)
            return self.answer(text(self.scenario), "stop")
        self.requests.append(request)
        message = self.replies.pop(0)
        return self.answer(message, self.finish_reason)

    def answer(self, message: dict[str, Any], finish_reason: str | None) -> httpx.Response:
        finish_reason = finish_reason or ("tool_calls" if message["tool_calls"] else "stop")
        choice = {"message": message, "finish_reason": finish_reason}
        body: dict[str, Any] = {"choices": [choice]}
        if self.usage is not None:
            body["usage"] = self.usage
        if self.fingerprint is not None:
            body["system_fingerprint"] = self.fingerprint
        return httpx.Response(200, json=body)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


async def run(
    mode: ToolChoice,
    llm: ScriptedLLM,
    seed: int | None = 7,
    tables: Sequence[Table] = (),
    documents: Sequence[Document] = (),
    retry: RetryPolicy = NO_RETRY,
    history: Sequence[Mapping[str, str]] = (),
) -> tuple[RunResult, ScriptedLLM]:
    config = replace(make_config(mode), retry=retry)
    transport = httpx.MockTransport(llm.handle)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
        llm_client = LLMClient(http, config)
        result = await run_task(
            TASK,
            RUN_ID,
            seed,
            config,
            llm_client,
            tables=tables,
            documents=documents,
            history=history,
        )
    return result, llm


@pytest.mark.parametrize("mode", MODES)
async def test_tool_results_are_fed_back(mode: ToolChoice) -> None:
    script = [calls(calc("7*8")), calls(calc("56+3")), final(mode, "59")]
    result, llm = await run(mode, ScriptedLLM(script))
    assert (result.status, result.answer, result.steps) == ("done", "59", 4)
    assert llm.bodies()[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": json.dumps({"result": 56}),
    }


@pytest.mark.parametrize("mode", MODES)
async def test_history_keeps_role_content_and_tool_calls_only(mode: ToolChoice) -> None:
    result, _ = await run(mode, ScriptedLLM([calls(calc("7*8")), final(mode, "56")]))
    final_message: dict[str, Any] = {"role": "assistant", "content": "56"}
    if mode == "required":
        final_message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [tool_call("final_answer", {"answer": "56"})],
        }
    assert result.messages == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": "arithmetic", "scenario": True},
        {"role": "assistant", "content": None, "tool_calls": [calc("7*8")]},
        {"role": "tool", "tool_call_id": "call-1", "content": json.dumps({"result": 56})},
        final_message,
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_headers_follow_the_schema_and_count_every_call(mode: ToolChoice) -> None:
    script = [calls(calc("7^8")), calls(calc("7*8")), final(mode, "56")]
    _, llm = await run(mode, ScriptedLLM(script))
    validator = Draft202012Validator(json.loads(HEADERS_SCHEMA.read_text()))
    sent = [
        {name: value for name, value in request.headers.items() if name.startswith("x-")}
        for request in [*llm.picks, *llm.requests]
    ]
    for headers in sent:
        validator.validate(headers)
        assert headers["x-run-id"] == RUN_ID
        assert "x-parallel-group" not in headers
    assert [headers["x-step"] for headers in sent] == ["1", "2", "3", "4"]
    # The default until step 1 has picked arithmetic, then arithmetic's (D13).
    assert [headers["x-expected-steps"] for headers in sent] == ["3", "2", "2", "2"]


@pytest.mark.parametrize("mode", MODES)
async def test_request_body(mode: ToolChoice) -> None:
    _, llm = await run(mode, ScriptedLLM([final(mode, "59")]))
    body = llm.bodies()[0]
    assert body["model"] == "test-model"
    assert body["stream"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert (body["temperature"], body["top_p"], body["top_k"]) == (0.7, 0.8, 20)
    assert body["seed"] == 7
    assert body["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": TASK},
    ]


async def test_no_seed_is_sent_when_the_run_has_none() -> None:
    _, llm = await run("auto", ScriptedLLM([text("59")]), seed=None)
    assert "seed" not in llm.bodies()[0]


@pytest.mark.parametrize(
    ("mode", "tool_names"),
    [("auto", ["calculator"]), ("required", ["calculator", "final_answer"])],
)
async def test_tool_choice_and_tool_list_per_mode(mode: ToolChoice, tool_names: list[str]) -> None:
    _, llm = await run(mode, ScriptedLLM([final(mode, "59")]))
    body = llm.bodies()[0]
    assert body["tool_choice"] == mode
    assert [tool["function"]["name"] for tool in body["tools"]] == tool_names


@pytest.mark.parametrize("mode", MODES)
async def test_final_answer_ends_the_run(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([final(mode, "59")]))
    assert (result.status, result.answer, result.invalid_calls) == ("done", "59", 0)
    assert len(llm.requests) == 1


async def test_required_final_answer_ends_the_run_before_later_calls() -> None:
    reply = calls(tool_call("final_answer", {"answer": "59"}), calc("1+1", call_id="call-2"))
    result, llm = await run("required", ScriptedLLM([reply]))
    assert (result.status, result.answer) == ("done", "59")
    assert result.messages[-1]["role"] == "assistant"
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_calls_of_one_response_run_in_order_as_one_step(mode: ToolChoice) -> None:
    reply = calls(calc("7*8", call_id="call-a"), calc("1+2", call_id="call-b"))
    result, llm = await run(mode, ScriptedLLM([reply, final(mode, "59")]))
    assert result.steps == 3
    assert llm.bodies()[1]["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "call-a", "content": json.dumps({"result": 56})},
        {"role": "tool", "tool_call_id": "call-b", "content": json.dumps({"result": 3})},
    ]
    assert llm.requests[1].headers["x-step"] == "3"


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_enum_is_retried_with_the_allowed_values(mode: ToolChoice) -> None:
    # The calculator has no enum since P1.6d; the aggregate enum of group_by stands in.
    script = [calls(group("region", "revenue", "median")), final(mode, "South")]
    result, llm = await run(mode, ScriptedLLM(script, scenario="csv"), tables=[SALES])
    assert (result.status, result.answer, result.invalid_calls) == ("done", "South", 1)
    feedback = llm.bodies()[1]["messages"][-1]
    assert (feedback["role"], feedback["tool_call_id"]) == ("tool", "call-1")
    for allowed in ("'sum'", "'mean'", "'min'", "'max'"):
        assert allowed in json.loads(feedback["content"])["error"]


@pytest.mark.parametrize("mode", MODES)
async def test_division_by_zero_goes_back_as_a_tool_error(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([calls(calc("1/0")), final(mode, "none")]))
    assert (result.status, result.invalid_calls) == ("done", 0)
    assert llm.bodies()[1]["messages"][-1]["content"] == json.dumps({"error": "division by zero"})


async def test_auto_marker_in_text_is_retried() -> None:
    broken = text(
        '<tool_call>\n{"name": "calculator", "arguments": {"op": "add", "a": 1\n</tool_call>'
    )
    result, llm = await run("auto", ScriptedLLM([broken, text("59")]))
    assert (result.status, result.answer, result.invalid_calls) == ("done", "59", 1)
    history = llm.bodies()[1]["messages"]
    assert history[-2] == {"role": "assistant", "content": broken["content"]}
    assert history[-1]["role"] == "user"
    assert "tool call that could not be parsed" in history[-1]["content"]


@pytest.mark.parametrize("empty", [text(""), calls()], ids=["empty-string", "null"])
async def test_auto_empty_text_is_retried(empty: dict[str, Any]) -> None:
    result, llm = await run("auto", ScriptedLLM([empty, text("59")]))
    assert (result.status, result.answer, result.invalid_calls) == ("done", "59", 1)
    feedback = llm.bodies()[1]["messages"][-1]
    assert feedback["role"] == "user"
    assert feedback["content"].startswith("Error: ")


async def test_required_text_without_a_tool_call_is_invalid() -> None:
    result, llm = await run("required", ScriptedLLM([text("59"), final("required", "59")]))
    assert (result.status, result.answer, result.invalid_calls) == ("done", "59", 1)
    feedback = llm.bodies()[1]["messages"][-1]
    assert feedback["role"] == "user"
    assert "calculator, final_answer" in feedback["content"]


@pytest.mark.parametrize("mode", MODES)
async def test_step_cap_fails_the_run(mode: ToolChoice) -> None:
    # Step 1 is the pick, so MAX_STEPS - 1 calls are left for tools.
    result, llm = await run(mode, ScriptedLLM([calls(calc("1+1"))] * (MAX_STEPS - 1)))
    assert (result.status, result.answer, result.steps) == ("failed", None, MAX_STEPS)
    assert result.error is not None
    assert "agent.max_steps" in result.error
    assert len(llm.requests) == MAX_STEPS - 1


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_call_cap_fails_the_run(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([calls(calc("2^3"))] * MAX_INVALID_CALLS))
    assert (result.status, result.invalid_calls) == ("failed", MAX_INVALID_CALLS)
    assert result.error is not None
    assert "agent.max_invalid_calls" in result.error
    assert len(llm.requests) == MAX_INVALID_CALLS


@pytest.mark.parametrize("mode", MODES)
async def test_http_error_fails_the_run(mode: ToolChoice) -> None:
    failure = httpx.Response(500, json={"error": "scripted failure"})
    result, llm = await run(mode, ScriptedLLM([], failure=failure))
    assert (result.status, result.steps) == ("failed", 1)
    assert result.error is not None
    assert "500" in result.error
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_timeout_fails_the_run(mode: ToolChoice) -> None:
    failure = httpx.ReadTimeout("scripted timeout")
    result, llm = await run(mode, ScriptedLLM([], failure=failure))
    assert (result.status, result.steps) == ("failed", 1)
    assert result.error is not None
    assert "ReadTimeout" in result.error
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_response_without_choices_fails_the_run(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([], failure=httpx.Response(200, json={})))
    assert (result.status, result.steps) == ("failed", 1)
    assert result.error is not None
    assert "LLMResponseError" in result.error
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_length_finish_reason_fails_the_run(mode: ToolChoice) -> None:
    llm = ScriptedLLM([text("7 times 8 is 56, and 56 plus"), final(mode, "59")], "length")
    result, _ = await run(mode, llm)
    assert (result.status, result.answer, result.steps) == ("failed", None, 2)
    assert result.error is not None
    assert "max_model_len" in result.error
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_call_is_logged_with_its_reason(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    await run(mode, ScriptedLLM([calls(calc("2^3")), final(mode, "8")]))
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    # Log fields are record attributes; "run-id" is not a Python name, so they come from __dict__.
    fields = warnings[0].__dict__
    assert (fields["run-id"], fields["step"]) == (RUN_ID, 2)
    assert "'2^3'" in fields["reason"]
    own = [record for record in caplog.records if record.name.startswith("agent_runtime")]
    assert own
    assert all(record.__dict__["run-id"] == RUN_ID for record in own)


@pytest.mark.parametrize("mode", MODES)
async def test_llm_call_is_logged_with_duration_and_finish_reason(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    await run(mode, ScriptedLLM([calls(calc("7*8")), final(mode, "56")]))
    llm_calls = [record.__dict__ for record in caplog.records if record.getMessage() == "llm call"]
    assert [fields["step"] for fields in llm_calls] == [1, 2, 3]
    assert llm_calls[0]["scenario"] == "arithmetic"
    for fields in llm_calls:
        assert isinstance(fields["duration_ms"], int | float)
        assert fields["duration_ms"] >= 0
    expected = ["stop", "tool_calls", "tool_calls" if mode == "required" else "stop"]
    assert [fields["finish_reason"] for fields in llm_calls] == expected


@pytest.mark.parametrize("mode", MODES)
async def test_on_step_sees_every_step_that_leaves_the_run_going(mode: ToolChoice) -> None:
    seen: list[tuple[int, int, int, list[dict[str, Any]]]] = []

    async def on_step(
        step: int,
        invalid_calls: int,
        expected_steps: int,
        messages: list[dict[str, Any]],
        records: list[CallRecord],
    ) -> None:
        seen.append((step, invalid_calls, expected_steps, messages))

    llm = ScriptedLLM([calls(calc("2^3")), calls(calc("7*8")), final(mode, "56")])
    config = make_config(mode)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        result = await run_task(TASK, RUN_ID, 7, config, LLMClient(http, config), on_step)
    assert result.status == "done"
    # The pick is a step too, saved with the scenario's expected steps (D13).
    assert [(step, invalid, expected) for step, invalid, expected, _ in seen] == [
        (1, 0, 2),
        (2, 1, 2),
        (3, 1, 2),
    ]
    assert [messages for _, _, _, messages in seen] == [
        result.messages[:3],
        result.messages[:5],
        result.messages[:7],
    ]


def group(by: str, value: str, agg: str, call_id: str = "call-1") -> dict[str, Any]:
    return tool_call("group_by", {"file": "sales", "by": by, "value": value, "agg": agg}, call_id)


@pytest.mark.parametrize("mode", MODES)
async def test_csv_tool_result_is_fed_back(mode: ToolChoice) -> None:
    script = [calls(group("region", "revenue", "sum")), final(mode, "South")]
    result, llm = await run(mode, ScriptedLLM(script, scenario="csv"), tables=[SALES])
    assert (result.status, result.answer, result.invalid_calls) == ("done", "South", 0)
    expected = group_by(
        SALES, "region", "revenue", "sum", MAX_RESULT_ROWS, MAX_RESULT_CHARS, RESULT_DECIMALS
    )
    assert llm.bodies()[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": json.dumps(expected),
    }


async def test_the_profiles_limit_reaches_a_csv_tools_result() -> None:
    long_ids = parse_file("sales", "sales.csv", f"region,revenue\n{'a' * 561},1\n")
    script = [calls(group("region", "revenue", "sum")), final("auto", "done")]
    _, llm = await run("auto", ScriptedLLM(script, scenario="csv"), tables=[long_ids])
    assert "too long to list" in llm.bodies()[1]["messages"][-1]["content"]


@pytest.mark.parametrize("mode", MODES)
async def test_tool_schemas_offer_the_runs_files(mode: ToolChoice) -> None:
    llm = ScriptedLLM([final(mode, "none")], scenario="csv")
    _, llm = await run(mode, llm, tables=[SALES])
    tools = {tool["function"]["name"]: tool["function"] for tool in llm.bodies()[0]["tools"]}
    properties = tools["group_by"]["parameters"]["properties"]
    assert properties["file"]["enum"] == ["sales"]
    assert properties["by"]["enum"] == ["region", "product", "units", "revenue"]


@pytest.mark.parametrize("mode", MODES)
async def test_text_value_column_is_a_tool_error_not_an_invalid_call(mode: ToolChoice) -> None:
    script = [calls(group("region", "product", "sum")), final(mode, "none")]
    result, llm = await run(mode, ScriptedLLM(script, scenario="csv"), tables=[SALES])
    assert (result.status, result.invalid_calls) == ("done", 0)
    error = json.loads(llm.bodies()[1]["messages"][-1]["content"])["error"]
    assert "text column" in error


@pytest.mark.parametrize("mode", MODES)
async def test_tool_error_is_logged_with_the_tool_and_its_error(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    script = [calls(group("region", "product", "sum")), final(mode, "none")]
    await run(mode, ScriptedLLM(script, scenario="csv"), tables=[SALES])
    errors = [record.__dict__ for record in caplog.records if record.getMessage() == "tool error"]
    assert len(errors) == 1
    assert (errors[0]["run-id"], errors[0]["step"], errors[0]["tool"]) == (RUN_ID, 2, "group_by")
    assert "text column" in errors[0]["error"]


async def test_auto_column_outside_the_enum_is_retried_with_the_allowed_values() -> None:
    script = [calls(group("country", "revenue", "sum")), text("South")]
    result, llm = await run("auto", ScriptedLLM(script, scenario="csv"), tables=[SALES])
    assert (result.status, result.answer, result.invalid_calls) == ("done", "South", 1)
    error = json.loads(llm.bodies()[1]["messages"][-1]["content"])["error"]
    for allowed in ("'country'", "'region'", "'product'", "'units'", "'revenue'"):
        assert allowed in error


def read(file_id: str, call_id: str) -> dict[str, Any]:
    return tool_call("read_document", {"file": file_id}, call_id)


@pytest.mark.parametrize("mode", MODES)
async def test_two_document_reads_in_one_reply_are_one_step(mode: ToolChoice) -> None:
    """The contracts task's "reads documents in parallel" (P2.1a-1): both calls in one reply run
    in order, their results go back together, and the step is saved once."""
    seen: list[int] = []

    async def on_step(
        step: int,
        invalid_calls: int,
        expected_steps: int,
        messages: list[dict[str, Any]],
        records: list[CallRecord],
    ) -> None:
        seen.append(step)

    reply = calls(read("contract-2025", "call-a"), read("contract-2026", "call-b"))
    llm = ScriptedLLM([reply, final(mode, "five clauses each")], scenario="contracts")
    config = make_config(mode)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        result = await run_task(
            TASK, RUN_ID, 7, config, LLMClient(http, config), on_step, documents=CONTRACTS
        )
    assert (result.status, result.steps, result.invalid_calls) == ("done", 3, 0)
    assert seen == [1, 2]
    assert llm.bodies()[1]["messages"][-2:] == [
        {
            "role": "tool",
            "tool_call_id": f"call-{letter}",
            "content": json.dumps(read_document(document, MAX_RESULT_ROWS)),
        }
        for letter, document in zip("ab", CONTRACTS, strict=True)
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_diff_of_a_document_with_itself_is_a_tool_error_not_an_invalid_call(
    mode: ToolChoice,
) -> None:
    arguments = {"left": "contract-2025", "right": "contract-2025", "clause": "termination"}
    script = [calls(tool_call("diff_clauses", arguments)), final(mode, "none")]
    llm = ScriptedLLM(script, scenario="contracts")
    result, llm = await run(mode, llm, documents=CONTRACTS)
    assert (result.status, result.invalid_calls) == ("done", 0)
    error = json.loads(llm.bodies()[1]["messages"][-1]["content"])["error"]
    assert "both 'contract-2025'" in error


@pytest.mark.parametrize("mode", MODES)
async def test_support_tools_run_on_the_runs_orders_and_policies(mode: ToolChoice) -> None:
    orders = parse_file("orders", "orders.csv", (FIXTURES / "orders.csv").read_text())
    policies = parse_document("policies", "policies.txt", (FIXTURES / "policies.txt").read_text())
    arguments = {
        "orders": "orders",
        "order_id": "1042",
        "policies": "policies",
        "topic": "damaged items",
    }
    unknown = {"file": "orders", "order_id": "9999"}
    reply = calls(
        tool_call("draft_reply", arguments, "call-a"), tool_call("get_order", unknown, "call-b")
    )
    script = [reply, final(mode, "done")]
    llm = ScriptedLLM(script, scenario="support")
    result, llm = await run(mode, llm, tables=[orders], documents=[policies])
    assert (result.status, result.invalid_calls) == ("done", 0)
    drafted, missing = (json.loads(m["content"]) for m in llm.bodies()[1]["messages"][-2:])
    assert drafted["summary"].startswith("Dear Marko Ilievski, about your order 1042")
    assert missing == {"error": "order '9999' is not in orders"}


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_repeated_with_the_same_arguments_is_not_run_again(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    """The tools are deterministic, so a repeat can only return what the history already holds;
    the demo model repeated draft_reply until max_steps instead of answering (P2.1a-2 demo run,
    2026-11-01). The repeat gets a tool error that points to the final answer; it is not an
    invalid call (D10), since it fits the schema."""
    caplog.set_level(logging.INFO, logger="agent_runtime")
    script = [
        calls(calc("7*8")),
        calls(calc("7*8", "call-2")),
        calls(calc("8*7", "call-3")),
        final(mode, "56"),
    ]
    result, llm = await run(mode, ScriptedLLM(script))
    assert (result.status, result.answer, result.invalid_calls) == ("done", "56", 0)
    repeated = json.loads(llm.bodies()[2]["messages"][-1]["content"])
    assert repeated == {
        "error": (
            "calculator was already called with these arguments in this run and its result is "
            "above; use that result, or give the final answer"
        )
    }
    assert json.loads(llm.bodies()[3]["messages"][-1]["content"]) == {"result": 56}
    lines = [r.__dict__ for r in caplog.records if r.getMessage() == "repeated call"]
    assert [(line["step"], line["tool"]) for line in lines] == [(3, "calculator")]


@pytest.mark.parametrize("mode", MODES)
async def test_a_repeat_within_one_reply_is_caught_too(mode: ToolChoice) -> None:
    reply = calls(calc("1+2", "call-a"), calc("1+2", "call-b"))
    result, llm = await run(mode, ScriptedLLM([reply, final(mode, "3")]))
    assert (result.status, result.invalid_calls) == ("done", 0)
    first, second = (json.loads(m["content"]) for m in llm.bodies()[1]["messages"][-2:])
    assert first == {"result": 3}
    assert "already called" in second["error"]


# The record of every LLM call (P2.2a).

WALL_START_MS = 1_793_600_000_000
# A monotonic clock counts from an arbitrary point, unrelated to the wall clock.
MONOTONIC_START_MS = 5_000.0
CALL_MS = 250
# What the agent does between two calls (tools, saving the step), per step it follows.
BETWEEN_CALLS_MS = 40
USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 9,
    "total_tokens": 129,
    "prompt_tokens_details": {"cached_tokens": 112},
}
POD = "vllm-6f9c8d7b5-x2kqp"
# Every field of the record of a call sent once by the runtime that started its run; the reply's
# measurements are left out when it lacks them, retries and resumed always (P4.6b).
RECORD_FIELDS = {
    "step",
    "ts_start",
    "duration_ms",
    "gap_before_ms",
    "expected_steps",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "pod",
}


# NTP setting the wall clock back while a call is out; larger than a whole run here, so a duration
# or gap timed on the wall clock would come out negative.
WALL_SET_BACK_MS = 60_000


class FakeClock:
    """Moves only when a test calls advance() or set_wall_back(), so the test decides how long each
    part of a run takes."""

    def __init__(self) -> None:
        self.elapsed_ms = 0
        self.wall_correction_ms = 0

    def wall_ms(self) -> int:
        return WALL_START_MS + self.elapsed_ms + self.wall_correction_ms

    def monotonic_ms(self) -> float:
        return MONOTONIC_START_MS + self.elapsed_ms

    def advance(self, ms: int) -> None:
        self.elapsed_ms += ms

    def set_wall_back(self, ms: int) -> None:
        """Moves the wall clock alone, as NTP does; time itself does not go back."""
        self.wall_correction_ms -= ms


class TimedLLM(ScriptedLLM):
    """A ScriptedLLM whose every answer takes CALL_MS on the clock."""

    def __init__(self, replies: list[dict[str, Any]], clock: FakeClock) -> None:
        super().__init__(replies)
        self.clock = clock

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.clock.advance(CALL_MS)
        return super().handle(request)


class WallSetBackLLM(TimedLLM):
    """A TimedLLM during whose every call the wall clock is set back WALL_SET_BACK_MS."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.clock.set_wall_back(WALL_SET_BACK_MS)
        return super().handle(request)


class TopLevelFieldsLLM(ScriptedLLM):
    """A ScriptedLLM whose every answer also carries these top-level fields as given, a null value
    included (ScriptedLLM leaves out a usage or fingerprint of None)."""

    def __init__(self, replies: list[dict[str, Any]], fields: dict[str, Any]) -> None:
        super().__init__(replies)
        self.fields = fields

    def handle(self, request: httpx.Request) -> httpx.Response:
        answer = super().handle(request)
        return httpx.Response(answer.status_code, json={**answer.json(), **self.fields})


class FailsAfterScript(ScriptedLLM):
    """Answers the scripted replies, then every later call with a 500."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.replies:
            return super().handle(request)
        self.requests.append(request)
        return httpx.Response(500, json={"error": "scripted failure"})


async def run_on_clock(
    mode: ToolChoice,
    llm: ScriptedLLM,
    clock: FakeClock,
    on_step: StepCallback | None = None,
    retry: RetryPolicy = NO_RETRY,
) -> RunResult:
    config = replace(make_config(mode), retry=retry)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        return await run_task(
            TASK, RUN_ID, 7, config, LLMClient(http, config), on_step, clock=clock
        )


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_record_times_the_call_and_the_gap_before_it(mode: ToolChoice) -> None:
    """gap_before_ms runs from the end of the previous reply to this call's start (D9): 0 on step
    1. on_step stands in for the time between calls here, a different length after each step."""
    clock = FakeClock()

    async def on_step(
        step: int,
        invalid_calls: int,
        expected_steps: int,
        messages: list[dict[str, Any]],
        records: list[CallRecord],
    ) -> None:
        clock.advance(BETWEEN_CALLS_MS * step)

    llm = TimedLLM([calls(calc("7*8")), calls(calc("56+3")), final(mode, "59")], clock)
    result = await run_on_clock(mode, llm, clock, on_step)
    timing = [(c.step, c.ts_start, c.duration_ms, c.gap_before_ms) for c in result.calls]
    gap_1, gap_2, gap_3 = BETWEEN_CALLS_MS, 2 * BETWEEN_CALLS_MS, 3 * BETWEEN_CALLS_MS
    assert timing == [
        (1, WALL_START_MS, CALL_MS, 0),
        (2, WALL_START_MS + CALL_MS + gap_1, CALL_MS, gap_1),
        (3, WALL_START_MS + 2 * CALL_MS + gap_1 + gap_2, CALL_MS, gap_2),
        (4, WALL_START_MS + 3 * CALL_MS + gap_1 + gap_2 + gap_3, CALL_MS, gap_3),
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_wall_clock_set_back_moves_ts_start_but_not_durations_or_gaps(
    mode: ToolChoice,
) -> None:
    """ts_start is wall time, so it shows the correction; durations and gaps are measured on the
    monotonic clock, which a corrected wall clock does not move."""
    clock = FakeClock()

    async def on_step(
        step: int,
        invalid_calls: int,
        expected_steps: int,
        messages: list[dict[str, Any]],
        records: list[CallRecord],
    ) -> None:
        clock.advance(BETWEEN_CALLS_MS)

    llm = WallSetBackLLM([calls(calc("7*8")), final(mode, "56")], clock)
    result = await run_on_clock(mode, llm, clock, on_step)
    timing = [(c.ts_start, c.duration_ms, c.gap_before_ms) for c in result.calls]
    step = CALL_MS + BETWEEN_CALLS_MS
    assert timing == [
        (WALL_START_MS, CALL_MS, 0),
        (WALL_START_MS - WALL_SET_BACK_MS + step, CALL_MS, BETWEEN_CALLS_MS),
        (WALL_START_MS - 2 * WALL_SET_BACK_MS + 2 * step, CALL_MS, BETWEEN_CALLS_MS),
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_record_takes_tokens_and_pod_from_the_reply(mode: ToolChoice) -> None:
    llm = ScriptedLLM([calls(calc("7*8")), final(mode, "56")], usage=USAGE, fingerprint=POD)
    result = await run_on_clock(mode, llm, FakeClock())
    measured = [
        (c.prompt_tokens, c.completion_tokens, c.cached_tokens, c.pod) for c in result.calls
    ]
    assert measured == [(120, 9, 112, POD)] * 3
    sent = [int(r.headers["x-expected-steps"]) for r in [*llm.picks, *llm.requests]]
    assert [c.expected_steps for c in result.calls] == sent == [3, 2, 2]


@pytest.mark.parametrize(
    ("usage", "fingerprint", "absent"),
    [
        (None, None, {"prompt_tokens", "completion_tokens", "cached_tokens", "pod"}),
        ({"prompt_tokens": 120, "completion_tokens": 9}, POD, {"cached_tokens"}),
        ({**USAGE, "prompt_tokens_details": None}, POD, {"cached_tokens"}),
        ({**USAGE, "prompt_tokens_details": {}}, POD, {"cached_tokens"}),
        (USAGE, None, {"pod"}),
    ],
    ids=["no-usage", "no-details", "null-details", "no-cached-tokens", "no-fingerprint"],
)
async def test_a_field_the_reply_leaves_out_is_left_out_of_the_record(
    usage: dict[str, Any] | None, fingerprint: str | None, absent: set[str]
) -> None:
    """vLLM fills prompt_tokens_details only with --enable-prompt-tokens-details; a missing
    measurement must not read as 0."""
    llm = ScriptedLLM([text("59")], usage=usage, fingerprint=fingerprint)
    pick, answer = (await run_on_clock("auto", llm, FakeClock())).calls
    assert set(pick.fields()) == set(answer.fields()) == RECORD_FIELDS - absent


@pytest.mark.parametrize(
    ("fields", "absent"),
    [
        (
            {"usage": None, "system_fingerprint": POD},
            {"prompt_tokens", "completion_tokens", "cached_tokens"},
        ),
        ({"usage": USAGE, "system_fingerprint": None}, {"pod"}),
        (
            {"usage": {**USAGE, "prompt_tokens": None}, "system_fingerprint": POD},
            {"prompt_tokens"},
        ),
        (
            {
                "usage": {**USAGE, "prompt_tokens_details": {"cached_tokens": None}},
                "system_fingerprint": POD,
            },
            {"cached_tokens"},
        ),
    ],
    ids=["null-usage", "null-fingerprint", "null-count", "null-cached-tokens"],
)
async def test_a_null_field_is_left_out_of_the_record(
    fields: dict[str, Any], absent: set[str]
) -> None:
    """null says the server has no value, the same as leaving the field out; it must not read as 0
    or fail the run."""
    llm = TopLevelFieldsLLM([text("59")], fields)
    result = await run_on_clock("auto", llm, FakeClock())
    assert result.status == "done"
    pick, answer = result.calls
    assert set(pick.fields()) == set(answer.fields()) == RECORD_FIELDS - absent


async def test_a_cache_miss_is_recorded_as_zero_cached_tokens() -> None:
    usage = {**USAGE, "prompt_tokens_details": {"cached_tokens": 0}}
    llm = ScriptedLLM([text("59")], usage=usage)
    calls = (await run_on_clock("auto", llm, FakeClock())).calls
    assert [call.fields()["cached_tokens"] for call in calls] == [0, 0]


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"usage": {**USAGE, "prompt_tokens": "120"}}, "prompt_tokens"),
        ({"usage": {**USAGE, "completion_tokens": True}}, "completion_tokens"),
        ({"usage": {**USAGE, "prompt_tokens_details": [112]}}, "prompt_tokens_details"),
        ({"usage": {**USAGE, "prompt_tokens_details": {"cached_tokens": "112"}}}, "cached_tokens"),
        ({"usage": [120, 9]}, "usage"),
        ({"usage": USAGE, "system_fingerprint": 7}, "system_fingerprint"),
    ],
    ids=[
        "text-count",
        "bool-count",
        "details-not-an-object",
        "text-cached-tokens",
        "usage-not-an-object",
        "fingerprint-not-text",
    ],
)
async def test_a_reply_field_of_another_type_fails_the_run(
    fields: dict[str, Any], field: str
) -> None:
    """Such a body is not a chat completion, like one without choices; the run fails with no
    record rather than save a wrong measurement."""
    result, _ = await run("auto", TopLevelFieldsLLM([text("59")], fields))
    assert (result.status, result.steps, result.calls) == ("failed", 1, [])
    assert result.error is not None
    assert "LLMResponseError" in result.error
    assert field in result.error


@pytest.mark.parametrize("mode", MODES)
async def test_every_reply_is_recorded_invalid_ones_too(mode: ToolChoice) -> None:
    script = [calls(calc("2^3")), calls(calc("7*8")), final(mode, "56")]
    result = await run_on_clock(mode, ScriptedLLM(script), FakeClock())
    assert (result.status, result.steps, result.invalid_calls) == ("done", 4, 1)
    assert [c.step for c in result.calls] == [1, 2, 3, 4]


@pytest.mark.parametrize("mode", MODES)
async def test_a_cut_off_reply_is_recorded(mode: ToolChoice) -> None:
    llm = ScriptedLLM([calls(calc("7*8"))], finish_reason="length", usage=USAGE)
    result = await run_on_clock(mode, llm, FakeClock())
    assert result.status == "failed"
    assert [(c.step, c.prompt_tokens) for c in result.calls] == [(1, 120), (2, 120)]


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_without_a_reply_gets_no_record(mode: ToolChoice) -> None:
    """The run's step still counts the call and its error says why; a record needs a reply, so the
    records match the steps GET /runs/{id} shows."""
    result = await run_on_clock(mode, FailsAfterScript([calls(calc("7*8"))]), FakeClock())
    assert (result.status, result.steps) == ("failed", 3)
    assert result.error is not None
    assert "500" in result.error
    assert [c.step for c in result.calls] == [1, 2]


@pytest.mark.parametrize("mode", MODES)
async def test_on_step_gets_the_records_of_the_steps_so_far(mode: ToolChoice) -> None:
    """So a step is saved with exactly its own calls (store.RedisStore.save)."""
    seen: list[list[CallRecord]] = []

    async def on_step(
        step: int,
        invalid_calls: int,
        expected_steps: int,
        messages: list[dict[str, Any]],
        records: list[CallRecord],
    ) -> None:
        seen.append(records)

    script = [calls(calc("7*8")), calls(calc("56+3")), final(mode, "59")]
    result = await run_on_clock(mode, ScriptedLLM(script), FakeClock(), on_step)
    assert len(result.calls) == 4
    assert seen == [result.calls[:1], result.calls[:2], result.calls[:3]]


@pytest.mark.parametrize("mode", MODES)
async def test_the_llm_call_line_logs_the_call_record(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    llm = ScriptedLLM([calls(calc("7*8")), final(mode, "56")], usage=USAGE)
    result = await run_on_clock(mode, llm, FakeClock())
    lines = [r.__dict__ for r in caplog.records if r.getMessage() == "llm call"]
    assert len(lines) == len(result.calls) == 3
    for line, call in zip(lines, result.calls, strict=True):
        assert {name: line.get(name) for name in call.fields()} == call.fields()
        assert line["run-id"] == RUN_ID
        # The reply had no system_fingerprint, so the line has no pod either.
        assert "pod" not in line


# Retry of a call whose pod died (P4.4). No wait between attempts, so the tests do not sleep.
RETRY = RetryPolicy(retries=2, backoff_seconds=0)


class FlakyLLM(ScriptedLLM):
    """Answers the requests whose index (from 0, step 1's pick being 0) is in failures with that
    failure, the others from the script; with a clock, every request takes CALL_MS on it."""

    def __init__(
        self,
        replies: list[dict[str, Any]],
        failures: dict[int, httpx.Response | httpx.HTTPError],
        clock: FakeClock | None = None,
    ) -> None:
        super().__init__(replies)
        self.failures = failures
        self.clock = clock
        self.sent = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.clock is not None:
            self.clock.advance(CALL_MS)
        failure = self.failures.get(self.sent)
        self.sent += 1
        if failure is None:
            return super().handle(request)
        self.requests.append(request)
        if isinstance(failure, httpx.HTTPError):
            raise failure
        return failure


def gateway_503() -> httpx.Response:
    return httpx.Response(503, text="no healthy upstream")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "failure",
    [gateway_503(), httpx.ConnectError("refused"), httpx.RemoteProtocolError("peer closed")],
    ids=["503", "refused", "cut"],
)
async def test_a_call_whose_pod_died_is_sent_again_as_the_same_step(
    mode: ToolChoice, failure: httpx.Response | httpx.HTTPError
) -> None:
    llm = FlakyLLM([calls(calc("7*8")), final(mode, "56")], {2: failure})
    result, _ = await run(mode, llm, retry=RETRY)
    assert (result.status, result.answer, result.steps) == ("done", "56", 3)
    assert [r.headers["x-step"] for r in llm.requests] == ["2", "3", "3"]
    bodies = llm.bodies()
    assert bodies[1] == bodies[2]
    assert [c.step for c in result.calls] == [1, 2, 3]


@pytest.mark.parametrize("mode", MODES)
async def test_a_retry_leaves_no_trace_in_the_history(mode: ToolChoice) -> None:
    """The step view numbers steps by the history's assistant messages (P2.6), so a failed attempt
    must add none."""
    plain, _ = await run(mode, ScriptedLLM([calls(calc("7*8")), final(mode, "56")]))
    retried, _ = await run(
        mode,
        FlakyLLM([calls(calc("7*8")), final(mode, "56")], {0: gateway_503()}),
        retry=RETRY,
    )
    assert retried.messages == plain.messages


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_past_its_deadline_is_not_sent_again(mode: ToolChoice) -> None:
    llm = FlakyLLM([final(mode, "56")], {0: httpx.ReadTimeout("deadline")})
    result, _ = await run(mode, llm, retry=RETRY)
    assert (result.status, result.steps) == ("failed", 1)
    assert result.error is not None
    assert "ReadTimeout" in result.error
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_a_4xx_is_not_sent_again(mode: ToolChoice) -> None:
    llm = FlakyLLM([final(mode, "56")], {0: httpx.Response(400, text="bad request")})
    result, _ = await run(mode, llm, retry=RETRY)
    assert result.status == "failed"
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_out_of_retries_the_run_fails_on_the_last_error(mode: ToolChoice) -> None:
    failures: dict[int, httpx.Response | httpx.HTTPError] = {
        0: gateway_503(),
        1: gateway_503(),
        2: httpx.Response(500, text="scripted failure"),
    }
    result, llm = await run(mode, FlakyLLM([final(mode, "56")], failures), retry=RETRY)
    assert (result.status, result.steps, result.calls) == ("failed", 1, [])
    assert result.error is not None
    assert result.error.startswith("LLM call failed: HTTPStatusError")
    assert "500" in result.error
    assert len(llm.requests) == 3


@pytest.mark.parametrize("mode", MODES)
async def test_a_retry_is_logged_with_its_step_attempt_and_reason(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    failures: dict[int, httpx.Response | httpx.HTTPError] = {
        2: httpx.ConnectError("refused"),
        3: gateway_503(),
    }
    llm = FlakyLLM([calls(calc("7*8")), final(mode, "56")], failures)
    await run(mode, llm, retry=RETRY)
    retried = [r.__dict__ for r in caplog.records if r.getMessage() == "llm call retried"]
    assert [(f["run-id"], f["step"], f["attempt"]) for f in retried] == [
        (RUN_ID, 3, 1),
        (RUN_ID, 3, 2),
    ]
    assert retried[0]["reason"] == "ConnectError: refused"
    assert retried[1]["reason"].startswith("HTTPStatusError")
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.__dict__ in retried)


@pytest.mark.parametrize("mode", MODES)
async def test_a_retried_calls_record_is_the_attempt_that_answered(mode: ToolChoice) -> None:
    """Its start, length and gap are those of the attempt that got the reply; the failed attempt
    took CALL_MS before it."""
    clock = FakeClock()
    llm = FlakyLLM([calls(calc("7*8")), final(mode, "56")], {2: gateway_503()}, clock)
    result = await run_on_clock(mode, llm, clock, retry=RETRY)
    timing = [(c.step, c.ts_start, c.duration_ms, c.gap_before_ms) for c in result.calls]
    assert timing == [
        (1, WALL_START_MS, CALL_MS, 0),
        (2, WALL_START_MS + CALL_MS, CALL_MS, 0),
        (3, WALL_START_MS + 3 * CALL_MS, CALL_MS, CALL_MS),
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_retried_calls_record_counts_the_times_it_was_sent_again(mode: ToolChoice) -> None:
    """The step view tags it "retry N" (P4.6b); a call sent once has no retries field."""
    failures: dict[int, httpx.Response | httpx.HTTPError] = {
        2: httpx.ConnectError("refused"),
        3: gateway_503(),
    }
    llm = FlakyLLM([calls(calc("7*8")), final(mode, "56")], failures)
    result, _ = await run(mode, llm, retry=RETRY)
    assert [c.retries for c in result.calls] == [None, None, 2]
    assert "retries" not in result.calls[1].fields()
    assert result.calls[2].fields()["retries"] == 2


@pytest.mark.parametrize("mode", MODES)
async def test_a_retried_call_is_not_marked_resumed(mode: ToolChoice) -> None:
    # Request 0 is step 1's pick: it is sent again like any call.
    llm = FlakyLLM([final(mode, "56")], {0: gateway_503()})
    result, _ = await run(mode, llm, retry=RETRY)
    assert [(c.retries, c.resumed) for c in result.calls] == [(1, None), (None, None)]


# Resuming a run a dead runtime left (P4.4): what it saved after step 2, step 1 having picked
# arithmetic and step 2 called the calculator.
SAVED_PICK = CallRecord(
    step=1,
    ts_start=WALL_START_MS - 14_000,
    duration_ms=1_000,
    gap_before_ms=0,
    expected_steps=3,
    prompt_tokens=None,
    completion_tokens=None,
    cached_tokens=None,
    pod=POD,
)
SAVED_CALL = CallRecord(
    step=2,
    ts_start=WALL_START_MS - 10_000,
    duration_ms=2_000,
    gap_before_ms=3_000,
    expected_steps=2,
    prompt_tokens=None,
    completion_tokens=None,
    cached_tokens=None,
    pod=POD,
)
SAVED_PROMPT: list[dict[str, Any]] = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": TASK},
    {"role": "assistant", "content": None, "tool_calls": [calc("7*8")]},
    {"role": "tool", "tool_call_id": "call-1", "content": json.dumps({"result": 56})},
]


def saved_after_step_2(invalid_calls: int = 0) -> SavedRun:
    messages: list[dict[str, Any]] = [
        *SAVED_PROMPT[:2],
        {"role": "assistant", "content": "arithmetic", "scenario": True},
        *SAVED_PROMPT[2:],
    ]
    calls = [SAVED_PICK, SAVED_CALL]
    return SavedRun(messages=messages, step=2, invalid_calls=invalid_calls, calls=calls)


def saved_before_step_1() -> SavedRun:
    return SavedRun(messages=SAVED_PROMPT[:2], step=0, invalid_calls=0, calls=[])


async def resume(
    mode: ToolChoice,
    llm: ScriptedLLM,
    saved: SavedRun,
    clock: FakeClock | None = None,
    retry: RetryPolicy = NO_RETRY,
) -> RunResult:
    config = replace(make_config(mode), retry=retry)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        return await run_task(
            TASK,
            RUN_ID,
            7,
            config,
            LLMClient(http, config),
            clock=clock or FakeClock(),
            saved=saved,
        )


@pytest.mark.parametrize("mode", MODES)
async def test_a_resumed_run_sends_the_lost_step_again_from_the_saved_history(
    mode: ToolChoice,
) -> None:
    saved = saved_after_step_2()
    llm = ScriptedLLM([final(mode, "56")])
    result = await resume(mode, llm, saved)
    assert (result.status, result.answer, result.steps) == ("done", "56", 3)
    # The saved pick holds: no new pick, and arithmetic's tools and expected steps.
    assert llm.picks == []
    assert [r.headers["x-step"] for r in llm.requests] == ["3"]
    assert llm.requests[0].headers["x-expected-steps"] == "2"
    assert llm.bodies()[0]["messages"] == SAVED_PROMPT
    assert llm.bodies()[0]["tools"][0]["function"]["name"] == "calculator"
    assert [c.step for c in result.calls] == [1, 2, 3]
    assert result.calls[:2] == [SAVED_PICK, SAVED_CALL]


@pytest.mark.parametrize("mode", MODES)
async def test_a_resumed_run_still_answers_a_repeat_of_a_saved_call_from_the_history(
    mode: ToolChoice,
) -> None:
    llm = ScriptedLLM([calls(calc("7*8", "call-2")), final(mode, "56")])
    result = await resume(mode, llm, saved_after_step_2())
    assert result.status == "done"
    repeated = json.loads(llm.bodies()[1]["messages"][-1]["content"])
    assert repeated == {"error": REPEATED_CALL.format(name="calculator")}


@pytest.mark.parametrize("mode", MODES)
async def test_a_saved_invalid_call_is_not_a_call_that_ran(mode: ToolChoice) -> None:
    """Only a call that passed the schema ran; the model may still make it right after resume."""
    saved = saved_after_step_2()
    saved.messages[3]["tool_calls"] = [calc("2^3")]
    llm = ScriptedLLM([calls(calc("2^3", "call-2")), final(mode, "8")])
    result = await resume(mode, llm, saved)
    assert result.invalid_calls == 1
    assert "already called" not in llm.bodies()[1]["messages"][-1]["content"]


@pytest.mark.parametrize("mode", MODES)
async def test_a_resumed_run_counts_its_saved_invalid_calls(mode: ToolChoice) -> None:
    llm = ScriptedLLM([calls(calc("2^3")), final(mode, "8")])
    result = await resume(mode, llm, saved_after_step_2(invalid_calls=MAX_INVALID_CALLS - 1))
    assert result.status == "failed"
    assert result.invalid_calls == MAX_INVALID_CALLS


@pytest.mark.parametrize("mode", MODES)
async def test_a_resumed_calls_gap_runs_from_the_saved_calls_end_on_the_wall_clock(
    mode: ToolChoice,
) -> None:
    """No monotonic clock outlives the dead runtime, so the gap holds the restart."""
    clock = FakeClock()
    result = await resume(mode, TimedLLM([final(mode, "56")], clock), saved_after_step_2(), clock)
    saved_end = SAVED_CALL.ts_start + SAVED_CALL.duration_ms
    assert result.calls[2].gap_before_ms == WALL_START_MS - saved_end
    assert result.calls[2].ts_start == WALL_START_MS


@pytest.mark.parametrize("mode", MODES)
async def test_a_wall_clock_behind_the_saved_calls_end_gives_a_gap_of_0(mode: ToolChoice) -> None:
    clock = FakeClock()
    clock.set_wall_back(WALL_SET_BACK_MS)
    result = await resume(mode, TimedLLM([final(mode, "56")], clock), saved_after_step_2(), clock)
    assert result.calls[2].gap_before_ms == 0


@pytest.mark.parametrize("mode", MODES)
async def test_a_run_that_died_before_its_first_reply_resumes_at_step_1(mode: ToolChoice) -> None:
    llm = ScriptedLLM([final(mode, "56")])
    result = await resume(mode, llm, saved_before_step_1())
    assert [r.headers["x-step"] for r in [*llm.picks, *llm.requests]] == ["1", "2"]
    assert result.calls[0].gap_before_ms == 0


@pytest.mark.parametrize("mode", MODES)
async def test_a_resumed_calls_gap_runs_from_the_last_saved_call(mode: ToolChoice) -> None:
    clock = FakeClock()
    earlier = replace(SAVED_CALL, ts_start=SAVED_CALL.ts_start - 30_000)
    later = replace(SAVED_CALL, step=3)
    saved = replace(saved_after_step_2(), step=3, calls=[SAVED_PICK, earlier, later])
    result = await resume(mode, TimedLLM([final(mode, "56")], clock), saved, clock)
    assert result.calls[3].gap_before_ms == WALL_START_MS - (later.ts_start + later.duration_ms)


@pytest.mark.parametrize("mode", MODES)
async def test_the_first_call_a_resumed_run_sends_is_marked_resumed(mode: ToolChoice) -> None:
    """The step view tags it "resumed" (P4.6b): a new runtime sent it after the old one died. The
    saved record is kept as it was, and later calls are not marked."""
    llm = ScriptedLLM([calls(calc("56+3")), final(mode, "59")])
    result = await resume(mode, llm, saved_after_step_2())
    assert result.status == "done"
    assert result.calls[:2] == [SAVED_PICK, SAVED_CALL]
    assert [c.resumed for c in result.calls] == [None, None, True, None]
    assert result.calls[2].fields()["resumed"] is True
    assert "resumed" not in result.calls[3].fields()


@pytest.mark.parametrize("mode", MODES)
async def test_a_run_resumed_before_its_first_reply_marks_step_1(mode: ToolChoice) -> None:
    result = await resume(mode, ScriptedLLM([final(mode, "56")]), saved_before_step_1())
    assert [(c.step, c.resumed) for c in result.calls] == [(1, True), (2, None)]


@pytest.mark.parametrize("mode", MODES)
async def test_a_resumed_call_sent_again_is_marked_both(mode: ToolChoice) -> None:
    """The new runtime's first call met a dead vLLM pod too: both events are on its record."""
    llm = FlakyLLM([final(mode, "56")], {0: gateway_503()})
    result = await resume(mode, llm, saved_after_step_2(), retry=RETRY)
    assert [(c.retries, c.resumed) for c in result.calls] == [
        (None, None),
        (None, None),
        (1, True),
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_run_that_was_not_resumed_has_no_call_marked_resumed(mode: ToolChoice) -> None:
    result, _ = await run(mode, ScriptedLLM([calls(calc("7*8")), final(mode, "56")]))
    assert [c.resumed for c in result.calls] == [None, None, None]


# The context the run holds in the tracker (P4.10): each call's prompt, counted with /tokenize
# before the call is sent (P4.8).
TRACKED_MODE: ToolChoice = "required"


class FakeCounter:
    """Counts the nth prompt it is asked for as counts[n], or raises failures[n]; notes what it was
    asked and the tracker's context tokens at that moment, before its count lands."""

    def __init__(
        self,
        counts: list[int],
        tracker: RunTracker,
        failures: dict[int, Exception] | None = None,
    ) -> None:
        self.counts = counts
        self.tracker = tracker
        self.failures = failures or {}
        self.asked: list[tuple[list[dict[str, Any]], list[dict[str, Any]], bool]] = []
        self.context_seen: list[int] = []

    async def count(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        add_generation_prompt: bool,
    ) -> int:
        number = len(self.asked)
        self.asked.append(
            ([dict(m) for m in messages], [dict(t) for t in tools], add_generation_prompt)
        )
        self.context_seen.append(self.tracker.context_tokens())
        if number in self.failures:
            raise self.failures[number]
        return self.counts[number]


class TrackedLLM(ScriptedLLM):
    """A ScriptedLLM that notes the tracker's context tokens and remaining work as each call
    arrives."""

    def __init__(self, replies: list[dict[str, Any]], tracker: RunTracker) -> None:
        super().__init__(replies)
        self.tracker = tracker
        self.context_seen: list[int] = []
        self.work_seen: list[int] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.context_seen.append(self.tracker.context_tokens())
        self.work_seen.append(self.tracker.remaining_work())
        return super().handle(request)


async def run_tracked(
    llm: TrackedLLM,
    counter: FakeCounter,
    saved: SavedRun | None = None,
    retry: RetryPolicy = NO_RETRY,
) -> RunResult:
    """The run as the app runs it: started in the tracker first, not ended after."""
    config = replace(make_config(TRACKED_MODE), retry=retry)
    llm.tracker.started(RUN_ID, config.expected_steps_default)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        return await run_task(
            TASK,
            RUN_ID,
            7,
            config,
            LLMClient(http, config),
            clock=FakeClock(),
            tracking=Tracking(llm.tracker, counter),
            saved=saved,
        )


def tracked_script() -> list[dict[str, Any]]:
    return [calls(calc("7*8")), calls(calc("56+3", "call-2")), final(TRACKED_MODE, "59")]


async def test_each_call_holds_its_prompt_count_from_before_it_is_sent() -> None:
    tracker = RunTracker()
    llm, counter = TrackedLLM(tracked_script(), tracker), FakeCounter([200, 300, 340, 380], tracker)
    result = await run_tracked(llm, counter)
    assert result.status == "done"
    assert llm.context_seen == [200, 300, 340, 380]
    assert counter.context_seen == [0, 200, 300, 340]
    assert tracker.context_tokens() == 380


async def test_the_count_is_of_the_prompt_the_call_sends() -> None:
    tracker = RunTracker()
    llm, counter = TrackedLLM(tracked_script(), tracker), FakeCounter([1, 2, 3, 4], tracker)
    await run_tracked(llm, counter)
    bodies = [json.loads(r.content) for r in [*llm.picks, *llm.requests]]
    sent = [(body["messages"], body.get("tools", []), True) for body in bodies]
    assert counter.asked == sent


async def test_the_tracker_hears_the_scenario_s_expected_steps_once_it_is_picked() -> None:
    """D13: the default's 3 until step 1 has picked arithmetic, then arithmetic's 2."""
    tracker = RunTracker()
    llm, counter = TrackedLLM(tracked_script(), tracker), FakeCounter([1, 2, 3, 4], tracker)
    await run_tracked(llm, counter)
    # max(expected - step, 1) as each call arrives: 3 - 1, then 2 - 2, 2 - 3 and 2 - 4.
    assert llm.work_seen == [2, 1, 1, 1]


async def test_a_resumed_run_holds_its_last_saved_count_from_the_start() -> None:
    """The latest saved call with a count, as the live run would have held it."""
    earlier = replace(SAVED_PICK, prompt_tokens=310)
    latest = replace(SAVED_CALL, prompt_tokens=330)
    uncounted = replace(SAVED_CALL, step=3)
    saved = replace(saved_after_step_2(), step=3, calls=[earlier, latest, uncounted])
    tracker = RunTracker()
    counter = FakeCounter([360], tracker)
    await run_tracked(TrackedLLM([final(TRACKED_MODE, "56")], tracker), counter, saved)
    assert counter.context_seen == [330]
    assert tracker.context_tokens() == 360


async def test_a_resumed_run_with_no_saved_count_holds_nothing_until_its_count() -> None:
    tracker = RunTracker()
    counter = FakeCounter([360], tracker)
    await run_tracked(
        TrackedLLM([final(TRACKED_MODE, "56")], tracker), counter, saved_after_step_2()
    )
    assert counter.context_seen == [0]


async def test_a_count_that_meets_a_dead_pod_is_sent_again_with_the_call() -> None:
    """/tokenize goes to the same pods, so the call's retry rule (P4.4) covers it."""
    tracker = RunTracker()
    failures: dict[int, Exception] = {1: httpx.ConnectError("refused")}
    counter = FakeCounter([200, 0, 300, 340, 380], tracker, failures)
    llm = TrackedLLM(tracked_script(), tracker)
    result = await run_tracked(llm, counter, retry=RETRY)
    assert result.status == "done"
    assert [c.retries for c in result.calls] == [None, 1, None, None]
    assert len(llm.requests) == 3


async def test_a_count_that_is_not_a_whole_number_fails_the_run() -> None:
    tracker = RunTracker()
    counter = FakeCounter([], tracker, {0: CountError("/tokenize answered without a whole count")})
    llm = TrackedLLM(tracked_script(), tracker)
    result = await run_tracked(llm, counter)
    assert (result.status, result.steps, result.calls) == ("failed", 1, [])
    assert result.error is not None
    assert result.error.startswith("LLM call failed: CountError")
    assert llm.picks == llm.requests == []


# Step 1 picks the scenario (D13, P4.8).

ORDERS = parse_file("orders", "orders.csv", (FIXTURES / "orders.csv").read_text())
POLICIES = parse_document("policies", "policies.txt", (FIXTURES / "policies.txt").read_text())
HISTORY = [
    {"role": "user", "content": "What is 2 times 3?"},
    {"role": "assistant", "content": "6"},
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("tables", "choices"),
    [([], ["arithmetic", "out_of_scope"]), ([SALES], ["csv", "out_of_scope"])],
    ids=["no-files", "csv"],
)
async def test_step_1_asks_for_a_scenario_the_files_can_feed_without_tools(
    mode: ToolChoice, tables: list[Table], choices: list[str]
) -> None:
    llm = ScriptedLLM([final(mode, "59")], scenario=choices[0])
    await run(mode, llm, tables=tables)
    pick = json.loads(llm.picks[0].content)
    assert pick["structured_outputs"] == {"choice": choices}
    assert "tools" not in pick
    assert "tool_choice" not in pick
    assert pick["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": TASK},
        {"role": "user", "content": SCENARIO_PROMPT},
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_later_steps_leave_the_pick_and_its_instruction_out(mode: ToolChoice) -> None:
    llm = ScriptedLLM([calls(calc("7*8")), final(mode, "56")])
    result, _ = await run(mode, llm)
    for body in llm.bodies():
        assert all("scenario" not in message for message in body["messages"])
        assert all(message["content"] != SCENARIO_PROMPT for message in body["messages"])
        assert "structured_outputs" not in body
    # The pick stays in the saved history, for the step view.
    assert result.messages[2] == {"role": "assistant", "content": "arithmetic", "scenario": True}


@pytest.mark.parametrize("mode", MODES)
async def test_an_out_of_scope_message_gets_the_fixed_reply_with_no_tool_call(
    mode: ToolChoice,
) -> None:
    llm = ScriptedLLM([], scenario="out_of_scope")
    result, _ = await run(mode, llm, tables=[SALES])
    assert (result.status, result.answer, result.steps) == ("done", OUT_OF_SCOPE_REPLY, 1)
    assert llm.requests == []
    assert all("tool_calls" not in message for message in result.messages)
    assert result.expected_steps == 3


@pytest.mark.parametrize("picked", ["csv", "Testing, testing 1,2,3."])
async def test_a_pick_outside_the_list_fails_the_run(picked: str) -> None:
    """Only a server that ignores structured outputs can send one, as the simulator does."""
    llm = ScriptedLLM([text("59")], scenario=picked)
    result, _ = await run("auto", llm)
    assert (result.status, result.steps) == ("failed", 1)
    assert result.error == f"step 1 picked {picked!r}, not one of arithmetic, out_of_scope"
    assert llm.requests == []


@pytest.mark.parametrize("mode", MODES)
async def test_later_steps_see_only_the_picked_scenario_s_tools(mode: ToolChoice) -> None:
    llm = ScriptedLLM([final(mode, "done")], scenario="support")
    await run(mode, llm, tables=[SALES, ORDERS], documents=[POLICIES, CONTRACTS[0]])
    assert json.loads(llm.picks[0].content)["structured_outputs"] == {
        "choice": ["csv", "contracts", "support", "out_of_scope"]
    }
    names = [tool["function"]["name"] for tool in llm.bodies()[0]["tools"]]
    expected = ["get_order", "get_policy", "draft_reply"]
    assert names == (expected if mode == "auto" else [*expected, "final_answer"])
    assert llm.requests[0].headers["x-expected-steps"] == str(SCENARIO_EXPECTED_STEPS["support"])


@pytest.mark.parametrize("mode", MODES)
async def test_the_history_sits_between_the_system_prompt_and_the_task(mode: ToolChoice) -> None:
    llm = ScriptedLLM([final(mode, "56")])
    result, _ = await run(mode, llm, history=HISTORY)
    assert result.messages[:4] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        *HISTORY,
        {"role": "user", "content": TASK},
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_steps_1_to_n_share_the_prefix_up_to_the_tool_list(mode: ToolChoice) -> None:
    """Every call starts with the system prompt, the history and the task, so in the chat template
    every step shares the text up to the tool list, which step 1 has none of (D13)."""
    llm = ScriptedLLM([calls(calc("7*8")), calls(calc("56+3", "call-2")), final(mode, "59")])
    await run(mode, llm, history=HISTORY)
    bodies = [json.loads(r.content) for r in [*llm.picks, *llm.requests]]
    prefix = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *HISTORY,
        {"role": "user", "content": TASK},
    ]
    assert all(body["messages"][: len(prefix)] == prefix for body in bodies)
    # The tool steps' tool list is the same on each of them.
    assert len({json.dumps(body["tools"]) for body in bodies[1:]}) == 1
