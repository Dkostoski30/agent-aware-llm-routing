"""The contracts tools of the demo (P2.1a-1): text documents from the run request, read by section.

A line "## <title>" starts a section; the text up to the next such line is its body. A contract's
sections are its clauses. The model names a document by its file ID and a clause by its normalised
title, both from enums (D10). Documents never enter the context; only the tool results do (D13).
"""

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runtime import csv_tools
from agent_runtime.config import AgentConfig
from agent_runtime.csv_tools import FileError, Table, ToolError

READ_DOCUMENT = "read_document"
EXTRACT_CLAUSES = "extract_clauses"
DIFF_CLAUSES = "diff_clauses"
TOOL_NAMES = (READ_DOCUMENT, EXTRACT_CLAUSES, DIFF_CLAUSES)

DESCRIPTIONS = {
    READ_DOCUMENT: (
        "Lists the clauses of a text document such as a contract: each clause's name, heading and "
        "word count. Pass a clause's name to extract_clauses or diff_clauses."
    ),
    EXTRACT_CLAUSES: "The text of one clause of a text document.",
    DIFF_CLAUSES: (
        "Compares one clause between two text documents word by word and says in one sentence "
        "what each changed passage was and became."
    ),
}

_HEADING = "## "
# Leading clause numbering: "4.", "4.1", "4)" or a bare "4 " before the title. A digit followed by
# a letter ("3D printing") is part of the title.
_NUMBERING = re.compile(r"^\d+(?:\.\d+)*(?:[.)]\s*|\s+)")
_CUT = "..."


@dataclass(frozen=True)
class Section:
    # The normalised title, the value the clause enum lists.
    clause: str
    title: str
    body: str


@dataclass(frozen=True)
class Document:
    id: str
    name: str
    sections: list[Section]

    def section(self, clause: str) -> Section:
        """The section of clause; a clause this document lacks is a tool error (D10)."""
        for section in self.sections:
            if section.clause == clause:
                return section
        clauses = ", ".join(section.clause for section in self.sections)
        raise ToolError(f"{self.id} has no clause {clause!r}; its clauses: {clauses}")


def normalise_title(title: str) -> str:
    """Lower case, numbering dropped, spaces collapsed, so "4. Termination" in one contract and
    "Termination" in another are the same clause."""
    return " ".join(_NUMBERING.sub("", title.strip()).lower().split())


def parse_document(file_id: str, name: str, content: str) -> Document:
    """Reads a text document; raises FileError for one the tools cannot use. Text above the first
    heading (a contract's title line) is in no section, so no tool reads it (decided 2026-11-01)."""
    sections: list[Section] = []
    title: str | None = None
    body: list[str] = []

    def close() -> None:
        if title is not None:
            clause = normalise_title(title)
            if not clause:
                raise FileError(f"{file_id}: heading {title!r} is empty without its numbering")
            for earlier in sections:
                if earlier.clause == clause:
                    raise FileError(
                        f"{file_id}: headings {earlier.title!r} and {title!r} are both clause "
                        f"{clause!r}"
                    )
            sections.append(Section(clause, title, "\n".join(body).strip()))

    # A byte-order mark, as Windows editors write it, would hide a heading on the first line.
    for line in content.removeprefix("﻿").splitlines():
        if line.startswith(_HEADING):
            close()
            title, body = line.removeprefix(_HEADING).strip(), []
        else:
            body.append(line)
    close()
    if not sections:
        raise FileError(f"{file_id}: no '{_HEADING}' heading, so no section the tools can read")
    return Document(id=file_id, name=name, sections=sections)


def read_run_files(
    files: Sequence[dict[str, str]], max_files: int, max_file_bytes: int
) -> tuple[list[Table], list[Document]]:
    """The run request's files as CSV tables and text documents, each in request order; a file
    without a type is CSV (schemas/run-request.v1.json). Raises FileError when the run must not
    start; the limits count files of both types."""
    csv_tools.check_files(files, max_files, max_file_bytes)
    tables = [
        csv_tools.parse_file(file["id"], file["name"], file["content"])
        for file in files
        if file.get("type", "csv") == "csv"
    ]
    documents = [
        parse_document(file["id"], file["name"], file["content"])
        for file in files
        if file.get("type") == "text"
    ]
    return tables, documents


def clause_enum(documents: Sequence[Document]) -> list[str]:
    """The clauses across the documents, in first-seen order; a clause several documents share is
    listed once."""
    return list(dict.fromkeys(s.clause for document in documents for s in document.sections))


