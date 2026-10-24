"""main.py 单元测试：工作日识别、健康检查与管道编排。"""

from __future__ import annotations

import os
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
    assert "智能日报" in html_file.read_text(encoding="utf-8")


def test_view_report_missing_date_returns_false(config) -> None:
    def must_not_open(uri: str) -> None:
        raise AssertionError("无日报时不应打开页面")

    assert main.view_report(config, "2026-09-26", opener=must_not_open) is False


# ---- 默认动作：启动后台服务 ----

def test_default_invocation_starts_service(config, monkeypatch) -> None:
    called: list[str] = []

    monkeypatch.setattr(
        main, "run_service", lambda cfg: called.append("run_service")
    )

    assert main.main(["--config", config.source_path]) == 0
    assert called == ["run_service"]


def test_explicit_serve_starts_service(config, monkeypatch) -> None:
    called: list[str] = []

    monkeypatch.setattr(
        main, "run_service", lambda cfg: called.append("run_service")
    )

    assert main.main(["--config", config.source_path, "--serve"]) == 0
    assert called == ["run_service"]


# ---- 管道 ----
_MONDAY = datetime(2026, 9, 28, 17, 0, tzinfo=timezone(timedelta(hours=8)))


def _patch_collectors(monkeypatch, *, fail: set[str] | None = None) -> dict:
    fail = fail or set()
    counts = {"commits": 0, "messages": 0}

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

    def fake_lark_msg(chat_id, keywords, since, until, **kwargs):
        if "lark_message" in fail:
            raise CollectorError("msg down", source="lark_message")
        counts["messages"] += 1
        return []

    monkeypatch.setattr(main.github, "collect", fake_github)
    monkeypatch.setattr(main.lark_msg, "collect", fake_lark_msg)
    # 管道测试不涉及真实清单读写：当日清单恒为空且不报失败
    monkeypatch.setattr(main.lark_task, "fetch_daily_tasklist", lambda *a, **k: None)
    return counts


def _enable_plan_capture(config, monkeypatch) -> None:
    """给测试配置打开群消息计划/完成捕捉（模板默认关闭）。"""

    monkeypatch.setitem(
        config._data["lark_message"], "plan_keywords", ["明天", "明日", "计划", "待办"]
    )
    monkeypatch.setitem(
        config._data["lark_message"], "done_keywords", ["完成", "做完", "搞定"]
    )


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
    assert "### github代码提交情况" in row["markdown"]


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
    _patch_collectors(monkeypatch, fail={"github", "lark_message"})
    # 清单保障也失败：三个数据源全部不可用
    def tasklist_boom(*a, **k):
        raise CollectorError("task down", source="lark_task")

    monkeypatch.setattr(main.lark_task, "fetch_daily_tasklist", tasklist_boom)

    def should_not_call(*args, **kwargs):
        raise AssertionError("no push when all sources fail")

    monkeypatch.setattr(main.email, "send", should_not_call)
    monkeypatch.setattr(main.lark_bot, "send", should_not_call)

    assert main.run_pipeline(config, now=_MONDAY) is False


