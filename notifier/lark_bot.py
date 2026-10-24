"""飞书机器人推送模块（design.md §2、§4.3）。

通过自定义机器人 webhook 发送 Markdown（以交互卡片承载）；
失败重试 2 次，仍失败则记录日志并返回 False。
"""

from __future__ import annotations

from typing import Any

import httpx

from shared.logger import get_logger
from shared.models import DailyReport

logger = get_logger(__name__)


def build_card_payload(report: DailyReport) -> dict:
    """构造交互式卡片消息体。"""

    # 单人日报以成员名命名卡片标题，多人用团队名
    if len(report.members) == 1:
        title = f"{report.members[0].name}的日报 {report.date.isoformat()}"
    else:
        title = f"{report.team_name} 日报 {report.date.isoformat()}"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": report.markdown},
                }
            ],
        },
    }


def _is_success(payload: dict) -> bool:
    # 新版返回 code；旧版自定义机器人返回 StatusCode
    if "code" in payload:
        return payload.get("code") == 0
    return payload.get("StatusCode", payload.get("status_code", -1)) == 0


def send_text(
    webhook: str,
    text: str,
    *,
    timeout: float = 10.0,
    max_retries: int = 2,
    http_client: Any = None,
) -> bool:
    """向飞书机器人 webhook 发送纯文本消息（群回执用）。"""

    http = http_client or httpx.Client(timeout=timeout)
    payload = {"msg_type": "text", "content": {"text": text}}

    for attempt in range(1, max_retries + 1):
        try:
            resp = http.post(webhook, json=payload)
            body = resp.json()
            if _is_success(body):
                return True
            logger.error(
                "飞书机器人拒绝文本消息",
                extra={"attempt": attempt, "response": body},
            )
        except httpx.HTTPError as exc:
            logger.error(
                "飞书机器人文本请求失败",
                extra={"attempt": attempt, "error": str(exc)},
            )

    return False


def send(
    report: DailyReport,
    chat_id: str,
    *,
    webhook: str,
    timeout: float = 10.0,
    max_retries: int = 2,
    http_client: Any = None,
) -> bool:
    """向飞书机器人 webhook 推送日报；成功返回 True，失败返回 False。"""

    http = http_client or httpx.Client(timeout=timeout)
    payload = build_card_payload(report)

    for attempt in range(1, max_retries + 1):
        try:
            resp = http.post(webhook, json=payload)
            body = resp.json()
            if _is_success(body):
                return True
            logger.error(
                "飞书机器人拒绝消息",
                extra={"attempt": attempt, "response": body, "chat_id": chat_id},
            )
        except httpx.HTTPError as exc:
            logger.error(
                "飞书机器人请求失败",
                extra={"attempt": attempt, "error": str(exc), "chat_id": chat_id},
            )

    return False
