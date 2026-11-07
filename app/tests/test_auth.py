"""Register, login, logout and the session cookie (P2.10a).

Each test gets its own SQLite file and a fake clock, so a test decides when a session expires.
The client talks to https://testserver because the cookie is Secure: httpx, like a browser, keeps
a Secure cookie off plain HTTP.
"""

import sqlite3
from pathlib import Path

import pytest
from app_backend.app import SESSION_COOKIE, create_app
from app_backend.config import AppConfig, ScryptParams
from fastapi.testclient import TestClient

TTL = 3600
MIN_LENGTH = 15
START = 1_000_000.0
EMAIL = "ana@example.com"
PASSWORD = "a long enough password"


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


def make_config() -> AppConfig:
    return AppConfig(
        session_ttl_seconds=TTL,
        scrypt=ScryptParams(n=2, r=1, p=1),
        min_password_length=MIN_LENGTH,
        cookie_secure=True,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(db_path: Path, clock: FakeClock) -> TestClient:
    return TestClient(create_app(make_config(), db_path, clock), base_url="https://testserver")


def register(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> int:
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    user_id: int = response.json()["id"]
    return user_id


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.cookies[SESSION_COOKIE]
    return token


def test_register_login_me(client: TestClient) -> None:
    user_id = register(client)
    response = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"id": user_id, "email": EMAIL}
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json() == {"id": user_id, "email": EMAIL}


def test_register_does_not_log_in(client: TestClient) -> None:
    register(client)
    assert SESSION_COOKIE not in client.cookies
    assert client.get("/auth/me").status_code == 401


def test_the_session_cookie_is_httponly_strict_secure_and_lasts_the_ttl(client: TestClient) -> None:
    register(client)
    response = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    attributes = [part.strip().lower() for part in response.headers["set-cookie"].split(";")]
    assert attributes[0].startswith(f"{SESSION_COOKIE}=")
    assert {"httponly", "samesite=strict", "secure", f"max-age={TTL}", "path=/"} <= set(attributes)


def test_a_wrong_password_and_an_unknown_email_get_the_same_401(client: TestClient) -> None:
    register(client)
    wrong = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD + "!"})
    unknown = client.post("/auth/login", json={"email": "bob@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert SESSION_COOKIE not in client.cookies


def test_a_session_ends_at_its_ttl(client: TestClient, clock: FakeClock) -> None:
    register(client)
    login(client)
    clock.now = START + TTL - 1
    assert client.get("/auth/me").status_code == 200
    clock.now = START + TTL
    assert client.get("/auth/me").status_code == 401


def test_a_login_deletes_every_expired_session(
    client: TestClient, clock: FakeClock, db_path: Path
) -> None:
    register(client, email="ana@example.com")
    register(client, email="bob@example.com")
    login(client, email="ana@example.com")
    clock.now = START + TTL
    login(client, email="bob@example.com")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT users.email FROM sessions JOIN users ON users.id = user_id")
        assert rows.fetchall() == [("bob@example.com",)]


def test_logout_ends_the_session_on_the_server(client: TestClient) -> None:
    register(client)
    token = login(client)
    assert client.post("/auth/logout").status_code == 204
    assert SESSION_COOKIE not in client.cookies
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/auth/me").status_code == 401


def test_logout_without_a_session_is_fine(client: TestClient) -> None:
    assert client.post("/auth/logout").status_code == 204


def test_logout_ends_only_its_own_session(db_path: Path, clock: FakeClock) -> None:
    app = create_app(make_config(), db_path, clock)
    first = TestClient(app, base_url="https://testserver")
    second = TestClient(app, base_url="https://testserver")
    register(first)
    login(first)
    login(second)
    first.post("/auth/logout")
    assert second.get("/auth/me").status_code == 200


def test_a_duplicate_email_gets_409(client: TestClient) -> None:
    register(client)
    again = client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert again.status_code == 409


def test_emails_are_trimmed_and_lowercased(client: TestClient) -> None:
    register(client, email=" Ana@Example.COM ")
    again = client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert again.status_code == 409
    login(client, email="ANA@example.com")
    assert client.get("/auth/me").json()["email"] == EMAIL


def test_the_password_must_reach_the_minimum_length(client: TestClient) -> None:
    short = client.post("/auth/register", json={"email": EMAIL, "password": "p" * (MIN_LENGTH - 1)})
    assert short.status_code == 422
    register(client, password="p" * MIN_LENGTH)


@pytest.mark.parametrize("email", ["ana", "@example.com", "ana@", ""])
def test_an_email_needs_text_on_both_sides_of_an_at(client: TestClient, email: str) -> None:
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 422


def test_me_without_a_session_or_with_an_unknown_token_is_401(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401
    client.cookies.set(SESSION_COOKIE, "not-a-token")
    assert client.get("/auth/me").status_code == 401


def test_the_database_holds_neither_the_password_nor_the_token(
    client: TestClient, db_path: Path
) -> None:
    register(client)
    token = login(client)
    with sqlite3.connect(db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert PASSWORD not in dump
    assert token not in dump


def test_two_users_with_one_password_get_different_hashes(
    client: TestClient, db_path: Path
) -> None:
    register(client, email="ana@example.com")
    register(client, email="bob@example.com")
    with sqlite3.connect(db_path) as conn:
        hashes = [row[0] for row in conn.execute("SELECT password_hash FROM users")]
    assert len(hashes) == len(set(hashes)) == 2


def test_data_survives_a_new_app_on_the_same_file(db_path: Path, clock: FakeClock) -> None:
    register(TestClient(create_app(make_config(), db_path, clock), base_url="https://testserver"))
    restarted = TestClient(create_app(make_config(), db_path, clock), base_url="https://testserver")
    login(restarted)
    assert restarted.get("/auth/me").json()["email"] == EMAIL


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