def test_pipeline_merges_auto_and_manual_tomorrow_plans(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    # 自动计划（群消息捕捉落库，带 msg_key）+ 手动计划，plan_date = 明天
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("准备明日评审", "张三", "2026-09-29", msg_key="ou_zhangsan|2026-09-28T12:00:00+08:00")
        storage.save_plan("整理周报", "张三", "2026-09-29")

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")

    assert row is not None
    assert "## 明日计划" in row["markdown"]
    assert "📌 准备明日评审" in row["markdown"]  # 自动：plans 表 msg_key 来源
    assert "📌 整理周报" in row["markdown"]      # 手动：看板添加
    assert '"tomorrow_plans"' in row["data_json"]


# ---- 群消息任务闭环：明日计划捕捉 / 清单保障 / 完成宣告 ----

def test_strip_plan_prefix_variants() -> None:
    keywords = ["明天", "明日", "计划", "待办"]
    assert main._strip_plan_prefix("明天：写周报", keywords) == "写周报"
    assert main._strip_plan_prefix("明日、复盘会", keywords) == "复盘会"
    assert main._strip_plan_prefix("计划 写代码", keywords) == "写代码"
    assert main._strip_plan_prefix("待办:提交作业", keywords) == "提交作业"
    assert main._strip_plan_prefix("明天：x", keywords) == ""  # 剩余不足 2 字无效
    assert main._strip_plan_prefix("明天：", keywords) == ""
    # 连续前导触发词逐个剥离（「明日计划：」= 明日 + 计划）
    assert main._strip_plan_prefix("明日计划：写周报", keywords) == "写周报"
    # 非前导触发词不剥离（消息里的「明天」在中间）
    assert main._strip_plan_prefix("聊聊明天的安排", keywords) == "聊聊明天的安排"


def test_split_numbered_items() -> None:
    assert main._split_numbered_items("写周报") == ["写周报"]  # 非编号原样返回
    assert main._split_numbered_items("1.写周报") == ["写周报"]  # 单项剥编号
    assert main._split_numbered_items(
        "1.接着迭代智能日报生成器。2.完成高级软件工程PPT与论文"
    ) == ["接着迭代智能日报生成器", "完成高级软件工程PPT与论文"]
    assert main._split_numbered_items("1、梳理需求 2、评审方案") == [
        "梳理需求",
        "评审方案",
    ]
    # 编号前无标点/空白分隔不拆（避免「升级2.0版本」误拆）
    assert main._split_numbered_items("1.迭代升级2.0版本") == ["迭代升级2.0版本"]
    assert main._split_numbered_items("第1.次迭代") == ["第1.次迭代"]


def test_extract_plan_payload_midtext_trigger() -> None:
    keywords = ["明天", "明日", "计划", "待办"]
    # 触发词在句中且紧跟冒号：截取其后的正文
    assert main._extract_plan_payload(
        "现在做出明日计划：1.接着迭代。2.写论文", keywords
    ) == "1.接着迭代。2.写论文"
    # 无冒号的句中触发词不算强信号，不捕捉
    assert main._extract_plan_payload("今天完成了迭代计划，效果不错", keywords) == ""
    assert main._extract_plan_payload("我制定了新计划", keywords) == ""


def test_collect_plan_messages_saves_dedupes_and_skips_done(config, monkeypatch) -> None:
    _enable_plan_capture(config, monkeypatch)

    t0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)

    def fake(chat_id, keywords, since, until, **kwargs):
        kws = list(keywords)
        if "明天" in kws:  # 计划捕捉二次采集
            return [
                MessageRecord(sender="ou_zhangsan", content="明天：写周报",
                              timestamp=t0, chat_name="研发群"),
                MessageRecord(sender="ou_zhangsan", content="完成 周报",
                              timestamp=t1, chat_name="研发群"),  # 完成宣告开头 → 跳过
            ]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_plan_messages(config, since, _MONDAY, _MONDAY.date())

    assert keys == {("ou_zhangsan", t0.isoformat())}
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-29")
    assert len(rows) == 1
    assert rows[0]["title"] == "写周报"
    assert rows[0]["status"] == "待完成"
    assert rows[0]["msg_key"] == f"ou_zhangsan|{t0.isoformat()}"

    # 重放同一批消息：msg_key 查重不重复入库，但键仍返回（防回流沟通板块）
    keys2 = main._collect_plan_messages(config, since, _MONDAY, _MONDAY.date())
    assert keys2 == keys
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        assert len(storage.iter_plans("2026-09-29")) == 1


