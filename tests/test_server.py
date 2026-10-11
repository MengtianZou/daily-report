"""webapp/server.py 只读 HTTP 服务测试（design-v2 §7）。"""

from __future__ import annotations

import threading

import httpx
import pytest

from dashboard_helpers import sample_report
from shared.storage import ReportStorage
from webapp.server import build_server


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


def test_server_is_bound_to_loopback(running_server) -> None:
    _, server = running_server
    assert server.server_address[0] == "127.0.0.1"


def test_index_serves_dynamic_dashboard(running_server) -> None:
    base, _ = running_server
    resp = httpx.get(base + "/", timeout=10)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "var REPORTS = null" in resp.text
    assert "var AUTO_REFRESH_INTERVAL = 60" in resp.text
    assert 'fetch("api/reports")' in resp.text


def test_api_reports_returns_real_data(running_server) -> None:
    base, _ = running_server
    resp = httpx.get(base + "/api/reports", timeout=10)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0
    assert len(payload["reports"]) == 1
    assert payload["reports"][0]["date"] == "2026-09-26"


def test_api_reports_date_filter(running_server) -> None:
    base, _ = running_server

    hit = httpx.get(base + "/api/reports", params={"date": "2026-09-26"}, timeout=10)
    assert len(hit.json()["reports"]) == 1

    miss = httpx.get(base + "/api/reports", params={"date": "2026-01-01"}, timeout=10)
    assert miss.json()["reports"] == []


def test_unknown_path_returns_404(running_server) -> None:
    base, _ = running_server
    resp = httpx.get(base + "/nope", timeout=10)

    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_non_get_methods_are_rejected(running_server) -> None:
    base, _ = running_server
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(httpx, method)(base + "/api/reports", timeout=10)
        assert resp.status_code == 405


