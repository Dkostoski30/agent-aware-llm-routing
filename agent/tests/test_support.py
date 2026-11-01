"""The support tools on an orders CSV and a policies document (P2.1a-2): each tool's result, the
reply draft_reply builds, and the per-run schemas (D10, D13)."""

import json
from pathlib import Path

import pytest
from agent_runtime.config import ToolChoice
from agent_runtime.csv_tools import CSV_SUMMARY, GROUP_BY, Table, ToolError, parse_file
from agent_runtime.documents import EXTRACT_CLAUSES, READ_DOCUMENT, Document, parse_document
from agent_runtime.support import (
    DRAFT_REPLY,
    GET_ORDER,
    GET_POLICY,
    draft_reply,
    get_order,
    get_policy,
)
from agent_runtime.tools import InvalidCallError, check_call, offered_tools

FIXTURES = Path(__file__).parent / "fixtures"
MAX_CHARS = 1600
DAMAGED_TEXT = (
    "An item that arrived damaged can be returned or replaced free of charge within 14 days of "
    "delivery.\nSend a photo of the damage with the request."
)


def orders() -> Table:
    return parse_file("orders", "orders.csv", (FIXTURES / "orders.csv").read_text())


def policies() -> Document:
    return parse_document("policies", "policies.txt", (FIXTURES / "policies.txt").read_text())


def contract() -> Document:
    name = "contract-2025.txt"
    return parse_document("contract-2025", name, (FIXTURES / name).read_text())


def test_get_order_returns_the_row_as_written() -> None:
    assert get_order(orders(), "1042") == {
        "file": "orders",
        "order": {
            "order_id": "1042",
            "customer": "Marko Ilievski",
            "item": "Office chair",
            "status": "delivered",
            "ordered_on": "2026-10-10",
            "delivered_on": "2026-10-15",
            "amount": "189.00",
        },
    }


def test_get_order_ignores_spaces_around_the_id() -> None:
    assert get_order(orders(), " 1043 ")["order"]["item"] == "Monitor stand"


def test_an_unknown_order_is_a_tool_error_without_the_ids() -> None:
    """Listing the IDs would put the whole column into the context (D13)."""
    with pytest.raises(ToolError) as raised:
        get_order(orders(), "9999")
    assert "'9999' is not in orders" in str(raised.value)
    assert "1041" not in str(raised.value)


def test_get_policy() -> None:
    assert get_policy(policies(), "damaged items", MAX_CHARS) == {
        "file": "policies",
        "topic": "damaged items",
        "title": "Damaged items",
        "text": DAMAGED_TEXT,
        "truncated": False,
    }


def test_a_topic_the_file_lacks_is_a_tool_error() -> None:
    with pytest.raises(ToolError) as raised:
        get_policy(policies(), "termination", MAX_CHARS)
    for part in ("'termination'", "returns, damaged items, refunds, shipping"):
        assert part in str(raised.value)


def test_draft_reply_is_built_from_the_row_and_the_section() -> None:
    assert draft_reply(orders(), "1042", policies(), "damaged items", MAX_CHARS) == {
        "order_id": "1042",
        "topic": "damaged items",
        "summary": (
            "Dear Marko Ilievski, about your order 1042 (item: Office chair, status: delivered, "
            "ordered_on: 2026-10-10, delivered_on: 2026-10-15, amount: 189.00): our Damaged items "
            'policy says: "An item that arrived damaged can be returned or replaced free of charge '
            'within 14 days of delivery. Send a photo of the damage with the request." Counted '
            "from the delivery on 2026-10-15, the 14 days end on 2026-10-29."
        ),
    }


DEADLINE_POLICIES = parse_document(
    "rules",
    "rules.txt",
    "## Returns\nWithin 30 days of delivery.\n"
    "## Refunds\nPaid within 5 working days.\n"
    "## Exchanges\nAsk within 10 days, send it back within 20 days.\n"
    "## Express\nReport problems within 1 day of delivery.\n"
    "## Warranty\nTwo years.\n",
)


@pytest.mark.parametrize(
    ("order_id", "topic", "deadline"),
    [
        (
            "1041",
            "returns",
            "Counted from the delivery on 2026-10-06, the 30 days end on 2026-11-05.",
        ),
        (
            "1044",
            "express",
            "Counted from the delivery on 2026-10-27, the 1 day ends on 2026-10-28.",
        ),
        ("1041", "refunds", None),
        ("1041", "exchanges", None),
        ("1041", "warranty", None),
        ("1043", "returns", None),
    ],
    ids=["days", "one-day", "working-days", "two-rules", "no-rule", "not-delivered"],
)
def test_the_deadline_is_computed_only_from_one_calendar_day_rule_and_a_delivery_date(
    order_id: str, topic: str, deadline: str | None
) -> None:
    reply = draft_reply(orders(), order_id, DEADLINE_POLICIES, topic, MAX_CHARS)["summary"]
    if deadline is None:
        assert "Counted from" not in reply
    else:
        assert reply.endswith(f'." {deadline}')