def test_collect_plan_messages_midtext_trigger_splits_numbered(
    config, monkeypatch
) -> None:
    """句中「触发词：」+ 编号列表：逐条拆分入库（真实用户写法）。"""
    _enable_plan_capture(config, monkeypatch)

    t0 = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    content = "现在做出明日计划：1.接着迭代智能日报生成器。2.完成高级软件工程PPT与论文"

    def fake(chat_id, keywords, since, until, **kwargs):
        if "明天" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan", content=content,
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_plan_messages(config, since, _MONDAY, _MONDAY.date())

    assert keys == {("ou_zhangsan", t0.isoformat())}
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-29")
    assert [r["title"] for r in rows] == [
        "接着迭代智能日报生成器",
        "完成高级软件工程PPT与论文",  # 子项含「完成」不阻碍计划捕捉
    ]
    assert all(r["owner"] == "张三" for r in rows)
    assert all(r["status"] == "待完成" for r in rows)


def test_collect_plan_messages_leading_plan_keyword_with_done_word(
    config, monkeypatch
) -> None:
    """「明天：完成作业」是计划而非完成宣告：按计划捕捉。"""
    _enable_plan_capture(config, monkeypatch)

    t0 = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)

    def fake(chat_id, keywords, since, until, **kwargs):
        if "明天" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan", content="明天：完成数据库作业",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_plan_messages(config, since, _MONDAY, _MONDAY.date())

    assert keys == {("ou_zhangsan", t0.isoformat())}
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-29")
    assert [r["title"] for r in rows] == ["完成数据库作业"]


def test_ensure_daily_tasklist_creates_when_missing(config, monkeypatch) -> None:
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")

    failed: list[str] = []
    tl = main._ensure_daily_tasklist(config, date(2026, 9, 28), failed)

    assert failed == []
    assert tl is not None
    assert tl.title == "9月28日任务清单"
    assert [s.title for s in tl.subtasks] == ["写周报"]

    # 幂等：再次保障不重复建清单、不重复建子任务
    tl2 = main._ensure_daily_tasklist(config, date(2026, 9, 28), failed)
    assert [s.title for s in tl2.subtasks] == ["写周报"]


def test_ensure_daily_tasklist_backfills_missing_subtasks(config, monkeypatch) -> None:
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")

    failed: list[str] = []
    assert main._ensure_daily_tasklist(config, date(2026, 9, 28), failed) is not None

    # 清单已存在后新布置的任务：按标题补缺，不重建清单
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("补测试", "张三", "2026-09-28")
    tl = main._ensure_daily_tasklist(config, date(2026, 9, 28), failed)

    assert [s.title for s in tl.subtasks] == ["写周报", "补测试"]
    assert sum(1 for m, p in fake.calls if m == "POST" and p.endswith("/subtasks")) == 2


class _CompletableClient:
    """记录 complete_task PATCH 调用的最小客户端。"""

    def __init__(self) -> None:
        self.patched: list[str] = []

    def request(self, method, path, *, params=None, json_body=None):
        self.patched.append(path)
        return {"task": {"completed_at": "1760000000000"}}


def test_done_message_completes_plan_and_task(config, monkeypatch) -> None:
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    client = _CompletableClient()
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: client)
    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan", content="完成 写周报",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    assert keys == {("ou_zhangsan", t0.isoformat())}
    assert client.patched == ["/open-apis/task/v2/tasks/" + task_list.subtasks[0].guid]
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-28")
    assert rows[0]["status"] == "已完成"


def test_done_message_with_ambiguous_match_is_skipped(config, monkeypatch) -> None:
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")
        storage.save_plan("周报整理", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: _CompletableClient())
    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan", content="完成 周报",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    # 「周报」同时包含命中两条子任务 → 全部放弃
    assert keys == set()
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        assert all(r["status"] == "待完成" for r in storage.iter_plans("2026-09-28"))


def test_done_message_for_completed_task_reconciles(config, monkeypatch) -> None:
    """对已完成子任务的完成宣告（含往轮重放）：只对账落库并消费消息。"""
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    # 子任务在飞书侧已被完成（往轮处理过）
    guid = task_list.subtasks[0].guid
    fake._tasks[guid]["completed_at"] = "1760000000000"
    task_list.subtasks[0].status_to = "已完成"

    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan", content="完成 写周报",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    assert keys == {("ou_zhangsan", t0.isoformat())}
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-28")
    assert rows[0]["status"] == "已完成"


