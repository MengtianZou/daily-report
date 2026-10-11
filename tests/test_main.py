"""main.py 单元测试：工作日识别、健康检查与管道编排。"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import pytest

import main
from shared.config import load_config
from shared.errors import CollectorError, ConfigurationError
from shared.models import CommitRecord, MessageRecord, TaskRecord
from shared.storage import ReportStorage

_CONFIG_YAML_TEMPLATE = """
team:
  name: "测试团队"
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
  issue_keywords: ["问题", "报错"]
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


@pytest.fixture()
def config(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    db_path = str(tmp_path / "reports.db").replace("\\", "/")
    yaml_path.write_text(
        _CONFIG_YAML_TEMPLATE.format(db_path=db_path),
        encoding="utf-8",
    )
    return load_config(yaml_path)


# ---- 工作日识别 ----

def test_is_workday_weekday(config) -> None:
    # 2026-09-28 是周一
    assert main.is_workday(date(2026, 9, 28), config) is True


def test_is_workday_weekend(config) -> None:
    # 2026-09-26 是周六
    assert main.is_workday(date(2026, 9, 26), config) is False


def test_config_workday_override(config, monkeypatch) -> None:
    monkeypatch.setitem(config._data["schedule"], "workdays", ["2026-09-26"])
    assert main.is_workday(date(2026, 9, 26), config) is True


def test_config_holiday_override(config, monkeypatch) -> None:
    monkeypatch.setitem(config._data["schedule"], "holidays", ["2026-09-28"])
    assert main.is_workday(date(2026, 9, 28), config) is False


# ---- 健康检查 ----

def test_run_check_all_pass(config, monkeypatch) -> None:
    monkeypatch.setattr(main, "_check_github", lambda cfg: None)
    monkeypatch.setattr(main, "_check_lark", lambda cfg, section: None)
    monkeypatch.setattr(main, "_check_smtp", lambda cfg: None)
    assert main.run_check(config) is True


def test_run_check_one_failure(config, monkeypatch) -> None:
    def boom(cfg) -> None:
        raise ConfigurationError("nope", source="check")

    monkeypatch.setattr(main, "_check_github", boom)
    monkeypatch.setattr(main, "_check_lark", lambda cfg, section: None)
    monkeypatch.setattr(main, "_check_smtp", lambda cfg: None)
    assert main.run_check(config) is False


# ---- 日报页面查看 ----

def test_view_report_opens_html_file(config) -> None:
    from generator.formatter import generate

    report = generate([], date(2026, 9, 28), "测试团队")
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_report(report)

    opened: list[str] = []
    assert main.view_report(config, "2026-09-28", opener=opened.append) is True

    assert len(opened) == 1
    assert opened[0].startswith("file:") and opened[0].endswith(".html")

    from pathlib import Path

    html_file = Path(config["storage"]["sqlite_path"]).parent / "report-2026-09-28.html"
    assert html_file.exists()
    assert "工作日报" in html_file.read_text(encoding="utf-8")


def test_view_report_missing_date_returns_false(config) -> None:
    def must_not_open(uri: str) -> None:
        raise AssertionError("无日报时不应打开页面")

    assert main.view_report(config, "2026-09-26", opener=must_not_open) is False


# ---- 默认动作：启动看板 ----

def test_default_invocation_starts_dashboard(config, monkeypatch) -> None:
    called: list[str] = []

    import webapp.server

    monkeypatch.setattr(
        webapp.server, "serve", lambda cfg: called.append("serve")
    )

    assert main.main(["--config", config.source_path]) == 0
    assert called == ["serve"]


def test_explicit_serve_starts_dashboard(config, monkeypatch) -> None:
    called: list[str] = []

    import webapp.server

    monkeypatch.setattr(
        webapp.server, "serve", lambda cfg: called.append("serve")
    )

    assert main.main(["--config", config.source_path, "--serve"]) == 0
    assert called == ["serve"]


# ---- 管道 ----
_MONDAY = datetime(2026, 9, 28, 17, 0, tzinfo=timezone(timedelta(hours=8)))


def _patch_collectors(monkeypatch, *, fail: set[str] | None = None) -> dict:
    fail = fail or set()
    counts = {"commits": 0, "tasks": 0, "messages": 0}

    def fake_github(repos, since, until, **kwargs):
        if "github" in fail:
            raise CollectorError("github down", source="github")
        counts["commits"] += 1
        return [
            CommitRecord(
                author="zhangsan",
                message="feat",
                timestamp=datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc),
                repo="org/repo",
                additions=1,
                deletions=0,
                files_changed=1,
            )
        ]

    def fake_lark_task(project_id, since, until, **kwargs):
        if "lark_task" in fail:
            raise CollectorError("task down", source="lark_task")
        counts["tasks"] += 1
        return [
            TaskRecord(
                assignee="ou_zhangsan",
                title="完成开发任务",
                status_from="进行中",
                status_to="已完成",
                updated_at=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc),
            )
        ]

    def fake_lark_msg(chat_id, keywords, since, until, **kwargs):
        if "lark_message" in fail:
            raise CollectorError("msg down", source="lark_message")
        counts["messages"] += 1
        return []

    def fake_lark_open(project_id, **kwargs):
        if "lark_task" in fail:
            raise CollectorError("task down", source="lark_task")
        return [
            TaskRecord(
                assignee="ou_zhangsan",
                title="准备明日评审",
                status_from="",
                status_to="进行中",
                updated_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr(main.github, "collect", fake_github)
    monkeypatch.setattr(main.lark_task, "collect", fake_lark_task)
    monkeypatch.setattr(main.lark_msg, "collect", fake_lark_msg)
    monkeypatch.setattr(main.lark_task, "collect_open", fake_lark_open)
    return counts


def test_pipeline_non_workday_is_skipped(config) -> None:
    saturday = datetime(2026, 9, 26, 17, 0, tzinfo=timezone(timedelta(hours=8)))
    assert main.run_pipeline(config, now=saturday) is True


def test_pipeline_full_run_pushes_and_persists(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    pushed: list[str] = []
    monkeypatch.setattr(
        main.email, "send", lambda report, recipients, **kw: pushed.append("email") or True
    )
    monkeypatch.setattr(
        main.lark_bot, "send", lambda report, chat_id, **kw: pushed.append("lark") or True
    )

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True
    assert pushed == ["email", "lark"]

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")
    assert row is not None
    assert "### 代码提交" in row["markdown"]


def test_pipeline_dry_run_does_not_push(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)

    def should_not_call(*args, **kwargs):
        raise AssertionError("push must not happen in dry-run")

    monkeypatch.setattr(main.email, "send", should_not_call)
    monkeypatch.setattr(main.lark_bot, "send", should_not_call)

    assert main.run_pipeline(config, now=_MONDAY, dry_run=True) is True


def test_pipeline_single_source_failure_marks_report(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch, fail={"github"})
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")
    assert row is not None
    assert "数据获取失败" in row["markdown"]


def test_pipeline_all_sources_fail_returns_false(config, monkeypatch) -> None:
    _patch_collectors(
        monkeypatch, fail={"github", "lark_task", "lark_message"}
    )

    def should_not_call(*args, **kwargs):
        raise AssertionError("no push when all sources fail")

    monkeypatch.setattr(main.email, "send", should_not_call)
    monkeypatch.setattr(main.lark_bot, "send", should_not_call)

    assert main.run_pipeline(config, now=_MONDAY) is False


def test_pipeline_merges_auto_and_manual_tomorrow_plans(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    # 手动计划：plan_date = 日报日期 + 1（2026-09-29）
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("整理周报", "张三", "2026-09-29")

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")

    assert row is not None
    assert "## 明日计划" in row["markdown"]
    assert "📌 张三：准备明日评审" in row["markdown"]      # 自动：飞书未完成
    assert "📌 张三：整理周报" in row["markdown"]  # 手动：plans 表
    assert '"tomorrow_plans"' in row["data_json"]


def test_tomorrow_plans_exclude_non_member_tasks(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)

    def fake_lark_open_with_outsider(project_id, **kwargs):
        return [
            TaskRecord(
                assignee="ou_zhangsan",
                title="成员内任务",
                status_from="",
                status_to="进行中",
                updated_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            ),
            TaskRecord(
                assignee="ou_outsider",
                title="非成员任务",
                status_from="",
                status_to="进行中",
                updated_at=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            ),
        ]

    monkeypatch.setattr(main.lark_task, "collect_open", fake_lark_open_with_outsider)

    plans = main._collect_tomorrow_plans(config, date(2026, 9, 28))

    titles = [p.title for p in plans if p.source == "auto"]
    assert titles == ["成员内任务"]


# ---- 问题与解决 ----
_ISSUE_T0 = datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc)
_ISSUE_T1 = _ISSUE_T0 + timedelta(hours=1)


def _patch_msg_by_keywords(monkeypatch, contents) -> None:
    """按 keywords 参数区分返回内容，模拟采集层的关键词过滤行为。

    两次调用返回不同时间戳的消息（真实场景中为不同消息）。
    """

    def fake_lark_msg(chat_id, keywords, since, until, **kwargs):
        if "问题" in keywords or "报错" in keywords:  # 问题板块的二次采集
            return [
                MessageRecord(
                    sender="ou_zhangsan", content=c,
                    timestamp=_ISSUE_T1, chat_name="研发群",
                )
                for c in contents
            ]
        return [
            MessageRecord(
                sender="ou_zhangsan", content="功能已上线",
                timestamp=_ISSUE_T0, chat_name="研发群",
            )
        ]

    monkeypatch.setattr(main.lark_msg, "collect", fake_lark_msg)


def test_pipeline_collects_issues_from_lark_messages(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    _patch_msg_by_keywords(monkeypatch, ["遇到问题：接口报错"])
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")

    assert row is not None
    assert "## 问题与解决" in row["markdown"]
    assert "- ⏳ 遇到问题：接口报错 → 方案待定" in row["markdown"]
    assert '"issues"' in row["data_json"]


def test_issues_dedupe_and_keyword_filter(config, monkeypatch) -> None:
    # 两次调用返回同一批消息（同 sender 同时间戳）→ 全部与协作沟通重复
    def fake_same_for_all(chat_id, keywords, since, until, **kwargs):
        return [
            MessageRecord(sender="ou_zhangsan", content=c,
                          timestamp=_ISSUE_T0, chat_name="研发群")
            for c in ["遇到问题：接口报错", "正常上线消息", "薪资问题聊一下"]
        ]

    monkeypatch.setattr(main.lark_msg, "collect", fake_same_for_all)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    messages = main.lark_msg.collect(
        "oc_x", ["上线"], since, _MONDAY, app_id_env="LARK_APP_ID",
        app_secret_env="LARK_APP_SECRET", sensitive_words=["薪资"],
        chat_name="研发群", timeout=5,
    )

    # 场景一：全部候选与协作沟通板块重复 → 无自动条目
    issues = main._collect_issues(config, since, _MONDAY, messages, _MONDAY.date())
    assert all(i.source != "auto" for i in issues)

    # 场景二：无历史消息 → 关键词/敏感词防御过滤生效
    issues2 = main._collect_issues(config, since, _MONDAY, [], _MONDAY.date())
    problems = [i.problem for i in issues2]
    assert "遇到问题：接口报错" in problems
    assert "正常上线消息" not in problems   # 无问题关键词 → 过滤
    assert "薪资问题聊一下" not in problems  # 命中敏感词 → 过滤


def test_issue_status_resolved_keyword(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    _patch_msg_by_keywords(monkeypatch, ["接口报错已解决"])

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues = main._collect_issues(config, since, _MONDAY, [], _MONDAY.date())

    assert len(issues) == 1
    assert issues[0].status == "已解决"
    assert issues[0].owner == "张三"


def test_auto_issue_carries_msg_key(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    _patch_msg_by_keywords(monkeypatch, ["遇到问题：接口报错"])

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues = main._collect_issues(config, since, _MONDAY, [], _MONDAY.date())

    auto = [i for i in issues if i.source == "auto"]
    assert len(auto) == 1
    assert auto[0].msg_key == f"ou_zhangsan|{_ISSUE_T1.isoformat()}"


def test_claimed_message_is_skipped(config, monkeypatch) -> None:
    """已在看板认领补充方案的消息，不再重复生成自动条目。"""
    _patch_collectors(monkeypatch)
    _patch_msg_by_keywords(monkeypatch, ["遇到问题：接口报错"])

    key = f"ou_zhangsan|{_ISSUE_T1.isoformat()}"
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_issue(
            "遇到问题：接口报错", "已修复", "张三",
            _MONDAY.date().isoformat(), status="已解决", msg_key=key,
        )

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues = main._collect_issues(config, since, _MONDAY, [], _MONDAY.date())

    assert [i.source for i in issues] == ["manual"]
    assert issues[0].msg_key == key
    assert issues[0].issue_id > 0


def test_issue_collection_failure_does_not_block(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)

    def fake_msg_fail_on_issue(chat_id, keywords, since, until, **kwargs):
        if "问题" in keywords or "报错" in keywords:
            raise CollectorError("msg down", source="lark_message")
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg_fail_on_issue)
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")
    assert row is not None
    assert "## 问题与解决" in row["markdown"]
