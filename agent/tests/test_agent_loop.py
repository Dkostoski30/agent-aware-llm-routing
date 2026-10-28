"""The agent loop against a mocked LLM (P1.3a-1): both D10 modes, D1 headers, caps, history.

The LLM is httpx.MockTransport answering each request with the next scripted assistant message, in
the shape vLLM's /v1/chat/completions returns it.
"""

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_runtime.config import AgentConfig, Sampling, ToolChoice
from agent_runtime.csv_tools import Table, group_by, parse_file
from agent_runtime.llm import LLMClient
from agent_runtime.loop import RunResult, run_task
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.anyio

HEADERS_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "headers.v1.json"
MODES: list[ToolChoice] = ["auto", "required"]
RUN_ID = "run-0123456789ab"
TASK = "What is 7 times 8 plus 3?"
SYSTEM_PROMPT = "You are a calculator agent."
MAX_STEPS = 4
MAX_INVALID_CALLS = 2
MAX_RESULT_ROWS = 20
RESULT_DECIMALS = 2
SALES = parse_file(
    "sales", "sales.csv", (Path(__file__).parent / "fixtures" / "sales.csv").read_text()
)


def make_config(mode: ToolChoice) -> AgentConfig:
    return AgentConfig(
        model_name="test-model",
        tool_choice=mode,
        max_steps=MAX_STEPS,
        max_invalid_calls=MAX_INVALID_CALLS,
        expected_steps_default=3,
        llm_timeout_seconds=5,
        history_ttl_seconds=60,
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20),
        system_prompt=SYSTEM_PROMPT,
        max_file_bytes=1000,
        max_files=2,
        max_result_rows=MAX_RESULT_ROWS,
        result_decimals=RESULT_DECIMALS,
    )


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def calc(op: str, a: float, b: float, call_id: str = "call-1") -> dict[str, Any]:
    return tool_call("calculator", {"op": op, "a": a, "b": b}, call_id)


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
    response, or raises that error.
    """

    def __init__(
        self,
        replies: list[dict[str, Any]],
        finish_reason: str | None = None,
        failure: httpx.Response | httpx.HTTPError | None = None,
    ) -> None:
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.failure = failure
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.failure, httpx.HTTPError):
            raise self.failure
        if self.failure is not None:
            return self.failure
        message = self.replies.pop(0)
        finish_reason = self.finish_reason or ("tool_calls" if message["tool_calls"] else "stop")
        choice = {"message": message, "finish_reason": finish_reason}
        return httpx.Response(200, json={"choices": [choice]})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


async def run(
    mode: ToolChoice, llm: ScriptedLLM, seed: int | None = 7, tables: Sequence[Table] = ()
) -> tuple[RunResult, ScriptedLLM]:
    config = make_config(mode)
    transport = httpx.MockTransport(llm.handle)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
        llm_client = LLMClient(http, config)
        result = await run_task(TASK, RUN_ID, seed, config, llm_client, tables=tables)
    return result, llm


@pytest.mark.parametrize("mode", MODES)
async def test_tool_results_are_fed_back(mode: ToolChoice) -> None:
    script = [calls(calc("multiply", 7, 8)), calls(calc("add", 56, 3)), final(mode, "59")]
    result, llm = await run(mode, ScriptedLLM(script))
    assert (result.status, result.answer, result.steps) == ("done", "59", 3)
    assert llm.bodies()[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": json.dumps({"result": 56}),
    }


@pytest.mark.parametrize("mode", MODES)
async def test_history_keeps_role_content_and_tool_calls_only(mode: ToolChoice) -> None:
    result, _ = await run(mode, ScriptedLLM([calls(calc("multiply", 7, 8)), final(mode, "56")]))
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
        {"role": "assistant", "content": None, "tool_calls": [calc("multiply", 7, 8)]},
        {"role": "tool", "tool_call_id": "call-1", "content": json.dumps({"result": 56})},
        final_message,
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_headers_follow_the_schema_and_count_every_call(mode: ToolChoice) -> None:
    script = [calls(calc("pow", 7, 8)), calls(calc("multiply", 7, 8)), final(mode, "56")]
    _, llm = await run(mode, ScriptedLLM(script))
    validator = Draft202012Validator(json.loads(HEADERS_SCHEMA.read_text()))
    sent = [
        {name: value for name, value in request.headers.items() if name.startswith("x-")}
        for request in llm.requests
    ]
    for headers in sent:
        validator.validate(headers)
        assert headers["x-run-id"] == RUN_ID
        assert headers["x-expected-steps"] == "3"
        assert "x-parallel-group" not in headers
    assert [headers["x-step"] for headers in sent] == ["1", "2", "3"]


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
    reply = calls(tool_call("final_answer", {"answer": "59"}), calc("add", 1, 1, "call-2"))
    result, llm = await run("required", ScriptedLLM([reply]))
    assert (result.status, result.answer) == ("done", "59")
    assert result.messages[-1]["role"] == "assistant"
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_calls_of_one_response_run_in_order_as_one_step(mode: ToolChoice) -> None:
    reply = calls(calc("multiply", 7, 8, "call-a"), calc("add", 1, 2, "call-b"))
    result, llm = await run(mode, ScriptedLLM([reply, final(mode, "59")]))
    assert result.steps == 2
    assert llm.bodies()[1]["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "call-a", "content": json.dumps({"result": 56})},
        {"role": "tool", "tool_call_id": "call-b", "content": json.dumps({"result": 3})},
    ]
    assert llm.requests[1].headers["x-step"] == "2"


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_enum_is_retried_with_the_allowed_values(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([calls(calc("pow", 2, 3)), final(mode, "8")]))
    assert (result.status, result.answer, result.invalid_calls) == ("done", "8", 1)
    feedback = llm.bodies()[1]["messages"][-1]
    assert (feedback["role"], feedback["tool_call_id"]) == ("tool", "call-1")
    for allowed in ("'add'", "'subtract'", "'multiply'", "'divide'"):
        assert allowed in json.loads(feedback["content"])["error"]


@pytest.mark.parametrize("mode", MODES)
async def test_division_by_zero_goes_back_as_a_tool_error(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([calls(calc("divide", 1, 0)), final(mode, "none")]))
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
    result, llm = await run(mode, ScriptedLLM([calls(calc("add", 1, 1))] * MAX_STEPS))
    assert (result.status, result.answer, result.steps) == ("failed", None, MAX_STEPS)
    assert result.error is not None
    assert "agent.max_steps" in result.error
    assert len(llm.requests) == MAX_STEPS


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_call_cap_fails_the_run(mode: ToolChoice) -> None:
    result, llm = await run(mode, ScriptedLLM([calls(calc("pow", 2, 3))] * MAX_INVALID_CALLS))
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
    assert (result.status, result.answer, result.steps) == ("failed", None, 1)
    assert result.error is not None
    assert "max_model_len" in result.error
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_call_is_logged_with_its_reason(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    await run(mode, ScriptedLLM([calls(calc("pow", 2, 3)), final(mode, "8")]))
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    # Log fields are record attributes; "run-id" is not a Python name, so they come from __dict__.
    fields = warnings[0].__dict__
    assert (fields["run-id"], fields["step"]) == (RUN_ID, 1)
    assert "'pow'" in fields["reason"]
    own = [record for record in caplog.records if record.name.startswith("agent_runtime")]
    assert own
    assert all(record.__dict__["run-id"] == RUN_ID for record in own)


@pytest.mark.parametrize("mode", MODES)
async def test_llm_call_is_logged_with_duration_and_finish_reason(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    await run(mode, ScriptedLLM([calls(calc("multiply", 7, 8)), final(mode, "56")]))
    llm_calls = [record.__dict__ for record in caplog.records if record.getMessage() == "llm call"]
    assert [fields["step"] for fields in llm_calls] == [1, 2]
    for fields in llm_calls:
        assert isinstance(fields["duration_ms"], int | float)
        assert fields["duration_ms"] >= 0
    expected = ["tool_calls", "tool_calls" if mode == "required" else "stop"]
    assert [fields["finish_reason"] for fields in llm_calls] == expected


@pytest.mark.parametrize("mode", MODES)
async def test_on_step_sees_every_step_that_leaves_the_run_going(mode: ToolChoice) -> None:
    seen: list[tuple[int, int, list[dict[str, Any]]]] = []

    async def on_step(step: int, invalid_calls: int, messages: list[dict[str, Any]]) -> None:
        seen.append((step, invalid_calls, messages))

    llm = ScriptedLLM([calls(calc("pow", 2, 3)), calls(calc("multiply", 7, 8)), final(mode, "56")])
    config = make_config(mode)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        result = await run_task(TASK, RUN_ID, 7, config, LLMClient(http, config), on_step)
    assert result.status == "done"
    assert [(step, invalid) for step, invalid, _ in seen] == [(1, 1), (2, 1)]
    assert [messages for _, _, messages in seen] == [result.messages[:4], result.messages[:6]]


def group(by: str, value: str, agg: str, call_id: str = "call-1") -> dict[str, Any]:
    return tool_call("group_by", {"file": "sales", "by": by, "value": value, "agg": agg}, call_id)


@pytest.mark.parametrize("mode", MODES)
async def test_csv_tool_result_is_fed_back(mode: ToolChoice) -> None:
    script = [calls(group("region", "revenue", "sum")), final(mode, "South")]
    result, llm = await run(mode, ScriptedLLM(script), tables=[SALES])
    assert (result.status, result.answer, result.invalid_calls) == ("done", "South", 0)
    expected = group_by(SALES, "region", "revenue", "sum", MAX_RESULT_ROWS, RESULT_DECIMALS)
    assert llm.bodies()[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": json.dumps(expected),
    }


@pytest.mark.parametrize("mode", MODES)
async def test_tool_schemas_offer_the_runs_files(mode: ToolChoice) -> None:
    _, llm = await run(mode, ScriptedLLM([final(mode, "none")]), tables=[SALES])
    tools = {tool["function"]["name"]: tool["function"] for tool in llm.bodies()[0]["tools"]}
    properties = tools["group_by"]["parameters"]["properties"]
    assert properties["file"]["enum"] == ["sales"]
    assert properties["by"]["enum"] == ["region", "product", "units", "revenue"]


@pytest.mark.parametrize("mode", MODES)
async def test_text_value_column_is_a_tool_error_not_an_invalid_call(mode: ToolChoice) -> None:
    script = [calls(group("region", "product", "sum")), final(mode, "none")]
    result, llm = await run(mode, ScriptedLLM(script), tables=[SALES])
    assert (result.status, result.invalid_calls) == ("done", 0)
    error = json.loads(llm.bodies()[1]["messages"][-1]["content"])["error"]
    assert "text column" in error


@pytest.mark.parametrize("mode", MODES)
async def test_tool_error_is_logged_with_the_tool_and_its_error(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    script = [calls(group("region", "product", "sum")), final(mode, "none")]
    await run(mode, ScriptedLLM(script), tables=[SALES])
    errors = [record.__dict__ for record in caplog.records if record.getMessage() == "tool error"]
    assert len(errors) == 1
    assert (errors[0]["run-id"], errors[0]["step"], errors[0]["tool"]) == (RUN_ID, 1, "group_by")
    assert "text column" in errors[0]["error"]


async def test_auto_column_outside_the_enum_is_retried_with_the_allowed_values() -> None:
    script = [calls(group("country", "revenue", "sum")), text("South")]
    result, llm = await run("auto", ScriptedLLM(script), tables=[SALES])
    assert (result.status, result.answer, result.invalid_calls) == ("done", "South", 1)
    error = json.loads(llm.bodies()[1]["messages"][-1]["content"])["error"]
    for allowed in ("'country'", "'region'", "'product'", "'units'", "'revenue'"):
        assert allowed in error