def test_done_message_with_trailing_completed_word(config, monkeypatch) -> None:
    """尾部完成写法「xxx已完成」：剥尾后与子任务标题（剥前导完成词）精确匹配。"""
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("完成高级软件工程PPT与论文", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    client = _CompletableClient()
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: client)
    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan",
                                  content="高级软件工程PPT与论文已完成",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    assert keys == {("ou_zhangsan", t0.isoformat())}
    assert client.patched == ["/open-apis/task/v2/tasks/" + task_list.subtasks[0].guid]
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-28")
    assert rows[0]["status"] == "已完成"


def test_done_message_with_reordered_text_fuzzy_matches(config, monkeypatch) -> None:
    """语序重排写法：归一化精确与包含都不中时，唯一字符重合超阈值者命中。"""
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("继续迭代智能日报生成器", "张三", "2026-09-28")
        storage.save_plan("继续补充高级软件工程PPT", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    client = _CompletableClient()
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: client)
    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan",
                                  content="高级软件工程PPT补充已完成",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    assert keys == {("ou_zhangsan", t0.isoformat())}
    # 命中的应是「继续补充高级软件工程PPT」（唯一重合超阈值）
    assert client.patched == ["/open-apis/task/v2/tasks/" + task_list.subtasks[1].guid]
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rows = storage.iter_plans("2026-09-28")
    statuses = {r["title"]: r["status"] for r in rows}
    assert statuses["继续补充高级软件工程PPT"] == "已完成"
    assert statuses["继续迭代智能日报生成器"] == "待完成"


def test_done_message_with_plan_prefix_is_skipped(config, monkeypatch) -> None:
    """「明天：完成写周报」是计划宣告：完成流程不得误打勾今天的任务。"""
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    client = _CompletableClient()
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: client)
    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan", content="明天：完成写周报",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    assert keys == set()
    assert client.patched == []  # 未打勾飞书
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        assert all(
            r["status"] == "待完成" for r in storage.iter_plans("2026-09-28")
        )


def test_done_message_mentioning_plan_midtext_still_completes(
    config, monkeypatch
) -> None:
    """完成宣告里夹带「明日计划」字样（非开头）仍正常匹配完成。"""
    _enable_plan_capture(config, monkeypatch)
    from test_lark_task import FakeLark

    fake = FakeLark([])
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: fake)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_plan("写周报", "张三", "2026-09-28")
    task_list = main._ensure_daily_tasklist(config, date(2026, 9, 28), [])

    client = _CompletableClient()
    monkeypatch.setattr(main, "_lark_task_client", lambda cfg: client)
    t0 = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        if "完成" in list(keywords):
            return [MessageRecord(sender="ou_zhangsan",
                                  content="完成了明日计划里的写周报",
                                  timestamp=t0, chat_name="研发群")]
        return []

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    keys = main._collect_done_messages(config, since, _MONDAY, task_list)

    assert keys == {("ou_zhangsan", t0.isoformat())}
    assert client.patched == ["/open-apis/task/v2/tasks/" + task_list.subtasks[0].guid]


