"""The contracts tools on text documents (P2.1a-1): parsing, the three tools, their schemas, and
the run request's files split by type (D10, D13)."""

import json
from pathlib import Path
from typing import Any

import pytest
from agent_runtime.config import ToolChoice
from agent_runtime.csv_tools import CSV_SUMMARY, FileError, ToolError, parse_file
from agent_runtime.documents import (
    DIFF_CLAUSES,
    EXTRACT_CLAUSES,
    READ_DOCUMENT,
    Document,
    diff_clauses,
    extract_clauses,
    normalise_title,
    parse_document,
    read_document,
    read_run_files,
)
from agent_runtime.support import GET_POLICY
from agent_runtime.tools import (
    CALCULATOR,
    FINAL_ANSWER,
    InvalidCallError,
    check_call,
    offered_tools,
)

FIXTURES = Path(__file__).parent / "fixtures"
MAX_ROWS = 20
MAX_CHARS = 1600
MAX_CHANGES = 20
CONTEXT_WORDS = 3
TERMINATION_2025 = "Either party may end this agreement with 30 days' written notice."


def contract(year: int) -> Document:
    name = f"contract-{year}.txt"
    return parse_document(f"contract-{year}", name, (FIXTURES / name).read_text())


def doc(content: str, file_id: str = "doc") -> Document:
    return parse_document(file_id, f"{file_id}.txt", content)


def test_parse_reads_sections_in_order_and_ignores_the_preamble() -> None:
    document = contract(2025)
    assert (document.id, document.name) == ("contract-2025", "contract-2025.txt")
    assert [(s.clause, s.title) for s in document.sections] == [
        ("parties", "1. Parties"),
        ("services", "2. Services"),
        ("fees", "3. Fees"),
        ("termination", "4. Termination"),
        ("confidentiality", "5. Confidentiality"),
    ]
    assert document.sections[3].body == TERMINATION_2025
    # The title line above the first heading is in no section.
    assert all("Service agreement" not in section.body for section in document.sections)


@pytest.mark.parametrize(
    ("title", "clause"),
    [
        ("4. Termination", "termination"),
        ("Termination", "termination"),
        ("4.1 Notice period", "notice period"),
        ("4) Fees", "fees"),
        ("12.Governing law", "governing law"),
        ("  Governing   Law ", "governing law"),
        ("3D printing", "3d printing"),
    ],
)
def test_normalise_title(title: str, clause: str) -> None:
    assert normalise_title(title) == clause


def test_a_heading_with_an_empty_body_is_a_section() -> None:
    document = doc("## Fees\n\n## Termination\nThirty days.\n")
    assert [(s.clause, s.body) for s in document.sections] == [
        ("fees", ""),
        ("termination", "Thirty days."),
    ]


def test_only_a_level_two_heading_starts_a_section() -> None:
    document = doc("﻿## Fees\n### Late payment\nTen percent.\n##Not a heading\n")
    assert [s.clause for s in document.sections] == ["fees"]
    assert document.sections[0].body == "### Late payment\nTen percent.\n##Not a heading"


@pytest.mark.parametrize(
    ("content", "error_parts"),
    [
        ("No headings here.\n", ["doc:", "no '## ' heading"]),
        ("### Fees\nTen.\n", ["doc:", "no '## ' heading"]),
        ("", ["doc:", "no '## ' heading"]),
        ("## 4.\nText.\n", ["doc:", "'4.'", "empty"]),
        ("## 3. Fees\nTen.\n## Fees\nTwelve.\n", ["doc:", "'3. Fees'", "'Fees'", "'fees'"]),
    ],
    ids=["no-heading", "only-level-three", "empty", "empty-title", "duplicate-clause"],
)
def test_parse_rejects_what_the_tools_cannot_read(content: str, error_parts: list[str]) -> None:
    with pytest.raises(FileError) as raised:
        doc(content)
    for part in error_parts:
        assert part in str(raised.value)


def test_read_document_lists_clauses_never_the_text() -> None:
    assert read_document(contract(2025), MAX_ROWS) == {
        "file": "contract-2025",
        "name": "contract-2025.txt",
        "clause_count": 5,
        "clauses": [
            {"clause": "parties", "title": "1. Parties", "word_count": 13},
            {"clause": "services", "title": "2. Services", "word_count": 15},
            {"clause": "fees", "title": "3. Fees", "word_count": 16},
            {"clause": "termination", "title": "4. Termination", "word_count": 11},
            {"clause": "confidentiality", "title": "5. Confidentiality", "word_count": 16},
        ],
    }


def test_read_document_lists_at_most_max_rows_clauses() -> None:
    result = read_document(contract(2025), 2)
    assert result["clause_count"] == 5
    assert [c["clause"] for c in result["clauses"]] == ["parties", "services"]