def parameters(documents: Sequence[Document]) -> dict[str, dict[str, Any]]:
    """The JSON schemas of the document tools for one run (D10). file, left and right are enums of
    the run's document IDs, clause an enum of the clauses across them; whether the named document
    has the clause is checked when the tool runs. diff_clauses needs two documents."""
    file = {"type": "string", "enum": [document.id for document in documents]}
    clause = {"type": "string", "enum": clause_enum(documents)}

    def schema(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    tools = {
        READ_DOCUMENT: schema({"file": file}),
        EXTRACT_CLAUSES: schema({"file": file, "clause": clause}),
    }
    if len(documents) >= 2:
        tools[DIFF_CLAUSES] = schema({"left": file, "right": file, "clause": clause})
    return tools


def read_document(document: Document, max_rows: int) -> dict[str, Any]:
    return {
        "file": document.id,
        "name": document.name,
        "clause_count": len(document.sections),
        "clauses": [
            {"clause": s.clause, "title": s.title, "word_count": len(s.body.split())}
            for s in document.sections[:max_rows]
        ],
    }


def extract_clauses(document: Document, clause: str, max_chars: int) -> dict[str, Any]:
    section = document.section(clause)
    return {
        "file": document.id,
        "clause": clause,
        "title": section.title,
        "text": section.body[:max_chars],
        "truncated": len(section.body) > max_chars,
    }


def cut_text(text: str, max_chars: int) -> str:
    """text, or its first max_chars characters ending with "..." so the model knows it goes on."""
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + _CUT


def _change(before: str, after: str) -> str:
    """One passage as words the model can copy; a side with no words (an added or removed word,
    no context) is said so instead of quoted empty."""
    if not before:
        return f'"{after}" was added'
    if not after:
        return f'"{before}" was removed'
    return f'"{before}" became "{after}"'


def diff_clauses(
    left: Document,
    right: Document,
    clause: str,
    max_changes: int,
    context_words: int,
    max_chars: int,
) -> dict[str, Any]:
    """The clause in two documents compared word by word, said in one summary sentence that holds
    every shown change: the demo model answers by copying the summary, and one that only counted
    the changes became the whole answer (P2.1a-1 demo run, 2026-11-01). Each passage is a run of
    changed words with context_words unchanged words on each side, so the model can tell what
    changed ("30" alone does not say it was a notice period; decided 2026-11-01); changes closer
    than twice that are one passage. Each side of a passage is cut at max_chars, as
    extract_clauses cuts a clause."""
    if left.id == right.id:
        raise ToolError(f"left and right are both {left.id!r}; name two different documents")
    before = left.section(clause).body.split()
    after = right.section(clause).body.split()
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    changes = [
        _change(
            cut_text(" ".join(before[group[0][1] : group[-1][2]]), max_chars),
            cut_text(" ".join(after[group[0][3] : group[-1][4]]), max_chars),
        )
        for group in matcher.get_grouped_opcodes(context_words)
    ]
    count = len(changes)
    if count == 0:
        summary = f"The {clause} clause is the same in {left.id} and {right.id}."
    else:
        shown = "; ".join(changes[:max_changes])
        summary = f"The {clause} clause changed from {left.id} to {right.id}: {shown}."
        if count > max_changes:
            summary += f" Only the first {max_changes} of {count} changes are shown."
    return {
        "clause": clause,
        "left": left.id,
        "right": right.id,
        "change_count": count,
        "summary": summary,
    }


def run_document_tool(
    name: str, arguments: dict[str, Any], documents: Mapping[str, Document], config: AgentConfig
) -> dict[str, Any]:
    """Runs a call to a document tool that passed check_call. The enums guarantee the documents
    exist; a call their sections cannot answer comes back as a tool error."""
    try:
        if name == READ_DOCUMENT:
            return read_document(documents[arguments["file"]], config.max_result_rows)
        if name == EXTRACT_CLAUSES:
            document = documents[arguments["file"]]
            return extract_clauses(document, arguments["clause"], config.max_clause_chars)
        if name == DIFF_CLAUSES:
            return diff_clauses(
                documents[arguments["left"]],
                documents[arguments["right"]],
                arguments["clause"],
                config.max_diff_changes,
                config.diff_context_words,
                config.max_clause_chars,
            )
    except ToolError as error:
        return {"error": str(error)}
    raise ValueError(f"{name!r} is not a document tool")
