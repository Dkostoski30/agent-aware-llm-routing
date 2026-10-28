"""The CSV tools (P1.6a, D10, D13): reading the files, each tool's output, the per-run schemas.

sales.csv has 5 rows: two text columns (region, product) and two number columns (units, with one
empty cell, and revenue), small enough to check every expected value by hand.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from agent_runtime.config import ToolChoice
from agent_runtime.csv_tools import (
    COMPARE,
    CSV_SUMMARY,
    GROUP_BY,
    FileError,
    Table,
    ToolError,
    compare,
    csv_summary,
    group_by,
    parse_file,
    read_files,
)
from agent_runtime.tools import (
    CALCULATOR,
    FINAL_ANSWER,
    InvalidCallError,
    check_call,
    offered_tools,
    run_tool,
)

SALES_CSV = (Path(__file__).parent / "fixtures" / "sales.csv").read_text()
MAX_ROWS = 20
DECIMALS = 2


def sales() -> Table:
    return parse_file("sales", "sales.csv", SALES_CSV)


def test_parse_reads_columns_types_and_rows() -> None:
    table = sales()
    assert (table.id, table.name) == ("sales", "sales.csv")
    assert table.columns == ["region", "product", "units", "revenue"]
    assert table.types == {
        "region": "text",
        "product": "text",
        "units": "number",
        "revenue": "number",
    }
    assert len(table.rows) == 5
    assert table.rows[3] == {"region": "South", "product": "Gadget", "units": "", "revenue": "300"}


@pytest.mark.parametrize(
    ("content", "types", "row_count"),
    [
        ("a,b\n1,x\n2,3\n", {"a": "number", "b": "text"}, 2),
        ("a,b\n1,\n2,\n", {"a": "number", "b": "text"}, 2),
        ("a\n-1.5e3\n 2 \n", {"a": "number"}, 2),
        ("a\nnan\n", {"a": "text"}, 1),
        ("a\ninf\n", {"a": "text"}, 1),
        ("a\n1\n\n2\n", {"a": "number"}, 2),
        ("a,b\r\n1,2\r\n", {"a": "number", "b": "number"}, 1),
        ('a,b\n"1,5",2\n', {"a": "text", "b": "number"}, 1),
        ("a,b\n", {"a": "text", "b": "text"}, 0),
        ("\ufeffa,b\n1,2\n", {"a": "number", "b": "number"}, 1),
        (" a , b\n1,2\n", {"a": "number", "b": "number"}, 1),
        ("a\n1_000\n", {"a": "text"}, 1),
    ],
    ids=[
        "mixed",
        "empty-column-is-text",
        "exponent-and-spaces",
        "nan-is-text",
        "inf-is-text",
        "blank-line-skipped",
        "crlf",
        "quoted-comma",
        "header-only",
        "bom-stripped",
        "header-names-trimmed",
        "underscore-is-text",
    ],
)
def test_parse_column_types(content: str, types: dict[str, str], row_count: int) -> None:
    table = parse_file("f", "f.csv", content)
    assert (table.types, len(table.rows)) == (types, row_count)


@pytest.mark.parametrize(
    ("content", "error_parts"),
    [
        ("", ["f", "no header"]),
        ("\n\n", ["f", "no header"]),
        ("a,,b\n1,2,3\n", ["f", "empty column name"]),
        ("a,a\n1,2\n", ["f", "duplicate column", "'a'"]),
        ("a, a\n1,2\n", ["f", "duplicate column", "'a'"]),
        ("a,b\n1,2\n3\n", ["f", "line 3", "1 fields", "header has 2"]),
        ('a\n"1\n', ["f", "not a valid CSV"]),
    ],
    ids=[
        "empty",
        "blank-lines",
        "empty-name",
        "duplicate-name",
        "duplicate-after-trim",
        "short-row",
        "open-quote",
    ],
)
def test_parse_rejects_what_the_tools_cannot_read(content: str, error_parts: list[str]) -> None:
    with pytest.raises(FileError) as raised:
        parse_file("f", "f.csv", content)
    for part in error_parts:
        assert part in str(raised.value)


def test_csv_summary() -> None:
    assert csv_summary(sales(), MAX_ROWS, DECIMALS) == {
        "file": "sales",
        "row_count": 5,
        "column_count": 4,
        "columns": [
            {"name": "region", "type": "text"},
            {"name": "product", "type": "text"},
            {"name": "units", "type": "number", "min": 3.0, "max": 10.0, "mean": 6.5},
            {"name": "revenue", "type": "number", "min": 30.0, "max": 300.0, "mean": 152.1},
        ],
    }


def test_csv_summary_lists_at_most_max_rows_columns() -> None:
    result = csv_summary(sales(), 2, DECIMALS)
    assert result["column_count"] == 4
    assert [column["name"] for column in result["columns"]] == ["region", "product"]


def test_csv_summary_rounds_to_decimals() -> None:
    table = parse_file("t", "t.csv", "a\n1\n2\n2.004\n")
    assert csv_summary(table, MAX_ROWS, DECIMALS)["columns"] == [
        {"name": "a", "type": "number", "min": 1.0, "max": 2.0, "mean": 1.67}
    ]


@pytest.mark.parametrize(
    ("by", "value", "agg", "rows"),
    [
        ("region", "revenue", "sum", [("South", 380.0), ("North", 350.5), ("East", 30.0)]),
        ("region", "revenue", "mean", [("South", 190.0), ("North", 175.25), ("East", 30.0)]),
        ("region", "units", "sum", [("North", 15.0), ("South", 8.0), ("East", 3.0)]),
        # row_count is rows: South's empty units cell still counts, so units and revenue agree.
        ("region", "units", "row_count", [("North", 2), ("South", 2), ("East", 1)]),
        ("region", "revenue", "row_count", [("North", 2), ("South", 2), ("East", 1)]),
        ("region", "units", "min", [("South", 8.0), ("North", 5.0), ("East", 3.0)]),
        ("region", "units", "max", [("North", 10.0), ("South", 8.0), ("East", 3.0)]),
        ("product", "revenue", "sum", [("Gadget", 550.0), ("Widget", 210.5)]),
    ],
)
def test_group_by(by: str, value: str, agg: str, rows: list[tuple[str, float]]) -> None:
    assert group_by(sales(), by, value, agg, MAX_ROWS, DECIMALS) == {
        "file": "sales",
        "by": by,
        "value": value,
        "agg": agg,
        "group_count": len(rows),
        "rows": [{"group": group, agg: result} for group, result in rows],
    }


def test_group_by_shows_at_most_max_rows_groups() -> None:
    result = group_by(sales(), "region", "revenue", "sum", 2, DECIMALS)
    assert result["group_count"] == 3
    assert result["rows"] == [{"group": "South", "sum": 380.0}, {"group": "North", "sum": 350.5}]


def test_group_by_rounds_to_decimals() -> None:
    table = parse_file("t", "t.csv", "team,score\na,1\na,2\na,2\nb,1\n")
    assert group_by(table, "team", "score", "mean", MAX_ROWS, DECIMALS)["rows"] == [
        {"group": "a", "mean": 1.67},
        {"group": "b", "mean": 1.0},
    ]


def test_row_count_includes_a_group_whose_value_cells_are_all_empty() -> None:
    table = parse_file("t", "t.csv", "team,score\na,\na,\nb,5\n")
    assert group_by(table, "team", "score", "row_count", MAX_ROWS, DECIMALS)["rows"] == [
        {"group": "a", "row_count": 2},
        {"group": "b", "row_count": 1},
    ]
    assert group_by(table, "team", "score", "sum", MAX_ROWS, DECIMALS)["rows"] == [
        {"group": "b", "sum": 5.0}
    ]


def test_compare() -> None:
    result = compare(sales(), "region", "revenue", "sum", "North", "South", MAX_ROWS, DECIMALS)
    assert result == {
        "file": "sales",
        "by": "region",
        "value": "revenue",
        "agg": "sum",
        "higher": {"group": "South", "sum": 380.0},
        "lower": {"group": "North", "sum": 350.5},
        "difference": 29.5,
        "percent_more": 8.42,
        "summary": (
            "South's sum of revenue (380.0) is 29.5 higher than North's (350.5), 8.42% more."
        ),
    }


def test_compare_does_not_depend_on_which_group_is_left() -> None:
    """P1.6b: given a signed right-minus-left difference, the model read it backwards."""
    north_first = compare(sales(), "region", "revenue", "sum", "North", "South", MAX_ROWS, DECIMALS)
    south_first = compare(sales(), "region", "revenue", "sum", "South", "North", MAX_ROWS, DECIMALS)
    assert north_first == south_first


def scores(content: str) -> Table:
    return parse_file("scores", "scores.csv", "team,score\n" + content)


@pytest.mark.parametrize(
    ("content", "agg", "higher", "lower", "difference", "percent_more", "summary"),
    [
        (
            "a,5\nb,5\n",
            "sum",
            ("a", 5.0),
            ("b", 5.0),
            0.0,
            0.0,
            "a's and b's sum of score are equal (5.0).",
        ),
        # A change from 0 has no percentage.
        (
            "a,0\nb,5\n",
            "sum",
            ("b", 5.0),
            ("a", 0.0),
            5.0,
            None,
            "b's sum of score (5.0) is 5.0 higher than a's (0.0).",
        ),
        # The percentage is of the lower value's size, so it stays positive.
        (
            "a,-10\nb,5\n",
            "sum",
            ("b", 5.0),
            ("a", -10.0),
            15.0,
            150.0,
            "b's sum of score (5.0) is 15.0 higher than a's (-10.0), 150.0% more.",
        ),
        # Rounded in the fields and in the sentence: a's mean is 1.6666..., the difference 0.6666...
        (
            "a,1\na,2\na,2\nb,1\n",
            "mean",
            ("a", 1.67),
            ("b", 1.0),
            0.67,
            66.67,
            "a's mean of score (1.67) is 0.67 higher than b's (1.0), 66.67% more.",
        ),
        # row_count is not a statistic of the value column, so the sentence does not name it.
        (
            "a,1\na,2\nb,1\n",
            "row_count",
            ("a", 2),
            ("b", 1),
            1,
            100.0,
            "a's row count (2) is 1 higher than b's (1), 100.0% more.",
        ),
    ],
    ids=["equal", "lower-is-zero", "lower-is-negative", "rounded", "row-count"],
)
def test_compare_cases(
    content: str,
    agg: str,
    higher: tuple[str, float],
    lower: tuple[str, float],
    difference: float,
    percent_more: float | None,
    summary: str,
) -> None:
    result = compare(scores(content), "team", "score", agg, "a", "b", MAX_ROWS, DECIMALS)
    assert result["higher"] == {"group": higher[0], agg: higher[1]}
    assert result["lower"] == {"group": lower[0], agg: lower[1]}
    assert (result["difference"], result["percent_more"]) == (difference, percent_more)
    assert result["summary"] == summary


@pytest.mark.parametrize(
    ("call", "error_parts"),
    [
        (
            lambda t: group_by(t, "region", "product", "sum", MAX_ROWS, DECIMALS),
            ["'product'", "text column", "units, revenue"],
        ),
        (
            lambda t: compare(
                t, "region", "region", "row_count", "North", "South", MAX_ROWS, DECIMALS
            ),
            ["'region'", "text column", "units, revenue"],
        ),
        (
            lambda t: group_by(t, "team", "revenue", "sum", MAX_ROWS, DECIMALS),
            ["'team'", "not in sales", "region, product, units, revenue"],
        ),
        (
            lambda t: group_by(t, "region", "score", "sum", MAX_ROWS, DECIMALS),
            ["'score'", "not in sales"],
        ),
        (
            lambda t: compare(t, "region", "revenue", "sum", "north", "South", MAX_ROWS, DECIMALS),
            ["'north'", "not a group of region", "East, North, South"],
        ),
        (
            lambda t: compare(t, "region", "revenue", "sum", "North", "West", MAX_ROWS, DECIMALS),
            ["'West'", "East, North, South"],
        ),
        (
            lambda t: compare(t, "region", "revenue", "sum", "North", "West", 2, DECIMALS),
            ["'West'", "East, North", "first 2 of 3"],
        ),
    ],
    ids=[
        "text-value",
        "text-value-compare",
        "unknown-by",
        "unknown-value",
        "unknown-left",
        "unknown-right",
        "groups-capped",
    ],
)
def test_data_errors_name_what_is_allowed(call: Any, error_parts: list[str]) -> None:
    with pytest.raises(ToolError) as raised:
        call(sales())
    for part in error_parts:
        assert part in str(raised.value)


def test_text_value_column_is_a_tool_error_not_a_crash() -> None:
    arguments = {"file": "sales", "by": "region", "value": "product", "agg": "sum"}
    result = run_tool(GROUP_BY, arguments, {"sales": sales()}, MAX_ROWS, DECIMALS)
    assert list(result) == ["error"]
    assert "text column" in result["error"]


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        # The calculator is not rounded: its result feeds the next operation.
        (CALCULATOR, {"op": "divide", "a": 1, "b": 3}, {"result": 1 / 3}),
        (CSV_SUMMARY, {"file": "sales"}, csv_summary(sales(), MAX_ROWS, DECIMALS)),
        (
            GROUP_BY,
            {"file": "sales", "by": "region", "value": "revenue", "agg": "sum"},
            group_by(sales(), "region", "revenue", "sum", MAX_ROWS, DECIMALS),
        ),
        (
            COMPARE,
            {
                "file": "sales",
                "by": "region",
                "value": "revenue",
                "agg": "sum",
                "left": "North",
                "right": "South",
            },
            compare(sales(), "region", "revenue", "sum", "North", "South", MAX_ROWS, DECIMALS),
        ),
    ],
)
def test_run_tool_runs_the_named_tool_on_the_named_file(
    name: str, arguments: dict[str, Any], expected: dict[str, Any]
) -> None:
    other = parse_file("other", "other.csv", "region,revenue\nNorth,1\n")
    tables = {"other": other, "sales": sales()}
    assert run_tool(name, arguments, tables, MAX_ROWS, DECIMALS) == expected


def two_tables() -> list[Table]:
    other = parse_file("staff", "staff.csv", "region,team,headcount\nNorth,a,3\n")
    return [sales(), other]


@pytest.mark.parametrize(
    ("mode", "names"),
    [
        ("auto", [CALCULATOR, CSV_SUMMARY, GROUP_BY, COMPARE]),
        ("required", [CALCULATOR, CSV_SUMMARY, GROUP_BY, COMPARE, FINAL_ANSWER]),
    ],
)
def test_offered_tools_per_mode(mode: ToolChoice, names: list[str]) -> None:
    assert list(offered_tools(mode, two_tables())) == names


def test_per_run_schemas_list_exactly_the_runs_files_and_columns() -> None:
    tools = offered_tools("auto", two_tables())
    files = ["sales", "staff"]
    columns = ["region", "product", "units", "revenue", "team", "headcount"]
    for name in (CSV_SUMMARY, GROUP_BY, COMPARE):
        assert tools[name]["properties"]["file"]["enum"] == files
    for name in (GROUP_BY, COMPARE):
        properties = tools[name]["properties"]
        assert properties["by"]["enum"] == columns
        assert properties["value"]["enum"] == columns
        assert properties["agg"]["enum"] == ["sum", "mean", "row_count", "min", "max"]
        assert tools[name]["required"] == list(properties)
        assert tools[name]["additionalProperties"] is False
    assert list(tools[CSV_SUMMARY]["properties"]) == ["file"]
    assert list(tools[COMPARE]["properties"]) == [
        "file",
        "by",
        "value",
        "agg",
        "left",
        "right",
    ]


@pytest.mark.parametrize(
    ("mode", "names"),
    [("auto", [CALCULATOR]), ("required", [CALCULATOR, FINAL_ANSWER])],
)
def test_a_run_without_files_offers_no_csv_tools(mode: ToolChoice, names: list[str]) -> None:
    assert list(offered_tools(mode, [])) == names


@pytest.mark.parametrize(
    ("arguments", "error_parts"),
    [
        (
            {"file": "sales", "by": "country", "value": "revenue", "agg": "sum"},
            ["'country'", "'region'", "'product'", "'units'", "'revenue'", "'headcount'"],
        ),
        (
            {"file": "orders", "by": "region", "value": "revenue", "agg": "sum"},
            ["'orders'", "'sales'", "'staff'"],
        ),
        (
            {"file": "sales", "by": "region", "value": "revenue", "agg": "median"},
            ["'median'", "'sum'", "'mean'", "'row_count'", "'min'", "'max'"],
        ),
        ({"file": "sales", "by": "region", "value": "revenue"}, ["'agg' is a required property"]),
    ],
    ids=["column", "file", "agg", "missing-agg"],
)
def test_call_outside_the_enums_is_rejected_with_the_allowed_values(
    arguments: dict[str, Any], error_parts: list[str]
) -> None:
    with pytest.raises(InvalidCallError) as raised:
        check_call(GROUP_BY, json.dumps(arguments), offered_tools("auto", two_tables()))
    for part in error_parts:
        assert part in str(raised.value)


def file(file_id: str, content: str = "a\n1\n") -> dict[str, str]:
    return {"id": file_id, "name": f"{file_id}.csv", "content": content}


def test_read_files_parses_every_file_in_order() -> None:
    tables = read_files([file("sales", SALES_CSV), file("small")], 2, len(SALES_CSV))
    assert [table.id for table in tables] == ["sales", "small"]
    assert tables[0] == sales()


@pytest.mark.parametrize(
    ("files", "max_files", "max_file_bytes", "error_parts"),
    [
        ([file("a"), file("b"), file("c")], 2, 100, ["3 files", "agent.max_files (2)"]),
        ([file("a", "a\n" + "1\n" * 10)], 2, 21, ["a:", "22 bytes", "agent.max_file_bytes (21)"]),
        # "é" is 2 bytes in UTF-8, so 11 characters are 12 bytes.
        ([file("a", "é,b\n1,2\n333")], 2, 11, ["12 bytes"]),
        ([file("a"), file("b"), file("a")], 3, 100, ["duplicate file id", "'a'"]),
        ([file("a", "a\n\ud800\n")], 2, 100, ["a:", "not UTF-8"]),
        ([file("a", "")], 2, 100, ["a:", "no header"]),
    ],
    ids=["too-many", "too-big", "bytes-not-characters", "duplicate-id", "surrogate", "not-csv"],
)
def test_read_files_rejects(
    files: list[dict[str, str]], max_files: int, max_file_bytes: int, error_parts: list[str]
) -> None:
    with pytest.raises(FileError) as raised:
        read_files(files, max_files, max_file_bytes)
    for part in error_parts:
        assert part in str(raised.value)
