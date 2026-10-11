"""智能日报生成器 — 主编排入口（design.md §2 main.py）。

按“采集 → 聚合 → 生成 → 存储 → 推送”顺序编排管道，处理全局异常并记录执行状态。

用法：
    python main.py                 # 默认：启动本地只读看板服务（浏览器自动打开）
    python main.py --serve         # 同上（显式写法）
    python main.py --run           # 执行一次完整流程（非工作日自动跳过）
    python main.py --run --dry-run # 只采集、生成，不推送
    python main.py --check         # 检查 API / 邮件 / 机器人配置
    python main.py --view          # 在浏览器中打开最新日报看板
    python main.py --view 2026-09-25  # 打开指定日期的日报看板
    python main.py --config x.yaml # 指定配置文件

凭证：配置文件同目录下的 .env 会被自动加载（见 .env 模板），无需每次手动设置。
"""

from __future__ import annotations

import argparse
import socket
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from collector import github, lark_msg, lark_task
from generator import formatter
from notifier import email, lark_bot
from shared.config import Config, load_config
from shared.envfile import load_envfile
from shared.errors import CollectorError, ConfigurationError, NotifierError
from shared.logger import get_logger
from shared.models import IssueItem, PlanItem
from shared.storage import ReportStorage
from webapp.export import render_dashboard

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 工作日识别
# ---------------------------------------------------------------------------

def is_workday(day: date, config: Config) -> bool:
    """判断给定日期是否为工作日。

    优先使用 chinese_calendar（含法定节假日与调休）；
    未安装时退化为“周末 + config 中显式配置的节假日 / 调休工作日”。
    """

    iso = day.isoformat()
    extra_workdays = set(config.get_path("schedule.workdays", []))
    extra_holidays = set(config.get_path("schedule.holidays", []))

    if iso in extra_workdays:
        return True
    if iso in extra_holidays:
        return False

    try:
        import chinese_calendar  # type: ignore
    except ImportError:
        return day.weekday() < 5

    try:
        return bool(chinese_calendar.is_workday(day))
    except NotImplementedError:
        return day.weekday() < 5


# ---------------------------------------------------------------------------
# 健康检查（--check）
# ---------------------------------------------------------------------------

def _check_github(config: Config) -> None:
    github_cfg = config["github"]
    headers = {"Accept": "application/vnd.github+json"}
    url = github_cfg["base_url"].rstrip("/") + "/rate_limit"
    try:
        resp = httpx.get(url, headers=headers, timeout=github_cfg.get("timeout", 10))
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ConfigurationError(f"GitHub 连接失败：{exc}", source="check") from exc


def _check_lark(config: Config, section: str) -> None:
    import os

    from collector._lark_client import LarkClient

    section_cfg = config[section]
    client = LarkClient(
        os.environ.get(section_cfg["app_id_env"], ""),
        os.environ.get(section_cfg["app_secret_env"], ""),
        timeout=section_cfg.get("timeout", 10),
    )
    try:
        client._valid_token()
    except CollectorError as exc:
        raise ConfigurationError(f"飞书凭证无效（{section}）：{exc}", source="check") from exc


def _check_smtp(config: Config) -> None:
    email_cfg = config["email"]
    try:
        with socket.create_connection(
            (email_cfg["smtp_host"], int(email_cfg["smtp_port"])),
            timeout=5,
        ):
            pass
    except OSError as exc:
        raise ConfigurationError(f"SMTP 连接失败：{exc}", source="check") from exc


def run_check(config: Config) -> bool:
    """逐项验证外部连接；任何一项失败返回 False。"""

    checks: list[tuple[str, Any]] = []
    if config["github"].get("enabled"):
        checks.append(("GitHub API", _check_github))
    if config["lark_task"].get("enabled"):
        checks.append(("飞书任务", lambda c: _check_lark(c, "lark_task")))
    if config["lark_message"].get("enabled"):
        checks.append(("飞书消息", lambda c: _check_lark(c, "lark_message")))
    if config["email"].get("enabled"):
        checks.append(("SMTP 邮件", _check_smtp))

    all_ok = True
    for name, probe in checks:
        try:
            probe(config)
            logger.info("健康检查通过", extra={"target": name})
        except ConfigurationError as exc:
            all_ok = False
            logger.error("健康检查失败", extra={"target": name, "error": str(exc)})
    return all_ok


