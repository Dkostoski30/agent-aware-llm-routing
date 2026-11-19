"""Chats, messages and attachments (P3.12a, D7, D13): one message starts one agent run.

A message's run gets the files of this message and then the chat's earlier ones, newest first, up
to agent.max_files, so a follow-up question can use a CSV attached before. The app saves the reply
as an assistant row holding the run's id as soon as the run starts; GET /chats/{id} finishes
unfinished rows by asking the runtime. There is no background task, so a restarted app pod loses
nothing and the SQLite file has one writer (D7).

The runtime is called before anything is written: Python's sqlite3 opens a transaction only at the
first INSERT or UPDATE, so no write lock is held while the app waits on the network.

A run's detail, calls and events (P3.12b) are the runtime's, passed on unchanged, so run-calls.v1
and run-events.v1 hold here too; the browser has no login at the runtime, so it asks the app, which
passes them on only to the user whose chat started the run.
"""

import logging
import sqlite3
from collections.abc import AsyncGenerator, Callable
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app_backend import logs
from app_backend.config import AppConfig
from app_backend.runtime import RunRefused, RuntimeClient, RuntimeUnavailable

# The reply of a message whose run never started; the user can send it again.
RUNTIME_UNREACHABLE = "The agent runtime could not be reached, so the run did not start."
# The reply of a run that was still running when the app last asked and that the runtime has since
# dropped: run history lives in Redis for agent.history_ttl_seconds (D7).
RUN_EXPIRED = (
    "The agent runtime no longer has this run: its history expired before the answer was read."
)
# One body for another user's chat and a missing one, so ids do not reveal which chats exist.
NO_CHAT = "no such chat"
# The same for runs: another user's run and an unknown id get this one body.
NO_RUN = "no such run"
# A run's detail, calls or events asked for while the runtime cannot answer; asking again may work.
RUNTIME_DOWN = "The agent runtime could not be reached."
# The headers of the runtime's event stream that the browser acts on, passed on with it.
STREAM_HEADERS = ("content-type", "cache-control")
# A file's id in schemas/run-request.v1.json: a lowercase slug of at most 64 characters.
FILE_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
MAX_FILE_ID_LENGTH = 64

logger = logging.getLogger(__name__)


class NewChat(BaseModel):
    title: str = Field(min_length=1)


class Chat(BaseModel):
    id: int
    title: str
    created: float


