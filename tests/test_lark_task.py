"""collector/lark_task.py 单元测试（Mock LarkClient）。"""

from __future__ import annotations

from datetime import datetime, timezone

from collector import lark_task
from shared.models import TaskRecord

_SINCE = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
_UNTIL = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)


class FakeLark:
    """paginate 返回精简任务（仅 guid），request 返回完整任务详情。"""

    def __init__(self, tasks: list[dict]) -> None:
        self._tasks = {t.get("guid", f"guid_{i}"): t for i, t in enumerate(tasks)}
        self.paths: list[str] = []

    def paginate(self, path: str, params: dict):
        self.paths.append(path)
        for guid, t in self._tasks.items():
            yield {"guid": guid, "summary": t.get("summary", "")}

    def request(self, method: str, path: str, *, params=None, json_body=None):
        guid = path.rsplit("/", 1)[-1]
        return {"task": self._tasks[guid]}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_collect_newly_created_task() -> None:
    task = {
        "summary": "实现日报模块",
        "created_at": str(_ms(datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc))),
        "completed_at": "0",
        "members": [{"id": "ou_zhangsan", "role": "assignee"}],
    }
    fake = FakeLark([task])
    records = lark_task.collect("proj_x", _SINCE, _UNTIL, lark_client=fake)

    # 请求路径必须携带清单 GUID，确保只采集该清单内的任务
    assert fake.paths == ["/open-apis/task/v2/tasklists/proj_x/tasks"]

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, TaskRecord)
    assert record.assignee == "ou_zhangsan"
    assert record.title == "实现日报模块"
    assert record.status_from == "(无)"
    assert record.status_to == "新建"


def test_collect_completed_task() -> None:
    task = {
        "summary": "修复缺陷",
        "created_at": str(_ms(datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))),
        "completed_at": str(_ms(datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc))),
        "members": [{"id": "ou_lisi", "role": "assignee"}],
    }
    fake = FakeLark([task])
    records = lark_task.collect("proj_x", _SINCE, _UNTIL, lark_client=fake)

    assert len(records) == 1
    assert records[0].status_from == "进行中"
    assert records[0].status_to == "已完成"
    assert records[0].updated_at == datetime(
        2026, 9, 25, 15, 0, tzinfo=timezone.utc
    )


def test_task_outside_window_is_ignored() -> None:
    task = {
        "summary": "昨天的任务",
        "created_at": str(_ms(datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))),
        "completed_at": "0",
        "members": [{"id": "ou_wangwu", "role": "assignee"}],
    }
    fake = FakeLark([task])
    assert lark_task.collect("proj_x", _SINCE, _UNTIL, lark_client=fake) == []


def test_assignee_falls_back_to_first_member() -> None:
    task = {
        "summary": "无角色任务",
        "created_at": str(_ms(datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))),
        "completed_at": "0",
        "members": [{"id": "ou_creator", "role": "creator"}],
    }
    fake = FakeLark([task])
    records = lark_task.collect("proj_x", _SINCE, _UNTIL, lark_client=fake)
    assert records[0].assignee == "ou_creator"


def test_collect_parent_attaches_all_subtasks_with_progress() -> None:
    # 父任务今日完成；子任务分别今日完成、昨日完成、进行中。
    tasks = [
        {
            "guid": "g_parent",
            "summary": "上线日报",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": str(_ms(datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc))),
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_sub1",
            "summary": "写采集",
            "parent_guid": "g_parent",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": str(_ms(datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))),
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_sub2",
            "summary": "写模板",
            "parent_guid": "g_parent",
            "created_at": str(_ms(datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc))),
            "completed_at": str(_ms(datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))),
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_sub3",
            "summary": "写测试",
            "parent_guid": "g_parent",
            "created_at": str(_ms(datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc))),
            "completed_at": "0",
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
    ]
    fake = FakeLark(tasks)
    records = lark_task.collect("proj_x", _SINCE, _UNTIL, lark_client=fake)

    assert len(records) == 1
    record = records[0]
    assert record.title == "上线日报"
    assert record.progress == (2, 3)  # 两个已完成 + 一个进行中
    assert [s.title for s in record.subtasks] == ["写采集", "写模板", "写测试"]
    assert [s.status_to for s in record.subtasks] == ["已完成", "已完成", "进行中"]


def test_collect_parent_surfaces_when_only_subtask_changed() -> None:
    # 父任务今日无变更，但子任务今日完成，父任务仍应出现在日报中。
    tasks = [
        {
            "guid": "g_parent",
            "summary": "上线日报",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": "0",
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_sub1",
            "summary": "写采集",
            "parent_guid": "g_parent",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": str(_ms(datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))),
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
    ]
    fake = FakeLark(tasks)
    records = lark_task.collect("proj_x", _SINCE, _UNTIL, lark_client=fake)

    assert len(records) == 1
    record = records[0]
    assert record.title == "上线日报"
    assert record.status_from == ""  # 父任务今日无变更，仅展示当前状态
    assert record.status_to == "进行中"
    assert record.progress == (1, 1)
