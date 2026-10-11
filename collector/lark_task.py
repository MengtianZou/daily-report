"""飞书任务采集模块（design.md §2、§4.1）。

通过 Task v2 接口列出项目内的任务，识别采集窗口内发生的状态变更：
- created_at 落在窗口内：(无) → 新建
- completed_at 落在窗口内：进行中 → 已完成
子任务不在清单列表中返回，须逐任务调用 subtasks 子接口获取；
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
    """返回 (status_from, status_to, event_time_ms)，无变更返回 None。

    同一窗口内先创建又完成时，以完成事件为准（对日报更有意义）。
    """

    created_at = int(task.get("created_at") or 0)
    completed_at = int(task.get("completed_at") or 0)

    if since_ms <= completed_at <= until_ms:
        return "进行中", "已完成", completed_at
    if since_ms <= created_at <= until_ms:
        return "(无)", "新建", created_at
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


def _fetch_task_tree(client: LarkClient, project_id: str) -> list[dict]:
    """拉取清单内全部顶层任务，并递归挂载子任务树（_children 字段）。"""

    def fetch_task(guid: str) -> dict:
        detail = client.request(
            "GET",
            f"/open-apis/task/v2/tasks/{guid}",
            params={"user_id_type": "open_id"},
        )
        task = detail.get("task", {})
        task["_guid"] = guid
        return task

    def fetch_children(guid: str) -> list[dict]:
        """子任务不在清单列表中返回，必须调用 subtasks 子接口。"""
        children: list[dict] = []
        for slim in client.paginate(
            f"/open-apis/task/v2/tasks/{guid}/subtasks",
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

    top_tasks = _fetch_task_tree(client, project_id)

    def has_window_change(task: dict) -> bool:
        """任务自身或其任一子孙在采集窗口内发生状态变更。"""
        if _detect_change(task, since_ms, until_ms) is not None:
            return True
        return any(has_window_change(child) for child in task.get("_children", []))

    def build_record(task: dict, *, is_child: bool) -> TaskRecord:
        change = _detect_change(task, since_ms, until_ms)
        if change is not None:
            status_from, status_to, event_time_ms = change
            return TaskRecord(
                assignee=_pick_assignee(task),
                title=task.get("summary", ""),
                status_from=status_from,
                status_to=status_to,
                updated_at=_from_milliseconds(event_time_ms),
                parent_guid=task.get("parent_guid") or "",
            )
        record = _snapshot_record(task)  # 窗口内无变更，仅展示当前状态
        if not is_child:
            record.parent_guid = ""  # 顶层快照不标父级
        return record

    def attach_subtasks(parent: TaskRecord, task: dict) -> None:
        """将某任务的全部子孙作为记录挂到父任务下（有变更记变更，无变更记快照）。"""
        for child in task.get("_children", []):
            sub = build_record(child, is_child=True)
            parent.subtasks.append(sub)
            attach_subtasks(sub, child)

    records: list[TaskRecord] = []
    for task in top_tasks:
        # 只有任务自身或子孙有今日变更时才进入日报
        if not has_window_change(task):
            continue
        record = build_record(task, is_child=False)
        attach_subtasks(record, task)
        records.append(record)

    return records


def collect_open(
    project_id: str,
    *,
    app_id_env: str = "LARK_APP_ID",
    app_secret_env: str = "LARK_APP_SECRET",
    base_url: str = LARK_BASE_URL,
    timeout: float = 10.0,
    lark_client: LarkClient | None = None,
) -> list[TaskRecord]:
    """获取清单中全部未完成（进行中）的顶层任务，供「明日计划」使用。

    与 collect 不同：不看采集窗口，只看当前状态快照；子任务一并挂载。
    """

    client = lark_client or LarkClient(
        os.environ.get(app_id_env, ""),
        os.environ.get(app_secret_env, ""),
        base_url=base_url,
        timeout=timeout,
    )

    top_tasks = _fetch_task_tree(client, project_id)

    records: list[TaskRecord] = []
    for task in top_tasks:
        if int(task.get("completed_at") or 0) > 0:
            continue  # 已完成，不进明日计划
        record = _snapshot_record(task)
        for child in task.get("_children", []):
            record.subtasks.append(_snapshot_record(child))
        records.append(record)
    return records
