"""智能日报生成器 — 主编排入口（design.md §2 main.py，specs/no-dashboard.md）。

按“采集 → 聚合 → 生成 → 存储 → 推送”顺序编排管道，处理全局异常并记录执行状态。

用法：
    python main.py                 # 默认：启动后台服务（15 秒采集 + 定时推送）
    python main.py --serve         # 同上（显式写法）
    python main.py --run           # 执行一次完整流程（非工作日自动跳过）
    python main.py --run --dry-run # 只采集、生成，不推送
    python main.py --check         # 检查 API / 邮件 / 机器人配置
    python main.py --status        # 打印今日捕获摘要（提交/消息/任务/计划/问题）
    python main.py --view          # 在浏览器中打开最新日报 HTML
    python main.py --view 2026-09-25  # 打开指定日期的日报 HTML
    python main.py --config x.yaml # 指定配置文件

凭证：配置文件同目录下的 .env 会被自动加载（见 .env 模板），无需每次手动设置。
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import socket
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from collector import github, lark_msg, lark_task
from collector._lark_client import LarkClient
from generator import formatter, summarizer
from notifier import archive, email, lark_bot
from shared.config import Config, load_config
from shared.envfile import load_envfile
from shared.errors import CollectorError, ConfigurationError, NotifierError
from shared.logger import get_logger
from shared.models import IssueItem, PlanItem, TaskRecord
from shared.storage import ReportStorage

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
_RESOLVED_HINTS = (
    "已解决",
    "解决了",
    "得到解决",
    "已修复",
    "修复了",
    "修复为",
    "修好了",
    "解决方案：",
    "已排查",
    "原因是",
    "原因：",
)


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
    receipts: list[str] | None = None,
) -> tuple[list[IssueItem], set[tuple[str, str]]]:
    """组装问题与解决：自动（群消息按问题关键词提取）+ 手动（issues 表）。

    返回 (条目列表, 问题消息键集合)。判定为「问题描述」的消息即使同时命中
    协作沟通关键词，也由 run_pipeline 从沟通板块剔除，保证同一句话只出现在
    问题与解决；「已解决/原因是」类总结消息自动挂为同负责人最近问题的方案。
    receipts 非 None 时，本轮新记录的问题 / 新挂载的方案追加回执文案
    （自动条目无持久态，靠进程内去重，重启后当日条目最多重发一次）。
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
                                if receipts is not None and _receipt_mark(
                                    f"resolved|{key}"
                                ):
                                    receipts.append(
                                        f"✅ 问题已更新方案：{item.problem}"
                                    )
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
                    if receipts is not None and _receipt_mark(f"issue|{key}"):
                        receipts.append(f"⚠️ 已记录问题：{m.content}")

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

# 飞书群回执：进程内已回执键集合（specs/no-dashboard.md §2.2）。
# plans 靠 plans 表 msg_key、done 靠飞书完成态天然跨进程去重；
# issues/方案挂载无持久态，靠本集合保证进程内每键只回执一次。
_RECEIPT_SENT: set[str] = set()


def _receipt_mark(key: str) -> bool:
    """登记回执键；首次登记返回 True（应发回执），重复返回 False。"""

    if key in _RECEIPT_SENT:
        return False
    _RECEIPT_SENT.add(key)
    return True


def _send_receipts(config: Config, receipts: list[str]) -> None:
    """把本轮新捕获条目合并为一条群回执发出；失败仅记日志，绝不阻断管道。

    前缀「【智能日报】」与日报卡片标题同词，保证过机器人 webhook 的
    自定义关键词安全校验。
    """

    if not receipts or not config["lark_bot"].get("enabled"):
        return
    cfg = config["lark_bot"]
    text = "【智能日报】\n" + "\n".join(receipts)
    try:
        ok = lark_bot.send_text(
            cfg["webhook"], text, timeout=cfg.get("timeout", 10)
        )
    except Exception as exc:  # 回执是附加环节，任何异常都不阻断管道
        logger.warning("群回执发送异常", extra={"error": str(exc)})
        return
    if not ok:
        logger.warning("群回执被机器人拒绝", extra={"count": len(receipts)})


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
    receipts: list[str] | None = None,
) -> set[tuple[str, str]]:
    """捕捉群消息中的明日计划（命中 plan_keywords），落 plans 表。

    done 优先于 plan：以完成关键词开头的消息（如「完成汇报PPT」）交由
    _collect_done_messages 处理；仅含「完成」二字的计划消息（如
    「明天：完成作业」）仍按计划捕捉。触发词支持前导或「触发词：」
    中段写法，编号列表（1.…。2.…）拆分为多条。
    返回已捕捉消息键集合，供 run_pipeline 从协作沟通板块剔除；早前轮次
    已入库的消息每轮也加入剔除集合，保证不回流到沟通板块。
    receipts 非 None 时，本轮新收录的条目追加回执文案（已入库的不回执）。
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
                if receipts is not None and _receipt_mark(f"plan|{key}|{title}"):
                    receipts.append(f"📌 已收录为明日计划：{title}")
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
    receipts: list[str] | None = None,
) -> set[tuple[str, str]]:
    """捕捉完成宣告（命中 done_keywords）并同步完成状态。

    匹配规则（防误伤梯度）：消息与子任务标题先做双侧归一化（剥前导完成
    触发词 + 剥尾部完成宣告词，兼容「完成 xxx」与「xxx 已完成」两种写法），
    再按 归一化精确 → 双向包含 → 唯一模糊兜底（字符重合度，仅唯一超阈值
    才命中）匹配；一条消息命中多个子任务全部放弃。命中后先飞书打勾（失败
    下轮重试），再落库标完成；对已完成子任务的完成宣告只做落库对账并从
    沟通剔除（防往轮消息重复处理）。
    返回已消费消息键集合。receipts 非 None 时，本轮新打勾成功的条目追加
    回执文案（对账分支不回执）。
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
            if receipts is not None and _receipt_mark(f"done|{title}"):
                receipts.append(f"✅ 已完成：{title}")
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

    receipts: list[str] = []
    plan_keys = _collect_plan_messages(config, since, until, today, receipts)
    task_list = _ensure_daily_tasklist(config, today, failed)
    done_keys = _collect_done_messages(config, since, until, task_list, receipts)

    attempted = config.enabled_sources()
    if attempted and len(failed) == len(attempted):
        # 所有数据源都不可用：不生成空日报
        logger.error(
            "所有数据源均失败，不生成日报",
            extra={"failed_sources": failed},
        )
        return False

    issues, issue_keys = _collect_issues(config, since, until, today, receipts)
    _send_receipts(config, receipts)  # 采集轮也发：群消息捕捉的实时确认
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
        # 日报网页归档（附加环节，失败不阻断）：仅真实推送轮次触发
        archive.publish(config, report)

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
# 采集间隔配置（specs/no-dashboard.md §2.1）
# ---------------------------------------------------------------------------