def test_pipeline_plan_message_only_in_tomorrow_plans(config, monkeypatch) -> None:
    """计划消息从协作沟通剔除，只出现在明日计划板块。"""
    _enable_plan_capture(config, monkeypatch)
    _patch_collectors(monkeypatch)
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    t0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)

    def fake_msg(chat_id, keywords, since, until, **kwargs):
        kws = list(keywords)
        if "明天" in kws:  # 计划捕捉
            return [MessageRecord(sender="ou_zhangsan", content="明天：写周报",
                                  timestamp=t0, chat_name="研发群")]
        if "完成" in kws:  # 完成捕捉
            return []
        # 主采集（该消息同时命中沟通关键词，模拟真实重叠）
        return [MessageRecord(sender="ou_zhangsan", content="明天：写周报",
                              timestamp=t0, chat_name="研发群"),
                MessageRecord(sender="ou_zhangsan", content="功能已上线",
                              timestamp=t1, chat_name="研发群")]

    monkeypatch.setattr(main.lark_msg, "collect", fake_msg)

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")
    assert row is not None
    assert "明天：写周报" not in row["markdown"]  # 原始消息已从沟通剔除
    assert "功能已上线" in row["markdown"]        # 普通沟通保留
    plans_section = row["markdown"].split("## 明日计划", 1)[1]
    assert "📌 写周报" in plans_section           # 剥前缀后进入明日计划


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


def test_issue_keywords_independent_of_chat_keywords(config, monkeypatch) -> None:
    """问题描述消息即使同时命中协作沟通关键词，也生成自动条目并返回待剔除键。"""

    def fake_same_for_all(chat_id, keywords, since, until, **kwargs):
        return [
            MessageRecord(sender="ou_zhangsan", content=c,
                          timestamp=_ISSUE_T0, chat_name="研发群")
            for c in ["遇到问题：接口报错", "正常上线消息", "薪资问题聊一下"]
        ]

    monkeypatch.setattr(main.lark_msg, "collect", fake_same_for_all)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues, keys = main._collect_issues(config, since, _MONDAY, _MONDAY.date())

    problems = [i.problem for i in issues]
    assert "遇到问题：接口报错" in problems      # 问题消息进问题板块
    assert "正常上线消息" not in problems        # 无问题关键词 → 普通沟通
    assert "薪资问题聊一下" not in problems      # 命中敏感词 → 过滤
    assert keys == {("ou_zhangsan", _ISSUE_T0.isoformat())}


def test_resolved_summary_attaches_to_issue(config, monkeypatch) -> None:
    """解决总结消息自动挂为同负责人最近问题的方案，不再单独成条目。"""

    def fake(chat_id, keywords, since, until, **kwargs):
        return [
            MessageRecord(sender="ou_zhangsan", content="遇到问题：接口报错",
                          timestamp=_ISSUE_T0, chat_name="研发群"),
            MessageRecord(sender="ou_zhangsan", content="问题得到解决，原因是配置写错了",
                          timestamp=_ISSUE_T1, chat_name="研发群"),
        ]

    monkeypatch.setattr(main.lark_msg, "collect", fake)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues, keys = main._collect_issues(config, since, _MONDAY, _MONDAY.date())

    assert len(issues) == 1
    assert issues[0].status == "已解决"
    assert issues[0].solution == "问题得到解决，原因是配置写错了"
    assert issues[0].owner == "张三"
    assert keys == {("ou_zhangsan", _ISSUE_T0.isoformat())}


def test_resolved_summary_without_issue_is_dropped(config, monkeypatch) -> None:
    """没有对应问题条目时，解决总结消息直接丢弃，不生成「问题」。"""

    def fake(chat_id, keywords, since, until, **kwargs):
        return [
            MessageRecord(sender="ou_zhangsan", content="问题得到解决，原因是配置写错了",
                          timestamp=_ISSUE_T0, chat_name="研发群")
        ]

    monkeypatch.setattr(main.lark_msg, "collect", fake)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues, keys = main._collect_issues(config, since, _MONDAY, _MONDAY.date())

    assert issues == []
    assert keys == set()


def test_classify_message_resolved_hint_takes_priority() -> None:
    kws = ["问题", "报错"]

    assert main.classify_message("遇到问题：接口报错", kws) == "issue"
    assert main.classify_message("报错已修复", kws) == "resolved"
    # 解决总结可能不含任何问题关键词
    assert main.classify_message("已修复，原因是配置写错", kws) == "resolved"
    # 自然写法的解决宣告：修复为 / 修好了 / 解决方案：（真实误判案例）
    assert (
        main.classify_message("时区问题解决方案：修复为按运行环境动态计算期望时刻", kws)
        == "resolved"
    )
    assert (
        main.classify_message("时区硬编码 bug，修复为按运行环境动态计算期望时刻", kws)
        == "resolved"
    )
    assert main.classify_message("接口修好了", kws) == "resolved"
    assert main.classify_message("正常上线消息", kws) == "normal"


