"""日报网页归档测试（notifier/archive.py，specs/archive.md）。

用假 GitHub 路由替身 mock `archive._request`，覆盖：首日新建、已有文件
带 sha 更新、索引与 manifest 维护、同日节流、失败冷却、缺分支/Pages
自动补建、禁用 no-op、publish 从 DailyReport 提取标题与进度。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from notifier import archive
from shared.config import Config
from shared.models import DailyReport, MemberReport, TaskRecord

_REPO = "org/repo.github.io"
_DAY = "2026-10-20"

_T0800 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json


class FakeGitHub:
    """按 URL 模式路由的 GitHub API 替身，记录全部调用。"""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.branches = {"main"}
        self.pages_enabled = False
        self.files: dict[str, str] = {}  # path -> sha
        self.default_branch = "main"

    # 路由 ------------------------------------------------------------
    def request(self, method: str, url: str, **kwargs) -> FakeResp:
        self.calls.append((method, url, kwargs.get("json_body")))
        path = url.split("api.github.com", 1)[1]

        if path == f"/repos/{_REPO}" and method == "GET":
            return FakeResp(200, {"default_branch": self.default_branch})
        if path.startswith("/repos/%s/branches/" % _REPO) and method == "GET":
            branch = path.rsplit("/", 1)[1]
            return FakeResp(200 if branch in self.branches else 404)
        if path.startswith("/repos/%s/git/ref/heads/" % _REPO) and method == "GET":
            return FakeResp(200, {"object": {"sha": "head-sha"}})
        if path == f"/repos/{_REPO}/git/refs" and method == "POST":
            branch = (kwargs["json_body"]["ref"]).rsplit("/", 1)[1]
            self.branches.add(branch)
            return FakeResp(201)
        if path == f"/repos/{_REPO}/pages":
            if method == "GET":
                return FakeResp(200 if self.pages_enabled else 404)
            self.pages_enabled = True
            return FakeResp(201)
        if path.startswith(f"/repos/{_REPO}/contents/"):
            fname = path.rsplit("/contents/", 1)[1]
            if method == "GET":
                return FakeResp(
                    200, {"sha": self.files[fname]}
                ) if fname in self.files else FakeResp(404)
            body = kwargs["json_body"]
            if "sha" in body and self.files.get(fname) != body["sha"]:
                return FakeResp(422)
            self.files[fname] = f"sha-{len(self.files)}"
            return FakeResp(201)
        return FakeResp(404)

    def put_contents(self, path: str) -> str:
        """取某文件最后一次 PUT 的解码内容（测试断言用）。"""
        for method, url, body in reversed(self.calls):
            if method == "PUT" and url.rstrip("/").endswith(f"/contents/{path}"):
                return base64.b64decode(body["content"]).decode("utf-8")
        raise AssertionError(f"未找到对 {path} 的 PUT 调用")

    def put_body(self, path: str) -> dict:
        for method, url, body in reversed(self.calls):
            if method == "PUT" and url.rstrip("/").endswith(f"/contents/{path}"):
                return body
        raise AssertionError(f"未找到对 {path} 的 PUT 调用")


def _config(tmp_path, *, enabled: bool = True) -> Config:
    return Config(
        {
            "team": {"name": "研发一组"},
            "storage": {"sqlite_path": str(tmp_path / "reports.db")},
            "archive": {
                "enabled": enabled,
                "repo": _REPO,
                "branch": "gh-pages",
                "token_env": "TEST_GITHUB_TOKEN",
                "timeout": 5,
            },
        }
    )


@pytest.fixture()
def fake(monkeypatch) -> FakeGitHub:
    gh = FakeGitHub()
    monkeypatch.setattr(archive, "_request", gh.request)
    return gh


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def test_first_day_creates_report_and_index(tmp_path, fake):
    assert archive.publish_report(
        _config(tmp_path), _DAY, "刘政剑的智能日报", "<html>日报</html>",
        done=2, total=3,
    ) is True

    assert f"{_DAY}.html" in fake.files
    assert "index.html" in fake.files
    assert fake.put_contents(f"{_DAY}.html") == "<html>日报</html>"
    index = fake.put_contents("index.html")
    assert f'href="{_DAY}.html"' in index
    assert "刘政剑的智能日报" in index
    assert "2/3" in index

    manifest = json.loads(
        (tmp_path / "archive" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest[_DAY] == {"title": "刘政剑的智能日报", "done": 2, "total": 3}


def test_existing_file_updated_with_sha(tmp_path, fake):
    fake.files[f"{_DAY}.html"] = "old-sha"
    assert archive.publish_report(
        _config(tmp_path), _DAY, "标题", "<html>v2</html>"
    ) is True
    body = fake.put_body(f"{_DAY}.html")
    assert body["sha"] == "old-sha"
    assert body["branch"] == "gh-pages"


def test_index_lists_multiple_days_desc(tmp_path, fake):
    cfg = _config(tmp_path)
    assert archive.publish_report(cfg, "2026-10-18", "日报A", "<p>a</p>")
    assert archive.publish_report(cfg, "2026-10-19", "日报B", "<p>b</p>")
    index = fake.put_contents("index.html")
    assert index.index("2026-10-19") < index.index("2026-10-18")


def test_missing_branch_and_pages_are_created(tmp_path, fake):
    assert archive.publish_report(
        _config(tmp_path), _DAY, "标题", "<p>x</p>"
    ) is True
    assert "gh-pages" in fake.branches
    assert fake.pages_enabled
    assert any(
        m == "POST" and u.endswith("/git/refs") for m, u, _ in fake.calls
    )


# ---------------------------------------------------------------------------
# 节流与降级
# ---------------------------------------------------------------------------

def test_same_day_throttled(tmp_path, fake):
    cfg = _config(tmp_path)
    assert archive.publish_report(cfg, _DAY, "标题", "<p>1</p>") is True
    fake.calls.clear()
    assert archive.publish_report(cfg, _DAY, "标题", "<p>2</p>") is False
    assert fake.calls == []  # 节流跳过，未发任何请求


def test_failure_cooldown(tmp_path, fake, monkeypatch):
    def broken(method, url, **kwargs):
        raise archive.NotifierError("网络不可达", source="archive")

    monkeypatch.setattr(archive, "_request", broken)
    assert archive.publish_report(_config(tmp_path), _DAY, "标题", "<p>x</p>") is False

    # 冷却期内恢复网络也不重试
    monkeypatch.setattr(archive, "_request", fake.request)
    fake.calls.clear()
    assert archive.publish_report(_config(tmp_path), _DAY, "标题", "<p>x</p>") is False
    assert fake.calls == []


def test_disabled_noop(tmp_path, fake):
    assert archive.publish(_config(tmp_path, enabled=False), _report()) is False
    assert fake.calls == []


# ---------------------------------------------------------------------------
# publish 入口（DailyReport 提取）
# ---------------------------------------------------------------------------

def _report() -> DailyReport:
    subtasks = [
        TaskRecord(assignee="", title="写周报", status_from="", status_to="已完成",
                   updated_at=_T0800),
        TaskRecord(assignee="", title="联调接口", status_from="", status_to="已完成",
                   updated_at=_T0800),
        TaskRecord(assignee="", title="补测试", status_from="", status_to="进行中",
                   updated_at=_T0800),
    ]
    task_list = TaskRecord(
        assignee="", title="10月20日任务清单", status_from="", status_to="",
        updated_at=_T0800, subtasks=subtasks,
    )
    return DailyReport(
        date=datetime(2026, 10, 20).date(),
        team_name="研发一组",
        members=[MemberReport(name="刘政剑", github_username="liu")],
        html="<html>日报</html>",
        task_list=task_list,
    )


def test_publish_extracts_title_and_progress(tmp_path, fake):
    assert archive.publish(_config(tmp_path), _report()) is True
    assert fake.put_contents(f"{_DAY}.html") == "<html>日报</html>"
    index = fake.put_contents("index.html")
    assert "刘政剑的智能日报" in index
    assert "2/3" in index