# ---------------------------------------------------------------------------
# 管道编排
# ---------------------------------------------------------------------------

def _window(now: datetime) -> tuple[datetime, datetime]:
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return since, now


def _collect_all(
    config: Config,
    since: datetime,
    until: datetime,
) -> tuple[list, list, list, list[str]]:
    """独立采集三个数据源；单个失败记入 failed_sources，不阻断其他源。"""

    commits: list = []
    tasks: list = []
    messages: list = []
    failed: list[str] = []

    if config["github"].get("enabled"):
        cfg = config["github"]
        try:
            commits = github.collect(
                cfg["repos"],
                since,
                until,
                base_url=cfg["base_url"],
                token_env=cfg.get("token_env", "GITHUB_TOKEN"),
                per_page=cfg.get("per_page", 30),
                timeout=cfg.get("timeout", 10),
            )
        except CollectorError as exc:
            failed.append("github")
            logger.error("GitHub 数据源失败", extra={"error": str(exc)})

    if config["lark_task"].get("enabled"):
        cfg = config["lark_task"]
        try:
            tasks = lark_task.collect(
                cfg["project_id"],
                since,
                until,
                app_id_env=cfg.get("app_id_env", "LARK_APP_ID"),
                app_secret_env=cfg.get("app_secret_env", "LARK_APP_SECRET"),
                timeout=cfg.get("timeout", 10),
            )
        except CollectorError as exc:
            failed.append("lark_task")
            logger.error("飞书任务数据源失败", extra={"error": str(exc)})

    if config["lark_message"].get("enabled"):
        cfg = config["lark_message"]
        try:
            messages = lark_msg.collect(
                cfg["chat_id"],
                cfg["keywords"],
                since,
                until,
                app_id_env=cfg.get("app_id_env", "LARK_APP_ID"),
                app_secret_env=cfg.get("app_secret_env", "LARK_APP_SECRET"),
                sensitive_words=cfg.get("sensitive_words", []),
                chat_name=cfg.get("chat_name", cfg["chat_id"]),
                timeout=cfg.get("timeout", 10),
            )
        except CollectorError as exc:
            failed.append("lark_message")
            logger.error("飞书消息数据源失败", extra={"error": str(exc)})

    return commits, tasks, messages, failed


def _push_all(config: Config, report) -> dict[str, bool]:
    results: dict[str, bool] = {}

    if config["email"].get("enabled"):
        cfg = config["email"]
        results["email"] = email.send(
            report,
            cfg["recipients"],
            smtp_host=cfg["smtp_host"],
            smtp_port=int(cfg["smtp_port"]),
            sender=cfg["sender"],
            use_ssl=cfg.get("use_ssl", True),
            username_env=cfg.get("username_env", "SMTP_USERNAME"),
            password_env=cfg.get("password_env", "SMTP_PASSWORD"),
        )

    if config["lark_bot"].get("enabled"):
        cfg = config["lark_bot"]
        results["lark_bot"] = lark_bot.send(
            report,
            cfg.get("chat_id", ""),
            webhook=cfg["webhook"],
            timeout=cfg.get("timeout", 10),
        )

    return results


