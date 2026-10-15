"""本地只读看板 HTTP 服务（design-v2 §7）。

仅绑定 127.0.0.1；页面经 / 提供，数据经 /api/reports 实时读取 SQLite。
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shared.logger import get_logger
from shared.storage import ReportStorage
from webapp.auth import hash_password, new_session_token, verify_password

_TEMPLATE_PATH = Path(__file__).with_name("dashboard.html")
_AUTH_TEMPLATE_PATH = Path(__file__).with_name("auth.html")
_AUTO_REFRESH_PLACEHOLDER = "__AUTO_REFRESH_INTERVAL__"

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 10

_SESSION_COOKIE = "session"
_SESSION_TTL = 7 * 24 * 3600  # 7 天绝对过期

logger = get_logger("webapp.server")


def render_served_page(interval_seconds: int = 0) -> str:
    """渲染动态版看板：数据占位符为 null，由前端 fetch 加载。"""

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__REPORTS_JSON__", "null")
        .replace("__INITIAL_DATE__", "null")
        .replace(_AUTO_REFRESH_PLACEHOLDER, str(int(interval_seconds)))
    )


def render_auth_page() -> str:
    """渲染登录/注册页（纯静态，无占位符）。"""

    return _AUTH_TEMPLATE_PATH.read_text(encoding="utf-8")


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


def _member_accounts(config) -> dict:
    """读 dashboard.member_accounts；非 dict 时安全回落 {}。"""

    raw = (config.get("dashboard") or {}).get("member_accounts")
    return raw if isinstance(raw, dict) else {}


def _member_for_user(config, username: str) -> str | None:
    """账号名 -> 绑定成员名；未绑定或未登录返回 None。

    member_accounts 的值支持 "账号" 或 ["账号1", "账号2"] 两种写法；
    一个账号绑定多个成员时取第一个匹配项。
    """

    if not username:
        return None
    for member, accounts in _member_accounts(config).items():
        if isinstance(accounts, str):
            accounts = [accounts]
        if username in accounts:
            return str(member)
    return None


def _filter_report_for_user(
    report: dict, member_name: str | None
) -> dict | None:
    """按绑定成员过滤单份报告；不应看到任何内容时返回 None。

    members 只留 name==member_name（过滤后为空则整份丢弃）；
    tomorrow_plans/issues 只留 owner==""（团队级）或 owner==member_name。
    """

    if member_name is None:
        return None
    members = [
        m for m in report.get("members", []) if m.get("name") == member_name
    ]
    if not members:
        return None
    out = dict(report)  # 浅拷贝；iter_report_data 每次请求产出新 dict，安全
    out["members"] = members
    out["tomorrow_plans"] = [
        p for p in report.get("tomorrow_plans", [])
        if p.get("owner", "") in ("", member_name)
    ]
    out["issues"] = [
        i for i in report.get("issues", [])
        if i.get("owner", "") in ("", member_name)
    ]
    return out


def _warn_unknown_member_accounts(config) -> None:
    """member_accounts 键与 config.members 不一致时告警（防拼写错误静默空看板）。"""

    known = {
        m.get("name")
        for m in (config.get("members") or [])
        if isinstance(m, dict)
    }
    unknown = [k for k in _member_accounts(config) if k not in known]
    if unknown:
        logger.warning(
            "dashboard.member_accounts 含有 config.members 中不存在的成员名，"
            "对应账号将看到空看板",
            extra={"unknown_members": unknown},
        )


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
    当天内补推一次。推送日期同时写入 SQLite meta 表，多实例并存时
    后启动的实例读到当日标记即跳过，避免重复推送。
    """

    CHECK_INTERVAL = 20  # 秒
    PUSH_MARKER = "last_push_date"

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
            if self._read_push_marker() == today:
                # 其他实例今天已推送过（跨进程防重），本实例不再补推
                self._last_push_date = today
                return
            self._last_push_date = today
            self._run(self._config)
            self._write_push_marker(today)

    def _sqlite_path(self) -> str:
        return (self._config.get("storage") or {}).get("sqlite_path", "")

    def _read_push_marker(self) -> str:
        """读跨进程推送标记；库不可用时返回空串（退化为各实例自行推送）。"""

        from shared.storage import ReportStorage

        db_path = self._sqlite_path()
        if not db_path:
            return ""
        try:
            with ReportStorage(db_path) as storage:
                return storage.get_meta(self.PUSH_MARKER)
        except Exception:
            return ""

    def _write_push_marker(self, date_str: str) -> None:
        from shared.storage import ReportStorage

        db_path = self._sqlite_path()
        if not db_path:
            return
        try:
            with ReportStorage(db_path) as storage:
                storage.set_meta(self.PUSH_MARKER, date_str)
        except Exception:
            logger.warning(
                "推送标记写入失败", extra={"date": date_str}
            )

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

    def _send_json(
        self, code: int, payload: dict, *, set_cookie: str | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- 会话管理 ----

    def _get_session_token(self) -> str:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        morsel = cookie.get(_SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _current_user(self) -> str:
        """校验会话 cookie，返回用户名；未登录/已过期返回空串。"""

        token = self._get_session_token()
        if not token:
            return ""
        entry = self.server.sessions.get(token)
        if not entry:
            return ""
        username, expires_at = entry
        if time.time() > expires_at:
            self.server.sessions.pop(token, None)
            return ""
        return username

    def _create_session(self, username: str) -> str:
        token = new_session_token()
        self.server.sessions[token] = (username, time.time() + _SESSION_TTL)
        return token

    def _session_cookie(self, token: str) -> str:
        return f"{_SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"

    def _clear_session_cookie(self) -> str:
        return f"{_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/login", "/register"):
            if self._current_user():
                self._redirect("/")
            else:
                self._send_html(render_auth_page())
            return

        if not self._current_user():
            if path.startswith("/api/"):
                self._send_json(401, {"code": 401, "msg": "未登录"})
            else:
                self._redirect("/login")
            return

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
            member = _member_for_user(self.server.config, self._current_user())
            reports = [
                out
                for out in (_filter_report_for_user(r, member) for r in reports)
                if out is not None
            ]
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
        if path.startswith("/api/auth/"):
            self._handle_auth(path)
            return
        if not self._current_user():
            self._send_json(401, {"code": 401, "msg": "未登录"})
            return
        if path == "/api/plans":
            self._post_plan()
        elif path == "/api/issues":
            self._post_issue()
        else:
            self._method_not_allowed()

    def _handle_auth(self, path: str) -> None:
        if path == "/api/auth/register":
            self._register()
        elif path == "/api/auth/login":
            self._login()
        elif path == "/api/auth/logout":
            self._logout()
        else:
            self._send_json(404, {"code": 404, "msg": "not found"})

    def _register(self) -> None:
        body = self._read_json_body()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or len(username) > 32:
            self._send_json(400, {"code": 400, "msg": "用户名需为 1-32 个字符"})
            return
        if len(password) < 6:
            self._send_json(400, {"code": 400, "msg": "密码至少 6 位"})
            return
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            user_id = storage.create_user(username, hash_password(password))
        if user_id is None:
            self._send_json(400, {"code": 400, "msg": "用户名已被注册"})
            return
        token = self._create_session(username)
        self._send_json(
            200, {"code": 0, "username": username},
            set_cookie=self._session_cookie(token),
        )

    def _login(self) -> None:
        body = self._read_json_body()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            user = storage.get_user_by_username(username)
        if not user or not verify_password(password, user["password_hash"]):
            self._send_json(401, {"code": 401, "msg": "用户名或密码错误"})
            return
        token = self._create_session(username)
        self._send_json(
            200, {"code": 0, "username": username},
            set_cookie=self._session_cookie(token),
        )

    def _logout(self) -> None:
        token = self._get_session_token()
        if token:
            self.server.sessions.pop(token, None)
        self._send_json(200, {"code": 0}, set_cookie=self._clear_session_cookie())

    def _post_plan(self) -> None:
        member = _member_for_user(self.server.config, self._current_user())
        if member is None:
            self._send_json(403, {"code": 403, "msg": "当前账号未绑定成员"})
            return
        body = self._read_json_body()
        title = str(body.get("title", "")).strip()
        if not title:
            self._send_json(400, {"code": 400, "msg": "title 不能为空"})
            return
        owner = str(body.get("owner", "")).strip()
        if owner not in ("", member):
            self._send_json(403, {"code": 403, "msg": "owner 与当前账号绑定的成员不符"})
            return
        tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
        with ReportStorage(
            self.server.config["storage"]["sqlite_path"]
        ) as storage:
            plan_id = storage.save_plan(title, owner, tomorrow)
        self._sync_latest_report()
        self._send_json(200, {"code": 0, "id": plan_id, "plan_date": tomorrow})

    def _post_issue(self) -> None:
        member = _member_for_user(self.server.config, self._current_user())
        if member is None:
            self._send_json(403, {"code": 403, "msg": "当前账号未绑定成员"})
            return
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
        if owner not in ("", member):
            self._send_json(403, {"code": 403, "msg": "owner 与当前账号绑定的成员不符"})
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
        if not self._current_user():
            self._send_json(401, {"code": 401, "msg": "未登录"})
            return
        if _member_for_user(self.server.config, self._current_user()) is None:
            self._send_json(403, {"code": 403, "msg": "当前账号未绑定成员"})
            return
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
        if not self._current_user():
            self._send_json(401, {"code": 401, "msg": "未登录"})
            return
        if _member_for_user(self.server.config, self._current_user()) is None:
            self._send_json(403, {"code": 403, "msg": "当前账号未绑定成员"})
            return
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
                    if r["status"] == "待完成"
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
    server.sessions = {}  # token -> (username, expires_at)，内存会话
    enabled, interval = auto_refresh_settings(config)
    server.auto_refresh_enabled = enabled
    server.auto_refresh_interval = interval
    _warn_unknown_member_accounts(config)
    return server


def _resolve_port(config, port: int | None) -> int:
    """确定监听端口：显式传参优先，否则取 dashboard.port，默认 8000。"""

    if port is not None:
        return port
    return int((config.get("dashboard") or {}).get("port", 8000))


def serve(
    config,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """启动看板服务并阻塞，Ctrl+C 正常退出（design-v2 §7、design-v3 §6）。

    端口默认固定 8000（可用 config.yaml dashboard.port 覆盖），网址稳定
    便于浏览器收藏；单实例锁已保证不会与旧实例冲突。
    """

    port = _resolve_port(config, port)
    try:
        server = build_server(config, host=host, port=port)
    except OSError as exc:
        print(f"[!] 端口 {port} 启动失败：{exc}")
        print(
            "    可能被其他程序占用：可关闭占用程序，或在 config.yaml 的"
            " dashboard.port 换一个端口后重试。"
        )
        raise
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"日报看板服务已启动：{url}（按 Ctrl+C 停止）")
    print("看板已启用登录鉴权：首次使用请在打开的页面中注册账号")
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
