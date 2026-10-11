"""generator 单元测试（formatter + template）。"""

from __future__ import annotations

from datetime import datetime, timezone

from generator import formatter
from shared.models import (
    CommitRecord,
    IssueItem,
    MemberReport,
    MessageRecord,
    PlanItem,
    TaskRecord,
)

_DAY = datetime(2026, 9, 25).date()
_T0800 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
_T1500 = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)

_MEMBERS_CONFIG = [
    {"name": "张三", "github": "zhangsan", "lark": "ou_zhangsan"},
    {"name": "李四", "github": "lisi-dev", "lark": "ou_lisi"},
]


def _sample_records():
    commits = [
        CommitRecord(
            author="zhangsan",
            message="fix: 修复登录问题",
            timestamp=_T0800,
            repo="org/repo-a",
            additions=10,
            deletions=2,
            files_changed=2,
        ),
        CommitRecord(
            author="outsider",
            message="不该出现",
            timestamp=_T0800,
            repo="org/repo-a",
            additions=1,
            deletions=0,
            files_changed=1,
        ),
    ]
    tasks = [
        TaskRecord(
            assignee="ou_zhangsan",
            title="修复缺陷",
            status_from="进行中",
            status_to="已完成",
            updated_at=_T1500,
        )
    ]
    messages = [
        MessageRecord(
            sender="ou_zhangsan",
            content="今晚准备上线发布",
            timestamp=_T1500,
            chat_name="研发群",
        )
    ]
    return commits, tasks, messages


def test_aggregate_groups_records_by_identity() -> None:
    commits, tasks, messages = _sample_records()
    reports = formatter.aggregate(_MEMBERS_CONFIG, commits, tasks, messages)

    assert len(reports) == 2
    zhangsan, lisi = reports
    assert isinstance(zhangsan, MemberReport)
    assert len(zhangsan.commits) == 1
    assert len(zhangsan.tasks) == 1
    assert len(zhangsan.messages) == 1
    assert lisi.commits == [] and lisi.tasks == [] and lisi.messages == []


def test_generate_markdown_has_three_sections() -> None:
    commits, tasks, messages = _sample_records()
    reports = formatter.aggregate(_MEMBERS_CONFIG, commits, tasks, messages)
    report = formatter.generate(reports, _DAY, "研发一组")

    assert "### 代码提交" in report.markdown
    assert "### 任务进展" in report.markdown
    assert "### 协作沟通" in report.markdown
    assert "# 研发一组 工作日报（2026-09-25）" in report.markdown
    assert "fix: 修复登录问题" in report.markdown
    assert "进行中 → **已完成**：修复缺陷" in report.markdown
    assert "[研发群] 今晚准备上线发布" in report.markdown


def test_generate_html_is_rendered() -> None:
    commits, tasks, messages = _sample_records()
    reports = formatter.aggregate(_MEMBERS_CONFIG, commits, tasks, messages)
    report = formatter.generate(reports, _DAY, "研发一组")

    assert report.html.lstrip().startswith("<!DOCTYPE html>")
    assert "张三" in report.html
    assert "fix: 修复登录问题" in report.html
    assert "今晚准备上线发布" in report.html


def test_empty_member_shows_no_records() -> None:
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组")

    lisi_section = report.markdown.split("## 李四", maxsplit=1)[1]
    assert lisi_section.count("今日无记录") == 3


def test_failed_source_is_marked_in_markdown_and_html() -> None:
    reports = [
        MemberReport(name="张三", github_username="zhangsan")
    ]
    report = formatter.generate(
        reports, _DAY, "研发一组", failed_sources=["github"]
    )

    assert "以下数据源获取失败" in report.markdown
    assert "数据获取失败" in report.markdown
    assert "数据获取失败" in report.html
    assert "以下数据源获取失败" in report.html


def test_subtask_progress_rendered_in_markdown_and_html() -> None:
    parent = TaskRecord(
        assignee="ou_zhangsan",
        title="上线日报",
        status_from="进行中",
        status_to="已完成",
        updated_at=_T1500,
        subtasks=[
            TaskRecord(
                assignee="",
                title="写采集",
                status_from="",
                status_to="已完成",
                updated_at=_T1500,
            ),
            TaskRecord(
                assignee="",
                title="写模板",
                status_from="",
                status_to="进行中",
                updated_at=_T1500,
            ),
        ],
    )
    reports = [
        MemberReport(name="张三", github_username="zhangsan", tasks=[parent])
    ]
    report = formatter.generate(reports, _DAY, "研发一组")

    assert "子任务进度：**1/2** 已完成" in report.markdown
    assert "✅ 写采集（已完成）" in report.markdown
    assert "⬜ 写模板（进行中）" in report.markdown

    assert "📋 子任务 1/2 已完成" in report.html
    assert "✅ 写采集（已完成）" in report.html
    assert "⬜ 写模板（进行中）" in report.html


def test_markdown_has_tomorrow_plan_section() -> None:
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组")

    assert "## 明日计划" in report.markdown
    assert "暂无" in report.markdown.split("## 明日计划", maxsplit=1)[1]


def test_tomorrow_plans_rendered_in_markdown_and_html() -> None:
    plans = [
        PlanItem(title="完成接口联调", owner="张三", source="auto"),
        PlanItem(title="整理周报", owner="", source="manual"),
    ]
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组", tomorrow_plans=plans)

    plan_section = report.markdown.split("## 明日计划", maxsplit=1)[1]
    assert "📌 张三：完成接口联调" in plan_section
    assert "📌 团队：整理周报" in plan_section

    assert "明日计划" in report.html
    assert "张三：完成接口联调" in report.html
    assert "团队：整理周报" in report.html
    assert report.tomorrow_plans == plans


def test_issues_rendered_in_markdown_and_html() -> None:
    issues = [
        IssueItem(problem="接口报错", solution="已定位修复",
                  owner="张三", status="已解决", source="manual", issue_id=3),
        IssueItem(problem="环境卡顿", source="auto"),
    ]
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组", issues=issues)

    # 板块顺序：协作沟通之后、明日计划之前
    md = report.markdown
    assert md.index("### 协作沟通") < md.index("## 问题与解决") < md.index("## 明日计划")
    section = md.split("## 问题与解决", maxsplit=1)[1].split("## 明日计划", maxsplit=1)[0]
    assert "✅ 接口报错 → 已定位修复" in section
    assert "⏳ 环境卡顿 → 方案待定" in section

    assert "问题与解决" in report.html
    assert "接口报错" in report.html
    assert "已定位修复" in report.html
    assert "方案待定" in report.html
    assert report.issues == issues


def test_empty_issues_show_placeholder() -> None:
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组")

    assert "## 问题与解决" in report.markdown
    assert "暂无" in report.markdown.split("## 问题与解决", maxsplit=1)[1]
    # HTML 端无条目时整段隐藏（与明日计划行为一致）
    assert "问题与解决" not in report.html


def test_single_member_report_uses_personal_title() -> None:
    reports = formatter.aggregate(_MEMBERS_CONFIG[:1], [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组")

    assert report.markdown.startswith("# 张三的工作日报（2026-09-25）")
    assert "## 张三" not in report.markdown  # 标题已含成员名，不再重复小节标题
    assert "### 代码提交" in report.markdown
    assert "## 明日计划" in report.markdown

    assert "张三的工作日报" in report.html
    assert ">张三</h2>" not in report.html
