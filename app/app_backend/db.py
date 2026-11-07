"""The app backend's SQLite file (D7): users, sessions, chats, messages, attachments.

The schema's version is kept in PRAGMA user_version, so a later task (P3.12) can add columns: it
bumps SCHEMA_VERSION and adds a step from the previous version.
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# Times are unix seconds from the app's clock. Deleting a user deletes their sessions, chats, and
# through those the messages and attachments.
_SCHEMA_V1 = f"""
BEGIN;
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
CREATE TABLE chats (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    -- The agent run that wrote an assistant message (D13); NULL on user messages.
    run_id TEXT,
    created REAL NOT NULL
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    -- The enum ID the agent's tools take the file by (D13).
    file_id TEXT NOT NULL,
    name TEXT NOT NULL,
    -- The run request's file types (schemas/run-request.v1.json).
    type TEXT NOT NULL CHECK (type IN ('csv', 'text')),
    size INTEGER NOT NULL,
    -- Kept in the database, so one file holds all the app's data (P2.10b's volume, make reset-app).
    content BLOB NOT NULL
);
PRAGMA user_version = {SCHEMA_VERSION};
COMMIT;
"""


class SchemaError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    """A connection with foreign keys enforced (SQLite leaves them off per connection).

    check_same_thread=False: FastAPI may run a request's dependency and its endpoint on different
    threads of its pool; each connection still serves one request at a time.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(path: Path) -> None:
    """Creates the tables in a new file; leaves a file at SCHEMA_VERSION as it is."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaError(
                f"{path} has schema version {version}, newer than this code's {SCHEMA_VERSION}"
            )
        if version == 0:
            # One transaction, the version included: a crash leaves either nothing or all of it.
            conn.executescript(_SCHEMA_V1)
    finally:
        conn.close()
