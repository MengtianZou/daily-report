"""看板自动刷新：配置解析、AutoCollector 调度与端到端（design-v3 §3/5/9）。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from dashboard_helpers import sample_report
from shared.storage import ReportStorage
from webapp.server import (
    MIN_INTERVAL,
    AutoCollector,
    ScheduledPusher,
    auto_refresh_settings,
    build_server,
)


# ---- auto_refresh_settings ----

def test_settings_default_when_section_missing() -> None:
    assert auto_refresh_settings({}) == (True, 60)
    assert auto_refresh_settings({"dashboard": {}}) == (True, 60)


def test_settings_respect_custom_values() -> None:
    config = {
        "dashboard": {
            "auto_refresh": {"enabled": False, "interval_seconds": 120}
        }
    }
    assert auto_refresh_settings(config) == (False, 120)


def test_settings_invalid_interval_falls_back() -> None:
    config = {"dashboard": {"auto_refresh": {"interval_seconds": "abc"}}}
    assert auto_refresh_settings(config) == (True, 60)


def test_settings_interval_is_clamped_to_minimum() -> None:
    config = {"dashboard": {"auto_refresh": {"interval_seconds": 1}}}
    assert auto_refresh_settings(config) == (True, MIN_INTERVAL)


# ---- AutoCollector 调度 ----

def test_collector_runs_immediately_and_periodically() -> None:
    calls: list[float] = []
    collector = AutoCollector(
        {"k": 1}, 0.05, collect_func=lambda cfg: calls.append(time.time())
    )
    collector.start()
    time.sleep(0.18)
    collector.stop()

    assert len(calls) >= 2  # 立即一次 + 至少一轮周期


def test_collector_survives_collect_errors() -> None:
    attempts = {"n": 0}

    def flaky(cfg):
        attempts["n"] += 1
        raise RuntimeError("boom")

    collector = AutoCollector({"k": 1}, 0.02, collect_func=flaky)
    collector.start()
    time.sleep(0.1)
    collector.stop()

    assert attempts["n"] >= 2  # 异常未中断循环


def test_collector_stop_terminates_thread() -> None:
    calls: list[int] = []
    collector = AutoCollector(
        {"k": 1}, 60, collect_func=lambda cfg: calls.append(1)
    )
    collector.start()
    collector.stop()

    assert not collector._thread.is_alive()
    snapshot = len(calls)
    time.sleep(0.05)
    assert len(calls) == snapshot  # 停止后不再执行


# ---- 端到端：采集写入 → 服务 API 可见 ----

def test_collector_writes_are_served(tmp_path) -> None:
    db_path = tmp_path / "reports.db"
    config = {
        "storage": {"sqlite_path": str(db_path)},
        "dashboard": {"member_accounts": {"刘政剑": "u"}},
    }

    server = build_server(config)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def write_report(cfg) -> None:
        with ReportStorage(db_path) as storage:
            storage.save_report(sample_report())

    collector = AutoCollector(config, 60, collect_func=write_report)
    collector.start()

    base = f"http://{host}:{port}"
    # 服务启用登录鉴权：注册（自动登录）后携带会话 cookie 轮询
    client = httpx.Client(base_url=base, timeout=5)
    reg = client.post(
        "/api/auth/register", json={"username": "u", "password": "secret123"}
    )
    assert reg.status_code == 200, reg.text

    deadline = time.time() + 5
    count = 0
    while time.time() < deadline:
        count = len(client.get("/api/reports").json()["reports"])
        if count == 1:
            break
        time.sleep(0.05)

    client.close()
    collector.stop()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

    assert count == 1


# ---- ScheduledPusher ----

def test_pusher_disabled_without_push_time() -> None:
    pusher = ScheduledPusher({})
    assert not pusher.enabled


def test_pusher_parses_valid_time() -> None:
    pusher = ScheduledPusher({"schedule": {"push_time": "14:25"}})
    assert pusher.enabled


def test_pusher_ignores_invalid_time() -> None:
    pusher = ScheduledPusher({"schedule": {"push_time": "abc"}})
    assert not pusher.enabled

    pusher2 = ScheduledPusher({"schedule": {"push_time": "25:00"}})
    assert not pusher2.enabled


def test_pusher_triggers_once_at_target_time() -> None:
    calls: list[datetime] = []
    base = datetime(2026, 9, 30, 14, 24, 50, tzinfo=timezone(timedelta(hours=8)))

    def fake_clock():
        fake_clock._now += timedelta(seconds=ScheduledPusher.CHECK_INTERVAL)
        return fake_clock._now

    fake_clock._now = base

    pusher = ScheduledPusher(
        {"schedule": {"push_time": "14:25"}},
        run_func=lambda cfg: calls.append(fake_clock._now),
        clock=fake_clock,
    )
    pusher.start()
    time.sleep(0.3)  # 给线程足够时间跑两轮检查
    pusher.stop()

    assert len(calls) == 1
    assert calls[0].strftime("%H:%M") == "14:25"


def test_pusher_does_not_trigger_twice_same_day() -> None:
    calls: list[datetime] = []
    base = datetime(2026, 9, 30, 14, 24, 55, tzinfo=timezone(timedelta(hours=8)))

    def fake_clock():
        fake_clock._now += timedelta(seconds=ScheduledPusher.CHECK_INTERVAL)
        return fake_clock._now

    fake_clock._now = base

    pusher = ScheduledPusher(
        {"schedule": {"push_time": "14:25"}},
        run_func=lambda cfg: calls.append(fake_clock._now),
        clock=fake_clock,
    )
    pusher.start()
    time.sleep(0.4)
    pusher.stop()

    assert len(calls) == 1  # 同一天只推一次


def _marker_config(db_path) -> dict:
    return {
        "schedule": {"push_time": "14:25"},
        "storage": {"sqlite_path": str(db_path)},
    }


def test_pusher_skips_when_other_instance_already_pushed(tmp_path) -> None:
    """库标记显示其他实例今天已推送：本实例不补推。"""
    db_path = tmp_path / "reports.db"
    with ReportStorage(db_path) as storage:
        storage.set_meta(ScheduledPusher.PUSH_MARKER, "2026-09-30")

    calls: list[datetime] = []
    base = datetime(2026, 9, 30, 15, 0, tzinfo=timezone(timedelta(hours=8)))

    def fake_clock():
        fake_clock._now += timedelta(seconds=ScheduledPusher.CHECK_INTERVAL)
        return fake_clock._now

    fake_clock._now = base

    pusher = ScheduledPusher(
        _marker_config(db_path),
        run_func=lambda cfg: calls.append(fake_clock._now),
        clock=fake_clock,
    )
    pusher.start()
    time.sleep(0.3)
    pusher.stop()

    assert calls == []  # 补推被跨进程标记拦下


def test_pusher_writes_marker_after_push(tmp_path) -> None:
    """推送成功后写入库标记；后启动的实例读到标记即跳过。"""
    db_path = tmp_path / "reports.db"
    calls: list[datetime] = []
    base = datetime(2026, 9, 30, 14, 24, 55, tzinfo=timezone(timedelta(hours=8)))

    def fake_clock():
        fake_clock._now += timedelta(seconds=ScheduledPusher.CHECK_INTERVAL)
        return fake_clock._now

    fake_clock._now = base

    pusher = ScheduledPusher(
        _marker_config(db_path),
        run_func=lambda cfg: calls.append(fake_clock._now),
        clock=fake_clock,
    )
    pusher.start()
    time.sleep(0.3)
    pusher.stop()

    assert len(calls) == 1
    with ReportStorage(db_path) as storage:
        assert storage.get_meta(ScheduledPusher.PUSH_MARKER) == "2026-09-30"

    # 后启动的第二个实例：读到当日标记，不再推送
    calls2: list[datetime] = []
    pusher2 = ScheduledPusher(
        _marker_config(db_path),
        run_func=lambda cfg: calls2.append(fake_clock._now),
        clock=fake_clock,
    )
    pusher2.start()
    time.sleep(0.3)
    pusher2.stop()

    assert calls2 == []
