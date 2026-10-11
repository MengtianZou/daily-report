"""DailyReport → 看板 JSON 序列化（design-v2 §3）。

输出字段名与 webapp/dashboard.html 的前端契约严格一致；
仅做结构转换与时间格式化，不涉及 HTML/模板。
"""

from __future__ import annotations

from datetime import datetime

from shared.models import DailyReport


def _hhmm(value: datetime) -> str:
    # 采集器产出的均为 UTC 感知时间，统一转换为运行环境本地时区后展示
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%H:%M")


def report_to_dict(report: DailyReport) -> dict:
    """将一份日报序列化为看板约定的字典结构。"""

    generated_at = report.generated_at
    members: list[dict] = []
    for member in report.members:
        github_username = member.github_username or None
        members.append(
            {
                "name": member.name,
                "github_username": github_username,
                "commits": [
                    {
                        "repo": c.repo,
                        "message": c.message,
                        "additions": c.additions,
                        "deletions": c.deletions,
                        "files_changed": c.files_changed,
                        "time": _hhmm(c.timestamp),
                    }
                    for c in member.commits
                ],
                "tasks": [
                    {
                        "title": t.title,
                        "status_from": t.status_from,
                        "status_to": t.status_to,
                        "time": _hhmm(t.updated_at),
                        "progress": f"{t.progress[0]}/{t.progress[1]}" if t.progress[1] > 0 else "",
                        "subtasks": [
                            {
                                "title": s.title,
                                "status_to": s.status_to,
                            }
                            for s in t.subtasks
                        ],
                    }
                    for t in member.tasks
                ],
                "messages": [
                    {
                        "chat_name": m.chat_name,
                        "content": m.content,
                        "time": _hhmm(m.timestamp),
                    }
                    for m in member.messages
                ],
            }
        )

    return {
        "date": report.date.isoformat(),
        "team_name": report.team_name,
        "generated_at": _hhmm(generated_at) if generated_at else "",
        "failed_sources": list(report.failed_sources),
        "members": members,
        "tomorrow_plans": [
            {
                "title": p.title,
                "owner": p.owner,
                "source": p.source,
                "id": p.plan_id,
            }
            for p in report.tomorrow_plans
        ],
        "issues": [
            {
                "problem": i.problem,
                "solution": i.solution,
                "owner": i.owner,
                "status": i.status,
                "source": i.source,
                "id": i.issue_id,
                "msg_key": i.msg_key,
            }
            for i in report.issues
        ],
    }
