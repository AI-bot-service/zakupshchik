from fastapi.testclient import TestClient
from pydantic import PostgresDsn, SecretStr

from app.core.provenance import BUILD_HEADER, BUILD_MARK
from app.core.settings import Settings
from app.main import create_app

UNREACHABLE_DB = PostgresDsn("postgresql+psycopg://u:p@127.0.0.1:1/none")


def make_client(password: str = "correct-horse") -> TestClient:
    settings = Settings(
        app_secret_key=SecretStr("x" * 32),
        database_url=UNREACHABLE_DB,
        debug=True,
        jury_login="jury",
        jury_password=SecretStr(password),
    )
    return TestClient(create_app(settings))


def test_healthz_and_build_header() -> None:
    r = make_client().get("/healthz")
    assert r.status_code == 200
    assert r.headers[BUILD_HEADER] == BUILD_MARK


def test_status_reports_db_down_without_leaking() -> None:
    r = make_client().get("/api/status")
    body = r.json()
    assert body["build"] == BUILD_MARK
    db = next(c for c in body["components"] if c["name"] == "База данных")
    assert db["state"] == "down"
    assert "127.0.0.1" not in r.text


def test_home_requires_login() -> None:
    r = make_client().get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_ok_then_home() -> None:
    client = make_client()
    r = client.post("/login", data={"login": "jury", "password": "correct-horse"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/").status_code == 200


def test_wrong_password_rejected() -> None:
    r = make_client().post("/login", data={"login": "jury", "password": "nope"})
    assert r.status_code == 401


def test_empty_configured_password_closes_login() -> None:
    r = make_client(password="").post("/login", data={"login": "jury", "password": ""})
    assert r.status_code in (401, 422)