def _collect_tomorrow_plans(config: Config, today: date) -> list[PlanItem]:
    """组装明日计划：自动（飞书未完成任务）+ 手动（plans 表，plan_date=明天）。"""

    plans: list[PlanItem] = []
    tomorrow = (today + timedelta(days=1)).isoformat()

    if config["lark_task"].get("enabled"):
        cfg = config["lark_task"]
        name_map = {m["lark"]: m["name"] for m in config.members}
        try:
            open_tasks = lark_task.collect_open(
                cfg["project_id"],
                app_id_env=cfg.get("app_id_env", "LARK_APP_ID"),
                app_secret_env=cfg.get("app_secret_env", "LARK_APP_SECRET"),
                timeout=cfg.get("timeout", 10),
            )
        except CollectorError as exc:
            # 明日计划为附加板块，获取失败不阻断日报生成
            logger.error("明日计划：飞书未完成任务获取失败", extra={"error": str(exc)})
        else:
            for t in open_tasks:
                if t.assignee not in name_map:
                    continue  # 非团队成员的未完成任务不进入明日计划
                plans.append(
                    PlanItem(
                        title=t.title,
                        owner=name_map[t.assignee],
                        source="auto",
                    )
                )

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        for row in storage.iter_plans(tomorrow):
            plans.append(
                PlanItem(
                    title=row["title"],
                    owner=row["owner"],
                    source="manual",
                    plan_id=int(row["id"]),
                )
            )
    return plans


def _collect_issues(
    config: Config,
    since: datetime,
    until: datetime,
    messages: list,
    today: date,
) -> list[IssueItem]:
    """组装问题与解决：自动（群消息按问题关键词提取）+ 手动（issues 表）。"""

    issues: list[IssueItem] = []
    issue_keywords = [
        kw for kw in config.get_path("lark_message.issue_keywords", []) if kw
    ]

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        manual_rows = storage.iter_issues(today.isoformat())
        # 已认领（看板补充过方案）的来源消息不再重复生成自动条目
        claimed = {r["msg_key"] for r in manual_rows if r["msg_key"]}

        if config["lark_message"].get("enabled") and issue_keywords:
            cfg = config["lark_message"]
            name_map = {m["lark"]: m["name"] for m in config.members}
            sensitive = set(cfg.get("sensitive_words", []))
            # 与「协作沟通」板块已采用的消息去重（同一条消息不在两处重复出现）
            seen = {(m.sender, m.timestamp.isoformat()) for m in messages}
            try:
                candidates = lark_msg.collect(
                    cfg["chat_id"],
                    issue_keywords,
                    since,
                    until,
                    app_id_env=cfg.get("app_id_env", "LARK_APP_ID"),
                    app_secret_env=cfg.get("app_secret_env", "LARK_APP_SECRET"),
                    sensitive_words=cfg.get("sensitive_words", []),
                    chat_name=cfg.get("chat_name", cfg["chat_id"]),
                    timeout=cfg.get("timeout", 10),
                )
            except CollectorError as exc:
                # 问题板块为附加板块，获取失败不阻断日报生成
                logger.error("问题与解决：飞书消息获取失败", extra={"error": str(exc)})
            else:
                for m in candidates:
                    # 采集层按关键词过滤后再做一次防御性过滤，避免误采
                    if not any(kw in m.content for kw in issue_keywords):
                        continue
                    if (m.sender, m.timestamp.isoformat()) in seen:
                        continue
                    if any(word in m.content for word in sensitive):
                        continue
                    key = f"{m.sender}|{m.timestamp.isoformat()}"
                    if key in claimed:
                        continue
                    issues.append(
                        IssueItem(
                            problem=m.content,
                            owner=name_map.get(m.sender, ""),
                            status="已解决" if "已解决" in m.content else "处理中",
                            source="auto",
                            msg_key=key,
                        )
                    )

        for row in manual_rows:
            issues.append(
                IssueItem(
                    problem=row["problem"],
                    solution=row["solution"],
                    owner=row["owner"],
                    status=row["status"],
                    source="manual",
                    issue_id=int(row["id"]),
                    msg_key=row["msg_key"],
                )
            )
    return issues


