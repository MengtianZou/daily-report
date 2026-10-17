"""数据模型（design.md §3）。

只承载语义、不绑定具体业务逻辑；使用标准库 dataclass 作为实现载体。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class CommitFile:
    """提交中单个文件的变更。"""

    filename: str            # 文件路径
    status: str = "modified"  # added / modified / removed / renamed
    additions: int = 0
    deletions: int = 0
    patch: str = ""           # GitHub unified diff 文本
    blob_url: str = ""        # 文件在 GitHub 上的查看地址


@dataclass(slots=True)
class CommitRecord:
    """代码提交记录。"""

    author: str            # 提交者（GitHub 用户名）
    message: str           # 提交信息（Commit Message）
    timestamp: datetime    # 提交时间
    repo: str              # 仓库名称
    additions: int         # 新增行数
    deletions: int         # 删除行数
    files_changed: int     # 变更文件数
    html_url: str = ""     # 提交在 GitHub 上的查看地址
    files: list[CommitFile] = field(default_factory=list)  # 变更文件明细


@dataclass(slots=True)
class TaskRecord:
    """任务变更记录。"""

    assignee: str          # 负责人（飞书用户名）
    title: str             # 任务标题
    status_from: str       # 原状态
    status_to: str         # 新状态
    updated_at: datetime  # 变更时间
    parent_guid: str = ""  # 父任务 GUID，空表示顶层任务
    subtasks: list["TaskRecord"] = field(default_factory=list)  # 子任务列表
    guid: str = ""         # 任务自身 GUID（飞书写操作定位用）

    @property
    def is_subtask(self) -> bool:
        return bool(self.parent_guid)

    @property
    def progress(self) -> tuple[int, int]:
        """返回 (已完成子任务数, 总子任务数)，无子任务返回 (0, 0)。"""
        if not self.subtasks:
            return (0, 0)
        completed = sum(1 for s in self.subtasks if s.status_to == "已完成")
        return (completed, len(self.subtasks))


@dataclass(slots=True)
class MessageRecord:
    """消息记录。"""

    sender: str            # 发送者（飞书用户名）
    content: str           # 消息内容（纯文本）
    timestamp: datetime   # 发送时间
    chat_name: str         # 群名称


@dataclass(slots=True)
class MemberReport:
    """成员报告：单个成员在采集窗口内的全部工作记录。"""

    name: str                                  # 成员姓名
    github_username: str                       # GitHub 用户名
    commits: list[CommitRecord] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    messages: list[MessageRecord] = field(default_factory=list)


@dataclass(slots=True)
class PlanItem:
    """明日计划条目。"""

    title: str            # 计划内容
    owner: str = ""       # 负责人姓名，空表示团队
    source: str = "auto"  # auto：飞书未完成任务；manual：看板手动添加
    plan_id: int = 0      # manual 条目的 plans 表 id，auto 恒为 0


@dataclass(slots=True)
class IssueItem:
    """问题与解决条目。"""

    problem: str          # 问题描述
    solution: str = ""    # 解决方案，空表示待定
    owner: str = ""       # 负责人姓名，空表示团队
    status: str = "处理中"  # 处理中 / 已解决
    source: str = "auto"  # auto：群消息提取；manual：看板手动添加
    issue_id: int = 0     # manual 条目的 issues 表 id，auto 恒为 0
    msg_key: str = ""     # 来源消息键（sender|timestamp），认领补充方案后去重用


@dataclass(slots=True)
class DailyReport:
    """每日报告：管道最终产物。"""

    date: date                                   # 日报日期
    team_name: str                               # 团队名称
    members: list[MemberReport] = field(default_factory=list)
    generated_at: datetime | None = None         # 生成时间
    markdown: str = ""                           # 完整 Markdown 日报
    html: str = ""                               # 完整 HTML 日报
    failed_sources: list[str] = field(default_factory=list)  # 失败数据源标识
    tomorrow_plans: list[PlanItem] = field(default_factory=list)  # 明日计划
    issues: list[IssueItem] = field(default_factory=list)  # 问题与解决
    task_list: TaskRecord | None = None  # 当日任务清单（团队级板块）
    summary: str = ""  # AI 生成的今日总结（未启用/生成失败时为空，三端均不渲染）
    task_trend: list[dict] = field(default_factory=list)
    # 任务完成趋势（近 N 个报告日聚合）：[{date, done, total, pct}]，日期正序；
    # 由管道在生成前聚合历史快照与当日实时数据，邮件/飞书/Markdown 三端共用
    # （看板另有前端实时聚合版本，不读此字段）
