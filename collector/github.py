"""GitHub 采集模块（design.md §2、§4.1）。

调用 GitHub REST API 获取指定仓库在时间范围内的 Commit 记录：
- 列表接口分页（Link header, rel="next"）；
- 逐条获取提交详情以得到变更统计；
- 超时 / 5xx 重试 3 次（间隔 5s）；
- 触发限流（403 + X-RateLimit-Remaining: 0）时等待 reset 后重试；
- 重试耗尽则记录错误日志并返回已采集到的记录（失败仓库跳过）。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from shared.errors import CollectorError
from shared.logger import get_logger
from shared.models import CommitFile, CommitRecord

logger = get_logger(__name__)

_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
_ISO_TRAILING_Z = "Z"


def _parse_datetime(value: str) -> datetime:
    if value.endswith(_ISO_TRAILING_Z):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _next_page_url(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        match = _NEXT_LINK_RE.search(part.strip())
        if match:
            return match.group(1)
    return None


def _wait_rate_limit(resp: Any, sleeper: Any) -> bool:
    """若处于限流状态，等待到 reset 时刻；返回是否进行了等待。"""

    try:
        if int(resp.headers.get("X-RateLimit-Remaining", "1")) > 0:
            return False
        reset_at = int(resp.headers["X-RateLimit-Reset"])
    except (KeyError, TypeError, ValueError):
        return False

    wait_seconds = max(reset_at - int(time.time()), 0)
    logger.error("GitHub API 触发限流，等待 reset", extra={"wait_seconds": wait_seconds})
    sleeper(wait_seconds)
    return True


def _get(
    url: str,
    http: Any,
    *,
    max_retries: int,
    interval: float,
    sleeper: Any,
) -> Any:
    """带重试的 GET；返回最后一个响应，重试耗尽时抛 CollectorError。"""

    last_response = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = http.get(url)
        except httpx.HTTPError as exc:
            logger.error(
                "GitHub 请求异常，准备重试",
                extra={"attempt": attempt, "url": url, "error": str(exc)},
            )
            if attempt < max_retries:
                sleeper(interval)
                continue
            raise CollectorError(f"GitHub 请求失败：{exc}", source="github") from exc

        last_response = resp
        if resp.status_code == 403 and _wait_rate_limit(resp, sleeper):
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            logger.error(
                "GitHub 服务端错误，准备重试",
                extra={"attempt": attempt, "status": resp.status_code},
            )
            if attempt < max_retries:
                sleeper(interval)
                continue
        return resp

    return last_response


def _build_commit_record(repo: str, summary: dict, detail: dict) -> CommitRecord:
    commit_info = summary.get("commit", {})
    author_obj = summary.get("author") or {}
    author = author_obj.get("login") or commit_info.get("author", {}).get("name", "")

    stats = detail.get("stats", {})
    raw_files = detail.get("files", [])
    sha = summary.get("sha", "")
    files = [
        CommitFile(
            filename=f.get("filename", ""),
            status=f.get("status", "modified"),
            additions=int(f.get("additions", 0)),
            deletions=int(f.get("deletions", 0)),
            patch=f.get("patch", "") or "",
            blob_url=f.get("blob_url", "") or "",
        )
        for f in raw_files
    ]
    html_url = detail.get("html_url") or (
        f"https://github.com/{repo}/commit/{sha}" if sha else ""
    )
    return CommitRecord(
        author=author,
        message=commit_info.get("message", "").split("\n", 1)[0].strip(),
        timestamp=_parse_datetime(commit_info["author"]["date"]),
        repo=repo,
        additions=int(stats.get("additions", 0)),
        deletions=int(stats.get("deletions", 0)),
        files_changed=len(files),
        html_url=html_url,
        files=files,
    )


def collect(
    repos: list[str],
    since: datetime,
    until: datetime,
    *,
    base_url: str = "https://api.github.com",
    token_env: str = "GITHUB_TOKEN",
    per_page: int = 30,
    timeout: float = 10.0,
    max_retries: int = 3,
    retry_interval: float = 5.0,
    http_client: Any = None,
    sleeper: Any = time.sleep,
) -> list[CommitRecord]:
    """采集多个仓库在 [since, until] 内的全部 Commit 记录。"""

    token = os.environ.get(token_env, "")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    http = http_client or httpx.Client(timeout=timeout, headers=headers)
    records: list[CommitRecord] = []

    for repo in repos:
        try:
            records.extend(
                _collect_repo(
                    repo,
                    since,
                    until,
                    base_url=base_url,
                    per_page=per_page,
                    http=http,
                    max_retries=max_retries,
                    retry_interval=retry_interval,
                    sleeper=sleeper,
                )
            )
        except CollectorError as exc:
            # 单个仓库失败不阻断其他仓库；数据源级失败由上层根据需要标注
            logger.error("仓库采集失败，跳过该仓库", extra={"repo": repo, "error": str(exc)})

    return records


def _collect_repo(
    repo: str,
    since: datetime,
    until: datetime,
    *,
    base_url: str,
    per_page: int,
    http: Any,
    max_retries: int,
    retry_interval: float,
    sleeper: Any,
) -> list[CommitRecord]:
    # isoformat() 的时区偏移含 '+'/':'，在查询串中 '+' 会被解释为空格，
    # 必须百分号编码，否则 GitHub 会静默返回空列表。
    list_url = (
        f"{base_url.rstrip('/')}/repos/{repo}/commits"
        f"?since={quote(since.isoformat(), safe='')}"
        f"&until={quote(until.isoformat(), safe='')}"
        f"&per_page={per_page}&page=1"
    )

    summaries: list[dict] = []
    url: str | None = list_url
    while url:
        resp = _get(
            url,
            http,
            max_retries=max_retries,
            interval=retry_interval,
            sleeper=sleeper,
        )
        if resp is None or resp.status_code != 200:
            status = getattr(resp, "status_code", "no-response")
            raise CollectorError(f"提交列表请求失败：HTTP {status}", source="github")

        summaries.extend(resp.json())
        next_url = _next_page_url(resp.headers.get("Link"))
        if next_url:
            # 相对路径补全（GitHub 返回的是绝对地址，此处为防御性处理）
            url = urljoin(url, next_url)
        else:
            url = None

    records: list[CommitRecord] = []
    for summary in summaries:
        sha = summary.get("sha")
        detail_url = f"{base_url.rstrip('/')}/repos/{repo}/commits/{sha}"
        detail_resp = _get(
            detail_url,
            http,
            max_retries=max_retries,
            interval=retry_interval,
            sleeper=sleeper,
        )
        if detail_resp is None or detail_resp.status_code != 200:
            logger.error("提交详情获取失败，跳过该提交", extra={"repo": repo, "sha": sha})
            continue
        records.append(_build_commit_record(repo, summary, detail_resp.json()))

    return records
