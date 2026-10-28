"""The CSV tools of the demo (P1.6a): the run request carries the files, the model names a file by
its ID and columns by name, Python computes (D10, D13). Files never enter the context; only the
tool results do, capped at agent.max_result_rows rows.
"""

import csv
import io
import math
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

CSV_SUMMARY = "csv_summary"
GROUP_BY = "group_by"
COMPARE = "compare"

# The aggregates the model can ask for, in the order the agg enum lists them.
AGGREGATES = ("sum", "mean", "count", "min", "max")
# count is the group's rows; the others take the group's non-empty numbers.
_NUMBER_AGGREGATES: dict[str, Callable[[list[float]], float]] = {
    "sum": math.fsum,
    "mean": statistics.fmean,
    "min": min,
    "max": max,
}

DESCRIPTIONS = {
    CSV_SUMMARY: "Row count and columns of a CSV file, with min, max and mean of number columns.",
    GROUP_BY: (
        "Groups the rows of a CSV file by one column and aggregates a number column per group "
        "(count is the number of rows in the group); largest first."
    ),
    COMPARE: (
        "Aggregates a number column for two groups of a CSV file (left and right are values of "
        "the by column; count is the number of rows) and returns both, right minus left, and the "
        "percent change from left."
    ),
}

ColumnType = Literal["number", "text"]


class FileError(Exception):
    """A file of the run request the tools cannot read; the request is rejected with 422."""


class ToolError(Exception):
    """A call that fits the tool's schema but not the file's data; it goes back to the model as a
    tool error and is not an invalid call (D10)."""


@dataclass(frozen=True)
class Table:
    id: str
    name: str
    columns: list[str]
    types: dict[str, ColumnType]
    # One dict per data row, column name -> the cell as written in the file.
    rows: list[dict[str, str]]


def _number(cell: str) -> float | None:
    """The cell as a finite number; None for an empty cell or text. nan and inf parse as floats but
    have no place in a sum or mean, so they count as text. float() also reads 1_000 as 1000, which
    no CSV export writes, so a cell with an underscore is text too."""
    if "_" in cell:
        return None
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _column_type(cells: list[str]) -> ColumnType:
    """number when the column has at least one value and every non-empty cell is a number."""
    filled = [cell for cell in cells if cell.strip()]
    if filled and all(_number(cell) is not None for cell in filled):
        return "number"
    return "text"


def parse_file(file_id: str, name: str, content: str) -> Table:
    """Reads a CSV with a header row; raises FileError for one the tools cannot use."""
    # Excel starts a UTF-8 CSV with a byte-order mark; left in, it would be part of the first
    # column's name, which the model then cannot write.
    reader = csv.reader(io.StringIO(content.removeprefix("\ufeff")), strict=True)
    try:
        # A blank line is a row with no fields; it carries no data, so it is skipped.
        lines = [(reader.line_num, row) for row in reader if row]
    except csv.Error as error:
        raise FileError(f"{file_id}: not a valid CSV: {error}") from error
    if not lines:
        raise FileError(f"{file_id}: no header row")
    (_, first), *body = lines
    # The model names a column without the spaces around it, so " revenue" is read as "revenue".
    header = [column.strip() for column in first]
    if any(not column for column in header):
        raise FileError(f"{file_id}: empty column name in the header")
    duplicates = sorted({column for column in header if header.count(column) > 1})
    if duplicates:
        raise FileError(f"{file_id}: duplicate column {', '.join(map(repr, duplicates))}")
    for line, row in body:
        if len(row) != len(header):
            raise FileError(
                f"{file_id}: line {line} has {len(row)} fields, the header has {len(header)}"
            )
    rows = [dict(zip(header, row, strict=True)) for _, row in body]
    types = {column: _column_type([row[column] for row in rows]) for column in header}
    return Table(id=file_id, name=name, columns=header, types=types, rows=rows)


def read_files(files: Sequence[dict[str, str]], max_files: int, max_file_bytes: int) -> list[Table]:
    """The run request's files as tables, in order; raises FileError when the run must not start."""
    if len(files) > max_files:
        raise FileError(f"{len(files)} files, more than agent.max_files ({max_files})")
    ids = [file["id"] for file in files]
    duplicates = sorted({file_id for file_id in ids if ids.count(file_id) > 1})
    if duplicates:
        raise FileError(f"duplicate file id {', '.join(map(repr, duplicates))}")
    tables = []
    for file in files:
        try:
            size = len(file["content"].encode("utf-8"))
        except UnicodeEncodeError as error:
            raise FileError(f"{file['id']}: content is not UTF-8: {error}") from error
        if size > max_file_bytes:
            raise FileError(
                f"{file['id']}: {size} bytes, more than agent.max_file_bytes ({max_file_bytes})"
            )
        tables.append(parse_file(file["id"], file["name"], file["content"]))
    return tables


