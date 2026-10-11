"""shared/storage.py 单元测试。"""

from __future__ import annotations

from datetime import datetime

from shared.models import DailyReport
from shared.storage import ReportStorage


def _make_report(day: str = "2026-09-25") -> DailyReport:
    return DailyReport(
        date=datetime.strptime(day, "%Y-%m-%d").date(),
        team_name="测试团队",
        generated_at=datetime(2026, 9, 25, 18, 0, 0),
        markdown="# 日报",
        html="<h1>日报</h1>",
    )


def test_save_and_get_report(tmp_path) -> None:
    storage = ReportStorage(tmp_path / "reports.db")
    storage.save_report(_make_report())

    row = storage.get_report("2026-09-25", "测试团队")
    assert row is not None
    assert row["markdown"] == "# 日报"
    assert row["html"] == "<h1>日报</h1>"
    storage.close()


def test_upsert_same_day(tmp_path) -> None:
    db_path = tmp_path / "reports.db"
    storage = ReportStorage(db_path)

    report = _make_report()
    storage.save_report(report)

    report.markdown = "# 更新后的日报"
    storage.save_report(report)

    rows = storage.list_reports()
    assert len(rows) == 1
    row = storage.get_report("2026-09-25", "测试团队")
    assert row is not None and row["markdown"] == "# 更新后的日报"
    storage.close()


def test_get_missing_report_returns_none(tmp_path) -> None:
    storage = ReportStorage(tmp_path / "reports.db")
    assert storage.get_report("2026-01-01", "不存在") is None
    storage.close()


def test_context_manager(tmp_path) -> None:
    with ReportStorage(tmp_path / "reports.db") as storage:
        storage.save_report(_make_report())
        assert len(storage.list_reports()) == 1


def test_save_iter_delete_plan(tmp_path) -> None:
    with ReportStorage(tmp_path / "reports.db") as storage:
        storage.save_plan("整理周报", "刘政剑", "2026-09-26")
        storage.save_plan("准备评审", "", "2026-09-26")
        storage.save_plan("无关日期", "李四", "2026-09-27")

        rows = storage.iter_plans("2026-09-26")
        assert [r["title"] for r in rows] == ["整理周报", "准备评审"]
        assert rows[0]["owner"] == "刘政剑"
        assert rows[1]["owner"] == ""

        assert storage.delete_plan(rows[0]["id"]) is True
        assert storage.delete_plan(rows[0]["id"]) is False  # 幂等：不存在返回 False
        assert [r["title"] for r in storage.iter_plans("2026-09-26")] == ["准备评审"]


def test_save_iter_update_delete_issue(tmp_path) -> None:
    with ReportStorage(tmp_path / "reports.db") as storage:
        storage.save_issue("接口报错", "已定位修复", "刘政剑", "2026-09-25")
        storage.save_issue("环境卡顿", "", "", "2026-09-25")
        storage.save_issue("无关日期", "", "李四", "2026-09-26", status="已解决")

        rows = storage.iter_issues("2026-09-25")
        assert [r["problem"] for r in rows] == ["接口报错", "环境卡顿"]
        assert rows[0]["solution"] == "已定位修复"
        assert rows[0]["status"] == "处理中"
        assert rows[1]["owner"] == ""

        # 状态更新与幂等
        assert storage.update_issue_status(rows[1]["id"], "已解决") is True
        assert storage.update_issue_status(rows[1]["id"], "不存在状态") is True  # SQL 层不校验，接口层校验
        assert storage.update_issue_status(99999, "已解决") is False

        # 删除与幂等
        assert storage.delete_issue(rows[0]["id"]) is True
        assert storage.delete_issue(rows[0]["id"]) is False
        assert [r["problem"] for r in storage.iter_issues("2026-09-25")] == ["环境卡顿"]


def test_issues_msg_key_roundtrip_and_legacy_migration(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "reports.db"
    # 模拟一期旧库：issues 表无 msg_key 列
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE issues ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " issue_date TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',"
        " problem TEXT NOT NULL, solution TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL DEFAULT '处理中', created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO issues (issue_date, owner, problem, created_at) "
        "VALUES ('2026-09-25', '刘政剑', '旧问题', '2026-09-25T18:00:00')"
    )
    conn.commit()
    conn.close()

    with ReportStorage(db_path) as storage:
        # 迁移后旧数据 msg_key 默认空串
        legacy = storage.iter_issues("2026-09-25")
        assert legacy[0]["msg_key"] == ""

        # 新写入携带 msg_key 并可回读
        storage.save_issue(
            "接口报错", "已定位", "刘政剑", "2026-09-25",
            msg_key="ou_x|2026-09-25T10:00:00+00:00",
        )
        rows = storage.iter_issues("2026-09-25")
        assert rows[1]["msg_key"] == "ou_x|2026-09-25T10:00:00+00:00"


def test_update_report_data_roundtrip_and_guards(tmp_path) -> None:
    with ReportStorage(tmp_path / "reports.db") as storage:
        storage.save_report(_make_report())

        # 正常回写
        data = {"date": "2026-09-25", "team_name": "测试团队", "issues": [{"problem": "x"}]}
        assert storage.update_report_data("2026-09-25", "测试团队", data) is True
        import json
        row = storage.get_report("2026-09-25", "测试团队")
        assert json.loads(row["data_json"])["issues"] == [{"problem": "x"}]

        # 记录不存在 → False
        assert storage.update_report_data("2026-01-01", "无", data) is False

        # data_json 为空的旧记录 → False（跳过）
        storage._conn.execute(
            "UPDATE reports SET data_json = '' WHERE date = '2026-09-25'"
        )
        storage._conn.commit()
        assert storage.update_report_data("2026-09-25", "测试团队", data) is False
