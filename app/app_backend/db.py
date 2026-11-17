"""The app backend's SQLite file (D7): users, sessions, chats, messages, attachments.

The schema's version is kept in PRAGMA user_version. SCHEMA_STEPS[n] takes a file from version n to
n + 1, so a new file runs every step and an older one only the steps it misses.
"""

import sqlite3
from pathlib import Path

# Times are unix seconds from the app's clock. Deleting a user deletes their sessions, chats, and
# through those the messages and attachments.
_SCHEMA_V1 = """
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
PRAGMA user_version = 1;
COMMIT;
"""

# P3.12a. A message's status: the run behind an assistant row is running, done or failed; user rows
# are done. Rows from version 1 had no runs yet, so they become done; ADD COLUMN with NOT NULL needs
# that default, and the app names the status on every insert.
# An attachment's chat is copied from its message, so (chat_id, file_id) can be unique: the tools
# take a file by its id (D13), and a run carries files from the whole chat. SQLite cannot add a
# column with a foreign key and NOT NULL, nor a UNIQUE constraint, to a table in place, so the table
# is copied into a new one. Nothing references attachments, so dropping the old table is safe.
_SCHEMA_V2 = """
BEGIN;
ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'done'
    CHECK (status IN ('running', 'done', 'failed'));
CREATE TABLE attachments_v2 (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('csv', 'text')),
    size INTEGER NOT NULL,
    content BLOB NOT NULL,
    UNIQUE (chat_id, file_id)
);
INSERT INTO attachments_v2 (id, chat_id, message_id, file_id, name, type, size, content)
    SELECT attachments.id, messages.chat_id, message_id, file_id, name, type, size, content
    FROM attachments JOIN messages ON messages.id = attachments.message_id;
DROP TABLE attachments;
ALTER TABLE attachments_v2 RENAME TO attachments;
PRAGMA user_version = 2;
COMMIT;
"""

# Each step is one transaction, its version included: a crash leaves the file at the version before
# the step or after it.
SCHEMA_STEPS = [_SCHEMA_V1, _SCHEMA_V2]
SCHEMA_VERSION = len(SCHEMA_STEPS)


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
    """Brings a new or older file to SCHEMA_VERSION; leaves a file at SCHEMA_VERSION as it is."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaError(
                f"{path} has schema version {version}, newer than this code's {SCHEMA_VERSION}"
            )
        for step in SCHEMA_STEPS[version:]:
            conn.executescript(step)
    finally:
        conn.close()
