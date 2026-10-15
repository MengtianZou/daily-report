"""webapp/auth.py 单元测试 + 看板登录/注册 HTTP 流程测试。"""

from __future__ import annotations

import threading

import httpx
import pytest

from dashboard_helpers import sample_report
from shared.storage import ReportStorage
from webapp.auth import hash_password, new_session_token, verify_password
from webapp.server import build_server


# ---- auth.py 单元测试 ----

def test_hash_and_verify_roundtrip() -> None:
    stored = hash_password("secret123")
    assert stored != "secret123"
    assert "$" in stored
    assert verify_password("secret123", stored) is True
    assert verify_password("wrong", stored) is False


def test_same_password_produces_different_salts() -> None:
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_verify_malformed_stored_hash_returns_false() -> None:
    assert verify_password("x", "not-a-valid-hash") is False


def test_new_session_tokens_are_unique() -> None:
    assert new_session_token() != new_session_token()


# ---- storage 用户表 ----

def test_create_user_and_duplicate_returns_none(tmp_path) -> None:
    with ReportStorage(tmp_path / "reports.db") as storage:
        user_id = storage.create_user("刘政剑", "salt$hash")
        assert user_id is not None
        assert storage.create_user("刘政剑", "salt$hash2") is None


def test_get_user_by_username(tmp_path) -> None:
    with ReportStorage(tmp_path / "reports.db") as storage:
        storage.create_user("刘政剑", "salt$hash")
        user = storage.get_user_by_username("刘政剑")
        assert user is not None
        assert user["password_hash"] == "salt$hash"
        assert storage.get_user_by_username("不存在") is None


# ---- HTTP 登录/注册流程 ----

@pytest.fixture
def running_server(tmp_path):
    db_path = tmp_path / "reports.db"
    with ReportStorage(db_path) as storage:
        storage.save_report(sample_report())

    config = {"storage": {"sqlite_path": str(db_path)}}
    server = build_server(config, host="127.0.0.1", port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://{host}:{port}", server

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_login_page_served_without_auth(running_server) -> None:
    base, _ = running_server
    for path in ("/login", "/register"):
        resp = httpx.get(base + path, timeout=10)
        assert resp.status_code == 200
        assert "智能日报看板" in resp.text


def test_register_creates_user_and_auto_login(running_server) -> None:
    base, _ = running_server
    with httpx.Client(base_url=base, timeout=10) as client:
        resp = client.post(
            "/api/auth/register",
            json={"username": "刘政剑", "password": "secret123"},
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "刘政剑"
        assert "session=" in resp.headers.get("set-cookie", "")

        # 注册即登录：带 cookie 可访问看板与 API
        assert client.get("/").status_code == 200
        assert client.get("/api/reports").status_code == 200

        # 已登录访问 /login → 回到看板
        assert client.get("/login").status_code == 302


def test_register_rejects_duplicate_username(running_server) -> None:
    base, _ = running_server
    payload = {"username": "刘政剑", "password": "secret123"}
    assert httpx.post(base + "/api/auth/register", json=payload, timeout=10).status_code == 200
    resp = httpx.post(base + "/api/auth/register", json=payload, timeout=10)
    assert resp.status_code == 400
    assert "已被注册" in resp.json()["msg"]


def test_register_rejects_short_password(running_server) -> None:
    base, _ = running_server
    resp = httpx.post(
        base + "/api/auth/register",
        json={"username": "abc", "password": "123"},
        timeout=10,
    )
    assert resp.status_code == 400


def test_register_rejects_blank_username(running_server) -> None:
    base, _ = running_server
    resp = httpx.post(
        base + "/api/auth/register",
        json={"username": "   ", "password": "secret123"},
        timeout=10,
    )
    assert resp.status_code == 400


def test_login_success_and_wrong_password(running_server) -> None:
    base, _ = running_server
    payload = {"username": "刘政剑", "password": "secret123"}
    httpx.post(base + "/api/auth/register", json=payload, timeout=10)

    with httpx.Client(base_url=base, timeout=10) as client:
        ok = client.post("/api/auth/login", json=payload)
        assert ok.status_code == 200
        assert client.get("/api/reports").status_code == 200

    with httpx.Client(base_url=base, timeout=10) as client:
        bad = client.post(
            "/api/auth/login", json={"username": "刘政剑", "password": "wrong!"}
        )
        assert bad.status_code == 401
        assert client.get("/api/reports").status_code == 401

    with httpx.Client(base_url=base, timeout=10) as client:
        ghost = client.post(
            "/api/auth/login", json={"username": "不存在", "password": "secret123"}
        )
        assert ghost.status_code == 401


def test_logout_clears_session(running_server) -> None:
    base, _ = running_server
    with httpx.Client(base_url=base, timeout=10) as client:
        client.post(
            "/api/auth/register",
            json={"username": "刘政剑", "password": "secret123"},
        )
        assert client.get("/api/reports").status_code == 200

        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert client.get("/api/reports").status_code == 401


def test_write_ops_blocked_without_login(running_server) -> None:
    base, _ = running_server
    assert httpx.post(
        base + "/api/plans", json={"title": "x"}, timeout=10
    ).status_code == 401
    assert httpx.delete(base + "/api/plans?id=1", timeout=10).status_code == 401
    assert httpx.patch(
        base + "/api/issues?id=1", json={"status": "已解决"}, timeout=10
    ).status_code == 401
