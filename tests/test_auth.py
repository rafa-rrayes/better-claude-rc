"""Local-mode auth: no Claude subprocess is spawned."""

import pytest
from fastapi.testclient import TestClient

from server import main as srv


@pytest.fixture()
def client():
    with TestClient(srv.app) as c:
        yield c


def test_requires_auth(client):
    assert client.get("/api/state").status_code == 401


def test_wrong_token_rejected(client):
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401


def test_token_login_sets_cookie(client):
    r = client.post("/api/login", json={"token": srv.config.token})
    assert r.status_code == 200
    assert client.get("/api/state").status_code == 200


def test_login_code_is_single_use(client):
    code = srv.issue_login_code()
    assert client.post("/api/login", json={"code": code}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/login", json={"code": code}).status_code == 401


def test_env_reports_local_mode(client):
    assert client.get("/api/env").json() == {"mode": "local"}