def parameters(tables: Sequence[Table]) -> dict[str, dict[str, Any]]:
    """The JSON schemas of the CSV tools for one run (D10).

    file is an enum of the run's file IDs, by and value enums of the columns across those files, so
    a name the files don't have is rejected (auto) or impossible (required). Whether a column is
    in the named file, and is a number column for value, is checked when the tool runs. left and
    right are free strings: the groups differ per column, which one schema cannot express.
    """
    file = {"type": "string", "enum": [table.id for table in tables]}
    # dict.fromkeys keeps the first-seen order and drops columns that several files share.
    columns = list(dict.fromkeys(column for table in tables for column in table.columns))
    column = {"type": "string", "enum": columns}
    agg = {"type": "string", "enum": list(AGGREGATES)}
    group = {"type": "string"}

    def schema(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    return {
        CSV_SUMMARY: schema({"file": file}),
        GROUP_BY: schema({"file": file, "by": column, "value": column, "agg": agg}),
        COMPARE: schema(
            {
                "file": file,
                "by": column,
                "value": column,
                "agg": agg,
                "left": group,
                "right": group,
            }
        ),
    }


def _numbers(table: Table, column: str) -> list[float]:
    return [number for row in table.rows if (number := _number(row[column])) is not None]


def csv_summary(table: Table, max_rows: int) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for name in table.columns[:max_rows]:
        column: dict[str, Any] = {"name": name, "type": table.types[name]}
        if table.types[name] == "number":
            numbers = _numbers(table, name)
            column.update(min=min(numbers), max=max(numbers), mean=statistics.fmean(numbers))
        columns.append(column)
    return {
        "file": table.id,
        "row_count": len(table.rows),
        "column_count": len(table.columns),
        "columns": columns,
    }


def _aggregate(table: Table, by: str, value: str, agg: str) -> dict[str, float]:
    """agg of the value column per group of the by column. count is the group's rows, so it does
    not depend on which value column the model picks. The other aggregates leave empty value cells
    out, so a group whose value cells are all empty has no result for them."""
    for column in (by, value):
        if column not in table.types:
            raise ToolError(
                f"column {column!r} is not in {table.id}; its columns: {', '.join(table.columns)}"
            )
    if table.types[value] != "number":
        numeric = [column for column in table.columns if table.types[column] == "number"]
        allowed = f"its number columns: {', '.join(numeric)}" if numeric else "it has none"
        raise ToolError(f"{value!r} is a text column of {table.id}; value must be a {allowed}")
    if agg == "count":
        return {group: size for group, size in Counter(row[by] for row in table.rows).items()}
    groups: dict[str, list[float]] = {}
    for row in table.rows:
        number = _number(row[value])
        if number is not None:
            groups.setdefault(row[by], []).append(number)
    return {group: _NUMBER_AGGREGATES[agg](numbers) for group, numbers in groups.items()}


def group_by(table: Table, by: str, value: str, agg: str, max_rows: int) -> dict[str, Any]:
    results = _aggregate(table, by, value, agg)
    # Largest first; ties by group name, so the order never depends on the file's row order.
    ordered = sorted(results.items(), key=lambda item: (-item[1], item[0]))
    return {
        "file": table.id,
        "by": by,
        "value": value,
        "agg": agg,
        "group_count": len(ordered),
        "rows": [{"group": group, agg: result} for group, result in ordered[:max_rows]],
    }


def compare(
    table: Table, by: str, value: str, agg: str, left: str, right: str, max_rows: int
) -> dict[str, Any]:
    results = _aggregate(table, by, value, agg)
    for group in (left, right):
        if group not in results:
            groups = sorted(results)
            shown = ", ".join(groups[:max_rows])
            if len(groups) > max_rows:
                shown += f" (first {max_rows} of {len(groups)})"
            raise ToolError(f"{group!r} is not a group of {by} in {table.id}; groups: {shown}")
    difference = results[right] - results[left]
    # A change from 0 has no percentage; abs keeps the sign of the change when left is negative.
    percent_change = None if results[left] == 0 else difference / abs(results[left]) * 100
    return {
        "file": table.id,
        "by": by,
        "value": value,
        "agg": agg,
        "left": {"group": left, agg: results[left]},
        "right": {"group": right, agg: results[right]},
        "difference": difference,
        "percent_change": percent_change,
    }