def test_resolved_summary_without_issue_keyword_is_fetched(config, monkeypatch) -> None:
    """采集词并入解决特征词：不含问题关键词的解决总结也能拉到并挂方案。"""

    seen_keywords: list[list[str]] = []

    def fake(chat_id, keywords, since, until, **kwargs):
        seen_keywords.append(list(keywords))
        return [
            MessageRecord(sender="ou_zhangsan", content="遇到问题：接口报错",
                          timestamp=_ISSUE_T0, chat_name="研发群"),
            MessageRecord(sender="ou_zhangsan", content="已修复，重新部署成功",
                          timestamp=_ISSUE_T1, chat_name="研发群"),
        ]

    monkeypatch.setattr(main.lark_msg, "collect", fake)

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues, keys = main._collect_issues(config, since, _MONDAY, _MONDAY.date())

    assert len(issues) == 1
    assert issues[0].status == "已解决"
    assert issues[0].solution == "已修复，重新部署成功"
    assert issues[0].owner == "张三"
    assert keys == {("ou_zhangsan", _ISSUE_T0.isoformat())}

    # 问题板块的采集关键词必须包含解决特征词，否则总结消息根本拉不到
    assert "问题" in seen_keywords[-1]
    assert "已修复" in seen_keywords[-1]


def test_auto_issue_carries_msg_key(config, monkeypatch) -> None:
    _patch_collectors(monkeypatch)
    _patch_msg_by_keywords(monkeypatch, ["遇到问题：接口报错"])

    since = _MONDAY.replace(hour=0, minute=0, second=0, microsecond=0)
    issues, _ = main._collect_issues(config, since, _MONDAY, _MONDAY.date())

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
    issues, _ = main._collect_issues(config, since, _MONDAY, _MONDAY.date())

    assert [i.source for i in issues] == ["manual"]
    assert issues[0].msg_key == key
    assert issues[0].issue_id > 0


def test_pipeline_moves_issue_message_out_of_chat(config, monkeypatch) -> None:
    """问题消息即使命中协作沟通关键词，也只出现在问题与解决板块。"""

    both = [
        MessageRecord(sender="ou_zhangsan", content="功能已上线",
                      timestamp=_ISSUE_T0, chat_name="研发群"),
        MessageRecord(sender="ou_zhangsan", content="登录测试遇到问题，账户变成空账户",
                      timestamp=_ISSUE_T1, chat_name="研发群"),
    ]

    def fake_all_keywords(chat_id, keywords, since, until, **kwargs):
        return list(both)  # 真实采集层按各自关键词过滤，这里返回全量模拟交集

    monkeypatch.setattr(main.lark_msg, "collect", fake_all_keywords)
    monkeypatch.setattr(main.email, "send", lambda *a, **kw: True)
    monkeypatch.setattr(main.lark_bot, "send", lambda *a, **kw: True)

    ok = main.run_pipeline(config, now=_MONDAY)
    assert ok is True

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        row = storage.get_report("2026-09-28", "测试团队")

    assert row is not None
    assert "- ⏳ 登录测试遇到问题，账户变成空账户 → 方案待定" in row["markdown"]
    assert row["markdown"].count("登录测试遇到问题") == 1  # 沟通板块不再出现


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


# ---- 单实例服务锁 ----


def test_service_lock_conflicts_and_release(tmp_path) -> None:
    cfg = {"storage": {"sqlite_path": str(tmp_path / "reports.db")}}

    assert main._acquire_service_lock(cfg) is True
    try:
        assert main._acquire_service_lock(cfg) is False  # 二次获取失败
        assert main._read_lock_pid(cfg) == str(os.getpid())
    finally:
        main._release_service_lock()

    assert main._acquire_service_lock(cfg) is True  # 释放后可重新获取
    main._release_service_lock()


