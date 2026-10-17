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
import atexit
import os
import re
import socket
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from collector import github, lark_msg, lark_task
from collector._lark_client import LarkClient
from generator import formatter, summarizer
from notifier import email, lark_bot
from shared.config import Config, load_config
from shared.envfile import load_envfile
from shared.errors import CollectorError, ConfigurationError, NotifierError
from shared.logger import get_logger
from shared.models import IssueItem, PlanItem, TaskRecord
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
) -> tuple[list, list, list[str]]:
    """独立采集各数据源；单个失败记入 failed_sources，不阻断其他源。

    飞书任务不再做窗口变更采集——「任务进展」改由当日任务清单承载，
    清单的滚动/创建/补缺见 _ensure_daily_tasklist。
    """

    commits: list = []
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

    return commits, messages, failed


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


def _strip_plan_prefix(content: str, keywords: list[str]) -> str:
    """剥离前导触发词及其后的分隔符，返回净条目内容。

    仅剥前导触发词（如「明天：写周报」→「写周报」），可连续剥离多个
    （如「明日计划：写周报」先剥「明日」再剥「计划」）；剩余不足 2 字
    视为无效内容返回空串。
    """

    text = content.strip()
    changed = True
    while changed:
        changed = False
        for kw in keywords:
            if kw and text.startswith(kw):
                text = text[len(kw):].lstrip(":：,，、 ")
                changed = True
                break
    text = text.strip()
    return text if len(text) >= 2 else ""


_NUM_HEAD = re.compile(r"^\s*\d+\s*[.、．]\s*")
_NUM_SEP = re.compile(r"[。；;，,\s]+\d+\s*[.、．]\s*")


def _split_numbered_items(payload: str) -> list[str]:
    """「1.xxx。2.yyy」编号列表拆分为多条计划；非编号内容原样单条返回。

    仅当正文以「1.」编号开头才拆分，且编号前须有标点或空白分隔，
    避免「升级2.0版本」这类正文被误拆。
    """

    text = payload.strip()
    if not _NUM_HEAD.match(text):
        return [text] if len(text) >= 2 else []
    items: list[str] = []
    for i, part in enumerate(_NUM_SEP.split(text)):
        if i == 0:
            part = _NUM_HEAD.sub("", part, count=1)
        part = part.strip("。；;，, 　")
        if len(part) >= 2:
            items.append(part)
    return items


def _extract_plan_payload(content: str, keywords: list[str]) -> str:
    """提取计划正文：优先剥前导触发词；未命中前导时截取「触发词：」后正文。

    中段截取（如「现在做出明日计划：1.…」）仅认触发词后紧跟冒号的
    强信号写法，避免把含「计划」二字的普通沟通误捕为计划。
    """

    text = content.strip()
    stripped = _strip_plan_prefix(text, keywords)
    if stripped != text:
        return stripped  # 剥离过前导触发词
    cut = -1
    for kw in keywords:
        if not kw:
            continue
        idx = text.rfind(kw)
        if idx == -1:
            continue
        sep = idx + len(kw)
        if sep < len(text) and text[sep] in ":：":
            while sep < len(text) and text[sep] in ":： 　":
                sep += 1
            cut = max(cut, sep)
    if cut != -1:
        payload = text[cut:].strip()
        if len(payload) >= 2:
            return payload
    return ""


def _collect_tomorrow_plans(config: Config, today: date) -> list[PlanItem]:
    """组装明日计划：纯读 plans 表（plan_date=明天 且 待完成）。

    群消息捕捉与看板手动条目统一落 plans 表；同一（负责人，标题）去重。
    """

    tomorrow = (today + timedelta(days=1)).isoformat()
    plans: list[PlanItem] = []
    seen: set[tuple[str, str]] = set()
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        for row in storage.iter_plans(tomorrow):
            if row["status"] != "待完成":
                continue
            key = (row["owner"], row["title"])
            if key in seen:
                continue
            seen.add(key)
            plans.append(
                PlanItem(
                    title=row["title"],
                    owner=row["owner"],
                    source="auto" if row["msg_key"] else "manual",
                    plan_id=int(row["id"]),
                )
            )
    return plans


# 解决类消息特征：命中视为「问题已解决/原因说明」的总结，不再当新问题采集
_RESOLVED_HINTS = ("已解决", "解决了", "得到解决", "已修复", "修复了", "已排查", "原因是", "原因：")


