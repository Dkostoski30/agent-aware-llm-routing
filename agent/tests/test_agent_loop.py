"""The agent loop against a mocked LLM (P1.3a-1): both D10 modes, D1 headers, caps, history, and
the record of every LLM call (P2.2a).

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
from agent_runtime.documents import Document, parse_document, read_document
from agent_runtime.llm import LLMClient
from agent_runtime.loop import RunResult, StepCallback, run_task
from agent_runtime.store import CallRecord
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
MAX_RESULT_CHARS = 560
RESULT_DECIMALS = 2
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
    every answer; without them the answer has neither.
    """

    def __init__(
        self,
        replies: list[dict[str, Any]],
        finish_reason: str | None = None,
        failure: httpx.Response | httpx.HTTPError | None = None,
        usage: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> None:
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.failure = failure
        self.usage = usage
        self.fingerprint = fingerprint
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
) -> tuple[RunResult, ScriptedLLM]:
    config = make_config(mode)
    transport = httpx.MockTransport(llm.handle)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
        llm_client = LLMClient(http, config)
        result = await run_task(
            TASK, RUN_ID, seed, config, llm_client, tables=tables, documents=documents
        )
    return result, llm


@pytest.mark.parametrize("mode", MODES)
async def test_tool_results_are_fed_back(mode: ToolChoice) -> None:
    script = [calls(calc("7*8")), calls(calc("56+3")), final(mode, "59")]
    result, llm = await run(mode, ScriptedLLM(script))
    assert (result.status, result.answer, result.steps) == ("done", "59", 3)
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
    reply = calls(tool_call("final_answer", {"answer": "59"}), calc("1+1", call_id="call-2"))
    result, llm = await run("required", ScriptedLLM([reply]))
    assert (result.status, result.answer) == ("done", "59")
    assert result.messages[-1]["role"] == "assistant"
    assert len(llm.requests) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_calls_of_one_response_run_in_order_as_one_step(mode: ToolChoice) -> None:
    reply = calls(calc("7*8", call_id="call-a"), calc("1+2", call_id="call-b"))
    result, llm = await run(mode, ScriptedLLM([reply, final(mode, "59")]))
    assert result.steps == 2
    assert llm.bodies()[1]["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "call-a", "content": json.dumps({"result": 56})},
        {"role": "tool", "tool_call_id": "call-b", "content": json.dumps({"result": 3})},
    ]
    assert llm.requests[1].headers["x-step"] == "2"


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_enum_is_retried_with_the_allowed_values(mode: ToolChoice) -> None:
    # The calculator has no enum since P1.6d; the aggregate enum of group_by stands in.
    script = [calls(group("region", "revenue", "median")), final(mode, "South")]
    result, llm = await run(mode, ScriptedLLM(script), tables=[SALES])
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
    result, llm = await run(mode, ScriptedLLM([calls(calc("1+1"))] * MAX_STEPS))
    assert (result.status, result.answer, result.steps) == ("failed", None, MAX_STEPS)
    assert result.error is not None
    assert "agent.max_steps" in result.error
    assert len(llm.requests) == MAX_STEPS


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
    assert (result.status, result.answer, result.steps) == ("failed", None, 1)
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
    assert (fields["run-id"], fields["step"]) == (RUN_ID, 1)
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
    assert [fields["step"] for fields in llm_calls] == [1, 2]
    for fields in llm_calls:
        assert isinstance(fields["duration_ms"], int | float)
        assert fields["duration_ms"] >= 0
    expected = ["tool_calls", "tool_calls" if mode == "required" else "stop"]
    assert [fields["finish_reason"] for fields in llm_calls] == expected


