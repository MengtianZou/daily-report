"""飞书任务采集与任务清单写入模块（design.md §2、§4.1）。

职责分两块：
- 采集：按名称「M月D日任务清单」查找当日任务清单，返回含子任务完成进度的快照树；
- 写入：为明日计划自动创建当日任务清单（父任务+子任务），并支持按 guid 完成任务。

子任务不在清单列表中返回，须逐任务调用 subtasks 子接口获取；
飞书 token 失效时由 LarkClient 自动刷新并重试。
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from typing import Any

from collector._lark_client import LARK_BASE_URL, LarkClient
from shared.models import TaskRecord

_TASKLIST_TASKS_PATH = "/open-apis/task/v2/tasklists/{guid}/tasks"
_TASKS_PATH = "/open-apis/task/v2/tasks"
_SUBTASKS_PATH = "/open-apis/task/v2/tasks/{guid}/subtasks"


def _tasklist_name(day: date) -> str:
    """当日任务清单的固定命名：M月D日任务清单。"""

    return f"{day.month}月{day.day}日任务清单"


def _from_milliseconds(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _pick_assignee(task: dict) -> str:
    members = task.get("members", [])
    for member in members:
        if member.get("role") == "assignee":
            return member.get("id", "")
    return members[0].get("id", "") if members else ""


def _current_status(task: dict) -> str:
    """任务当前状态：已完成 / 进行中。"""
    return "已完成" if int(task.get("completed_at") or 0) > 0 else "进行中"


def _reference_time(task: dict) -> int:
    """快照任务的展示时间戳：优先完成时间，其次更新时间，最后创建时间。"""
    for key in ("completed_at", "updated_at", "created_at"):
        value = int(task.get(key) or 0)
        if value > 0:
            return value
    return 0


def _snapshot_record(task: dict) -> TaskRecord:
    """构建任务当前状态快照（status_from 为空表示非今日变更）。"""
    return TaskRecord(
        assignee=_pick_assignee(task),
        title=task.get("summary", ""),
        status_from="",
        status_to=_current_status(task),
        updated_at=_from_milliseconds(_reference_time(task)),
        parent_guid=task.get("parent_guid") or "",
        guid=task.get("_guid", ""),
    )


def _fetch_task_tree(client: LarkClient, project_id: str) -> list[dict]:
    """拉取清单内全部顶层任务，并递归挂载子任务树（_children 字段）。"""

    def fetch_task(guid: str) -> dict:
        detail = client.request(
            "GET",
            f"{_TASKS_PATH}/{guid}",
            params={"user_id_type": "open_id"},
        )
        task = detail.get("task", {})
        task["_guid"] = guid
        return task

    def fetch_children(guid: str) -> list[dict]:
        """子任务不在清单列表中返回，必须调用 subtasks 子接口。"""
        children: list[dict] = []
        for slim in client.paginate(
            _SUBTASKS_PATH.format(guid=guid),
            {"page_size": 50, "user_id_type": "open_id"},
        ):
            child = fetch_task(slim["guid"])
            child["_children"] = fetch_children(child["_guid"])
            children.append(child)
        return children

    top_tasks: list[dict] = []
    for slim in client.paginate(
        _TASKLIST_TASKS_PATH.format(guid=project_id),
        {"page_size": 50, "user_id_type": "open_id"},
    ):
        task = fetch_task(slim["guid"])
        task["_children"] = fetch_children(task["_guid"])
        top_tasks.append(task)
    return top_tasks


def _attach_snapshots(parent: TaskRecord, task: dict) -> None:
    """递归把全部子孙任务的状态快照挂到父记录下。"""

    for child in task.get("_children", []):
        sub = _snapshot_record(child)
        parent.subtasks.append(sub)
        _attach_snapshots(sub, child)


def fetch_daily_tasklist(
    client: LarkClient,
    project_id: str,
    day: date,
) -> TaskRecord | None:
    """按名称「M月D日任务清单」查找当日任务清单。

    返回带子任务进度快照的记录；不存在返回 None。
    """

    name = _tasklist_name(day)
    for task in _fetch_task_tree(client, project_id):
        if task.get("summary") != name:
            continue
        record = _snapshot_record(task)
        record.parent_guid = ""  # 顶层清单不标父级
        _attach_snapshots(record, task)
        return record
    return None


def add_subtasks(client: LarkClient, parent_guid: str, titles: list[str]) -> list[str]:
    """为父任务逐条创建子任务，返回新建子任务 guid 列表。"""

    guids: list[str] = []
    for title in titles:
        data = client.request(
            "POST",
            _SUBTASKS_PATH.format(guid=parent_guid),
            params={"user_id_type": "open_id"},
            json_body={"summary": title},
        )
        guids.append(data.get("task", {}).get("guid", ""))
    return guids


def create_daily_tasklist(
    client: LarkClient,
    project_id: str,
    day: date,
    titles: list[str],
) -> str:
    """创建「M月D日任务清单」父任务并逐条挂载子任务，返回父任务 guid。

    父任务通过 tasklists 字段直接挂入目标清单；子任务经 subtasks 接口创建。
    """

    data = client.request(
        "POST",
        _TASKS_PATH,
        params={"user_id_type": "open_id"},
        json_body={
            "summary": _tasklist_name(day),
            "tasklists": [{"tasklist_guid": project_id}],
        },
    )
    parent_guid = data.get("task", {}).get("guid", "")
    if not parent_guid:
        return ""
    add_subtasks(client, parent_guid, titles)
    return parent_guid


def complete_task(client: LarkClient, guid: str) -> bool:
    """完成任务：PATCH completed_at 为当前毫秒时间戳（Task v2 更新约定）。

    成功返回 True；失败抛 CollectorError，由调用方决定是否重试。
    """

    data = client.request(
        "PATCH",
        f"{_TASKS_PATH}/{guid}",
        params={"user_id_type": "open_id"},
        json_body={
            "task": {"completed_at": int(time.time() * 1000)},
            "update_fields": ["completed_at"],
        },
    )
    return bool(int(data.get("task", {}).get("completed_at") or 0))
