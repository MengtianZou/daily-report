"""任务完成趋势（近 7 日聚合 + 三端渲染）测试。"""

from __future__ import annotations

from datetime import date

from generator.formatter import (
    aggregate,
    build_markdown,
    compute_task_trend,
    generate,
)
from generator.template import render_report_html
from shared.models import TaskRecord


def _hist(date_iso: str, subtasks: list[dict] | None = None, members: list[dict] | None = None) -> dict:
    data: dict = {"date": date_iso, "task_list": None, "members": members or []}
    if subtasks is not None:
        data["task_list"] = {"title": "任务清单", "subtasks": subtasks}
    return data


def _tl(subtasks: list[dict] | None) -> dict | None:
    return {"subtasks": subtasks} if subtasks is not None else None


class TestComputeTaskTrend:
    def test_history_plus_today_chronological(self):
        history = [
            _hist("2026-10-15", [{"status_to": "已完成"}]),  # 今日历史行应被忽略
            _hist("2026-10-14", [{"status_to": "已完成"}, {"status_to": "处理中"}]),
            _hist("2026-10-13", [{"status_to": "已完成"}]),
        ]
        trend = compute_task_trend(history, "2026-10-15", _tl([{"status_to": "处理中"}]))
        assert [d["date"] for d in trend] == ["2026-10-13", "2026-10-14", "2026-10-15"]
        assert [d["done"] for d in trend] == [1, 1, 0]
        assert [d["total"] for d in trend] == [1, 2, 1]

    def test_empty_days_skipped(self):
        history = [
            _hist("2026-10-14"),
            _hist("2026-10-13", [{"status_to": "已完成"}]),
        ]
        trend = compute_task_trend(history, "2026-10-15", _tl([]))
        assert [d["date"] for d in trend] == ["2026-10-13"]

    def test_legacy_member_tasks_fallback(self):
        history = [
            _hist(
                "2026-10-13",
                members=[{"tasks": [{"status_to": "已完成"}, {"status_to": "已完成"}]}],
            ),
        ]
        trend = compute_task_trend(history, "2026-10-15", None)
        assert trend == [
            {"date": "2026-10-13", "done": 2, "total": 2, "pct": 100}
        ]

    def test_limit_seven_days(self):
        history = [
            _hist(f"2026-10-{9 - i:02d}", [{"status_to": "已完成"}]) for i in range(9)
        ]
        trend = compute_task_trend(history, "2026-10-15", None)
        assert len(trend) == 6  # 9 天历史只取最近 6 天（今日实时无数据）
        assert trend[0]["date"] == "2026-10-04"
        assert trend[-1]["date"] == "2026-10-09"

    def test_empty_history_no_today_data(self):
        assert compute_task_trend([], "2026-10-15", None) == []

    def test_pct_rounded(self):
        history = [_hist("2026-10-13", [{"status_to": "已完成"}, {"status_to": "处理中"}, {"status_to": "处理中"}])]
        trend = compute_task_trend(history, "2026-10-15", None)
        assert trend[0]["pct"] == 33


class TestMarkdownRendering:
    def test_trend_section_rendered(self):
        trend = [
            {"date": "2026-10-13", "done": 1, "total": 2, "pct": 50},
            {"date": "2026-10-14", "done": 2, "total": 2, "pct": 100},
        ]
        md = build_markdown("团队", date(2026, 10, 15), [], task_trend=trend)
        assert "## 任务完成趋势" in md
        assert "近 2 天完成率 **75%**（完成 3/4）" in md
        assert "- 10-13 ▓░ 1/2" in md
        assert "- 10-14 ▓▓ 2/2" in md

    def test_empty_trend_shows_placeholder(self):
        md = build_markdown("团队", date(2026, 10, 15), [], task_trend=[])
        assert "## 任务完成趋势" in md
        assert "暂无" in md


class TestGenerateAndHtml:
    def _tl_record(self) -> TaskRecord:
        done = TaskRecord("刘政剑", "子任务A", "", "已完成", date(2026, 10, 15, 10).replace(hour=10))
        todo = TaskRecord("刘政剑", "子任务B", "", "处理中", date(2026, 10, 15, 11).replace(hour=11))
        return TaskRecord("刘政剑", "10月15日任务清单", "", "", date(2026, 10, 15, 9).replace(hour=9), subtasks=[done, todo])

    def test_generate_stores_trend(self):
        trend = [{"date": "2026-10-14", "done": 1, "total": 1, "pct": 100}]
        report = generate([], date(2026, 10, 15), "团队", task_trend=trend)
        assert report.task_trend == trend
        assert "## 任务完成趋势" in report.markdown

    def test_html_section_rendered(self):
        trend = [
            {"date": "2026-10-13", "done": 1, "total": 3, "pct": 33},
            {"date": "2026-10-14", "done": 2, "total": 2, "pct": 100},
        ]
        html = render_report_html("团队", date(2026, 10, 15), [], task_trend=trend)
        assert "任务完成趋势" in html
        assert "近 2 天完成率 60%（完成 3/5）" in html
        assert "width:33%" in html
        assert "width:100%" in html
        assert "1/3" in html

    def test_html_empty_trend(self):
        html = render_report_html("团队", date(2026, 10, 15), [], task_trend=[])
        assert "任务完成趋势" in html
        assert "暂无" in html

    def test_html_has_table_bars(self):
        trend = [{"date": "2026-10-14", "done": 2, "total": 4, "pct": 50}]
        html = render_report_html("团队", date(2026, 10, 15), [], task_trend=trend)
        # 邮件客户端兼容：进度条用表格行 + 内联样式承载
        assert "<table" in html
        assert "background:#16a34a" in html


class TestAggregateUnaffected:
    def test_aggregate_still_works(self):
        members = aggregate([{"name": "刘政剑", "lark": "ou_x"}], [], [], [])
        assert len(members) == 1
        assert members[0].name == "刘政剑"
