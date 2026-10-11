"""本地只读看板 HTTP 服务（design-v2 §7）。

仅绑定 127.0.0.1；页面经 / 提供，数据经 /api/reports 实时读取 SQLite。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shared.logger import get_logger
from shared.storage import ReportStorage

_TEMPLATE_PATH = Path(__file__).with_name("dashboard.html")
_AUTO_REFRESH_PLACEHOLDER = "__AUTO_REFRESH_INTERVAL__"

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 10

logger = get_logger("webapp.server")


def auto_refresh_settings(config) -> tuple[bool, int]:
    """读取 dashboard.auto_refresh 配置，返回 (enabled, interval_seconds)。

    缺省 (True, 60)；非法值回落 60；间隔最小钳制为 10 秒。
    """

    section = ((config.get("dashboard") or {}).get("auto_refresh")) or {}
    enabled = bool(section.get("enabled", True))
    try:
        interval = int(section.get("interval_seconds", DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL
    return enabled, max(interval, MIN_INTERVAL)


class AutoCollector:
    """守护线程：立即采集一次，之后按固定间隔循环；异常不中断。"""

    def __init__(
        self,
        config,
        interval_seconds: int,
        *,
        collect_func=None,
    ) -> None:
        self._config = config
        self._interval = interval_seconds
        self._collect = collect_func or self._default_collect
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._collect(self._config)
            except Exception as exc:  # 采集失败不影响服务与下一轮
                logger.warning(
                    "自动采集失败，将在下一轮重试",
                    extra={"error": str(exc)},
                )
            self._stop_event.wait(self._interval)

    @staticmethod
    def _default_collect(config) -> None:
        from main import run_pipeline  # 延迟导入避免循环依赖

        run_pipeline(config, dry_run=True, force=True)


class ScheduledPusher:
    """守护线程：每天在 schedule.push_time 到点时执行一次完整管道（含推送）。

    防重：记录上次推送的日期，同一天只推一次；错过时间（服务晚于 push_time 启动）
    当天内补推一次。
    """

    CHECK_INTERVAL = 20  # 秒

    def __init__(self, config, *, run_func=None, clock=None) -> None:
        self._config = config
        self._run = run_func or self._default_run
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._push_time = self._parse_push_time(config)
        self._last_push_date: str | None = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def _parse_push_time(config) -> tuple[int, int] | None:
        raw = ((config.get("schedule") or {}).get("push_time")) or ""
        try:
            hh, mm = str(raw).split(":")
            hh, mm = int(hh), int(mm)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return None
            return (hh, mm)
        except (ValueError, AttributeError):
            return None

    @property
    def enabled(self) -> bool:
        return self._push_time is not None

    def start(self) -> None:
        if self.enabled:
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._maybe_push()
            except Exception as exc:  # 推送失败不影响服务与后续调度
                logger.warning(
                    "定时推送失败，明天再试",
                    extra={"error": str(exc)},
                )
            self._stop_event.wait(self.CHECK_INTERVAL)

    def _maybe_push(self) -> None:
        now = self._clock()
        today = now.date().isoformat()
        if self._last_push_date == today:
            return
        hh, mm = self._push_time
        if (now.hour, now.minute) >= (hh, mm):
            self._last_push_date = today
            self._run(self._config)

    @staticmethod
    def _default_run(config) -> None:
        from main import run_pipeline  # 延迟导入避免循环依赖

        run_pipeline(config)  # 完整流程：采集 + 生成 + 推送


def render_served_page(interval_seconds: int = 0) -> str:
    """渲染动态版看板：数据占位符为 null，由前端 fetch 加载。"""

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__REPORTS_JSON__", "null")
        .replace("__INITIAL_DATE__", "null")
        .replace(_AUTO_REFRESH_PLACEHOLDER, str(int(interval_seconds)))
    )


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DailyReportDashboard/1.0"

    def log_message(self, *args: object) -> None:  # 静默默认访问日志
        return

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_html(
                render_served_page(self.server.auto_refresh_interval)
            )
            return

        if path == "/api/reports":
            date_filter = parse_qs(parsed.query).get("date", [None])[0]
            with ReportStorage(
                self.server.config["storage"]["sqlite_path"]
            ) as storage:
                reports = storage.iter_report_data()
            if date_filter is not None:
                reports = [r for r in reports if r["date"] == date_filter]
            self._send_json(200, {"code": 0, "reports": reports})
            return

        self._send_json(404, {"code": 404, "msg": "not found"})

    def _read_json_body(self) -> dict:
        """读取请求体 JSON；非法时返回 {}。"""

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        path = urlparse(self.path).path
        if path == "/api/plans":
            self._post_plan()
        elif path == "/api/issues":
            self._post_issue()
        else:
            self._method_not_allowed()

    def _post_plan(self) -> None:
        body = self._read_json_body()
        title = str(body.get("title", "")).strip()
        if not title:
            self._send_json(400, {"code": 400, "msg": "title 不能为空"})
            return
        owner = str(body.get("owner", "")).strip()
        tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            plan_id = storage.save_plan(title, owner, tomorrow)
        self._sync_latest_report()
        self._send_json(200, {"code": 0, "id": plan_id, "plan_date": tomorrow})

    def _post_issue(self) -> None:
        body = self._read_json_body()
        problem = str(body.get("problem", "")).strip()
        if not problem:
            self._send_json(400, {"code": 400, "msg": "problem 不能为空"})
            return
        solution = str(body.get("solution", "")).strip()
        owner = str(body.get("owner", "")).strip()
        status = str(body.get("status", "")).strip() or "处理中"
        if status not in ("处理中", "已解决"):
            self._send_json(400, {"code": 400, "msg": "status 仅支持 处理中/已解决"})
            return
        msg_key = str(body.get("msg_key", "")).strip()
        today = datetime.now().astimezone().date().isoformat()
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            issue_id = storage.save_issue(
                problem, solution, owner, today, status=status, msg_key=msg_key
            )
        self._sync_latest_report()
        self._send_json(200, {"code": 0, "id": issue_id, "issue_date": today})

    def do_DELETE(self) -> None:  # noqa: N802 - http.server 约定
        path = urlparse(self.path).path
        if path not in ("/api/plans", "/api/issues"):
            self._method_not_allowed()
            return

        raw = parse_qs(urlparse(self.path).query).get("id", [None])[0]
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            self._send_json(400, {"code": 400, "msg": "id 非法"})
            return
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            if path == "/api/plans":
                deleted = storage.delete_plan(item_id)
            else:
                deleted = storage.delete_issue(item_id)
        if not deleted:
            self._send_json(404, {"code": 404, "msg": "条目不存在"})
            return
        self._sync_latest_report()
        self._send_json(200, {"code": 0})

    def do_PATCH(self) -> None:  # noqa: N802 - http.server 约定
        if urlparse(self.path).path != "/api/issues":
            self._method_not_allowed()
            return

        raw = parse_qs(urlparse(self.path).query).get("id", [None])[0]
        try:
            issue_id = int(raw)
        except (TypeError, ValueError):
            self._send_json(400, {"code": 400, "msg": "id 非法"})
            return
        status = str(self._read_json_body().get("status", "")).strip()
        if status not in ("处理中", "已解决"):
            self._send_json(400, {"code": 400, "msg": "status 仅支持 处理中/已解决"})
            return
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            updated = storage.update_issue_status(issue_id, status)
        if not updated:
            self._send_json(404, {"code": 404, "msg": "条目不存在"})
            return
        self._sync_latest_report()
        self._send_json(200, {"code": 0, "status": status})

    def _sync_latest_report(self) -> None:
        """把 plans/issues 表中的手动条目同步回当日报告 data_json。

        使页面刷新（重新拉取 /api/reports）立即看到最新手动条目，
        无需等待下一轮 60s 管道重建；失败仅记日志，不影响写操作结果。
        """

        now = datetime.now().astimezone()
        today = now.date().isoformat()
        tomorrow = (now + timedelta(days=1)).date().isoformat()
        try:
            with ReportStorage(
                self.server.config["storage"]["sqlite_path"]
            ) as storage:
                latest = storage.iter_report_data(limit=1)
                if not latest or latest[0].get("date") != today:
                    return
                data = latest[0]
                auto_plans = [
                    p for p in data.get("tomorrow_plans", [])
                    if p.get("source") != "manual"
                ]
                data["tomorrow_plans"] = auto_plans + [
                    {"title": r["title"], "owner": r["owner"],
                     "source": "manual", "id": int(r["id"])}
                    for r in storage.iter_plans(tomorrow)
                ]
                manual_rows = storage.iter_issues(today)
                claimed = {r["msg_key"] for r in manual_rows if r["msg_key"]}
                auto_issues = [
                    i for i in data.get("issues", [])
                    if i.get("source") != "manual"
                    and i.get("msg_key", "") not in claimed
                ]
                data["issues"] = auto_issues + [
                    {"problem": r["problem"], "solution": r["solution"],
                     "owner": r["owner"], "status": r["status"],
                     "source": "manual", "id": int(r["id"]),
                     "msg_key": r["msg_key"]}
                    for r in manual_rows
                ]
                storage.update_report_data(today, data["team_name"], data)
        except Exception as exc:  # 同步失败不影响写操作
            logger.warning(
                "data_json 同步失败", extra={"error": str(exc)}
            )

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"code": 405, "msg": "method not allowed"})

    do_PUT = _method_not_allowed  # type: ignore[assignment]


def build_server(
    config, *, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """创建（但不启动）看板服务，供测试或自定义编排使用。"""

    server = ThreadingHTTPServer((host, port), _DashboardHandler)
    server.config = config
    enabled, interval = auto_refresh_settings(config)
    server.auto_refresh_enabled = enabled
    server.auto_refresh_interval = interval
    return server


def serve(
    config,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """启动看板服务并阻塞，Ctrl+C 正常退出（design-v2 §7、design-v3 §6）。"""

    server = build_server(config, host=host, port=port)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"日报看板服务已启动：{url}（按 Ctrl+C 停止）")
    if server.auto_refresh_enabled:
        print(f"自动刷新已开启：每 {server.auto_refresh_interval} 秒更新一次")
    if open_browser:
        webbrowser.open(url)

    collector = (
        AutoCollector(config, server.auto_refresh_interval)
        if server.auto_refresh_enabled
        else None
    )
    if collector:
        collector.start()

    pusher = ScheduledPusher(config)
    if pusher.enabled:
        pusher.start()
        print(f"定时推送已开启：每天 {config.get('schedule', {}).get('push_time')} 自动推送日报")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if collector:
            collector.stop()
        pusher.stop()
        server.shutdown()
        server.server_close()
        print("看板服务已停止")