_MIN_COLLECT_INTERVAL = 10
_DEFAULT_COLLECT_INTERVAL = 15


def collect_interval(config: Config) -> int:
    """读取 schedule.collect_interval_seconds；非法值回落默认 15，下限钳制 10。"""

    raw = ((config.get("schedule") or {}).get("collect_interval_seconds"))
    try:
        value = int(raw) if raw is not None else _DEFAULT_COLLECT_INTERVAL
    except (TypeError, ValueError):
        value = _DEFAULT_COLLECT_INTERVAL
    return max(value, _MIN_COLLECT_INTERVAL)


# ---------------------------------------------------------------------------
# 调度线程（AutoCollector / ScheduledPusher，迁自 webapp/server.py，specs/no-dashboard.md §2.1）
# ---------------------------------------------------------------------------

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
            except Exception as exc:
                logger.warning(
                    "自动采集失败，将在下一轮重试",
                    extra={"error": str(exc)},
                )
            self._stop_event.wait(self._interval)

    @staticmethod
    def _default_collect(config) -> None:
        run_pipeline(config, dry_run=True, force=True)


class ScheduledPusher:
    """守护线程：每天在 schedule.push_time 到点时执行一次完整管道（含推送）。

    防重：记录上次推送的日期，同一天只推一次；错过时间（服务晚于 push_time 启动）
    当天内补推一次。推送日期同时写入 SQLite meta 表，多实例并存时
    后启动的实例读到当日标记即跳过，避免重复推送。
    """

    CHECK_INTERVAL = 20
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
            except Exception as exc:
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
                self._last_push_date = today
                return
            self._last_push_date = today
            self._run(self._config)
            self._write_push_marker(today)

    def _sqlite_path(self) -> str:
        return (self._config.get("storage") or {}).get("sqlite_path", "")

    def _read_push_marker(self) -> str:
        db_path = self._sqlite_path()
        if not db_path:
            return ""
        try:
            with ReportStorage(db_path) as storage:
                return storage.get_meta(self.PUSH_MARKER)
        except Exception:
            return ""

    def _write_push_marker(self, date_str: str) -> None:
        db_path = self._sqlite_path()
        if not db_path:
            return
        try:
            with ReportStorage(db_path) as storage:
                storage.set_meta(self.PUSH_MARKER, date_str)
        except Exception:
            logger.warning("推送标记写入失败", extra={"date": date_str})

    @staticmethod
    def _default_run(config) -> None:
        run_pipeline(config)


def run_service(config: Config, *, stop_event: threading.Event | None = None) -> None:
    """启动后台服务并阻塞：15 秒采集 + 定时推送；stop_event 置位或 Ctrl+C 退出。"""

    interval = collect_interval(config)
    collector = AutoCollector(config, interval)
    pusher = ScheduledPusher(config)

    archive_enabled = bool((config.get("archive") or {}).get("enabled", False))
    print(f"Daily Report Service started (interval={interval}s). Ctrl+C to stop.")
    if pusher.enabled:
        push_time = (config.get("schedule") or {}).get("push_time")
        print(f"  Daily push scheduled at {push_time}.")
    if archive_enabled:
        print("  GitHub Pages archive enabled.")

    collector.start()
    pusher.start()

    stop = stop_event or threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        pusher.stop()
        print("Service stopped.")


