"""邮件推送模块（design.md §2、§4.3）。

通过 SMTP 发送 HTML 格式日报；失败重试 2 次，仍失败则记录日志并返回 False。
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any

from shared.logger import get_logger
from shared.models import DailyReport

logger = get_logger(__name__)


def _build_message(report: DailyReport, sender: str, recipients: list[str]) -> EmailMessage:
    # 单人日报主题：研一+姓名+日期+智能日报（老师指定格式）；多人用团队名
    if len(report.members) == 1:
        subject = f"研一{report.members[0].name}{report.date.isoformat()}智能日报"
    else:
        subject = f"{report.team_name} 智能日报 {report.date.isoformat()}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content("请使用支持 HTML 的客户端查看本日报。")
    message.add_alternative(report.html, subtype="html")
    return message


def send(
    report: DailyReport,
    recipients: list[str],
    *,
    smtp_host: str,
    smtp_port: int,
    sender: str,
    use_ssl: bool = True,
    username_env: str = "SMTP_USERNAME",
    password_env: str = "SMTP_PASSWORD",
    timeout: float = 10.0,
    max_retries: int = 2,
    smtp_server: Any = None,
) -> bool:
    """发送 HTML 日报；成功返回 True，失败返回 False。"""

    message = _build_message(report, sender, recipients)

    for attempt in range(1, max_retries + 1):
        server = smtp_server
        try:
            if server is None:
                if use_ssl:
                    server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout)
                else:
                    server = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
                    server.starttls()

            username = os.environ.get(username_env, "")
            password = os.environ.get(password_env, "")
            if username:
                server.login(username, password)
            server.sendmail(sender, recipients, message.as_string())

            if smtp_server is None:
                server.quit()
            return True

        except smtplib.SMTPException as exc:
            logger.error(
                "邮件发送失败",
                extra={"attempt": attempt, "error": str(exc)},
            )
            if server is not None and smtp_server is None:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass
            if attempt >= max_retries:
                return False

    return False