def classify_message(content: str, issue_keywords: list[str]) -> str:
    """群消息归类：issue=问题描述（进问题与解决）；resolved=解决总结；normal=普通沟通。

    解决特征词优先判定：总结消息可能不含问题关键词（如「已修复，原因是配置
    写错」），只按问题关键词过滤会漏采。
    """

    if any(h in content for h in _RESOLVED_HINTS):
        return "resolved"
    if not any(kw in content for kw in issue_keywords):
        return "normal"
    return "issue"


def _collect_issues(
    config: Config,
    since: datetime,
    until: datetime,
    today: date,
) -> tuple[list[IssueItem], set[tuple[str, str]]]:
    """组装问题与解决：自动（群消息按问题关键词提取）+ 手动（issues 表）。

    返回 (条目列表, 问题消息键集合)。判定为「问题描述」的消息即使同时命中
    协作沟通关键词，也由 run_pipeline 从沟通板块剔除，保证同一句话只出现在
    问题与解决；「已解决/原因是」类总结消息自动挂为同负责人最近问题的方案。
    """

    issues: list[IssueItem] = []
    issue_keys: set[tuple[str, str]] = set()
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
            # 解决总结可能不含问题关键词，采集词需并入解决特征词才能拉到
            fetch_keywords = list(issue_keywords) + [
                h for h in _RESOLVED_HINTS if h not in issue_keywords
            ]
            try:
                candidates = lark_msg.collect(
                    cfg["chat_id"],
                    fetch_keywords,
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
                    kind = classify_message(m.content, issue_keywords)
                    if kind == "normal":
                        continue
                    if any(word in m.content for word in sensitive):
                        continue
                    key = f"{m.sender}|{m.timestamp.isoformat()}"
                    if kind == "resolved":
                        # 解决总结：挂到同负责人最近一个还没有方案的自动条目
                        owner = name_map.get(m.sender, "")
                        for item in reversed(issues):
                            if (
                                item.source == "auto"
                                and item.owner == owner
                                and not item.solution
                            ):
                                item.solution = m.content
                                item.status = "已解决"
                                break
                        continue
                    if key in claimed or (m.sender, m.timestamp.isoformat()) in issue_keys:
                        continue
                    issue_keys.add((m.sender, m.timestamp.isoformat()))
                    issues.append(
                        IssueItem(
                            problem=m.content,
                            owner=name_map.get(m.sender, ""),
                            status="处理中",
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
    return issues, issue_keys


# ---------------------------------------------------------------------------
# 群消息任务闭环：明日计划捕捉 / 当日清单保障 / 完成宣告
# ---------------------------------------------------------------------------

def _lark_task_client(config: Config) -> LarkClient:
    """按 lark_task 配置构建飞书客户端（token 惰性获取）。"""

    cfg = config["lark_task"]
    return LarkClient(
        os.environ.get(cfg.get("app_id_env", "LARK_APP_ID"), ""),
        os.environ.get(cfg.get("app_secret_env", "LARK_APP_SECRET"), ""),
        timeout=cfg.get("timeout", 10),
    )


def _collect_plan_messages(
    config: Config,
    since: datetime,
    until: datetime,
    today: date,
) -> set[tuple[str, str]]:
    """捕捉群消息中的明日计划（命中 plan_keywords），落 plans 表。

    done 优先于 plan：以完成关键词开头的消息（如「完成汇报PPT」）交由
    _collect_done_messages 处理；仅含「完成」二字的计划消息（如
    「明天：完成作业」）仍按计划捕捉。触发词支持前导或「触发词：」
    中段写法，编号列表（1.…。2.…）拆分为多条。
    返回已捕捉消息键集合，供 run_pipeline 从协作沟通板块剔除；早前轮次
    已入库的消息每轮也加入剔除集合，保证不回流到沟通板块。
    """

    plan_keys: set[tuple[str, str]] = set()
    cfg = config["lark_message"]
    if not cfg.get("enabled"):
        return plan_keys
    plan_keywords = [kw for kw in cfg.get("plan_keywords", []) if kw]
    done_keywords = [kw for kw in cfg.get("done_keywords", []) if kw]
    if not plan_keywords:
        return plan_keys

    name_map = {m["lark"]: m["name"] for m in config.members}
    sensitive = set(cfg.get("sensitive_words", []))
    tomorrow = (today + timedelta(days=1)).isoformat()

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        try:
            candidates = lark_msg.collect(
                cfg["chat_id"],
                plan_keywords,
                since,
                until,
                app_id_env=cfg.get("app_id_env", "LARK_APP_ID"),
                app_secret_env=cfg.get("app_secret_env", "LARK_APP_SECRET"),
                sensitive_words=cfg.get("sensitive_words", []),
                chat_name=cfg.get("chat_name", cfg["chat_id"]),
                timeout=cfg.get("timeout", 10),
            )
        except CollectorError as exc:
            # 明日计划为附加板块，捕捉失败不阻断日报生成
            logger.error("明日计划：飞书消息获取失败", extra={"error": str(exc)})
            return plan_keys

        for m in candidates:
            if any(
                m.content.strip().startswith(dk) for dk in done_keywords
            ):
                continue  # 完成宣告（触发词开头）优先，不进计划
            if any(word in m.content for word in sensitive):
                continue
            key = f"{m.sender}|{m.timestamp.isoformat()}"
            sender_key = (m.sender, m.timestamp.isoformat())
            if storage.plan_msg_key_exists(key):
                plan_keys.add(sender_key)  # 已入库，仅保证不回流沟通
                continue
            payload = _extract_plan_payload(m.content, plan_keywords)
            items = _split_numbered_items(payload)
            if not items:
                continue
            for title in items:
                storage.save_plan(
                    title=title,
                    owner=name_map.get(m.sender, ""),
                    plan_date=tomorrow,
                    msg_key=key,
                )
            plan_keys.add(sender_key)
    return plan_keys


def _ensure_daily_tasklist(
    config: Config,
    today: date,
    failed: list[str],
) -> TaskRecord | None:
    """保障当日「M月D日任务清单」存在且子任务齐全，返回清单快照树。

    流程：读当天待完成计划标题 → 按名查清单 → 不存在则创建（父任务+
    全部子任务）→ 存在则按标题比对补缺。标题集为空时不创建（仍返回
    已存在清单）。任一步骤失败仅记日志并标记 lark_task 失败，不阻断日报。
    """

    cfg = config["lark_task"]
    if not cfg.get("enabled"):
        return None

    titles: list[str] = []
    seen: set[str] = set()
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        for row in storage.iter_plans(today.isoformat()):
            if row["status"] != "待完成" or row["title"] in seen:
                continue
            seen.add(row["title"])
            titles.append(row["title"])

    try:
        client = _lark_task_client(config)
        existing = lark_task.fetch_daily_tasklist(client, cfg["project_id"], today)
        if not cfg.get("auto_create_tasklist", True) or not titles:
            return existing
        if existing is None:
            lark_task.create_daily_tasklist(client, cfg["project_id"], today, titles)
        else:
            have = {s.title for s in existing.subtasks}
            missing = [t for t in titles if t not in have]
            if missing and existing.guid:
                lark_task.add_subtasks(client, existing.guid, missing)
        return lark_task.fetch_daily_tasklist(client, cfg["project_id"], today)
    except CollectorError as exc:
        failed.append("lark_task")
        logger.error("今日任务清单保障失败", extra={"error": str(exc)})
        return None


# 完成宣告的尾部变体：剥掉后拿核心文本做匹配（兼容「xxx 已完成」写法）
_DONE_SUFFIXES = ("已完成", "完成了", "已搞定", "搞定了", "做完了", "完成", "做完", "搞定")


def _strip_done_suffix(text: str) -> str:
    """剥尾部完成宣告词（如「高级软件工程PPT已完成」→「高级软件工程PPT」）。

    可连续剥除多个变体；剥后不足 2 字视为无效返回原文（由调用方判长度）。
    """

    text = text.strip()
    changed = True
    while changed:
        changed = False
        for suf in _DONE_SUFFIXES:
            if text.endswith(suf) and len(text) > len(suf):
                text = text[: -len(suf)].strip("，。,.、!！?？ ")
                changed = True
                break
    return text


def _char_overlap(left: str, right: str) -> float:
    """字符集 Jaccard 重合度（0~1）：语序重排消息的模糊匹配兜底。"""

    a, b = set(left), set(right)
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _collect_done_messages(
    config: Config,
    since: datetime,
    until: datetime,
    task_list: TaskRecord | None,
) -> set[tuple[str, str]]:
    """捕捉完成宣告（命中 done_keywords）并同步完成状态。

    匹配规则（防误伤梯度）：消息与子任务标题先做双侧归一化（剥前导完成
    触发词 + 剥尾部完成宣告词，兼容「完成 xxx」与「xxx 已完成」两种写法），
    再按 归一化精确 → 双向包含 → 唯一模糊兜底（字符重合度，仅唯一超阈值
    才命中）匹配；一条消息命中多个子任务全部放弃。命中后先飞书打勾（失败
    下轮重试），再落库标完成；对已完成子任务的完成宣告只做落库对账并从
    沟通剔除（防往轮消息重复处理）。
    返回已消费消息键集合。
    """

    def _norm(title: str) -> str:
        # 双侧归一化：剥前导完成触发词与尾部完成宣告词后比对
        return _strip_done_suffix(_strip_plan_prefix(title, done_keywords))

    def _match(pool) -> list[str]:
        """在候选标题集合中匹配核心文本，可能返回多个命中。"""

        core = _strip_done_suffix(target)
        if not core or len(core) < 2:
            return []
        hits = [t for t in pool if _norm(t) == core]
        if not hits:
            hits = [t for t in pool if core in t or t in core]
        if not hits:
            # 语序重排兜底（如「高级软件工程PPT补充」vs「继续补充高级
            # 软件工程PPT」）：仅当唯一候选字符重合度超阈值才命中
            over = [
                t for t in pool
                if _char_overlap(core, _norm(t)) >= 0.7
            ]
            if len(over) == 1:
                hits = over
        return hits

    done_keys: set[tuple[str, str]] = set()
    cfg = config["lark_message"]
    if task_list is None or not cfg.get("enabled"):
        return done_keys
    done_keywords = [kw for kw in cfg.get("done_keywords", []) if kw]
    plan_keywords = [kw for kw in cfg.get("plan_keywords", []) if kw]
    if not done_keywords:
        return done_keys

    sensitive = set(cfg.get("sensitive_words", []))
    pending = {
        s.title: s.guid
        for s in task_list.subtasks
        if s.status_to != "已完成" and s.guid
    }
    completed = {s.title for s in task_list.subtasks if s.status_to == "已完成"}

    try:
        candidates = lark_msg.collect(
            cfg["chat_id"],
            done_keywords,
            since,
            until,
            app_id_env=cfg.get("app_id_env", "LARK_APP_ID"),
            app_secret_env=cfg.get("app_secret_env", "LARK_APP_SECRET"),
            sensitive_words=cfg.get("sensitive_words", []),
            chat_name=cfg.get("chat_name", cfg["chat_id"]),
            timeout=cfg.get("timeout", 10),
        )
    except CollectorError as exc:
        logger.error("完成宣告：飞书消息获取失败", extra={"error": str(exc)})
        return done_keys

    client = _lark_task_client(config)
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        for m in candidates:
            if any(word in m.content for word in sensitive):
                continue
            if any(
                m.content.strip().startswith(pk) for pk in plan_keywords
            ):
                continue  # 计划宣告（如「明天：完成作业」）优先，不误判完成
            target = _strip_plan_prefix(m.content, done_keywords)
            if not target:
                continue
            key = (m.sender, m.timestamp.isoformat())

            titles = _match(pending)
            if len(titles) > 1:
                # 一条消息命中多个子任务：全部放弃，避免误伤
                logger.warning(
                    "完成宣告命中多个子任务，放弃处理",
                    extra={"target": target, "hits": titles},
                )
                continue

            if not titles:
                # 无未完成命中：若为已完成条目的宣告（含往轮重放），仅对账落库
                done_hits = _match(completed)
                if done_hits:
                    storage.mark_plans_done_by_title(done_hits[0])
                    done_keys.add(key)
                continue

            title = titles[0]
            try:
                lark_task.complete_task(client, pending[title])
            except CollectorError as exc:
                # 飞书打勾失败：不落库不剔除，下一轮重试
                logger.error(
                    "飞书完成任务失败", extra={"title": title, "error": str(exc)}
                )
                continue
            storage.mark_plans_done_by_title(title)
            done_keys.add(key)
    return done_keys


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

    commits, messages, failed = _collect_all(config, since, until)

    # 任务闭环：滚动 → 计划捕捉 → 清单保障 → 完成宣告
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        rolled = storage.roll_pending_plans(today.isoformat())
    if rolled:
        logger.info("未完成计划滚动至今日", extra={"count": rolled})

    plan_keys = _collect_plan_messages(config, since, until, today)
    task_list = _ensure_daily_tasklist(config, today, failed)
    done_keys = _collect_done_messages(config, since, until, task_list)

    attempted = config.enabled_sources()
    if attempted and len(failed) == len(attempted):
        # 所有数据源都不可用：不生成空日报
        logger.error(
            "所有数据源均失败，不生成日报",
            extra={"failed_sources": failed},
        )
        return False

    issues, issue_keys = _collect_issues(config, since, until, today)
    consumed = issue_keys | plan_keys | done_keys
    if consumed:
        # 已归入问题/计划/完成宣告的消息不再重复出现在协作沟通板块
        messages = [
            m for m in messages
            if (m.sender, m.timestamp.isoformat()) not in consumed
        ]
    members = formatter.aggregate(config.members, commits, [], messages)
    tomorrow_plans = _collect_tomorrow_plans(config, today)

    # AI 摘要（可选环节）：未启用/未配 Key/接口失败均返回空串，不阻断日报
    summary = summarizer.generate_summary(
        today, members, task_list, tomorrow_plans, issues,
        config.get_path("ai", {}) or {}, failed_sources=failed,
    )

    # 任务完成趋势：聚合近 7 个报告日（历史快照 + 今日实时清单），推送三端共用
    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        history = storage.iter_report_data()
    today_tl = (
        {"subtasks": [{"status_to": s.status_to} for s in task_list.subtasks]}
        if task_list and task_list.subtasks
        else None
    )
    task_trend = formatter.compute_task_trend(history, today.isoformat(), today_tl)

    report = formatter.generate(
        members, today, config.team_name, failed_sources=failed,
        tomorrow_plans=tomorrow_plans, issues=issues, task_list=task_list,
        summary=summary, task_trend=task_trend,
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
# 单实例保护
# ---------------------------------------------------------------------------

try:  # Windows 独占文件锁；非 Windows 平台降级为不防护
    import msvcrt
except ImportError:  # pragma: no cover - 部署环境为 Windows
    msvcrt = None  # type: ignore[assignment]

_LOCK_HANDLE: Any = None
_LOCK_OFFSET = 1024  # 锁定远离 PID 内容的字节，避免锁区导致冲突方读取被拒


def _service_lock_path(config: Config) -> Path:
    """服务锁文件路径：与数据库同目录（data/service.lock）。"""

    return Path(config["storage"]["sqlite_path"]).parent / "service.lock"


def _acquire_service_lock(config: Config) -> bool:
    """尝试获取独占服务锁；已有实例在跑时返回 False。

    通过 msvcrt 对锁文件偏移 _LOCK_OFFSET 处的字节加独占锁：进程退出时
    操作系统自动释放，无需清理残留锁文件；锁持有者的 PID 写在文件头
    （锁区之外，冲突方可正常读取）便于排查。
    """

    global _LOCK_HANDLE
    if msvcrt is None:
        return True

    lock_path = _service_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_RDWR|O_CREAT 不截断：避免冲突方 open("w") 清空持有者写入的 PID
        handle = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT), "r+b")
    except OSError as exc:
        logger.warning(
            "服务锁文件不可用，跳过单实例保护", extra={"error": str(exc)}
        )
        return True

    try:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode())
    handle.flush()
    _LOCK_HANDLE = handle
    atexit.register(_release_service_lock)
    return True


def _release_service_lock() -> None:
    """释放服务锁（进程退出时 atexit 自动调用，一般无需显式调用）。"""

    global _LOCK_HANDLE
    if _LOCK_HANDLE is None:
        return
    handle, _LOCK_HANDLE = _LOCK_HANDLE, None
    try:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()


def _read_lock_pid(config: Config) -> str:
    """读取锁文件中记录的持有者 PID（读取失败返回空串）。"""

    try:
        return _service_lock_path(config).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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

    if not _acquire_service_lock(config):
        pid = _read_lock_pid(config)
        logger.error(
            "已有看板服务实例在运行，拒绝重复启动",
            extra={"running_pid": pid or "未知"},
        )
        print(
            f"检测到看板服务已在运行（PID {pid or '未知'}），请勿重复启动；"
            "如需重启请先关闭现有实例。"
        )
        return 1

    try:
        serve(config)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