def test_main_serve_rejected_when_already_locked(tmp_path, monkeypatch) -> None:
    yaml_path = tmp_path / "config.yaml"
    db_path = str(tmp_path / "reports.db").replace("\\", "/")
    yaml_path.write_text(
        _CONFIG_YAML_TEMPLATE.format(db_path=db_path), encoding="utf-8"
    )

    config = load_config(yaml_path)
    assert main._acquire_service_lock(config) is True
    try:

        def _boom(_config):
            raise AssertionError("run_service 不应被调用")

        monkeypatch.setattr(main, "run_service", _boom)
        assert main.main(["--config", str(yaml_path), "--serve"]) == 1
    finally:
        main._release_service_lock()

    # 锁释放后 --serve 正常进入 run_service（用哨兵验证被调用）
    sentinel: list = []
    monkeypatch.setattr(main, "run_service", lambda cfg: sentinel.append(cfg))
    assert main.main(["--config", str(yaml_path), "--serve"]) == 0
    assert len(sentinel) == 1


# ---- 飞书群回执（specs/no-dashboard.md §2.2） ----

def test_receipt_mark_dedup() -> None:
    main._RECEIPT_SENT.clear()
    assert main._receipt_mark("plan|k1|t1") is True
    assert main._receipt_mark("plan|k1|t1") is False
    assert main._receipt_mark("plan|k2|t2") is True


def test_send_receipts_skips_when_disabled(config) -> None:
    config["lark_bot"]["enabled"] = False
    called: list = []
    monkeypatch_obj = __import__("pytest").MonkeyPatch()
    monkeypatch_obj.setattr(main.lark_bot, "send_text", lambda *a, **kw: called.append(1))
    main._send_receipts(config, ["📌 x"])
    assert called == []


def test_send_receipts_skips_empty(config) -> None:
    config["lark_bot"]["enabled"] = True
    called: list = []
    monkeypatch_obj = __import__("pytest").MonkeyPatch()
    monkeypatch_obj.setattr(main.lark_bot, "send_text", lambda *a, **kw: called.append(1))
    main._send_receipts(config, [])
    assert called == []


def test_send_receipts_builds_prefix_and_calls(config, monkeypatch) -> None:
    config["lark_bot"]["enabled"] = True
    sentinel: dict = {}

    def fake_send(webhook, text, timeout=10):
        sentinel["text"] = text
        sentinel["webhook"] = webhook
        return True

    monkeypatch.setattr(main.lark_bot, "send_text", fake_send)

    main._send_receipts(
        config,
        [
            "📌 已收录为明日计划：写周报",
            "✅ 已完成：PPT",
            "⚠️ 已记录问题：GitHub 限流",
        ],
    )

    assert sentinel["webhook"] == config["lark_bot"]["webhook"]
    assert sentinel["text"].startswith("【智能日报】\n")
    assert "📌 已收录为明日计划：写周报" in sentinel["text"]
    assert "✅ 已完成：PPT" in sentinel["text"]
    assert "⚠️ 已记录问题：GitHub 限流" in sentinel["text"]


def test_send_receipts_exception_swallowed(config, monkeypatch) -> None:
    config["lark_bot"]["enabled"] = True

    def fake_send(*a, **kw):
        raise RuntimeError("网络崩了")

    monkeypatch.setattr(main.lark_bot, "send_text", fake_send)
    main._send_receipts(config, ["📌 x"])  # 不应抛


def test_send_receipts_false_ignored(config, monkeypatch) -> None:
    config["lark_bot"]["enabled"] = True
    monkeypatch.setattr(main.lark_bot, "send_text", lambda *a, **kw: False)
    main._send_receipts(config, ["📌 x"])  # 不应抛
