"""SQLite 日报历史存储（ADR-002，design.md §2 shared/storage.py）。

每天仅由定时任务写入一次，无并发写入场景，故使用标准库 sqlite3 即可。
同一 (日期, 团队) 重复写入时执行 upsert。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from shared.models import DailyReport
from shared.serializer import report_to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    team_name    TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    markdown     TEXT NOT NULL,
    html         TEXT NOT NULL,
    UNIQUE (date, team_name)
);
"""

_PLANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date  TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT '待完成',
    msg_key    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

_ISSUES_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_date TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    problem    TEXT NOT NULL,
    solution   TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '处理中',
    msg_key    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ReportStorage:
    """日报历史的 SQLite 存取。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.execute(_PLANS_SCHEMA)
        self._conn.execute(_ISSUES_SCHEMA)
        self._conn.execute(_USERS_SCHEMA)
        self._conn.execute(_META_SCHEMA)
        self._migrate_data_json()
        self._migrate_issues_msg_key()
        self._migrate_plans_v2()
        self._conn.commit()

    def _migrate_data_json(self) -> None:
        """对一期库幂等添加 data_json 列（design-v2 §4）。"""

        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(reports)")
        }
        if "data_json" not in columns:
            self._conn.execute(
                "ALTER TABLE reports ADD COLUMN data_json TEXT NOT NULL DEFAULT ''"
            )

    def _migrate_issues_msg_key(self) -> None:
        """对一期库幂等添加 issues.msg_key 列（认领补充方案功能）。"""

        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(issues)")
        }
        if "msg_key" not in columns:
            self._conn.execute(
                "ALTER TABLE issues ADD COLUMN msg_key TEXT NOT NULL DEFAULT ''"
            )

    def _migrate_plans_v2(self) -> None:
        """对一期库幂等添加 plans.status / plans.msg_key 列（群消息任务闭环）。"""

        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(plans)")
        }
        if "status" not in columns:
            self._conn.execute(
                "ALTER TABLE plans ADD COLUMN status TEXT NOT NULL DEFAULT '待完成'"
            )
        if "msg_key" not in columns:
            self._conn.execute(
                "ALTER TABLE plans ADD COLUMN msg_key TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReportStorage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save_report(self, report: DailyReport) -> None:
        """写入或更新一条日报记录。"""

        generated_at = report.generated_at or datetime.now()
        data_json = json.dumps(
            report_to_dict(report), ensure_ascii=False
        )
        self._conn.execute(
            """
            INSERT INTO reports
                (date, team_name, generated_at, markdown, html, data_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, team_name) DO UPDATE SET
                generated_at = excluded.generated_at,
                markdown     = excluded.markdown,
                html         = excluded.html,
                data_json    = excluded.data_json
            """,
            (
                report.date.isoformat(),
                report.team_name,
                generated_at.isoformat(),
                report.markdown,
                report.html,
                data_json,
            ),
        )
        self._conn.commit()

    def get_report(self, date_iso: str, team_name: str) -> dict | None:
        """按日期与团队名查询日报。"""

        row = self._conn.execute(
            "SELECT * FROM reports WHERE date = ? AND team_name = ?",
            (date_iso, team_name),
        ).fetchone()
        return dict(row) if row else None

    def list_reports(self, limit: int = 30) -> list[dict]:
        """按日期倒序列出日报历史。"""

        rows = self._conn.execute(
            "SELECT id, date, team_name, generated_at FROM reports "
            "ORDER BY date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def iter_report_data(self, limit: int = 30) -> list[dict]:
        """返回具备结构化数据的日报字典列表（看板历史来源，design-v2 §4）。"""

        rows = self._conn.execute(
            "SELECT data_json FROM reports WHERE data_json <> '' "
            "ORDER BY date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    # ------------------------------------------------------------------
    # 明日计划（手动条目 + 群消息捕捉）
    # ------------------------------------------------------------------

    def save_plan(
        self, title: str, owner: str, plan_date: str,
        status: str = "待完成", msg_key: str = "",
    ) -> int:
        """写入一条明日计划条目，返回其 id。msg_key 用于群消息捕捉去重。"""

        cursor = self._conn.execute(
            "INSERT INTO plans (plan_date, owner, title, status, msg_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                plan_date,
                owner,
                title,
                status,
                msg_key,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def iter_plans(self, plan_date: str) -> list[dict]:
        """返回指定计划日期的全部条目（按创建顺序）。"""

        rows = self._conn.execute(
            "SELECT id, plan_date, owner, title, status, msg_key "
            "FROM plans WHERE plan_date = ? ORDER BY id",
            (plan_date,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_plan(self, plan_id: int) -> bool:
        """按 id 删除计划条目；存在且删除成功返回 True。"""

        cursor = self._conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def roll_pending_plans(self, today: str) -> int:
        """把今天之前所有未完成条目滚动到今天，返回滚动条数。

        未完成计划跨天自动顺延，直到被标完成；已完成行不滚动。
        """

        cursor = self._conn.execute(
            "UPDATE plans SET plan_date = ? WHERE plan_date < ? AND status = '待完成'",
            (today, today),
        )
        self._conn.commit()
        return cursor.rowcount

    def plan_msg_key_exists(self, msg_key: str) -> bool:
        """判断来源消息键是否已捕捉过，防止同一条群消息重复入计划。"""

        row = self._conn.execute(
            "SELECT 1 FROM plans WHERE msg_key = ? LIMIT 1", (msg_key,)
        ).fetchone()
        return row is not None

    def get_meta(self, key: str) -> str:
        """读取跨进程共享的键值标记（不存在返回空串）。"""

        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else ""

    def set_meta(self, key: str, value: str) -> None:
        """写入跨进程共享的键值标记（upsert）。"""

        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def mark_plan_done(self, plan_id: int) -> bool:
        """按 id 将计划条目标记为已完成；条目存在且更新成功返回 True。"""

        cursor = self._conn.execute(
            "UPDATE plans SET status = '已完成' WHERE id = ?", (plan_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_plans_done_by_title(self, title: str) -> int:
        """把指定标题的全部未完成条目标记为已完成，返回标记条数。

        同标题可能存在多条捕捉记录（不同消息重复布置），完成宣告一并落账。
        """

        cursor = self._conn.execute(
            "UPDATE plans SET status = '已完成' WHERE title = ? AND status = '待完成'",
            (title,),
        )
        self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # 问题与解决（手动条目）
    # ------------------------------------------------------------------

    def save_issue(
        self, problem: str, solution: str, owner: str, issue_date: str,
        status: str = "处理中", msg_key: str = "",
    ) -> int:
        """写入一条手动问题条目，返回其 id。msg_key 用于认领自动条目去重。"""

        cursor = self._conn.execute(
            "INSERT INTO issues (issue_date, owner, problem, solution, status, msg_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                issue_date,
                owner,
                problem,
                solution,
                status,
                msg_key,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def iter_issues(self, issue_date: str) -> list[dict]:
        """返回指定日期的全部手动问题条目（按创建顺序）。"""

        rows = self._conn.execute(
            "SELECT id, issue_date, owner, problem, solution, status, msg_key "
            "FROM issues WHERE issue_date = ? ORDER BY id",
            (issue_date,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_issue_status(self, issue_id: int, status: str) -> bool:
        """更新问题状态；条目存在且更新成功返回 True。"""

        cursor = self._conn.execute(
            "UPDATE issues SET status = ? WHERE id = ?", (status, issue_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_issue(self, issue_id: int) -> bool:
        """按 id 删除手动问题条目；存在且删除成功返回 True。"""

        cursor = self._conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 看板账号（登录/注册）
    # ------------------------------------------------------------------

    def create_user(self, username: str, password_hash: str) -> int | None:
        """创建账号；用户名已存在（UNIQUE 冲突）返回 None。"""

        created_at = datetime.now().isoformat(timespec="seconds")
        try:
            cursor = self._conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, created_at),
            )
        except sqlite3.IntegrityError:
            return None
        self._conn.commit()
        return int(cursor.lastrowid)

    def get_user_by_username(self, username: str) -> dict | None:
        """按用户名查询账号；不存在返回 None。"""

        row = self._conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 日报 data_json 局部更新（看板写操作后同步手动条目）
    # ------------------------------------------------------------------

    def update_report_data(
        self, date_iso: str, team_name: str, data: dict
    ) -> bool:
        """更新指定日报的 data_json；记录不存在或无结构化数据时返回 False。"""

        try:
            row = self._conn.execute(
                "SELECT data_json FROM reports WHERE date = ? AND team_name = ?",
                (date_iso, team_name),
            ).fetchone()
            if row is None or not row["data_json"]:
                return False
            self._conn.execute(
                "UPDATE reports SET data_json = ? WHERE date = ? AND team_name = ?",
                (json.dumps(data, ensure_ascii=False), date_iso, team_name),
            )
            self._conn.commit()
            return True
        except sqlite3.OperationalError:
            return False
