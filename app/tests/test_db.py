"""The SQLite schema (P2.10a, D7): applied at startup, its version in PRAGMA user_version so P3.12
can add columns."""

import sqlite3
from pathlib import Path

import pytest
from app_backend.db import SCHEMA_VERSION, SchemaError, apply_schema, connect

COLUMNS = {
    "users": ["id", "email", "password_hash", "created"],
    "sessions": ["token_hash", "user_id", "expires"],
    "chats": ["id", "user_id", "title", "created"],
    "messages": ["id", "chat_id", "role", "text", "run_id", "created"],
    "attachments": ["id", "message_id", "file_id", "name", "type", "size", "content"],
}


def version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        value: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return value


def test_a_new_file_gets_the_five_tables_and_the_version(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    apply_schema(path)
    conn = connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == set(COLUMNS)
    for table, columns in COLUMNS.items():
        assert [row[1] for row in conn.execute(f"PRAGMA table_info({table})")] == columns
    conn.close()
    assert version(path) == SCHEMA_VERSION == 1


def test_applying_again_keeps_the_data(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    apply_schema(path)
    conn = connect(path)
    with conn:
        conn.execute("INSERT INTO users (email, password_hash, created) VALUES ('a@x.com', 'h', 0)")
    conn.close()
    apply_schema(path)
    conn = connect(path)
    assert conn.execute("SELECT email FROM users").fetchall() == [("a@x.com",)]
    conn.close()
    assert version(path) == SCHEMA_VERSION


def test_a_file_from_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    apply_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaError, match="newer"):
        apply_schema(path)


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    apply_schema(path)
    conn = connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES ('t', 999, 0)")
    conn.close()


def test_a_message_role_is_user_or_assistant(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    apply_schema(path)
    conn = connect(path)
    conn.execute("INSERT INTO users (email, password_hash, created) VALUES ('a@x.com', 'h', 0)")
    conn.execute("INSERT INTO chats (user_id, title, created) VALUES (1, 'c', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages (chat_id, role, text, created) VALUES (1, 'system', 't', 0)"
        )
    conn.close()