@pytest.mark.parametrize("mode", MODES)
async def test_on_step_sees_every_step_that_leaves_the_run_going(mode: ToolChoice) -> None:
    seen: list[tuple[int, int, list[dict[str, Any]]]] = []

    async def on_step(
        step: int, invalid_calls: int, messages: list[dict[str, Any]], records: list[CallRecord]
    ) -> None:
        seen.append((step, invalid_calls, messages))

    llm = ScriptedLLM([calls(calc("2^3")), calls(calc("7*8")), final(mode, "56")])
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
    _, llm = await run("auto", ScriptedLLM(script), tables=[long_ids])
    assert "too long to list" in llm.bodies()[1]["messages"][-1]["content"]


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


def read(file_id: str, call_id: str) -> dict[str, Any]:
    return tool_call("read_document", {"file": file_id}, call_id)


@pytest.mark.parametrize("mode", MODES)
async def test_two_document_reads_in_one_reply_are_one_step(mode: ToolChoice) -> None:
    """The contracts task's "reads documents in parallel" (P2.1a-1): both calls in one reply run
    in order, their results go back together, and the step is saved once."""
    seen: list[int] = []

    async def on_step(
        step: int, invalid_calls: int, messages: list[dict[str, Any]], records: list[CallRecord]
    ) -> None:
        seen.append(step)

    reply = calls(read("contract-2025", "call-a"), read("contract-2026", "call-b"))
    llm = ScriptedLLM([reply, final(mode, "five clauses each")])
    config = make_config(mode)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(llm.handle), base_url="http://gateway"
    ) as http:
        result = await run_task(
            TASK, RUN_ID, 7, config, LLMClient(http, config), on_step, documents=CONTRACTS
        )
    assert (result.status, result.steps, result.invalid_calls) == ("done", 2, 0)
    assert seen == [1]
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
    result, llm = await run(mode, ScriptedLLM(script), documents=CONTRACTS)
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
    result, llm = await run(mode, ScriptedLLM(script), tables=[orders], documents=[policies])
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
    assert [(line["step"], line["tool"]) for line in lines] == [(2, "calculator")]


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
# Every field of a call record; the reply's measurements are left out when it lacks them.
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
    mode: ToolChoice, llm: ScriptedLLM, clock: FakeClock, on_step: StepCallback | None = None
) -> RunResult:
    config = make_config(mode)
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
        step: int, invalid_calls: int, messages: list[dict[str, Any]], records: list[CallRecord]
    ) -> None:
        clock.advance(BETWEEN_CALLS_MS * step)

    llm = TimedLLM([calls(calc("7*8")), calls(calc("56+3")), final(mode, "59")], clock)
    result = await run_on_clock(mode, llm, clock, on_step)
    timing = [(c.step, c.ts_start, c.duration_ms, c.gap_before_ms) for c in result.calls]
    gap_1, gap_2 = BETWEEN_CALLS_MS, 2 * BETWEEN_CALLS_MS
    assert timing == [
        (1, WALL_START_MS, CALL_MS, 0),
        (2, WALL_START_MS + CALL_MS + gap_1, CALL_MS, gap_1),
        (3, WALL_START_MS + 2 * CALL_MS + gap_1 + gap_2, CALL_MS, gap_2),
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_wall_clock_set_back_moves_ts_start_but_not_durations_or_gaps(
    mode: ToolChoice,
) -> None:
    """ts_start is wall time, so it shows the correction; durations and gaps are measured on the
    monotonic clock, which a corrected wall clock does not move."""
    clock = FakeClock()

    async def on_step(
        step: int, invalid_calls: int, messages: list[dict[str, Any]], records: list[CallRecord]
    ) -> None:
        clock.advance(BETWEEN_CALLS_MS)

    llm = WallSetBackLLM([calls(calc("7*8")), final(mode, "56")], clock)
    result = await run_on_clock(mode, llm, clock, on_step)
    timing = [(c.ts_start, c.duration_ms, c.gap_before_ms) for c in result.calls]
    assert timing == [
        (WALL_START_MS, CALL_MS, 0),
        (WALL_START_MS - WALL_SET_BACK_MS + CALL_MS + BETWEEN_CALLS_MS, CALL_MS, BETWEEN_CALLS_MS),
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_record_takes_tokens_and_pod_from_the_reply(mode: ToolChoice) -> None:
    llm = ScriptedLLM([calls(calc("7*8")), final(mode, "56")], usage=USAGE, fingerprint=POD)
    result = await run_on_clock(mode, llm, FakeClock())
    measured = [
        (c.prompt_tokens, c.completion_tokens, c.cached_tokens, c.pod) for c in result.calls
    ]
    assert measured == [(120, 9, 112, POD)] * 2
    sent = [int(request.headers["x-expected-steps"]) for request in llm.requests]
    assert [c.expected_steps for c in result.calls] == sent == [3, 3]


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
    [call] = (await run_on_clock("auto", llm, FakeClock())).calls
    assert set(call.fields()) == RECORD_FIELDS - absent


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
    [call] = result.calls
    assert set(call.fields()) == RECORD_FIELDS - absent


async def test_a_cache_miss_is_recorded_as_zero_cached_tokens() -> None:
    usage = {**USAGE, "prompt_tokens_details": {"cached_tokens": 0}}
    llm = ScriptedLLM([text("59")], usage=usage)
    [call] = (await run_on_clock("auto", llm, FakeClock())).calls
    assert call.fields()["cached_tokens"] == 0


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
    assert (result.status, result.steps, result.invalid_calls) == ("done", 3, 1)
    assert [c.step for c in result.calls] == [1, 2, 3]


@pytest.mark.parametrize("mode", MODES)
async def test_a_cut_off_reply_is_recorded(mode: ToolChoice) -> None:
    llm = ScriptedLLM([calls(calc("7*8"))], finish_reason="length", usage=USAGE)
    result = await run_on_clock(mode, llm, FakeClock())
    assert result.status == "failed"
    assert [(c.step, c.prompt_tokens) for c in result.calls] == [(1, 120)]


@pytest.mark.parametrize("mode", MODES)
async def test_a_call_without_a_reply_gets_no_record(mode: ToolChoice) -> None:
    """The run's step still counts the call and its error says why; a record needs a reply, so the
    records match the steps GET /runs/{id} shows."""
    result = await run_on_clock(mode, FailsAfterScript([calls(calc("7*8"))]), FakeClock())
    assert (result.status, result.steps) == ("failed", 2)
    assert result.error is not None
    assert "500" in result.error
    assert [c.step for c in result.calls] == [1]


@pytest.mark.parametrize("mode", MODES)
async def test_on_step_gets_the_records_of_the_steps_so_far(mode: ToolChoice) -> None:
    """So a step is saved with exactly its own calls (store.RedisStore.save)."""
    seen: list[list[CallRecord]] = []

    async def on_step(
        step: int, invalid_calls: int, messages: list[dict[str, Any]], records: list[CallRecord]
    ) -> None:
        seen.append(records)

    script = [calls(calc("7*8")), calls(calc("56+3")), final(mode, "59")]
    result = await run_on_clock(mode, ScriptedLLM(script), FakeClock(), on_step)
    assert len(result.calls) == 3
    assert seen == [result.calls[:1], result.calls[:2]]


@pytest.mark.parametrize("mode", MODES)
async def test_the_llm_call_line_logs_the_call_record(
    mode: ToolChoice, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_runtime")
    llm = ScriptedLLM([calls(calc("7*8")), final(mode, "56")], usage=USAGE)
    result = await run_on_clock(mode, llm, FakeClock())
    lines = [r.__dict__ for r in caplog.records if r.getMessage() == "llm call"]
    assert len(lines) == len(result.calls) == 2
    for line, call in zip(lines, result.calls, strict=True):
        assert {name: line.get(name) for name in call.fields()} == call.fields()
        assert line["run-id"] == RUN_ID
        # The reply had no system_fingerprint, so the line has no pod either.
        assert "pod" not in line
