"""The app backend's HTTP API (P2.10a): register, login, logout, the logged-in user, health.

A login sets a session cookie holding a random token; the database keeps only the token's sha256,
so a copy of the file holds no usable session. Sessions last app.session_ttl_seconds from login.
"""

import hashlib
import logging
import os
import secrets
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from app_backend import db, logs
from app_backend.config import AppConfig, load_config
from app_backend.passwords import hash_password, verify_password

# The port P2.10b's Service targets.
PORT = 8080
SESSION_COOKIE = "session"
# 32 random bytes (256 bits) cannot be guessed, so a plain sha256 of the token is enough in the
# database; a slow hash like scrypt only matters for passwords people choose.
TOKEN_BYTES = 32
# One body for a wrong password and an unknown email, so login does not say which emails exist.
LOGIN_REFUSED = "wrong email or password"

Clock = Callable[[], float]
logger = logging.getLogger(__name__)


class Credentials(BaseModel):
    email: str
    password: str


class User(BaseModel):
    id: int
    email: str


def normalize_email(raw: str) -> str:
    """Trimmed and lowercased, with text on both sides of an @.

    The app sends no mail, so the email is only a login name and needs no stricter check (and no
    email-validator package)."""
    email = raw.strip().lower()
    local, at, domain = email.rpartition("@")
    if not (local and at and domain):
        raise HTTPException(status_code=422, detail="the email needs text on both sides of an @")
    return email


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_app(config: AppConfig, db_path: Path, clock: Clock = time.time) -> FastAPI:
    """The API on the SQLite file at db_path, its schema applied first. clock gives unix seconds;
    tests pass a fake one to expire sessions."""
    db.apply_schema(db_path)
    app = FastAPI(title="app backend")

    @app.middleware("http")
    async def with_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        context = logs.REQUEST_ID.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            logs.log(
                logger,
                logging.INFO,
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=round((time.perf_counter() - started) * 1000, 1),
            )
        finally:
            logs.REQUEST_ID.reset(context)
        response.headers["x-request-id"] = request_id
        return response

    def connection() -> Iterator[sqlite3.Connection]:
        """One connection per request: committed when the endpoint returns, rolled back when it
        raises. One writer pod (D7), so no pool."""
        conn = db.connect(db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # scope="function": the commit happens before the response is sent, so a client that gets its
    # login answer finds the session on its next request.
    use_conn = Depends(connection, scope="function")

    def current_user(request: Request, conn: Annotated[sqlite3.Connection, use_conn]) -> User:
        token = request.cookies.get(SESSION_COOKIE)
        if token is None:
            raise HTTPException(status_code=401, detail="not logged in")
        row = conn.execute(
            "SELECT users.id, users.email FROM sessions JOIN users ON users.id = sessions.user_id"
            " WHERE sessions.token_hash = ? AND sessions.expires > ?",
            (token_hash(token), clock()),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="not logged in")
        return User(id=row[0], email=row[1])

    @app.post("/auth/register", status_code=201)
    def register(credentials: Credentials, conn: Annotated[sqlite3.Connection, use_conn]) -> User:
        email = normalize_email(credentials.email)
        if len(credentials.password) < config.min_password_length:
            raise HTTPException(
                status_code=422,
                detail=f"the password needs at least {config.min_password_length} characters",
            )
        try:
            (user_id,) = conn.execute(
                "INSERT INTO users (email, password_hash, created) VALUES (?, ?, ?) RETURNING id",
                (email, hash_password(credentials.password, config.scrypt), clock()),
            ).fetchone()
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="this email is registered") from error
        logs.log(logger, logging.INFO, "registered", user=user_id)
        return User(id=user_id, email=email)

    @app.post("/auth/login")
    def login(
        credentials: Credentials,
        response: Response,
        conn: Annotated[sqlite3.Connection, use_conn],
    ) -> User:
        email = normalize_email(credentials.email)
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is None or not verify_password(credentials.password, row[1]):
            logs.log(logger, logging.INFO, "login refused")
            raise HTTPException(status_code=401, detail=LOGIN_REFUSED)
        now = clock()
        # Expired sessions of every user are swept at any login, so sessions nobody logged out of
        # do not pile up.
        conn.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        token = secrets.token_urlsafe(TOKEN_BYTES)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires) VALUES (?, ?, ?)",
            (token_hash(token), row[0], now + config.session_ttl_seconds),
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=config.session_ttl_seconds,
            path="/",
            secure=config.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        logs.log(logger, logging.INFO, "logged in", user=row[0])
        return User(id=row[0], email=email)

    @app.post("/auth/logout", status_code=204)
    def logout(
        request: Request,
        response: Response,
        conn: Annotated[sqlite3.Connection, use_conn],
    ) -> None:
        """Deletes the session row, so the token stops working even if a copy is sent again."""
        token = request.cookies.get(SESSION_COOKIE)
        if token is not None:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=config.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        logs.log(logger, logging.INFO, "logged out")

    @app.get("/auth/me")
    def me(user: Annotated[User, Depends(current_user)]) -> User:
        return user

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def from_env() -> FastAPI:
    """The app as P2.10b's Deployment runs it: the profile and the database file from the
    environment."""
    config = load_config(Path(os.environ["THESIS_CONFIG"]))
    return create_app(config, Path(os.environ["THESIS_APP_DB"]))


def main() -> None:
    logs.configure()
    # log_config=None keeps uvicorn's own lines on the JSON handler that logs.configure set up.
    # access_log=False: uvicorn writes its access line outside the request, without a request-id;
    # the middleware writes one with it.
    uvicorn.run(from_env(), host="0.0.0.0", port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
