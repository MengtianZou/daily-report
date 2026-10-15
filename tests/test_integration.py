"""端到端集成测试：从 CLI 入口跑完整管道。

覆盖四种场景（tasks.md Task 10）：
1. 正常：所有数据源可用，日报生成并推送；
2. 降级：单个数据源失败，其余正常，日报标注"数据获取失败"；
3. 空数据：数据源均成功但当天无记录，显示"今日无记录"；
4. 全部失败：不生成日报，退出码非 0。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

import main
from shared.errors import CollectorError
from shared.models import CommitRecord, MessageRecord
from shared.storage import ReportStorage

_CONFIG = """
team:
  name: "集成测试团队"
members:
  - name: "张三"
    github: "zhangsan"
    lark: "ou_zhangsan"
github:
  enabled: true
  base_url: "https://api.github.com"
  token_env: "GITHUB_TOKEN"
  repos: ["org/repo"]
  per_page: 30
  timeout: 5
lark_task:
  enabled: true
  app_id_env: "LARK_APP_ID"
  app_secret_env: "LARK_APP_SECRET"
  project_id: "proj_x"
  timeout: 5
lark_message:
  enabled: true
  app_id_env: "LARK_APP_ID"
  app_secret_env: "LARK_APP_SECRET"
  chat_id: "oc_x"
  keywords: ["上线"]
  sensitive_words: ["薪资"]
  chat_name: "研发群"
  timeout: 5
email:
  enabled: true
  smtp_host: "smtp.company.com"
  smtp_port: 465
  use_ssl: true
  username_env: "SMTP_USERNAME"
  password_env: "SMTP_PASSWORD"
  sender: "report@company.com"
  recipients: ["leader@company.com"]
lark_bot:
  enabled: true
  webhook: "https://open.feishu.cn/hook/x"
  chat_id: "oc_x"
  timeout: 5
storage:
  sqlite_path: "{db_path}"
schedule:
  holidays: []
  workdays: []
"""

_RECORDS = {
    "commits": [
        CommitRecord(
            author="zhangsan",
            message="feat: 支持日报推送",
            timestamp=datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc),
            repo="org/repo",
            additions=42,
            deletions=3,
            files_changed=2,
        )
    ],
    "messages": [
        MessageRecord(
            sender="ou_zhangsan",
            content="功能已上线",
            timestamp=datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc),
            chat_name="研发群",
        )
    ],
}


@pytest.fixture()
def config_path(tmp_path):
    db_path = str(tmp_path / "reports.db").replace("\\", "/")
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG.format(db_path=db_path), encoding="utf-8")
    return path


def _patch(monkeypatch, *, mode: str, pushed: list[str]) -> None:
    """mode: normal / degraded / empty / all_fail。"""

    def make_failing(source: str):
        def fake(*args, **kwargs):
            raise CollectorError(f"{source} down", source=source)

        return fake

    def make_returning(records: list):
        def fake(*args, **kwargs):
            return list(records)

        return fake

    if mode == "all_fail":
        monkeypatch.setattr(main.github, "collect", make_failing("github"))
        monkeypatch.setattr(
            main.lark_task, "fetch_daily_tasklist", make_failing("lark_task")
        )
        monkeypatch.setattr(main.lark_msg, "collect", make_failing("lark_message"))
    elif mode == "empty":
        monkeypatch.setattr(main.github, "collect", make_returning([]))
        monkeypatch.setattr(main.lark_task, "fetch_daily_tasklist",
                            lambda *a, **k: None)
        monkeypatch.setattr(main.lark_msg, "collect", make_returning([]))
    elif mode == "degraded":
        monkeypatch.setattr(main.github, "collect", make_failing("github"))
        monkeypatch.setattr(main.lark_task, "fetch_daily_tasklist",
                            lambda *a, **k: None)
        monkeypatch.setattr(main.lark_msg, "collect", make_returning(_RECORDS["messages"]))
    else:  # normal
        monkeypatch.setattr(main.github, "collect", make_returning(_RECORDS["commits"]))
        monkeypatch.setattr(main.lark_task, "fetch_daily_tasklist",
                            lambda *a, **k: None)
        monkeypatch.setattr(main.lark_msg, "collect", make_returning(_RECORDS["messages"]))

    monkeypatch.setattr(
        main.email, "send", lambda report, recipients, **kw: pushed.append("email") or True
    )
    monkeypatch.setattr(
        main.lark_bot, "send", lambda report, chat_id, **kw: pushed.append("lark") or True
    )


def _read_report(config_path) -> dict | None:
    from shared.config import load_config

    config = load_config(config_path)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        return storage.get_report("2026-09-28", "集成测试团队")


def test_integration_normal(config_path, monkeypatch) -> None:
    pushed: list[str] = []
    _patch(monkeypatch, mode="normal", pushed=pushed)

    started = time.time()
    exit_code = main.main(["--config", str(config_path), "--run", "--date", "2026-09-28"])
    elapsed = time.time() - started

    assert exit_code == 0
    assert pushed == ["email", "lark"]  # 两个渠道均推送成功

    row = _read_report(config_path)
    assert row is not None
    assert "支持日报推送" in row["markdown"]
    assert "功能已上线" in row["markdown"]
    assert "<html" in row["html"]

    assert elapsed < 60  # 性能验收：Mock 环境下 < 60s


def test_integration_degraded_single_source_failure(config_path, monkeypatch) -> None:
    pushed: list[str] = []
    _patch(monkeypatch, mode="degraded", pushed=pushed)

    exit_code = main.main(["--config", str(config_path), "--run", "--date", "2026-09-28"])

    assert exit_code == 0
    assert pushed == ["email", "lark"]
    row = _read_report(config_path)
    assert row is not None
    assert "数据获取失败" in row["markdown"]


def test_integration_empty_day(config_path, monkeypatch) -> None:
    pushed: list[str] = []
    _patch(monkeypatch, mode="empty", pushed=pushed)

    exit_code = main.main(["--config", str(config_path), "--run", "--date", "2026-09-28"])

    assert exit_code == 0
    row = _read_report(config_path)
    assert row is not None
    assert "今日无记录" in row["markdown"]


def test_integration_all_sources_fail(config_path, monkeypatch) -> None:
    pushed: list[str] = []
    _patch(monkeypatch, mode="all_fail", pushed=pushed)

    exit_code = main.main(["--config", str(config_path), "--run", "--date", "2026-09-28"])

    assert exit_code == 1
    assert pushed == []  # 不推送
    assert _read_report(config_path) is None  # 不落库