def test_extract_clauses() -> None:
    assert extract_clauses(contract(2025), "termination", MAX_CHARS) == {
        "file": "contract-2025",
        "clause": "termination",
        "title": "4. Termination",
        "text": TERMINATION_2025,
        "truncated": False,
    }


def test_extract_clauses_cuts_the_text_at_max_chars() -> None:
    result = extract_clauses(contract(2025), "termination", 12)
    assert (result["text"], result["truncated"]) == ("Either party", True)


def test_a_clause_the_file_lacks_is_a_tool_error_naming_its_clauses() -> None:
    with pytest.raises(ToolError) as raised:
        extract_clauses(doc("## Fees\nTen.\n## Scope\nAll.\n"), "termination", MAX_CHARS)
    for part in ("'termination'", "doc", "fees, scope"):
        assert part in str(raised.value)


def diff(clause: str, left: Document | None = None, right: Document | None = None) -> Any:
    return diff_clauses(
        left or contract(2025),
        right or contract(2026),
        clause,
        MAX_CHANGES,
        CONTEXT_WORDS,
        MAX_CHARS,
    )


def test_diff_says_each_change_with_its_context_in_one_sentence() -> None:
    """The summary carries the change itself: the demo model answers by copying it (P2.1a-1 demo
    run, 2026-11-01, where a summary that only counted changes became the answer)."""
    assert diff("termination") == {
        "clause": "termination",
        "left": "contract-2025",
        "right": "contract-2026",
        "change_count": 1,
        "summary": (
            "The termination clause changed from contract-2025 to contract-2026: "
            '"this agreement with 30 days\' written notice." became '
            '"this agreement with 60 days\' written notice.".'
        ),
    }
    assert diff("fees")["summary"] == (
        "The fees clause changed from contract-2025 to contract-2026: "
        '"The client pays 1,200 EUR per month," became "The client pays 1,350 EUR per month,".'
    )


def test_identical_clauses_have_no_changes() -> None:
    result = diff("parties")
    assert result["change_count"] == 0
    assert result["summary"] == "The parties clause is the same in contract-2025 and contract-2026."


def summary(left: Document, right: Document, max_changes: int, context: int, chars: int) -> str:
    text: str = diff_clauses(left, right, "fees", max_changes, context, chars)["summary"]
    return text


def test_changes_closer_than_twice_the_context_are_one_passage() -> None:
    left = doc("## Fees\na b c d e f g h i j\n", "left")
    right = doc("## Fees\na X c d Y f g h i j\n", "right")
    assert summary(left, right, MAX_CHANGES, 1, MAX_CHARS) == (
        'The fees clause changed from left to right: "a b c d e f" became "a X c d Y f".'
    )
    far = doc("## Fees\na X c d e f g h i Y\n", "far")
    assert diff_clauses(left, far, "fees", MAX_CHANGES, 1, MAX_CHARS)["change_count"] == 2
    assert summary(left, far, MAX_CHANGES, 1, MAX_CHARS) == (
        'The fees clause changed from left to far: "a b c" became "a X c"; "i j" became "i Y".'
    )


def test_without_context_added_and_removed_words_are_said_so() -> None:
    left = doc("## Fees\nTen days.\n", "left")
    right = doc("## Fees\nTen working days.\n", "right")
    assert summary(left, right, MAX_CHANGES, 0, MAX_CHARS) == (
        'The fees clause changed from left to right: "working" was added.'
    )
    assert summary(right, left, MAX_CHANGES, 0, MAX_CHARS) == (
        'The fees clause changed from right to left: "working" was removed.'
    )


def test_diff_caps_the_changes_and_each_side_of_a_passage() -> None:
    left = doc("## Fees\n" + " ".join(f"w{i}" for i in range(10)) + "\n", "left")
    right = doc(
        "## Fees\n" + " ".join(f"w{i}" if i % 2 else "x" for i in range(10)) + "\n", "right"
    )
    assert diff_clauses(left, right, "fees", 2, 0, MAX_CHARS)["change_count"] == 5
    assert summary(left, right, 2, 0, MAX_CHARS) == (
        'The fees clause changed from left to right: "w0" became "x"; "w2" became "x". '
        "Only the first 2 of 5 changes are shown."
    )
    # A side cut at max_chars ends with "...", so the model knows the passage goes on.
    long = doc("## Fees\n" + "y " * 40 + "\n", "long")
    assert summary(left, long, MAX_CHANGES, 0, 8) == (
        'The fees clause changed from left to long: "w0 w1 w2..." became "y y y y...".'
    )


@pytest.mark.parametrize(
    ("left", "right", "error_parts"),
    [
        (2025, 2025, ["left and right are both 'contract-2025'"]),
        (2025, None, ["'termination'", "doc", "fees"]),
    ],
    ids=["same-file", "missing-in-one"],
)
def test_diff_errors(left: int, right: int | None, error_parts: list[str]) -> None:
    other = contract(right) if right is not None else doc("## Fees\nTen.\n")
    with pytest.raises(ToolError) as raised:
        diff("termination", contract(left), other)
    for part in error_parts:
        assert part in str(raised.value)