def test_post_plan_persists_for_tomorrow(running_server) -> None:
    base, server = running_server
    resp = httpx.post(
        base + "/api/plans",
        json={"title": "整理周报", "owner": "刘政剑"},
        timeout=10,
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0

    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        plans = storage.iter_plans(payload["plan_date"])
    assert len(plans) == 1
    assert plans[0]["title"] == "整理周报"
    assert plans[0]["owner"] == "刘政剑"


def test_post_plan_requires_title(running_server) -> None:
    base, _ = running_server
    resp = httpx.post(base + "/api/plans", json={"title": "  "}, timeout=10)
    assert resp.status_code == 400


def test_delete_plan_removes_entry(running_server) -> None:
    base, _ = running_server
    created = httpx.post(
        base + "/api/plans", json={"title": "临时计划", "owner": ""}, timeout=10
    ).json()
    plan_id = created["id"]

    resp = httpx.delete(base + f"/api/plans?id={plan_id}", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    # 删除后再次删除应 404
    resp2 = httpx.delete(base + f"/api/plans?id={plan_id}", timeout=10)
    assert resp2.status_code == 404


def test_delete_plan_rejects_invalid_id(running_server) -> None:
    base, _ = running_server
    resp = httpx.delete(base + "/api/plans?id=abc", timeout=10)
    assert resp.status_code == 400


# ---- /api/issues ----

def test_post_issue_persists_for_today(running_server) -> None:
    base, server = running_server
    resp = httpx.post(
        base + "/api/issues",
        json={"problem": "接口报错", "solution": "已定位", "owner": "刘政剑"},
        timeout=10,
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0

    today = payload["issue_date"]
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_issues(today)
    assert len(rows) == 1
    assert rows[0]["problem"] == "接口报错"
    assert rows[0]["solution"] == "已定位"
    assert rows[0]["owner"] == "刘政剑"
    assert rows[0]["status"] == "处理中"


def test_post_issue_requires_problem(running_server) -> None:
    base, _ = running_server
    resp = httpx.post(base + "/api/issues", json={"problem": "  "}, timeout=10)
    assert resp.status_code == 400


def test_patch_issue_toggles_status(running_server) -> None:
    base, _ = running_server
    created = httpx.post(
        base + "/api/issues", json={"problem": "临时问题"}, timeout=10
    ).json()
    issue_id = created["id"]

    resp = httpx.patch(
        base + f"/api/issues?id={issue_id}", json={"status": "已解决"}, timeout=10
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "已解决"

    # 非法状态 → 400
    resp2 = httpx.patch(
        base + f"/api/issues?id={issue_id}", json={"status": "随便"}, timeout=10
    )
    assert resp2.status_code == 400

    # 不存在的 id → 404
    resp3 = httpx.patch(
        base + "/api/issues?id=99999", json={"status": "已解决"}, timeout=10
    )
    assert resp3.status_code == 404


def test_delete_issue_removes_entry(running_server) -> None:
    base, _ = running_server
    created = httpx.post(
        base + "/api/issues", json={"problem": "待删除问题"}, timeout=10
    ).json()

    resp = httpx.delete(base + f"/api/issues?id={created['id']}", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    # 删除后再次删除应 404
    resp2 = httpx.delete(base + f"/api/issues?id={created['id']}", timeout=10)
    assert resp2.status_code == 404


def test_put_rejected_on_issues(running_server) -> None:
    base, _ = running_server
    resp = httpx.put(base + "/api/issues", json={}, timeout=10)
    assert resp.status_code == 405


def test_patch_rejected_on_reports_and_plans(running_server) -> None:
    base, _ = running_server
    assert httpx.patch(base + "/api/reports", json={}, timeout=10).status_code == 405
    assert httpx.patch(base + "/api/plans", json={}, timeout=10).status_code == 405


def test_write_ops_refresh_data_json(running_server) -> None:
    """写操作后 data_json 立即更新，页面刷新（重新 GET）即可见 —— F5 丢失缺陷回归。"""
    base, server = running_server

    # 准备一份「今天」的日报（默认样例日期是 2026-09-26，非当天不会同步）
    from datetime import datetime

    from dashboard_helpers import sample_report

    today = datetime.now().astimezone().date().isoformat()
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        storage.save_report(sample_report(today))

    # POST 后立即 GET：issues 中已含手动条目
    created = httpx.post(
        base + "/api/issues", json={"problem": "刷新可见问题", "owner": "刘政剑"},
        timeout=10,
    ).json()
    reports = httpx.get(base + "/api/reports", params={"date": today}, timeout=10).json()["reports"]
    problems = [i["problem"] for i in reports[0]["issues"] if i["source"] == "manual"]
    assert "刷新可见问题" in problems

    # PATCH 状态后同样立即可见
    httpx.patch(
        base + f"/api/issues?id={created['id']}", json={"status": "已解决"}, timeout=10
    )
    reports = httpx.get(base + "/api/reports", params={"date": today}, timeout=10).json()["reports"]
    target = [i for i in reports[0]["issues"] if i.get("id") == created["id"]]
    assert target and target[0]["status"] == "已解决"

    # DELETE 后立即 GET：条目已移除
    httpx.delete(base + f"/api/issues?id={created['id']}", timeout=10)
    reports = httpx.get(base + "/api/reports", params={"date": today}, timeout=10).json()["reports"]
    problems = [i["problem"] for i in reports[0]["issues"] if i["source"] == "manual"]
    assert "刷新可见问题" not in problems


def test_write_plan_refreshes_data_json(running_server) -> None:
    """手动明日计划写入后，data_json 的 tomorrow_plans 同步更新。"""
    base, server = running_server

    from datetime import datetime, timedelta

    from dashboard_helpers import sample_report

    now = datetime.now().astimezone()
    today = now.date().isoformat()
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        storage.save_report(sample_report(today))

    httpx.post(
        base + "/api/plans", json={"title": "刷新可见计划", "owner": "刘政剑"},
        timeout=10,
    )
    reports = httpx.get(base + "/api/reports", params={"date": today}, timeout=10).json()["reports"]
    titles = [p["title"] for p in reports[0]["tomorrow_plans"] if p["source"] == "manual"]
    assert "刷新可见计划" in titles


def test_claim_issue_replaces_auto_entry(running_server) -> None:
    """认领补充方案：POST 带 msg_key/status 入库；data_json 中匹配的 auto 条目被剔除。"""
    base, server = running_server

    from datetime import datetime

    from dashboard_helpers import sample_report

    today = datetime.now().astimezone().date().isoformat()
    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        storage.save_report(sample_report(today))

    # 与 dashboard_helpers 中样例 auto 条目的 msg_key 一致
    key = "ou_x|2026-09-26T10:00:00+00:00"
    created = httpx.post(
        base + "/api/issues",
        json={"problem": "GitHub 限流导致采集失败", "solution": "等待限流重置",
              "owner": "刘政剑", "status": "处理中", "msg_key": key},
        timeout=10,
    ).json()
    assert created["code"] == 0

    with ReportStorage(server.config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_issues(today)
    saved = [r for r in rows if r["msg_key"] == key]
    assert saved and saved[0]["status"] == "处理中"

    reports = httpx.get(base + "/api/reports", params={"date": today}, timeout=10).json()["reports"]
    issues = reports[0]["issues"]
    assert not any(i["source"] == "auto" and i["msg_key"] == key for i in issues)
    assert any(i["source"] == "manual" and i["msg_key"] == key for i in issues)


def test_post_issue_rejects_invalid_status(running_server) -> None:
    base, _ = running_server
    resp = httpx.post(
        base + "/api/issues", json={"problem": "x", "status": "完成"}, timeout=10,
    )
    assert resp.status_code == 400
