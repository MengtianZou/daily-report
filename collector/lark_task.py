"""飞书任务采集模块（design.md §2、§4.1）。

通过 Task v2 接口列出项目内的任务，识别采集窗口内发生的状态变更：
- created_at 落在窗口内：(无) → 新建
- completed_at 落在窗口内：进行中 → 已完成
飞书 token 失效时由 LarkClient 自动刷新并重试。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from collector._lark_client import LARK_BASE_URL, LarkClient
from shared.models import TaskRecord

_TASKLIST_TASKS_PATH = "/open-apis/task/v2/tasklists/{guid}/tasks"


def _to_milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _from_milliseconds(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _pick_assignee(task: dict) -> str:
    members = task.get("members", [])
    for member in members:
        if member.get("role") == "assignee":
            return member.get("id", "")
    return members[0].get("id", "") if members else ""


def _detect_change(
    task: dict,
    since_ms: int,
    until_ms: int,
) -> tuple[str, str, int] | None:
    """返回 (status_from, status_to, event_time_ms)，无变更返回 None。"""

    created_at = int(task.get("created_at") or 0)
    completed_at = int(task.get("completed_at") or 0)

    if since_ms <= created_at <= until_ms:
        return "(无)", "新建", created_at
    if since_ms <= completed_at <= until_ms:
        return "进行中", "已完成", completed_at
    return None


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
    """为子任务构建“当前状态”快照（非今日变更，故 status_from 为空）。"""
    return TaskRecord(
        assignee=_pick_assignee(task),
        title=task.get("summary", ""),
        status_from="",
        status_to=_current_status(task),
        updated_at=_from_milliseconds(_reference_time(task)),
        parent_guid=task.get("parent_guid") or "",
    )


def collect(
    project_id: str,
    since: datetime,
    until: datetime,
    *,
    app_id_env: str = "LARK_APP_ID",
    app_secret_env: str = "LARK_APP_SECRET",
    base_url: str = LARK_BASE_URL,
    timeout: float = 10.0,
    lark_client: LarkClient | None = None,
) -> list[TaskRecord]:
    """获取指定项目中在 [since, until] 内发生状态变更的任务记录（含子任务）。"""

    client = lark_client or LarkClient(
        os.environ.get(app_id_env, ""),
        os.environ.get(app_secret_env, ""),
        base_url=base_url,
        timeout=timeout,
    )

    since_ms = _to_milliseconds(since)
    until_ms = _to_milliseconds(until)

    # 先收集所有任务（含子任务），并按 parent_guid 建立父子关系
    all_tasks: list[dict] = []
    for slim in client.paginate(
        _TASKLIST_TASKS_PATH.format(guid=project_id),
        {"page_size": 50, "user_id_type": "open_id"},
    ):
        detail = client.request(
            "GET",
            f"/open-apis/task/v2/tasks/{slim['guid']}",
            params={"user_id_type": "open_id"},
        )
        task = detail.get("task", slim)
        task["_guid"] = slim["guid"]
        all_tasks.append(task)

    tasks_by_guid: dict[str, dict] = {t["_guid"]: t for t in all_tasks}
    children_map: dict[str, list[dict]] = {}
    for task in all_tasks:
        parent_guid = task.get("parent_guid") or ""
        if parent_guid and parent_guid in tasks_by_guid:
            children_map.setdefault(parent_guid, []).append(task)

    def has_window_change(task: dict) -> bool:
        """任务自身或其任一子孙在采集窗口内发生状态变更。"""
        if _detect_change(task, since_ms, until_ms) is not None:
            return True
        return any(
            has_window_change(child)
            for child in children_map.get(task["_guid"], [])
        )

    def attach_subtasks(parent: TaskRecord, guid: str) -> None:
        """将某任务的全部子孙作为“当前状态”快照挂到父任务下。"""
        for child in children_map.get(guid, []):
            sub = _snapshot_record(child)
            parent.subtasks.append(sub)
            attach_subtasks(sub, child["_guid"])

    records: list[TaskRecord] = []

    # 处理顶层任务（无 parent_guid 或 parent_guid 不在清单内）
    for task in all_tasks:
        parent_guid = task.get("parent_guid") or ""
        if parent_guid and parent_guid in tasks_by_guid:
            continue  # 子任务，统一挂到其顶层父任务下呈现

        # 只有任务自身或子孙有今日变更时才进入日报
        if not has_window_change(task):
            continue

        change = _detect_change(task, since_ms, until_ms)
        if change is None:
            record = _snapshot_record(task)  # 父任务今日无变更，仅展示当前状态
        else:
            status_from, status_to, event_time_ms = change
            record = TaskRecord(
                assignee=_pick_assignee(task),
                title=task.get("summary", ""),
                status_from=status_from,
                status_to=status_to,
                updated_at=_from_milliseconds(event_time_ms),
                parent_guid="",
            )

        attach_subtasks(record, task["_guid"])
        records.append(record)

    return records
