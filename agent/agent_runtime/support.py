"""The support tools of the demo (P2.1a-2): an orders CSV and a policies text document from the run
request (decided 2026-11-01), read by Python. draft_reply builds the reply to the customer from the
order's row and the policy's section, so every date and amount in it is the data's (D10).
"""

import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from agent_runtime.config import AgentConfig
from agent_runtime.csv_tools import Table, ToolError
from agent_runtime.documents import Document, clause_enum, cut_text, extract_clauses

GET_ORDER = "get_order"
GET_POLICY = "get_policy"
DRAFT_REPLY = "draft_reply"
TOOL_NAMES = (GET_ORDER, GET_POLICY, DRAFT_REPLY)

DESCRIPTIONS = {
    GET_ORDER: "The row of one order in an orders CSV file, looked up by its order_id.",
    GET_POLICY: "The text of one policy (a section) of a policies document.",
    DRAFT_REPLY: (
        "Writes the reply to the customer about one order: the order's details and the policy "
        "that applies, with the deadline the policy gives, if any. The reply is the final answer "
        "to give the user."
    ),
}

ORDER_ID = "order_id"
# Columns draft_reply uses by name when the file has them: the greeting, and the date a "<n> days"
# rule counts from. Every other column is listed as written.
_CUSTOMER = "customer"
_DELIVERED_ON = "delivered_on"
# A calendar-day period in a policy: "within 30 days", "1 day". "5 working days" does not match,
# since working days need a holiday calendar the data does not have.
_DAY_RULE = re.compile(r"\b(\d+) days?\b")


def order_tables(tables: Sequence[Table]) -> list[Table]:
    """The tables get_order can read: those with an order_id column."""
    return [table for table in tables if ORDER_ID in table.columns]


def parameters(tables: Sequence[Table], documents: Sequence[Document]) -> dict[str, dict[str, Any]]:
    """The JSON schemas of the support tools for one run (D10), each only when its files exist:
    get_order with an orders CSV, get_policy with a text document, draft_reply with both.

    order_id is a free string checked against the file, like compare's left and right (P1.6a): an
    enum of every ID would put the whole column into the tool schema that every call carries. topic
    is an enum of the documents' section titles, normalised as the clause enum is (P2.1a-1).
    """
    orders = {"type": "string", "enum": [table.id for table in order_tables(tables)]}
    policies = {"type": "string", "enum": [document.id for document in documents]}
    topic = {"type": "string", "enum": clause_enum(documents)}
    order_id = {"type": "string"}

    def schema(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    tools: dict[str, dict[str, Any]] = {}
    if orders["enum"]:
        tools[GET_ORDER] = schema({"file": orders, ORDER_ID: order_id})
    if documents:
        tools[GET_POLICY] = schema({"file": policies, "topic": topic})
    if orders["enum"] and documents:
        tools[DRAFT_REPLY] = schema(
            {"orders": orders, ORDER_ID: order_id, "policies": policies, "topic": topic}
        )
    return tools


def _row(table: Table, order_id: str) -> dict[str, str]:
    """The order's row; the model may copy the ID with spaces around it. An unknown ID lists no
    IDs, which would put the whole column into the context (D13)."""
    wanted = order_id.strip()
    for row in table.rows:
        if row[ORDER_ID].strip() == wanted:
            return row
    raise ToolError(f"order {wanted!r} is not in {table.id}")


def get_order(table: Table, order_id: str) -> dict[str, Any]:
    return {"file": table.id, "order": _row(table, order_id)}


def get_policy(document: Document, topic: str, max_chars: int) -> dict[str, Any]:
    """extract_clauses in the support scenario's words: a policies document's sections are its
    topics."""
    section = extract_clauses(document, topic, max_chars)
    return {
        "file": section["file"],
        "topic": topic,
        "title": section["title"],
        "text": section["text"],
        "truncated": section["truncated"],
    }


def _deadline(policy: str, delivered_on: str) -> str | None:
    """The sentence with the end of the policy's period, when the policy has exactly one
    calendar-day rule and the order has an ISO delivery date; otherwise None, so the reply never
    guesses which rule applies or how a date is written."""
    rules = _DAY_RULE.findall(policy)
    if len(rules) != 1:
        return None
    try:
        delivered = date.fromisoformat(delivered_on)
    except ValueError:
        return None
    days = int(rules[0])
    period = "1 day ends" if days == 1 else f"{days} days end"
    end = delivered + timedelta(days=days)
    return f"Counted from the delivery on {delivered_on}, the {period} on {end.isoformat()}."


def draft_reply(
    table: Table, order_id: str, document: Document, topic: str, max_chars: int
) -> dict[str, Any]:
    """The reply, every value from the row and the section. The model copies a tool's summary as
    its answer (P1.6c, P2.1a-1), so the reply is one string under that key: under "reply" the demo
    model called draft_reply again instead of answering (P2.1a-2 demo run, 2026-11-01). The policy
    is quoted on one line, cut at max_chars like extract_clauses; the deadline is read from the
    whole section."""
    row = _row(table, order_id)
    section = document.section(topic)
    greeting = f"Dear {row[_CUSTOMER]}," if row.get(_CUSTOMER) else "Hello,"
    details = ", ".join(
        f"{column}: {value}"
        for column, value in row.items()
        if column not in (ORDER_ID, _CUSTOMER) and value.strip()
    )
    order = f"your order {row[ORDER_ID]}" + (f" ({details})" if details else "")
    policy = " ".join(section.body.split())
    reply = (
        f"{greeting} about {order}: our {section.title} policy says: "
        f'"{cut_text(policy, max_chars)}"'
    )
    deadline = _deadline(policy, row.get(_DELIVERED_ON, ""))
    if deadline is not None:
        reply += f" {deadline}"
    return {"order_id": row[ORDER_ID], "topic": topic, "summary": reply}


def run_support_tool(
    name: str,
    arguments: dict[str, Any],
    tables: Mapping[str, Table],
    documents: Mapping[str, Document],
    config: AgentConfig,
) -> dict[str, Any]:
    """Runs a call to a support tool that passed check_call. The enums guarantee the files exist;
    an unknown order or a topic the named document lacks comes back as a tool error."""
    try:
        if name == GET_ORDER:
            return get_order(tables[arguments["file"]], arguments[ORDER_ID])
        if name == GET_POLICY:
            document = documents[arguments["file"]]
            return get_policy(document, arguments["topic"], config.max_clause_chars)
        if name == DRAFT_REPLY:
            return draft_reply(
                tables[arguments["orders"]],
                arguments[ORDER_ID],
                documents[arguments["policies"]],
                arguments["topic"],
                config.max_clause_chars,
            )
    except ToolError as error:
        return {"error": str(error)}
    raise ValueError(f"{name!r} is not a support tool")
