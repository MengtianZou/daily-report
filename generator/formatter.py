"""数据整理与日报生成（design.md §2、§4.2）。

- aggregate：按成员身份映射将三个数据源的记录聚合到人；
- build_markdown：生成三段式 Markdown；
- generate：组装 DailyReport（Markdown + HTML）。
"""

from __future__ import annotations

from datetime import date, datetime

from generator.diffview import inline_groups
from generator.template import render_report_html
from shared.models import (
    CommitRecord,
    DailyReport,
    IssueItem,
    MemberReport,
    MessageRecord,
    PlanItem,
    TaskRecord,
)

# 数据源标识 -> 板块中文名
_SOURCE_TITLES = {
    "github": "github代码提交情况",
    "lark_task": "今日任务清单",
    "lark_message": "飞书SDD测试群协作沟通情况",
}


def aggregate(
    member_configs: list[dict[str, str]],
    commits: list[CommitRecord],
    tasks: list[TaskRecord],
    messages: list[MessageRecord],
) -> list[MemberReport]:
    """把来自三个数据源的记录按映射表聚合为成员报告列表。"""

    reports: list[MemberReport] = []
    for config in member_configs:
        github_name = config.get("github") or None
        lark_name = config["lark"]
        reports.append(
            MemberReport(
                name=config["name"],
                github_username=github_name,
                commits=(
                    [c for c in commits if c.author == github_name]
                    if github_name
                    else []
                ),
                tasks=[t for t in tasks if t.assignee == lark_name],
                messages=[m for m in messages if m.sender == lark_name],
            )
        )
    return reports


def _hhmm(value: datetime) -> str:
    # 采集器产出的均为 UTC 感知时间，统一按运行环境本地时区（部署时为
    # Asia/Shanghai）展示，避免出现时间整体偏移 8 小时。
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%H:%M")


def _render_section(records: list, failed: bool, kind: str) -> list[str]:
    if failed:
        return ["- ⚠️ 数据获取失败"]
    if not records:
        return ["今日无记录"]

    lines: list[str] = []
    if kind == "github":
        for c in records:
            link = f" [查看提交]({c.html_url})" if c.html_url else ""
            lines.append(
                f"- `{c.repo}` {c.message}（+{c.additions}/-{c.deletions}，"
                f"{c.files_changed} 文件，{_hhmm(c.timestamp)}）{link}"
            )
            groups = inline_groups(c)
            if groups:
                # 小改动：内联展示每个文件的新增行
                for f, added in groups:
                    file_link = (
                        f"[{f.filename}]({f.blob_url})"
                        if f.blob_url
                        else f.filename
                    )
                    lines.append(
                        f"  - 📄 {file_link}（+{f.additions}/-{f.deletions}）"
                    )
                    for content in added:
                        lines.append(f"    - + {content}")
            elif c.files:
                # 改动过大：不展示代码，仅提供文件跳转链接
                for f in c.files:
                    file_link = (
                        f"[{f.filename}]({f.blob_url})"
                        if f.blob_url
                        else f.filename
                    )
                    lines.append(f"  - 📄 {file_link}")
    else:
        for idx, m in enumerate(records, start=1):
            lines.append(f"- 沟通记录{idx}：{m.content}（{_hhmm(m.timestamp)}）")
    return lines


def _render_task_list(task_list: TaskRecord | None, failed: bool) -> list[str]:
    """渲染团队级「今日任务清单」板块行：父任务+进度徽章+子任务勾选态。"""

    if failed:
        return ["- ⚠️ 数据获取失败"]
    if task_list is None:
        return ["暂无"]
    lines: list[str] = []
    completed, total = task_list.progress
    if total > 0:
        lines.append(f"- 📋 {task_list.title}（**{completed}/{total}** 已完成）")
        for sub in task_list.subtasks:
            icon = "✅" if sub.status_to == "已完成" else "⬜"
            lines.append(f"  - {icon} {sub.title}")
    else:
        lines.append(f"- 📋 {task_list.title}")
    return lines


