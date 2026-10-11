"""collector/lark_task.py 单元测试（Mock LarkClient）。"""

from __future__ import annotations

from datetime import datetime, timezone

from collector import lark_task
from shared.models import TaskRecord

_SINCE = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
_UNTIL = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)


class FakeLark:
    """模拟真实飞书 API：清单只返回顶层任务，子任务走 subtasks 子接口。"""

    def __init__(self, tasks: list[dict]) -> None:
        self._tasks = {t.get("guid", f"guid_{i}"): t for i, t in enumerate(tasks)}
        self.paths: list[str] = []

    def paginate(self, path: str, params: dict):
        self.paths.append(path)
        if path.endswith("/subtasks"):
            parent_guid = path.split("/")[-2]
            for guid, t in self._tasks.items():
                if t.get("parent_guid") == parent_guid:
                    yield {"guid": guid, "summary": t.get("summary", "")}
        else:  # 清单任务列表：仅顶层任务
            for guid, t in self._tasks.items():
                if not t.get("parent_guid"):
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

    # 首个请求路径必须携带清单 GUID；子任务通过 subtasks 子接口获取
    assert fake.paths[0] == "/open-apis/task/v2/tasklists/proj_x/tasks"
    assert any(p.endswith("/subtasks") for p in fake.paths[1:])

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


def test_collect_open_returns_pending_tasks_with_subtasks() -> None:
    # 未完成父任务返回（含子任务进度）；已完成任务被排除。
    tasks = [
        {
            "guid": "g_open",
            "summary": "上线日报",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": "0",
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_sub1",
            "summary": "写采集",
            "parent_guid": "g_open",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": str(_ms(datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))),
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_sub2",
            "summary": "写模板",
            "parent_guid": "g_open",
            "created_at": str(_ms(datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc))),
            "completed_at": "0",
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
        {
            "guid": "g_done",
            "summary": "已完成的任务",
            "created_at": str(_ms(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))),
            "completed_at": str(_ms(datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc))),
            "members": [{"id": "ou_zhangsan", "role": "assignee"}],
        },
    ]
    fake = FakeLark(tasks)
    records = lark_task.collect_open("proj_x", lark_client=fake)

    assert [r.title for r in records] == ["上线日报"]
    record = records[0]
    assert record.status_from == ""
    assert record.status_to == "进行中"
    assert record.progress == (1, 2)
    assert [s.title for s in record.subtasks] == ["写采集", "写模板"]
