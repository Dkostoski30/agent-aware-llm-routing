"""The scenarios step 1 may pick and the tools each one offers afterwards (D13, P4.8)."""

from pathlib import Path

import pytest
from agent_runtime.config import SCENARIOS, Scenario, ToolChoice
from agent_runtime.csv_tools import COMPARE, CSV_SUMMARY, GROUP_BY, Table, parse_file
from agent_runtime.documents import (
    DIFF_CLAUSES,
    EXTRACT_CLAUSES,
    READ_DOCUMENT,
    Document,
    parse_document,
)
from agent_runtime.support import DRAFT_REPLY, GET_ORDER, GET_POLICY
from agent_runtime.tools import (
    CALCULATOR,
    FINAL_ANSWER,
    scenario_tools,
    scenarios,
)

FIXTURES = Path(__file__).parent / "fixtures"


def sales() -> Table:
    return parse_file("sales", "sales.csv", (FIXTURES / "sales.csv").read_text())


def orders() -> Table:
    return parse_file("orders", "orders.csv", (FIXTURES / "orders.csv").read_text())


def policies() -> Document:
    return parse_document("policies", "policies.txt", (FIXTURES / "policies.txt").read_text())


def contract(year: int = 2025) -> Document:
    name = f"contract-{year}.txt"
    return parse_document(f"contract-{year}", name, (FIXTURES / name).read_text())


@pytest.mark.parametrize(
    ("tables", "texts", "expected"),
    [
        ([], [], ["arithmetic", "out_of_scope"]),
        ([sales()], [], ["csv", "out_of_scope"]),
        ([], [contract()], ["contracts", "out_of_scope"]),
        # An orders CSV is a CSV too (the user, 2026-11-23), and a policies document a text
        # document, so csv and contracts stay possible beside support.
        ([orders()], [], ["csv", "support", "out_of_scope"]),
        ([orders()], [policies()], ["csv", "contracts", "support", "out_of_scope"]),
        ([sales(), orders()], [contract()], ["csv", "contracts", "support", "out_of_scope"]),
    ],
    ids=["none", "csv", "text", "orders", "support-set", "all"],
)
def test_the_files_narrow_the_scenarios_and_out_of_scope_is_always_there(
    tables: list[Table], texts: list[Document], expected: list[Scenario]
) -> None:
    assert scenarios(tables, texts) == expected


def test_the_scenarios_cover_d13() -> None:
    assert SCENARIOS == ("csv", "contracts", "support", "arithmetic", "out_of_scope")


@pytest.mark.parametrize(
    ("scenario", "tables", "texts", "names"),
    [
        ("arithmetic", [], [], [CALCULATOR]),
        ("csv", [sales(), orders()], [contract()], [CSV_SUMMARY, GROUP_BY, COMPARE]),
        (
            "contracts",
            [sales(), orders()],
            [contract(2025), contract(2026)],
            [READ_DOCUMENT, EXTRACT_CLAUSES, DIFF_CLAUSES],
        ),
        ("support", [sales(), orders()], [policies()], [GET_ORDER, GET_POLICY, DRAFT_REPLY]),
        ("support", [orders()], [], [GET_ORDER]),
    ],
    ids=["arithmetic", "csv", "contracts", "support", "support-orders-only"],
)
def test_a_scenario_offers_only_its_own_tools(
    scenario: Scenario, tables: list[Table], texts: list[Document], names: list[str]
) -> None:
    assert list(scenario_tools(scenario, "auto", tables, texts)) == names
    assert list(scenario_tools(scenario, "required", tables, texts)) == [*names, FINAL_ANSWER]


@pytest.mark.parametrize("mode", ["auto", "required"])
def test_out_of_scope_offers_no_tool(mode: ToolChoice) -> None:
    assert scenario_tools("out_of_scope", mode, [sales()], [contract()]) == {}