def test_a_delivery_date_that_is_not_iso_gives_no_deadline() -> None:
    table = parse_file("orders", "orders.csv", "order_id,delivered_on\n7,27/10/2026\n")
    reply = draft_reply(table, "7", DEADLINE_POLICIES, "returns", MAX_CHARS)["summary"]
    assert reply == (
        'Hello, about your order 7 (delivered_on: 27/10/2026): our Returns policy says: "Within 30 '
        'days of delivery."'
    )


def test_draft_reply_cuts_the_policy_at_max_chars_and_leaves_out_empty_cells() -> None:
    reply = draft_reply(orders(), "1043", policies(), "damaged items", 21)["summary"]
    assert reply == (
        "Dear Elena Stojanova, about your order 1043 (item: Monitor stand, status: shipped, "
        'ordered_on: 2026-10-20, amount: 49.50): our Damaged items policy says: "An item that '
        'arrived..."'
    )


def test_draft_reply_errors() -> None:
    with pytest.raises(ToolError, match="'9999' is not in orders"):
        draft_reply(orders(), "9999", policies(), "returns", MAX_CHARS)
    with pytest.raises(ToolError, match="no clause 'termination'"):
        draft_reply(orders(), "1042", policies(), "termination", MAX_CHARS)


def sales() -> Table:
    return parse_file("sales", "sales.csv", "region,revenue\nNorth,1\n")


@pytest.mark.parametrize(
    ("tables", "documents", "support_tools"),
    [
        ([orders()], [policies()], [GET_ORDER, GET_POLICY, DRAFT_REPLY]),
        ([orders()], [], [GET_ORDER]),
        ([], [policies()], [GET_POLICY]),
        ([sales()], [policies()], [GET_POLICY]),
        ([sales()], [], []),
    ],
    ids=["orders-and-policies", "orders-only", "policies-only", "csv-without-order-id", "csv"],
)
def test_support_tools_are_offered_by_file_type(
    tables: list[Table], documents: list[Document], support_tools: list[str]
) -> None:
    offered = list(offered_tools("auto", tables, documents))
    assert [name for name in offered if name in (GET_ORDER, GET_POLICY, DRAFT_REPLY)] == (
        support_tools
    )


@pytest.mark.parametrize("mode", ["auto", "required"])
def test_an_orders_csv_gets_no_csv_tools(mode: ToolChoice) -> None:
    """The demo model answered "Order 1042 arrived damaged" with group_by(status, "damaged") on
    the orders file, twice, whatever the prompt said (P2.1a-2 demo runs, 2026-11-01); a CSV with an
    order_id column is the support scenario's, so until P4.8's scenario step it gets only the
    support tools. Text files keep the contracts tools."""
    offered = list(offered_tools(mode, [orders()], [policies()]))
    assert CSV_SUMMARY not in offered
    assert offered.index(EXTRACT_CLAUSES) < offered.index(GET_ORDER)
    assert READ_DOCUMENT in offered


def test_the_csv_tools_list_only_the_files_without_order_id() -> None:
    tools = offered_tools("auto", [orders(), sales()], [])
    assert tools[CSV_SUMMARY]["properties"]["file"]["enum"] == ["sales"]
    assert tools[GROUP_BY]["properties"]["by"]["enum"] == ["region", "revenue"]
    assert tools[GET_ORDER]["properties"]["file"]["enum"] == ["orders"]


def test_per_run_schemas_list_exactly_the_runs_files_and_topics() -> None:
    tools = offered_tools("auto", [orders(), sales()], [policies(), contract()])
    texts = ["policies", "contract-2025"]
    topics = [
        "returns",
        "damaged items",
        "refunds",
        "shipping",
        "parties",
        "services",
        "fees",
        "termination",
        "confidentiality",
    ]
    assert tools[GET_ORDER]["properties"] == {
        "file": {"type": "string", "enum": ["orders"]},
        "order_id": {"type": "string"},
    }
    assert tools[GET_POLICY]["properties"] == {
        "file": {"type": "string", "enum": texts},
        "topic": {"type": "string", "enum": topics},
    }
    assert tools[DRAFT_REPLY]["properties"] == {
        "orders": {"type": "string", "enum": ["orders"]},
        "order_id": {"type": "string"},
        "policies": {"type": "string", "enum": texts},
        "topic": {"type": "string", "enum": topics},
    }
    for name in (GET_ORDER, GET_POLICY, DRAFT_REPLY):
        assert tools[name]["required"] == list(tools[name]["properties"])
        assert tools[name]["additionalProperties"] is False


def test_auto_topic_outside_the_enum_is_rejected_with_the_allowed_values() -> None:
    tools = offered_tools("auto", [orders()], [policies()])
    arguments = {"file": "policies", "topic": "Damaged"}
    with pytest.raises(InvalidCallError) as raised:
        check_call(GET_POLICY, json.dumps(arguments), tools)
    for part in ("'Damaged'", "'damaged items'", "'returns'"):
        assert part in str(raised.value)