class RunFile(BaseModel):
    """One file as run-request.v1 has it; the UI builds it (ui/src/app/runs/run-files.ts)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=FILE_ID_PATTERN, max_length=MAX_FILE_ID_LENGTH)
    name: str = Field(min_length=1)
    type: Literal["csv", "text"] = "csv"
    content: str


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    files: list[RunFile] = []
    # run-request.v1's seed, sent with this message's run only and not saved: make smoke sends one
    # so its answer repeats (P3.12b); the UI sends none.
    seed: int | None = None


class FileInfo(BaseModel):
    """An attachment without its content, which only the run needs."""

    id: str
    name: str
    type: str
    size: int


class Message(BaseModel):
    id: int
    role: str
    # The user's task, or the run's answer or error; empty while the run is running.
    text: str
    status: str
    run_id: str | None
    created: float
    files: list[FileInfo]


class ChatWithMessages(Chat):
    messages: list[Message]


class Sent(BaseModel):
    user_message_id: int
    assistant_message_id: int
    run_id: str


def unique_id(file_id: str, taken: set[str]) -> str:
    """file_id, or the first of file_id-2, file_id-3, ... not taken. The id is cut to leave room
    for the number, and a dash the cut leaves at its end is dropped, so it stays a slug."""
    if file_id not in taken:
        return file_id
    number = 2
    while True:
        suffix = f"-{number}"
        candidate = file_id[: MAX_FILE_ID_LENGTH - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            return candidate
        number += 1


def owned_chat(conn: sqlite3.Connection, chat_id: int, user_id: int) -> Chat:
    row = conn.execute(
        "SELECT id, title, created FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=NO_CHAT)
    return Chat(id=row[0], title=row[1], created=row[2])


def run_request(
    conn: sqlite3.Connection,
    chat_id: int,
    message: NewMessage,
    files: list[RunFile],
    max_files: int,
) -> dict[str, Any]:
    """The run-request.v1 body: the message's task and seed, its files (renamed where taken), then
    the chat's earlier ones, newest first, max_files in all. A message without files or seed
    sends none, like a run request without them."""
    earlier = conn.execute(
        "SELECT file_id, name, type, content FROM attachments WHERE chat_id = ?"
        " ORDER BY message_id DESC, id LIMIT ?",
        (chat_id, max_files - len(files)),
    ).fetchall()
    run_files = [file.model_dump() for file in files] + [
        {"id": row[0], "name": row[1], "type": row[2], "content": row[3].decode()}
        for row in earlier
    ]
    request: dict[str, Any] = {"task": message.task}
    if message.seed is not None:
        request["seed"] = message.seed
    if run_files:
        request["files"] = run_files
    return request


def save_message(
    conn: sqlite3.Connection,
    chat_id: int,
    task: str,
    files: list[RunFile],
    reply: tuple[str, str, str | None],
    now: float,
) -> tuple[int, int]:
    """The user message, its files and the assistant row (status, text, run_id); their ids."""
    (user_message_id,) = conn.execute(
        "INSERT INTO messages (chat_id, role, text, created, status)"
        " VALUES (?, 'user', ?, ?, 'done') RETURNING id",
        (chat_id, task, now),
    ).fetchone()
    for file in files:
        content = file.content.encode()
        conn.execute(
            "INSERT INTO attachments (chat_id, message_id, file_id, name, type, size, content)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user_message_id, file.id, file.name, file.type, len(content), content),
        )
    status, text, run_id = reply
    (assistant_message_id,) = conn.execute(
        "INSERT INTO messages (chat_id, role, text, run_id, created, status)"
        " VALUES (?, 'assistant', ?, ?, ?, ?) RETURNING id",
        (chat_id, text, run_id, now, status),
    ).fetchone()
    return user_message_id, assistant_message_id


def finished_rows(
    runtime: RuntimeClient, running: list[tuple[int, str]]
) -> list[tuple[str, str | None, int]]:
    """(status, text, message id) for each running row whose run has ended. The runtime sets the
    answer on every done run and the error on every failed one (agent_runtime/loop.py)."""
    updates: list[tuple[str, str | None, int]] = []
    for message_id, run_id in running:
        try:
            state = runtime.get_run(run_id)
        except RuntimeUnavailable as error:
            # The rows left stay running and are asked for again on the next read.
            logs.log(logger, logging.WARNING, "runtime unavailable", error=str(error))
            return updates
        if state is None:
            updates.append(("failed", RUN_EXPIRED, message_id))
        elif state.status == "done":
            updates.append(("done", state.answer, message_id))
        elif state.status == "failed":
            updates.append(("failed", state.error, message_id))
    return updates


def chat_messages(conn: sqlite3.Connection, chat_id: int) -> list[Message]:
    files: dict[int, list[FileInfo]] = {}
    for message_id, file_id, name, file_type, size in conn.execute(
        "SELECT message_id, file_id, name, type, size FROM attachments WHERE chat_id = ?"
        " ORDER BY id",
        (chat_id,),
    ):
        files.setdefault(message_id, []).append(
            FileInfo(id=file_id, name=name, type=file_type, size=size)
        )
    return [
        Message(
            id=row[0],
            role=row[1],
            text=row[2],
            status=row[3],
            run_id=row[4],
            created=row[5],
            files=files.get(row[0], []),
        )
        for row in conn.execute(
            "SELECT id, role, text, status, run_id, created FROM messages WHERE chat_id = ?"
            " ORDER BY id",
            (chat_id,),
        )
    ]


async def passed_on(response: httpx.Response, run_id: str) -> AsyncGenerator[bytes]:
    """The runtime's event stream, each piece as it arrives, then closed. A runtime that drops
    mid-stream ends it after what it sent; the browser's EventSource then opens it again and the
    runtime sends the run's events from the start (agent_runtime/app.py, run_events)."""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.TransportError as error:
        logs.log(
            logger, logging.WARNING, "run events dropped", **{"run-id": run_id}, error=repr(error)
        )
    finally:
        await response.aclose()