def run_pipeline(
    config: Config,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    """执行一次完整管道；整体成功返回 True。

    force=True 时跳过工作日判断（供看板后台自动采集使用）。
    """

    started = time.time()
    now = now or datetime.now().astimezone()
    today = now.date()

    if not force and not is_workday(today, config):
        logger.info("非工作日，跳过日报生成", extra={"date": today.isoformat()})
        return True

    since, until = _window(now)
    logger.info(
        "开始生成日报",
        extra={"date": today.isoformat(), "sources": config.enabled_sources()},
    )

    commits, tasks, messages, failed = _collect_all(config, since, until)

    attempted = config.enabled_sources()
    if attempted and len(failed) == len(attempted):
        # 所有数据源都不可用：不生成空日报
        logger.error(
            "所有数据源均失败，不生成日报",
            extra={"failed_sources": failed},
        )
        return False

    members = formatter.aggregate(config.members, commits, tasks, messages)
    tomorrow_plans = _collect_tomorrow_plans(config, today)
    issues = _collect_issues(config, since, until, messages, today)
    report = formatter.generate(
        members, today, config.team_name, failed_sources=failed,
        tomorrow_plans=tomorrow_plans, issues=issues,
    )

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.save_report(report)

    push_results: dict[str, bool] = {}
    if dry_run:
        logger.info("dry-run 模式：跳过推送", extra={"report_date": today.isoformat()})
    else:
        try:
            push_results = _push_all(config, report)
        except NotifierError as exc:
            logger.error("推送过程异常", extra={"error": str(exc)})

    elapsed = time.time() - started
    logger.info(
        "日报流程结束",
        extra={
            "commits": len(commits),
            "tasks": len(tasks),
            "messages": len(messages),
            "failed_sources": failed,
            "push_results": push_results,
            "elapsed_seconds": round(elapsed, 2),
        },
    )
    return True


# ---------------------------------------------------------------------------
# 日报页面查看
# ---------------------------------------------------------------------------

def view_report(
    config: Config,
    date_str: str | None,
    *,
    opener: Any = webbrowser.open,
) -> bool:
    """导出注入真实数据的看板并在默认浏览器打开（design-v2 §8）。"""

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        reports = storage.iter_report_data()

    if not reports:
        logger.info("看板暂无可用的日报数据")
        return False

    if date_str is not None:
        available = {r["date"] for r in reports}
        if date_str not in available:
            logger.info(
                "指定日期没有可展示的日报",
                extra={"date": date_str},
            )
            return False
        out_name = f"report-{date_str}.html"
        initial_date: str | None = date_str
    else:
        out_name = "dashboard.html"
        initial_date = None

    out_path = (
        Path(config["storage"]["sqlite_path"]).parent / out_name
    )
    out_path.write_text(
        render_dashboard(reports, initial_date), encoding="utf-8"
    )
    opener(out_path.resolve().as_uri())
    logger.info("已在浏览器打开日报看板", extra={"path": str(out_path)})
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智能日报生成器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--check", action="store_true", help="检查外部连接与配置")
    parser.add_argument(
        "--run",
        action="store_true",
        help="执行一次日报采集与生成（默认动作是启动看板服务）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只采集生成，不推送（配合 --run）")
    parser.add_argument("--date", default=None, help="指定日报日期（YYYY-MM-DD，配合 --run）")
    parser.add_argument(
        "--view",
        nargs="?",
        const="",
        default=None,
        help="在浏览器中打开日报看板（可选日期 YYYY-MM-DD，默认最新）",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="启动本地只读看板服务（仅 127.0.0.1）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # 自动加载配置文件同目录下的 .env（若存在）；已存在的环境变量优先
    env_path = Path(args.config).resolve().parent / ".env"
    load_envfile(env_path)

    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        logger.error("配置加载失败", extra={"error": str(exc)})
        return 2

    if args.check:
        return 0 if run_check(config) else 1

    if args.view is not None:
        date_str = args.view or None
        return 0 if view_report(config, date_str) else 1

    if args.run:
        forced_now = None
        if args.date:
            forced_now = datetime.strptime(args.date, "%Y-%m-%d").replace(
                hour=18, tzinfo=datetime.now().astimezone().tzinfo
            )
        return 0 if run_pipeline(config, now=forced_now, dry_run=args.dry_run) else 1

    # 默认动作（含显式 --serve）：启动本地只读看板服务
    from webapp.server import serve

    try:
        serve(config)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
