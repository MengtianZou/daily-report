"""notifier/email.py 单元测试（Mock SMTP）。"""

from __future__ import annotations

import smtplib
from datetime import datetime
from email import message_from_string
from email.header import decode_header, make_header

from notifier import email
from shared.models import DailyReport, MemberReport


def _make_report() -> DailyReport:
    return DailyReport(
        date=datetime(2026, 9, 25).date(),
        team_name="研发一组",
        generated_at=datetime(2026, 9, 25, 18, 0),
        markdown="# 日报",
        html="<h1>日报</h1>",
    )


class FakeSMTP:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, list[str], str]] = []
        self.logged_in = False
        self.quitted = False

    def login(self, username: str, password: str) -> None:
        self.logged_in = True

    def sendmail(self, sender: str, recipients: list[str], raw: str) -> None:
        if self.fail:
            raise smtplib.SMTPException("send failed")
        self.sent.append((sender, list(recipients), raw))

    def quit(self) -> None:
        self.quitted = True


def test_send_email_success() -> None:
    server = FakeSMTP()
    ok = email.send(
        _make_report(),
        ["leader@company.com"],
        smtp_host="smtp.company.com",
        smtp_port=465,
        sender="report@company.com",
        smtp_server=server,
    )
    assert ok is True
    assert len(server.sent) == 1
    sender, recipients, raw = server.sent[0]
    assert sender == "report@company.com"
    assert recipients == ["leader@company.com"]

    parsed = message_from_string(raw)
    subject = str(make_header(decode_header(parsed["Subject"])))
    assert subject == "研发一组 智能日报 2026-09-25"  # 主题含日期与团队名

    html_part = next(
        part for part in parsed.walk() if part.get_content_subtype() == "html"
    )
    assert html_part.get_payload(decode=True).decode().strip() == "<h1>日报</h1>"


def test_subject_single_member_uses_teacher_format() -> None:
    report = _make_report()
    report.members = [
        MemberReport(name="刘政剑", github_username="", commits=[], tasks=[], messages=[])
    ]

    message = email._build_message(report, sender="a@b.c", recipients=["x@y.z"])
    subject = str(make_header(decode_header(message["Subject"])))
    assert subject == "研一刘政剑2026-09-25智能日报"


def test_send_email_retries_then_returns_false() -> None:
    server = FakeSMTP(fail=True)
    ok = email.send(
        _make_report(),
        ["leader@company.com"],
        smtp_host="smtp.company.com",
        smtp_port=465,
        sender="report@company.com",
        smtp_server=server,
    )
    assert ok is False
    assert len(server.sent) == 0


def test_send_email_logs_in_when_credentials_present(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pass")
    server = FakeSMTP()
    email.send(
        _make_report(),
        ["leader@company.com"],
        smtp_host="h",
        smtp_port=465,
        sender="s",
        username_env="SMTP_USERNAME",
        password_env="SMTP_PASSWORD",
        smtp_server=server,
    )
    assert server.logged_in is True