def router(
    config: AppConfig,
    runtime: RuntimeClient,
    clock: Callable[[], float],
    use_conn: Any,
    use_user_id: Any,
) -> APIRouter:
    """The chat and run endpoints. use_conn and use_user_id are app.py's dependencies: the
    request's connection and the logged-in user's id (401 without a session)."""
    chats = APIRouter()
    Conn = Annotated[sqlite3.Connection, use_conn]
    UserId = Annotated[int, use_user_id]

    def owned_run(run_id: str, user_id: UserId, conn: Conn) -> str:
        """run_id, if a message in one of the user's chats holds it (only assistant rows hold
        one, save_message); else 404. A dependency, so the async events endpoint gets it from
        FastAPI's threadpool, off the event loop, and the connection is closed before the stream
        starts (use_conn's scope)."""
        row = conn.execute(
            "SELECT 1 FROM messages JOIN chats ON chats.id = messages.chat_id"
            " WHERE messages.run_id = ? AND chats.user_id = ?",
            (run_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=NO_RUN)
        return run_id

    OwnedRun = Annotated[str, Depends(owned_run)]

    def unreachable(run_id: str, error: RuntimeUnavailable) -> HTTPException:
        logs.log(
            logger, logging.WARNING, "runtime unavailable", **{"run-id": run_id}, error=str(error)
        )
        return HTTPException(status_code=502, detail=RUNTIME_DOWN)

    def forwarded(run_id: str, path: str) -> Response:
        try:
            answer = runtime.forward(path)
        except RuntimeUnavailable as error:
            raise unreachable(run_id, error) from error
        return Response(answer.body, status_code=answer.status, media_type=answer.content_type)

    @chats.get("/runs/{run_id}")
    def read_run(run_id: OwnedRun) -> Response:
        return forwarded(run_id, f"/runs/{run_id}")

    @chats.get("/runs/{run_id}/calls")
    def read_calls(run_id: OwnedRun) -> Response:
        return forwarded(run_id, f"/runs/{run_id}/calls")

    @chats.get("/runs/{run_id}/events")
    async def read_events(run_id: OwnedRun) -> Response:
        try:
            response = await runtime.open_events(run_id)
        except RuntimeUnavailable as error:
            raise unreachable(run_id, error) from error
        if response.status_code != httpx.codes.OK:
            # The runtime's 404 for a run its Redis TTL dropped: a short JSON body, read whole.
            body = await response.aread()
            await response.aclose()
            return Response(
                body, status_code=response.status_code, media_type=response.headers["content-type"]
            )
        return StreamingResponse(
            passed_on(response, run_id),
            headers={name: response.headers[name] for name in STREAM_HEADERS},
        )

    @chats.post("/chats", status_code=201)
    def create_chat(body: NewChat, user_id: UserId, conn: Conn) -> Chat:
        now = clock()
        (chat_id,) = conn.execute(
            "INSERT INTO chats (user_id, title, created) VALUES (?, ?, ?) RETURNING id",
            (user_id, body.title, now),
        ).fetchone()
        return Chat(id=chat_id, title=body.title, created=now)

    @chats.get("/chats")
    def list_chats(user_id: UserId, conn: Conn) -> list[Chat]:
        return [
            Chat(id=row[0], title=row[1], created=row[2])
            for row in conn.execute(
                "SELECT id, title, created FROM chats WHERE user_id = ?"
                " ORDER BY created DESC, id DESC",
                (user_id,),
            )
        ]

    @chats.get("/chats/{chat_id}")
    def read_chat(chat_id: int, user_id: UserId, conn: Conn) -> ChatWithMessages:
        chat = owned_chat(conn, chat_id, user_id)
        running = conn.execute(
            "SELECT id, run_id FROM messages WHERE chat_id = ? AND status = 'running' ORDER BY id",
            (chat_id,),
        ).fetchall()
        conn.executemany(
            "UPDATE messages SET status = ?, text = ? WHERE id = ?", finished_rows(runtime, running)
        )
        return ChatWithMessages(**chat.model_dump(), messages=chat_messages(conn, chat_id))

    @chats.post("/chats/{chat_id}/messages", status_code=202, response_model=Sent)
    def send_message(
        chat_id: int, body: NewMessage, user_id: UserId, conn: Conn
    ) -> Sent | JSONResponse:
        owned_chat(conn, chat_id, user_id)
        if len(body.files) > config.max_files:
            raise HTTPException(
                status_code=422,
                detail=f"at most {config.max_files} files per message (agent.max_files)",
            )
        taken = {
            row[0]
            for row in conn.execute("SELECT file_id FROM attachments WHERE chat_id = ?", (chat_id,))
        }
        files = []
        for file in body.files:
            file_id = unique_id(file.id, taken)
            taken.add(file_id)
            files.append(file.model_copy(update={"id": file_id}))
        request = run_request(conn, chat_id, body, files, config.max_files)
        try:
            run_id = runtime.start_run(request)
        except RunRefused as refused:
            # Nothing is saved, so a file the runtime cannot read never enters the chat and every
            # later run of the chat, which carries its files.
            raise HTTPException(status_code=422, detail=str(refused)) from refused
        except RuntimeUnavailable as error:
            logs.log(logger, logging.WARNING, "runtime unavailable", error=str(error))
            save_message(
                conn, chat_id, body.task, files, ("failed", RUNTIME_UNREACHABLE, None), clock()
            )
            # A response, not an exception, so the request's transaction commits the rows.
            return JSONResponse(status_code=502, content={"detail": RUNTIME_UNREACHABLE})
        user_message_id, assistant_message_id = save_message(
            conn, chat_id, body.task, files, ("running", "", run_id), clock()
        )
        logs.log(logger, logging.INFO, "run started", **{"run-id": run_id, "chat": chat_id})
        return Sent(
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            run_id=run_id,
        )

    return chats
