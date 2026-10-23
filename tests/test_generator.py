"""generator 单元测试（formatter + template）。"""

from __future__ import annotations

from datetime import datetime, timezone

from generator import formatter
from shared.models import (
    CommitFile,
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

    assert "### github代码提交情况" in report.markdown
    assert "### 飞书SDD测试群协作沟通情况" in report.markdown
    assert "## 今日任务清单" in report.markdown
    assert "任务进展情况" not in report.markdown  # 成员级任务变更板块已下线
    assert "# 研发一组 智能日报（2026-09-25）" in report.markdown
    assert "fix: 修复登录问题" in report.markdown
    assert "沟通记录1：今晚准备上线发布" in report.markdown


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
    assert lisi_section.count("今日无记录") == 2  # github + 协作沟通两个板块


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


def _tasklist_section(markdown: str) -> str:
    return markdown.split("## 今日任务清单", 1)[1].split("## 问题与解决", 1)[0]


def test_task_list_rendered_in_markdown_and_html() -> None:
    # 团队级「今日任务清单」板块：父任务+进度徽章+子任务勾选态
    task_list = TaskRecord(
        assignee="",
        title="9月25日任务清单",
        status_from="",
        status_to="进行中",
        updated_at=_T1500,
        subtasks=[
            TaskRecord(assignee="", title="写采集", status_from="",
                       status_to="已完成", updated_at=_T1500),
            TaskRecord(assignee="", title="写模板", status_from="",
                       status_to="进行中", updated_at=_T1500),
        ],
    )
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])
    report = formatter.generate(reports, _DAY, "研发一组", task_list=task_list)

    section = _tasklist_section(report.markdown)
    assert "- 📋 9月25日任务清单（**1/2** 已完成）" in section
    assert "- ✅ 写采集" in section
    assert "- ⬜ 写模板" in section

    assert "今日任务清单" in report.html
    assert "📋 9月25日任务清单" in report.html
    assert "已完成 1/2" in report.html
    assert "✅ 写采集" in report.html
    assert "⬜ 写模板" in report.html


def test_task_list_empty_and_failed_states() -> None:
    reports = formatter.aggregate(_MEMBERS_CONFIG, [], [], [])

    # 无清单：板块显示「暂无」
    empty = formatter.generate(reports, _DAY, "研发一组")
    assert "暂无" in _tasklist_section(empty.markdown)

    # 有父任务但无子任务：只显示标题不带进度
    bare = TaskRecord(assignee="", title="9月25日任务清单", status_from="",
                      status_to="进行中", updated_at=_T1500)
    bare_report = formatter.generate(reports, _DAY, "研发一组", task_list=bare)
    section = _tasklist_section(bare_report.markdown)
    assert "- 📋 9月25日任务清单" in section
    assert "已完成" not in section

    # lark_task 失败：显式标注获取失败
    failed = formatter.generate(
        reports, _DAY, "研发一组", failed_sources=["lark_task"]
    )
    assert "- ⚠️ 数据获取失败" in _tasklist_section(failed.markdown)
    assert "⚠️ 数据获取失败" in failed.html


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
    assert "📌 完成接口联调" in plan_section
    assert "📌 整理周报" in plan_section
    # 单人场景不显示负责人前缀
    assert "张三：" not in plan_section and "团队：" not in plan_section

    assert "明日计划" in report.html
    assert "完成接口联调" in report.html
    assert "整理周报" in report.html
    assert report.tomorrow_plans == plans


_COMMIT_URL = "https://github.com/org/repo/commit/sha1"
_BLOB_URL = "https://github.com/org/repo/blob/sha1/config.py"


def _report_with_commit(commit: CommitRecord):
    member = MemberReport(
        name="张三", github_username="zhangsan", commits=[commit]
    )
    return formatter.generate([member], _DAY, "研发一组")


def test_small_commit_renders_summary_with_link() -> None:
    # 精简模式：只展示提交摘要，无文件内联/代码展示
    tiny = CommitFile(
        filename="config.py",
        status="added",
        additions=1,
        deletions=0,
        patch="@@ -0,0 +1 @@\n+x\n",
        blob_url=_BLOB_URL,
    )
    commit = CommitRecord(
        author="zhangsan",
        message="feat: 一个字",
        timestamp=_T0800,
        repo="org/repo",
        additions=1,
        deletions=0,
        files_changed=1,
        html_url=_COMMIT_URL,
        files=[tiny],
    )
    report = _report_with_commit(commit)

    assert f"[查看提交]({_COMMIT_URL})" in report.markdown
    assert "+1/-0" in report.markdown
    assert "📄" not in report.markdown
    assert _COMMIT_URL in report.html
    assert "长按复制链接到浏览器" in report.html
    assert "+1" in report.html and "-0" in report.html


def test_large_commit_renders_summary_without_code_body() -> None:
    # 精简模式：小改动大改动都只展示提交摘要，不展示文件和代码
    patch = "@@ -0,0 +1,21 @@\n" + "\n".join(
        f"+SECRET_CODE_LINE_{i}" for i in range(21)
    ) + "\n"
    big = CommitFile(
        filename="big.py",
        status="added",
        additions=21,
        deletions=0,
        patch=patch,
        blob_url="https://github.com/org/repo/blob/sha1/big.py",
    )
    commit = CommitRecord(
        author="zhangsan",
        message="feat: 大量代码",
        timestamp=_T0800,
        repo="org/repo",
        additions=21,
        deletions=0,
        files_changed=1,
        html_url=_COMMIT_URL,
        files=[big],
    )
    report = _report_with_commit(commit)

    # 提交链接始终存在；代码内容和文件链接都不展示
    assert f"[查看提交]({_COMMIT_URL})" in report.markdown
    assert "📄" not in report.markdown
    assert "SECRET_CODE_LINE_0" not in report.markdown
    assert _COMMIT_URL in report.html
    assert "SECRET_CODE_LINE_0" not in report.html
    assert "big.py" not in report.html


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
    assert md.index("### 飞书SDD测试群协作沟通情况") < md.index("## 问题与解决") < md.index("## 明日计划")
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

    assert report.markdown.startswith("# 张三的智能日报（2026-09-25）")
    assert "## 张三" not in report.markdown  # 标题已含成员名，不再重复小节标题
    assert "### github代码提交情况" in report.markdown
    assert "## 明日计划" in report.markdown

    assert "张三的智能日报" in report.html
    assert ">张三</h2>" not in report.html
