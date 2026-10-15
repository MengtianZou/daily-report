"""collector/lark_task.py 单元测试（Mock LarkClient，含写操作）。"""

from __future__ import annotations

from datetime import date

from collector import lark_task
from shared.models import TaskRecord


class FakeLark:
    """模拟真实飞书 API：清单/子任务查询 + 建任务/建子任务/完成任务写操作。"""

    def __init__(self, tasks: list[dict] | None = None) -> None:
        self._tasks: dict[str, dict] = {}
        for i, t in enumerate(tasks or []):
            guid = t.get("guid", f"guid_{i}")
            self._tasks[guid] = dict(t, guid=guid)
        self._seq = 0
        self.calls: list[tuple[str, str]] = []  # (method, path) 审计

    def paginate(self, path: str, params: dict):
        self.calls.append(("PAGINATE", path))
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
        self.calls.append((method, path))
        if method == "GET":
            guid = path.rsplit("/", 1)[-1]
            return {"task": self._tasks[guid]}
        if method == "POST":
            summary = (json_body or {}).get("summary", "")
            self._seq += 1
            if path.endswith("/subtasks") or "/subtasks" in path:
                parent_guid = path.split("/")[-2]
                guid = f"new_sub_{self._seq}"
                self._tasks[guid] = {
                    "guid": guid,
                    "summary": summary,
                    "parent_guid": parent_guid,
                    "completed_at": "0",
                }
            else:  # 创建顶层任务（挂清单）
                guid = f"new_parent_{self._seq}"
                self._tasks[guid] = {
                    "guid": guid,
                    "summary": summary,
                    "completed_at": "0",
                }
            return {"task": {"guid": guid, "summary": summary}}
        if method == "PATCH":
            guid = path.rsplit("/", 1)[-1]
            fields = (json_body or {}).get("task", {})
            if "completed_at" in fields:
                self._tasks[guid]["completed_at"] = str(fields["completed_at"])
            return {"task": self._tasks[guid]}
        raise AssertionError(f"unexpected {method} {path}")

    def guids_by_summary(self, summary: str) -> list[str]:
        return [g for g, t in self._tasks.items() if t.get("summary") == summary]


_DAY = date(2026, 10, 13)


def test_tasklist_name_format() -> None:
    assert lark_task._tasklist_name(_DAY) == "10月13日任务清单"
    assert lark_task._tasklist_name(date(2026, 9, 5)) == "9月5日任务清单"


def test_fetch_daily_tasklist_hit_returns_snapshot_tree() -> None:
    fake = FakeLark([
        {
            "guid": "g_parent",
            "summary": "10月13日任务清单",
            "created_at": "1760000000000",
            "completed_at": "0",
        },
        {
            "guid": "g_sub1",
            "summary": "写周报",
            "parent_guid": "g_parent",
            "created_at": "1760000000000",
            "completed_at": "1760000100000",
        },
        {
            "guid": "g_sub2",
            "summary": "写日报",
            "parent_guid": "g_parent",
            "created_at": "1760000000000",
            "completed_at": "0",
        },
    ])
    record = lark_task.fetch_daily_tasklist(fake, "proj_x", _DAY)

    assert isinstance(record, TaskRecord)
    assert record.title == "10月13日任务清单"
    assert record.guid == "g_parent"
    assert record.parent_guid == ""  # 顶层清单不标父级
    assert record.status_to == "进行中"
    assert record.progress == (1, 2)
    assert [s.title for s in record.subtasks] == ["写周报", "写日报"]
    assert [s.status_to for s in record.subtasks] == ["已完成", "进行中"]
    assert [s.guid for s in record.subtasks] == ["g_sub1", "g_sub2"]


def test_fetch_daily_tasklist_miss_returns_none() -> None:
    fake = FakeLark([{"summary": "别的任务", "completed_at": "0"}])
    assert lark_task.fetch_daily_tasklist(fake, "proj_x", _DAY) is None


def test_create_daily_tasklist_creates_parent_and_subtasks() -> None:
    fake = FakeLark([])
    guid = lark_task.create_daily_tasklist(fake, "proj_x", _DAY, ["写周报", "写日报"])

    assert guid.startswith("new_parent_")
    creates = [(m, p) for m, p in fake.calls if m == "POST"]
    # 首个 POST 为父任务，且通过 tasklists 字段挂入目标清单
    assert creates[0][1] == "/open-apis/task/v2/tasks"
    # 子任务走 subtasks 子接口
    assert sum(1 for _, p in creates if p.endswith("/subtasks")) == 2

    # 建完即可按名查到，子任务齐全
    record = lark_task.fetch_daily_tasklist(fake, "proj_x", _DAY)
    assert record is not None and record.title == "10月13日任务清单"
    assert [s.title for s in record.subtasks] == ["写周报", "写日报"]


def test_add_subtasks_posts_each_title() -> None:
    fake = FakeLark([{
        "guid": "g_parent",
        "summary": "10月13日任务清单",
        "completed_at": "0",
    }])
    guids = lark_task.add_subtasks(fake, "g_parent", ["写周报", "复盘"])

    assert len(guids) == 2 and all(g.startswith("new_sub_") for g in guids)
    posts = [(m, p) for m, p in fake.calls if m == "POST"]
    assert all(p.endswith("/subtasks") for _, p in posts)
    assert [fake._tasks[g]["summary"] for g in guids] == ["写周报", "复盘"]


def test_complete_task_patches_completed_at() -> None:
    fake = FakeLark([{
        "guid": "g_sub",
        "summary": "写周报",
        "parent_guid": "g_parent",
        "completed_at": "0",
    }])
    assert lark_task.complete_task(fake, "g_sub") is True

    patches = [(m, p) for m, p in fake.calls if m == "PATCH"]
    assert patches == [("PATCH", "/open-apis/task/v2/tasks/g_sub")]
    # 完成后快照状态变为已完成
    record = lark_task._snapshot_record(fake._tasks["g_sub"])
    assert record.status_to == "已完成"