def _render_plans(plans: list[PlanItem]) -> list[str]:
    """渲染「明日计划」板块行。"""

    if not plans:
        return ["暂无"]
    lines: list[str] = []
    for p in plans:
        lines.append(f"- 📌 {p.title}")
    return lines


def _render_issues(issues: list[IssueItem]) -> list[str]:
    """渲染「问题与解决」板块行。"""

    if not issues:
        return ["暂无"]
    lines: list[str] = []
    for i in issues:
        icon = "✅" if i.status == "已解决" else "⏳"
        solution = i.solution or "方案待定"
        lines.append(f"- {icon} {i.problem} → {solution}")
    return lines


def report_title(members: list[MemberReport], team_name: str) -> str:
    """日报展示标题：单人日报以「成员名的智能日报」命名，多人用团队名。"""
    if len(members) == 1:
        return f"{members[0].name}的智能日报"
    return f"{team_name} 智能日报"


def build_markdown(
    team_name: str,
    report_date: date,
    members: list[MemberReport],
    failed_sources: list[str] | None = None,
    tomorrow_plans: list[PlanItem] | None = None,
    issues: list[IssueItem] | None = None,
    task_list: TaskRecord | None = None,
    summary: str = "",
) -> str:
    """生成完整 Markdown 日报。"""

    failed = set(failed_sources or [])
    lines: list[str] = [
        f"# {report_title(members, team_name)}（{report_date.isoformat()}）",
        "",
    ]
    if failed:
        labels = "、".join(_SOURCE_TITLES[s] for s in failed if s in _SOURCE_TITLES)
        lines.extend(
            [f"> ⚠️ 以下数据源获取失败：{labels}", ""]
        )
    if summary:
        # AI 摘要附在日报头部：失败提示之后、正文各板块之前
        lines.extend(["## 今日总结", ""])
        lines.extend(summary.strip().splitlines())
        lines.append("")

    # 单人日报标题已含成员名，不再重复成员小节标题
    single = len(members) == 1
    for member in members:
        if not single:
            lines.extend([f"## {member.name}", ""])
        lines.append("### github代码提交情况")
        lines.extend(_render_section(member.commits, "github" in failed, "github"))
        lines.append("")
        lines.append("### 飞书SDD测试群协作沟通情况")
        lines.extend(
            _render_section(member.messages, "lark_message" in failed, "lark_message")
        )
        lines.append("")

    lines.extend(["## 今日任务清单", ""])
    lines.extend(_render_task_list(task_list, "lark_task" in failed))
    lines.append("")

    lines.extend(["## 问题与解决", ""])
    lines.extend(_render_issues(list(issues or [])))
    lines.append("")

    lines.extend(["## 明日计划", ""])
    lines.extend(_render_plans(list(tomorrow_plans or [])))

    return "\n".join(lines).rstrip() + "\n"


def generate(
    members: list[MemberReport],
    report_date: date,
    team_name: str,
    *,
    failed_sources: list[str] | None = None,
    tomorrow_plans: list[PlanItem] | None = None,
    issues: list[IssueItem] | None = None,
    task_list: TaskRecord | None = None,
    summary: str = "",
) -> DailyReport:
    """根据成员数据生成日报（design.md §4.2 生成层接口）。"""

    markdown = build_markdown(
        team_name, report_date, members,
        failed_sources=failed_sources, tomorrow_plans=tomorrow_plans,
        issues=issues, task_list=task_list, summary=summary,
    )
    html = render_report_html(
        team_name, report_date, members,
        failed_sources=failed_sources, tomorrow_plans=tomorrow_plans,
        issues=issues, task_list=task_list,
        report_title=report_title(members, team_name),
        show_member_names=len(members) > 1,
        summary=summary,
    )
    return DailyReport(
        date=report_date,
        team_name=team_name,
        members=members,
        generated_at=datetime.now(),
        markdown=markdown,
        html=html,
        failed_sources=list(failed_sources or []),
        tomorrow_plans=list(tomorrow_plans or []),
        issues=list(issues or []),
        task_list=task_list,
        summary=summary,
    )
