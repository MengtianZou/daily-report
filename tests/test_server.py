"""webapp/server.py HTTP 服务测试（design-v2 §7，含登录鉴权与数据隔离）。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import httpx
import pytest

from dashboard_helpers import sample_report
from shared.storage import ReportStorage
from webapp.server import (
    _filter_report_for_user,
    _member_for_user,
    _resolve_port,
    build_server,
)


def test_resolve_port_config_and_default() -> None:
    # 显式传参最优先
    assert _resolve_port({}, 9999) == 9999
    # config.dashboard.port 次之
    assert _resolve_port({"dashboard": {"port": 8123}}, None) == 8123
    # 都没有则默认固定 8000
    assert _resolve_port({}, None) == 8000


@pytest.fixture
def running_server(tmp_path):
    db_path = tmp_path / "reports.db"
    with ReportStorage(db_path) as storage:
        storage.save_report(sample_report())

    config = {
        "storage": {"sqlite_path": str(db_path)},
        "members": [{"name": "刘政剑"}, {"name": "李四"}],
        "dashboard": {"member_accounts": {"刘政剑": "testuser"}},
    }
    server = build_server(config, host="127.0.0.1", port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://{host}:{port}", server

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def authed_client(running_server):
    """注册并自动登录的 httpx 客户端（cookie 会话）。"""

    base, _ = running_server
    with httpx.Client(base_url=base, timeout=10) as client:
        resp = client.post(
            "/api/auth/register",
            json={"username": "testuser", "password": "secret123"},
        )
        assert resp.status_code == 200, resp.text
        yield client


def test_server_is_bound_to_loopback(running_server) -> None:
    _, server = running_server
    assert server.server_address[0] == "127.0.0.1"


# ---- 鉴权拦截（未登录） ----

def test_unauthenticated_index_redirects_to_login(running_server) -> None:
    base, _ = running_server
    resp = httpx.get(base + "/", timeout=10)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_unauthenticated_api_returns_401(running_server) -> None:
    base, _ = running_server
    resp = httpx.get(base + "/api/reports", timeout=10)

    assert resp.status_code == 401
    assert resp.json()["code"] == 401


# ---- 登录后的页面与 API ----

def test_index_serves_dynamic_dashboard(authed_client) -> None:
    resp = authed_client.get("/")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "var REPORTS = null" in resp.text
    assert "var AUTO_REFRESH_INTERVAL = 60" in resp.text
    assert 'fetch("api/reports")' in resp.text


def test_api_reports_returns_real_data(authed_client) -> None:
    resp = authed_client.get("/api/reports")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0
    assert len(payload["reports"]) == 1
    assert payload["reports"][0]["date"] == "2026-09-26"


def test_api_reports_date_filter(authed_client) -> None:
    hit = authed_client.get("/api/reports", params={"date": "2026-09-26"})
    assert len(hit.json()["reports"]) == 1

    miss = authed_client.get("/api/reports", params={"date": "2026-01-01"})
    assert miss.json()["reports"] == []


def test_unknown_path_returns_404(authed_client) -> None:
    resp = authed_client.get("/nope")

    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_non_get_methods_are_rejected(authed_client) -> None:
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(authed_client, method)("/api/reports")
        assert resp.status_code == 405


def test_post_plan_persists_for_tomorrow(authed_client, running_server) -> None:
    resp = authed_client.post(
        "/api/plans", json={"title": "整理周报", "owner": "刘政剑"}
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0

    _, server = running_server
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        plans = storage.iter_plans(payload["plan_date"])
    assert len(plans) == 1
    assert plans[0]["title"] == "整理周报"
    assert plans[0]["owner"] == "刘政剑"


def test_post_plan_requires_title(authed_client) -> None:
    resp = authed_client.post("/api/plans", json={"title": "  "})
    assert resp.status_code == 400


def test_delete_plan_removes_entry(authed_client) -> None:
    created = authed_client.post(
        "/api/plans", json={"title": "临时计划", "owner": ""}
    ).json()
    plan_id = created["id"]

    resp = authed_client.delete(f"/api/plans?id={plan_id}")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    # 删除后再次删除应 404
    resp2 = authed_client.delete(f"/api/plans?id={plan_id}")
    assert resp2.status_code == 404


def test_delete_plan_rejects_invalid_id(authed_client) -> None:
    resp = authed_client.delete("/api/plans?id=abc")
    assert resp.status_code == 400


# ---- /api/issues ----

def test_post_issue_persists_for_today(authed_client, running_server) -> None:
    resp = authed_client.post(
        "/api/issues",
        json={"problem": "接口报错", "solution": "已定位", "owner": "刘政剑"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0

    today = payload["issue_date"]
    _, server = running_server
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_issues(today)
    assert len(rows) == 1
    assert rows[0]["problem"] == "接口报错"
    assert rows[0]["solution"] == "已定位"
    assert rows[0]["owner"] == "刘政剑"
    assert rows[0]["status"] == "处理中"


def test_post_issue_requires_problem(authed_client) -> None:
    resp = authed_client.post("/api/issues", json={"problem": "  "})
    assert resp.status_code == 400


def test_patch_issue_toggles_status(authed_client) -> None:
    created = authed_client.post(
        "/api/issues", json={"problem": "临时问题"}
    ).json()
    issue_id = created["id"]

    resp = authed_client.patch(f"/api/issues?id={issue_id}", json={"status": "已解决"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "已解决"

    # 非法状态 → 400
    resp2 = authed_client.patch(f"/api/issues?id={issue_id}", json={"status": "随便"})
    assert resp2.status_code == 400

    # 不存在的 id → 404
    resp3 = authed_client.patch("/api/issues?id=99999", json={"status": "已解决"})
    assert resp3.status_code == 404


def test_delete_issue_removes_entry(authed_client) -> None:
    created = authed_client.post(
        "/api/issues", json={"problem": "待删除问题"}
    ).json()

    resp = authed_client.delete(f"/api/issues?id={created['id']}")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    # 删除后再次删除应 404
    resp2 = authed_client.delete(f"/api/issues?id={created['id']}")
    assert resp2.status_code == 404


def test_put_rejected_on_issues(authed_client) -> None:
    resp = authed_client.put("/api/issues", json={})
    assert resp.status_code == 405


def test_patch_rejected_on_reports_and_plans(authed_client) -> None:
    assert authed_client.patch("/api/reports", json={}).status_code == 405
    assert authed_client.patch("/api/plans", json={}).status_code == 405


def test_write_ops_refresh_data_json(authed_client, running_server) -> None:
    """写操作后 data_json 立即更新，页面刷新（重新 GET）即可见 —— F5 丢失缺陷回归。"""
    _, server = running_server

    # 准备一份「今天」的日报（默认样例日期是 2026-09-26，非当天不会同步）
    from datetime import datetime

    from dashboard_helpers import sample_report

    today = datetime.now().astimezone().date().isoformat()
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        storage.save_report(sample_report(today))

    # POST 后立即 GET：issues 中已含手动条目
    created = authed_client.post(
        "/api/issues", json={"problem": "刷新可见问题", "owner": "刘政剑"}
    ).json()
    reports = authed_client.get("/api/reports", params={"date": today}).json()["reports"]
    problems = [i["problem"] for i in reports[0]["issues"] if i["source"] == "manual"]
    assert "刷新可见问题" in problems

    # PATCH 状态后同样立即可见
    authed_client.patch(
        f"/api/issues?id={created['id']}", json={"status": "已解决"}
    )
    reports = authed_client.get("/api/reports", params={"date": today}).json()["reports"]
    target = [i for i in reports[0]["issues"] if i.get("id") == created["id"]]
    assert target and target[0]["status"] == "已解决"

    # DELETE 后立即 GET：条目已移除
    authed_client.delete(f"/api/issues?id={created['id']}")
    reports = authed_client.get("/api/reports", params={"date": today}).json()["reports"]
    problems = [i["problem"] for i in reports[0]["issues"] if i["source"] == "manual"]
    assert "刷新可见问题" not in problems


def test_write_plan_refreshes_data_json(authed_client, running_server) -> None:
    """手动明日计划写入后，data_json 的 tomorrow_plans 同步更新。"""
    _, server = running_server

    from datetime import datetime, timedelta

    from dashboard_helpers import sample_report

    now = datetime.now().astimezone()
    today = now.date().isoformat()
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        storage.save_report(sample_report(today))

    authed_client.post("/api/plans", json={"title": "刷新可见计划", "owner": "刘政剑"})
    reports = authed_client.get("/api/reports", params={"date": today}).json()["reports"]
    titles = [p["title"] for p in reports[0]["tomorrow_plans"] if p["source"] == "manual"]
    assert "刷新可见计划" in titles


def test_claim_issue_replaces_auto_entry(authed_client, running_server) -> None:
    """认领补充方案：POST 带 msg_key/status 入库；data_json 中匹配的 auto 条目被剔除。"""
    _, server = running_server

    from datetime import datetime

    from dashboard_helpers import sample_report

    today = datetime.now().astimezone().date().isoformat()
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        storage.save_report(sample_report(today))

    # 与 dashboard_helpers 中样例 auto 条目的 msg_key 一致
    key = "ou_x|2026-09-26T10:00:00+00:00"
    created = authed_client.post(
        "/api/issues",
        json={"problem": "GitHub 限流导致采集失败", "solution": "等待限流重置",
              "owner": "刘政剑", "status": "处理中", "msg_key": key},
    ).json()
    assert created["code"] == 0

    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_issues(today)
    saved = [r for r in rows if r["msg_key"] == key]
    assert saved and saved[0]["status"] == "处理中"

    reports = authed_client.get("/api/reports", params={"date": today}).json()["reports"]
    issues = reports[0]["issues"]
    assert not any(i["source"] == "auto" and i["msg_key"] == key for i in issues)
    assert any(i["source"] == "manual" and i["msg_key"] == key for i in issues)


def test_post_issue_rejects_invalid_status(authed_client) -> None:
    resp = authed_client.post("/api/issues", json={"problem": "x", "status": "完成"})
    assert resp.status_code == 400


# ---- 数据隔离（账号 ↔ 成员绑定） ----

def _register_unbound(running_server) -> httpx.Client:
    """注册一个未绑定成员的账号并返回其已登录客户端（调用方负责 close）。"""

    base, _ = running_server
    client = httpx.Client(base_url=base, timeout=10)
    resp = client.post(
        "/api/auth/register", json={"username": "nobody", "password": "secret123"}
    )
    assert resp.status_code == 200, resp.text
    return client


def test_unbound_user_gets_empty_reports(running_server) -> None:
    client = _register_unbound(running_server)
    try:
        resp = client.get("/api/reports")
    finally:
        client.close()

    assert resp.status_code == 200
    assert resp.json()["reports"] == []


def test_unbound_user_cannot_post(running_server) -> None:
    base, server = running_server
    today = datetime.now().astimezone().date().isoformat()
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    client = _register_unbound(running_server)
    try:
        assert client.post(
            "/api/plans", json={"title": "x", "owner": "刘政剑"}
        ).status_code == 403
        assert client.post(
            "/api/issues", json={"problem": "x", "owner": "刘政剑"}
        ).status_code == 403
    finally:
        client.close()

    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        assert list(storage.iter_plans(tomorrow)) == []
        assert list(storage.iter_issues(today)) == []


def test_unbound_user_cannot_delete_or_patch(running_server) -> None:
    base, server = running_server
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        plan_id = storage.save_plan("旧计划", "刘政剑", "2026-09-27")
        issue_id = storage.save_issue("旧问题", "方案", "刘政剑", "2026-09-26")

    client = _register_unbound(running_server)
    try:
        assert client.delete(f"/api/plans?id={plan_id}").status_code == 403
        assert client.delete(f"/api/issues?id={issue_id}").status_code == 403
        assert client.patch(
            f"/api/issues?id={issue_id}", json={"status": "已解决"}
        ).status_code == 403
    finally:
        client.close()

    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        assert list(storage.iter_plans("2026-09-27"))
        assert list(storage.iter_issues("2026-09-26"))


def test_reports_filtered_to_bound_member(authed_client, running_server) -> None:
    _, server = running_server
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        data = storage.iter_report_data()[0]
        data["tomorrow_plans"].append(
            {"title": "李四的计划", "owner": "李四", "source": "manual", "id": 99}
        )
        data["issues"].append(
            {"problem": "李四的问题", "solution": "", "owner": "李四",
             "status": "处理中", "source": "manual", "id": 98, "msg_key": ""}
        )
        storage.update_report_data("2026-09-26", data["team_name"], data)

    reports = authed_client.get("/api/reports").json()["reports"]
    assert len(reports) == 1
    report = reports[0]
    assert [m["name"] for m in report["members"]] == ["刘政剑"]
    assert all(p["owner"] != "李四" for p in report["tomorrow_plans"])
    assert all(i["owner"] != "李四" for i in report["issues"])
    # 团队级（owner 为空）与绑定成员条目保留
    assert any(p["owner"] == "" for p in report["tomorrow_plans"])
    assert any(p["owner"] == "刘政剑" for p in report["tomorrow_plans"])


def test_post_plan_rejects_foreign_owner(authed_client) -> None:
    resp = authed_client.post("/api/plans", json={"title": "x", "owner": "李四"})

    assert resp.status_code == 403
    assert resp.json()["code"] == 403


def test_post_issue_rejects_foreign_owner(authed_client) -> None:
    resp = authed_client.post("/api/issues", json={"problem": "x", "owner": "李四"})

    assert resp.status_code == 403
    assert resp.json()["code"] == 403


# ---- 成员映射 helper 单测 ----

def test_member_for_user_supports_str_and_list() -> None:
    config = {
        "dashboard": {"member_accounts": {"刘政剑": "u1", "李四": ["u2", "u3"]}}
    }
    assert _member_for_user(config, "u1") == "刘政剑"
    assert _member_for_user(config, "u2") == "李四"
    assert _member_for_user(config, "u3") == "李四"
    assert _member_for_user(config, "unknown") is None
    assert _member_for_user(config, "") is None


def test_filter_report_drops_report_without_member() -> None:
    report = {"members": [{"name": "李四"}], "tomorrow_plans": [], "issues": []}

    assert _filter_report_for_user(report, "刘政剑") is None
    assert _filter_report_for_user(report, None) is None
