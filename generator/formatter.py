"""数据整理与日报生成（design.md §2、§4.2）。

- aggregate：按成员身份映射将三个数据源的记录聚合到人；
- build_markdown：生成三段式 Markdown；
- generate：组装 DailyReport（Markdown + HTML）。
"""

from __future__ import annotations

from datetime import date, datetime

from generator.template import render_report_html
from shared.models import (
    CommitRecord,
    DailyReport,
    MemberReport,
    MessageRecord,
    TaskRecord,
)

# 数据源标识 -> 板块中文名
_SOURCE_TITLES = {
    "github": "代码提交",
    "lark_task": "任务进展",
    "lark_message": "协作沟通",
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
            lines.append(
                f"- `{c.repo}` {c.message}（+{c.additions}/-{c.deletions}，"
                f"{c.files_changed} 文件，{_hhmm(c.timestamp)}）"
            )
    elif kind == "lark_task":
        for t in records:
            if t.status_from:
                lines.append(
                    f"- {t.status_from} → **{t.status_to}**：{t.title}"
                    f"（{_hhmm(t.updated_at)}）"
                )
            else:
                # 父任务今日无变更，仅因子任务变更进入日报，展示当前状态即可
                lines.append(f"- **{t.status_to}**：{t.title}")
            # 子任务内容与完成程度
            completed, total = t.progress
            if total > 0:
                lines.append(f"  - 子任务进度：**{completed}/{total}** 已完成")
                for sub in t.subtasks:
                    status_icon = "✅" if sub.status_to == "已完成" else "⬜"
                    lines.append(f"    - {status_icon} {sub.title}（{sub.status_to}）")
    else:
        for m in records:
            lines.append(f"- [{m.chat_name}] {m.content}（{_hhmm(m.timestamp)}）")
    return lines


def build_markdown(
    team_name: str,
    report_date: date,
    members: list[MemberReport],
    failed_sources: list[str] | None = None,
) -> str:
    """生成完整 Markdown 日报。"""

    failed = set(failed_sources or [])
    lines: list[str] = [
        f"# {team_name} 工作日报（{report_date.isoformat()}）",
        "",
    ]
    if failed:
        labels = "、".join(_SOURCE_TITLES[s] for s in failed if s in _SOURCE_TITLES)
        lines.extend(
            [f"> ⚠️ 以下数据源获取失败：{labels}", ""]
        )

    for member in members:
        lines.extend([f"## {member.name}", ""])
        lines.append("### 代码提交")
        lines.extend(_render_section(member.commits, "github" in failed, "github"))
        lines.append("")
        lines.append("### 任务进展")
        lines.extend(_render_section(member.tasks, "lark_task" in failed, "lark_task"))
        lines.append("")
        lines.append("### 协作沟通")
        lines.extend(
            _render_section(member.messages, "lark_message" in failed, "lark_message")
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate(
    members: list[MemberReport],
    report_date: date,
    team_name: str,
    *,
    failed_sources: list[str] | None = None,
) -> DailyReport:
    """根据成员数据生成日报（design.md §4.2 生成层接口）。"""

    markdown = build_markdown(
        team_name, report_date, members, failed_sources=failed_sources
    )
    html = render_report_html(
        team_name, report_date, members, failed_sources=failed_sources
    )
    return DailyReport(
        date=report_date,
        team_name=team_name,
        members=members,
        generated_at=datetime.now(),
        markdown=markdown,
        html=html,
        failed_sources=list(failed_sources or []),
    )