# ---------------------------------------------------------------------------
# --status 命令（specs/no-dashboard.md §2.3）
# ---------------------------------------------------------------------------

def run_status(config: Config) -> int:
    """打印今日捕获摘要；全读 SQLite，不触网。无报告时返回 1 并引导。"""

    now = datetime.now().astimezone()
    today_iso = now.date().isoformat()
    tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        reports = storage.iter_report_data(limit=1)
        latest = reports[0] if reports else None

        plans_tomorrow = list(storage.iter_plans(tomorrow_iso))
        issues_today = list(storage.iter_issues(today_iso))
        push_date = storage.get_meta(ScheduledPusher.PUSH_MARKER)
        archive_date = storage.get_meta("archive_last_date")

    if latest and latest.get("date") == today_iso:
        print(f"=== {config.team_name} · 今日日报（{today_iso}） ===")
        print(f"生成时间：{latest.get('generated_at', '—')}")
        commits = latest.get("commits", []) or []
        messages = latest.get("messages", []) or []
        task_list = latest.get("task_list") or {}
        subtasks = task_list.get("subtasks", []) or []
        done_n = sum(1 for s in subtasks if s.get("status_to") == "已完成")
        total_n = len(subtasks)
        print(f"GitHub 提交：{len(commits)}")
        print(f"协作消息：{len(messages)}")
        print(f"今日任务清单：{done_n}/{total_n} 已完成")
        for s in subtasks:
            mark = "✅" if s.get("status_to") == "已完成" else "⬜"
            print(f"  {mark} {s.get('title', '（无标题）')}")

        plans = latest.get("tomorrow_plans", []) or []
        if plans:
            print(f"明日计划（{len(plans)}）：")
            for p in plans:
                print(f"  📌 {p.get('title')}")

        if issues_today:
            print(f"问题与解决（{len(issues_today)}）：")
            for i in issues_today:
                tag = "✅" if i.get("status") == "已解决" else "⚠️"
                owner = f" [{i['owner']}]" if i.get("owner") else ""
                print(f"  {tag} {i['problem']}{owner}")

        if push_date:
            print(f"最近推送：{push_date}")
        if archive_date:
            print(f"最近归档：{archive_date}")
        return 0

    print(f"=== {config.team_name} · {today_iso} 尚无日报 ===")
    if not push_date:
        print("  （服务未在运行或尚未到 10:35 定时推送）")
    if plans_tomorrow:
        print(f"明日计划已捕获 {len(plans_tomorrow)} 条：")
        for p in plans_tomorrow:
            print(f"  📌 {p['title']}")
    if issues_today:
        print(f"今日问题已记录 {len(issues_today)} 条")
    return 1


# ---------------------------------------------------------------------------
# 日报页面查看（改造为直读 reports 表 html 字段，specs/no-dashboard.md §2.4）
# ---------------------------------------------------------------------------

def view_report(
    config: Config,
    date_str: str | None,
    *,
    opener: Any = webbrowser.open,
) -> bool:
    """直读 reports 表 html 字段落盘并浏览器打开；无日报返回 False。"""

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        if date_str:
            report_row = storage.get_report(date_str, config.team_name)
        else:
            rows = storage.list_reports(limit=1)
            if rows:
                date_str = rows[0]["date"]
                report_row = storage.get_report(date_str, config.team_name)
            else:
                report_row = None

    if not report_row or not report_row.get("html"):
        logger.info("暂无可用的日报 HTML")
        print("暂无可用的日报 HTML。")
        return False

    out_path = (
        Path(config["storage"]["sqlite_path"]).parent
        / f"report-{date_str}.html"
    )
    out_path.write_text(report_row["html"], encoding="utf-8")
    opener(out_path.resolve().as_uri())
    logger.info("已在浏览器打开日报 HTML", extra={"path": str(out_path)})
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
        help="执行一次日报采集与生成（默认动作是启动后台服务）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只采集生成，不推送（配合 --run）")
    parser.add_argument("--date", default=None, help="指定日报日期（YYYY-MM-DD，配合 --run）")
    parser.add_argument(
        "--view",
        nargs="?",
        const="",
        default=None,
        help="在浏览器中打开日报 HTML（可选日期 YYYY-MM-DD，默认最新）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="打印今日捕获摘要（提交/消息/任务/计划/问题）",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="启动后台服务（默认动作；含 15s 采集 + 定时推送）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    env_path = Path(args.config).resolve().parent / ".env"
    load_envfile(env_path)

    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        logger.error("配置加载失败", extra={"error": str(exc)})
        return 2

    if args.check:
        return 0 if run_check(config) else 1

    if args.status:
        return run_status(config)

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

    # 默认动作（含显式 --serve）：启动后台服务
    if not _acquire_service_lock(config):
        pid = _read_lock_pid(config)
        logger.error(
            "已有后台服务实例在运行，拒绝重复启动",
            extra={"running_pid": pid or "未知"},
        )
        print(
            f"检测到服务已在运行（PID {pid or '未知'}），请勿重复启动；"
            "如需重启请先关闭现有实例。"
        )
        return 1

    try:
        run_service(config)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