@pytest.mark.parametrize(
    ("mode", "names"),
    [
        ("auto", [CALCULATOR, READ_DOCUMENT, EXTRACT_CLAUSES, DIFF_CLAUSES, GET_POLICY]),
        (
            "required",
            [CALCULATOR, READ_DOCUMENT, EXTRACT_CLAUSES, DIFF_CLAUSES, GET_POLICY, FINAL_ANSWER],
        ),
    ],
)
def test_two_documents_offer_the_three_tools(mode: ToolChoice, names: list[str]) -> None:
    """get_policy comes with every text file too (card 21, P2.1a-2), until P4.8's scenario step."""
    assert list(offered_tools(mode, [], [contract(2025), contract(2026)])) == names


def test_one_document_offers_no_diff() -> None:
    assert list(offered_tools("auto", [], [contract(2025)])) == [
        CALCULATOR,
        READ_DOCUMENT,
        EXTRACT_CLAUSES,
        GET_POLICY,
    ]


def test_csv_and_text_files_offer_both_tool_sets() -> None:
    table = parse_file("sales", "sales.csv", "region,revenue\nNorth,1\n")
    tools = list(offered_tools("auto", [table], [contract(2025)]))
    assert tools[0] == CALCULATOR
    assert tools.index(CSV_SUMMARY) < tools.index(READ_DOCUMENT)


def test_per_run_schemas_list_exactly_the_runs_documents_and_clauses() -> None:
    extra = doc("## Payment terms\nNet 30.\n## Termination\nAny time.\n", "addendum")
    tools = offered_tools("auto", [], [contract(2025), contract(2026), extra])
    files = ["contract-2025", "contract-2026", "addendum"]
    clauses = ["parties", "services", "fees", "termination", "confidentiality", "payment terms"]
    for name in (READ_DOCUMENT, EXTRACT_CLAUSES):
        assert tools[name]["properties"]["file"]["enum"] == files
    assert tools[EXTRACT_CLAUSES]["properties"]["clause"]["enum"] == clauses
    diff_properties = tools[DIFF_CLAUSES]["properties"]
    assert list(diff_properties) == ["left", "right", "clause"]
    assert diff_properties["left"]["enum"] == files
    assert diff_properties["right"]["enum"] == files
    assert diff_properties["clause"]["enum"] == clauses
    for schema in tools.values():
        assert schema["required"] == list(schema["properties"])
        assert schema["additionalProperties"] is False


def test_auto_clause_outside_the_enum_is_rejected_with_the_allowed_values() -> None:
    tools = offered_tools("auto", [], [contract(2025), contract(2026)])
    arguments = {"file": "contract-2025", "clause": "4. Termination"}
    with pytest.raises(InvalidCallError) as raised:
        check_call(EXTRACT_CLAUSES, json.dumps(arguments), tools)
    for part in ("'4. Termination'", "'termination'", "'fees'"):
        assert part in str(raised.value)


def file(file_id: str, content: str, file_type: str | None = None) -> dict[str, str]:
    entry = {"id": file_id, "name": file_id, "content": content}
    return entry if file_type is None else {**entry, "type": file_type}


def test_read_run_files_splits_by_type_csv_by_default() -> None:
    files = [
        file("a", "## Fees\nTen.\n", "text"),
        file("b", "x\n1\n"),
        file("c", "y\n2\n", "csv"),
        file("d", "## Scope\nAll.\n", "text"),
    ]
    tables, documents = read_run_files(files, 4, 100)
    assert [table.id for table in tables] == ["b", "c"]
    assert [document.id for document in documents] == ["a", "d"]
    assert documents[0] == parse_document("a", "a", "## Fees\nTen.\n")


@pytest.mark.parametrize(
    ("files", "error_parts"),
    [
        (
            [file("a", "x\n1\n"), file("b", "## F\n.", "text"), file("c", "## G\n.", "text")],
            ["3 files", "agent.max_files (2)"],
        ),
        ([file("a", "x\n1\n"), file("a", "## F\n.", "text")], ["duplicate file id", "'a'"]),
        ([file("a", "## F\n" + "w" * 20, "text")], ["a:", "25 bytes", "agent.max_file_bytes (20)"]),
        ([file("a", "## F\n\ud800", "text")], ["a:", "not UTF-8"]),
        ([file("a", "no heading", "text")], ["a:", "no '## ' heading"]),
        ([file("a", "", "csv")], ["a:", "no header"]),
    ],
    ids=["too-many", "duplicate-id-across-types", "too-big", "surrogate", "no-heading", "bad-csv"],
)
def test_read_run_files_rejects(files: list[dict[str, str]], error_parts: list[str]) -> None:
    with pytest.raises(FileError) as raised:
        read_run_files(files, 2, 20)
    for part in error_parts:
        assert part in str(raised.value)
