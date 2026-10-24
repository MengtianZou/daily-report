"""二期测试共用：构建含成员明细的 DailyReport。"""

from __future__ import annotations

from datetime import datetime, timezone

from shared.models import (
    CommitRecord,
    DailyReport,
    IssueItem,
    MemberReport,
    MessageRecord,
    PlanItem,
    TaskRecord,
)

_UTC = timezone.utc


def sample_report(day: str = "2026-09-26") -> DailyReport:
    """一份两成员、三源齐全的日报（时间均为 UTC 感知）。"""

    report_date = datetime.strptime(day, "%Y-%m-%d").date()

    liu = MemberReport(
        name="刘政剑",
        github_username="chenpingan6158-a11y",
        commits=[
            CommitRecord(
                author="chenpingan6158-a11y",
                message="新增日报采集模块",
                timestamp=datetime(2026, 9, 26, 2, 24, tzinfo=_UTC),
                repo="usermanagersystem.github.io",
                additions=214,
                deletions=32,
                files_changed=6,
            )
        ],
        tasks=[
            TaskRecord(
                assignee="ou_lzj",
                title="飞书任务采集",
                status_from="进行中",
                status_to="已完成",
                updated_at=datetime(2026, 9, 26, 7, 10, tzinfo=_UTC),
            )
        ],
        messages=[
            MessageRecord(
                sender="ou_lzj",
                content="模板已调试验通过",
                timestamp=datetime(2026, 9, 26, 3, 2, tzinfo=_UTC),
                chat_name="SDD测试",
            )
        ],
    )

    lisi = MemberReport(
        name="李四",
        github_username="",
        commits=[],
        tasks=[],
        messages=[
            MessageRecord(
                sender="ou_cpa",
                content="上线前跑 --check",
                timestamp=datetime(2026, 9, 26, 9, 50, tzinfo=_UTC),
                chat_name="SDD测试",
            )
        ],
    )

    return DailyReport(
        date=report_date,
        team_name="研发一组",
        members=[liu, lisi],
        generated_at=datetime(2026, 9, 26, 10, 2, tzinfo=_UTC),
        markdown="# md",
        html="<p>html</p>",
        failed_sources=[],
        tomorrow_plans=[
            PlanItem(title="继续推进飞书任务采集", owner="刘政剑", source="auto"),
            PlanItem(title="整理周报", owner="", source="manual", plan_id=7),
        ],
        issues=[
            IssueItem(problem="GitHub 限流导致采集失败", source="auto",
                      msg_key="ou_x|2026-09-26T10:00:00+00:00"),
            IssueItem(problem="模板字段缺失", solution="补齐 data_json 字段",
                      owner="刘政剑", status="已解决", source="manual", issue_id=9),
        ],
    )
