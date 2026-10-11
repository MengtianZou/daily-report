"""shared/serializer.py 单元测试（design-v2 §3）。"""

from __future__ import annotations

from datetime import datetime, timezone

from dashboard_helpers import sample_report
from shared.models import TaskRecord
from shared.serializer import report_to_dict


def test_report_to_dict_matches_dashboard_contract() -> None:
    data = report_to_dict(sample_report())

    assert data["date"] == "2026-09-26"
    assert data["team_name"] == "研发一组"
    assert data["failed_sources"] == []
    assert data["generated_at"] == "18:02"  # UTC 10:02 → 本地 +8

    liu = data["members"][0]
    assert liu["name"] == "刘政剑"
    assert liu["github_username"] == "chenpingan6158-a11y"

    commit = liu["commits"][0]
    assert commit == {
        "repo": "usermanagersystem.github.io",
        "message": "新增日报采集模块",
        "additions": 214,
        "deletions": 32,
        "files_changed": 6,
        "time": "10:24",
    }

    task = liu["tasks"][0]
    assert task == {
        "title": "飞书任务采集",
        "status_from": "进行中",
        "status_to": "已完成",
        "time": "15:10",
        "progress": "",
        "subtasks": [],
    }

    message = liu["messages"][0]
    assert message == {
        "chat_name": "SDD测试",
        "content": "模板已联调通过",
        "time": "11:02",
    }


def test_tomorrow_plans_serialize_with_owner_and_source() -> None:
    data = report_to_dict(sample_report())

    assert data["tomorrow_plans"] == [
        {"title": "继续推进飞书任务采集", "owner": "刘政剑", "source": "auto", "id": 0},
        {"title": "整理周报", "owner": "", "source": "manual", "id": 7},
    ]


def test_issues_serialize_with_status_and_owner() -> None:
    data = report_to_dict(sample_report())

    assert data["issues"] == [
        {
            "problem": "GitHub 限流导致采集失败",
            "solution": "",
            "owner": "",
            "status": "处理中",
            "source": "auto",
            "id": 0,
            "msg_key": "ou_x|2026-09-26T10:00:00+00:00",
        },
        {
            "problem": "模板字段缺失",
            "solution": "补齐 data_json 字段",
            "owner": "刘政剑",
            "status": "已解决",
            "source": "manual",
            "id": 9,
            "msg_key": "",
        },
    ]


def test_empty_github_username_serializes_to_null() -> None:
    data = report_to_dict(sample_report())
    lisi = data["members"][1]
    assert lisi["github_username"] is None
    assert lisi["commits"] == []
    assert lisi["messages"][0]["content"] == "上线前跑 --check"
    assert lisi["messages"][0]["time"] == "17:50"


def test_failed_sources_are_carried() -> None:
    report = sample_report()
    report.failed_sources = ["github", "lark_message"]
    data = report_to_dict(report)
    assert data["failed_sources"] == ["github", "lark_message"]


def test_subtasks_serialize_with_status_and_progress() -> None:
    report = sample_report()
    report.members[0].tasks = [
        TaskRecord(
            assignee="ou_lzj",
            title="上线日报",
            status_from="进行中",
            status_to="已完成",
            updated_at=datetime(2026, 9, 26, 7, 10, tzinfo=timezone.utc),
            subtasks=[
                TaskRecord(
                    assignee="",
                    title="写采集",
                    status_from="",
                    status_to="已完成",
                    updated_at=datetime(2026, 9, 26, 7, 10, tzinfo=timezone.utc),
                ),
                TaskRecord(
                    assignee="",
                    title="写模板",
                    status_from="",
                    status_to="进行中",
                    updated_at=datetime(2026, 9, 26, 7, 10, tzinfo=timezone.utc),
                ),
            ],
        )
    ]
    data = report_to_dict(report)
    task = data["members"][0]["tasks"][0]

    assert task["progress"] == "1/2"
    assert task["subtasks"] == [
        {"title": "写采集", "status_to": "已完成"},
        {"title": "写模板", "status_to": "进行中"},
    ]
