"""日报网页归档（specs/archive.md）。

每天日报推送轮次结束后，把当天日报 HTML 发布到 GitHub Pages（gh-pages
孤儿分支根目录）：`YYYY-MM-DD.html` 为当日日报，`index.html` 为按日期
倒序的归档索引。走 GitHub Contents API（httpx），token 走环境变量，
需 contents:write 权限。

归档是附加环节：任何失败（网络 / 403 权限 / 限流）只记日志，绝不阻断
日报主管道；同一天只归档一次（meta 表跨进程节流），失败后 10 分钟
冷却期内不重试。
"""

from __future__ import annotations

import base64
import html as html_module
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from generator.formatter import report_title
from shared.errors import NotifierError
from shared.logger import get_logger
from shared.models import DailyReport
from shared.storage import ReportStorage

logger = get_logger(__name__)

# 失败后的重试冷却（秒）
_RETRY_COOLDOWN = 600

_META_LAST_DATE = "archive_last_date"
_META_ATTEMPT_AT = "archive_attempt_at"


# ---------------------------------------------------------------------------
# GitHub API 交互
# ---------------------------------------------------------------------------

def _headers(config: Any) -> dict[str, str]:
    import os

    cfg = config["archive"]
    token = os.environ.get(cfg.get("token_env", "GITHUB_TOKEN"), "")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    """单次请求；网络异常包装为 NotifierError，由调用方判定状态码。"""

    try:
        return httpx.request(
            method, url, headers=headers, json=json_body,
            params=params, timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise NotifierError(f"GitHub 归档请求失败：{exc}", source="archive") from exc


def _ensure_branch(
    repo: str, branch: str, *, headers: dict, base_url: str, timeout: float
) -> None:
    """保障目标分支存在：不存在时从仓库默认分支 HEAD 创建。"""

    resp = _request(
        "GET", f"{base_url}/repos/{repo}/branches/{branch}",
        headers=headers, timeout=timeout,
    )
    if resp.status_code == 200:
        return
    if resp.status_code != 404:
        raise NotifierError(
            f"查询分支 {branch} 失败：HTTP {resp.status_code}", source="archive"
        )

    repo_resp = _request(
        "GET", f"{base_url}/repos/{repo}", headers=headers, timeout=timeout
    )
    if repo_resp.status_code != 200:
        raise NotifierError(
            f"查询仓库信息失败：HTTP {repo_resp.status_code}", source="archive"
        )
    default = repo_resp.json().get("default_branch", "main")
    head = _request(
        "GET", f"{base_url}/repos/{repo}/git/ref/heads/{default}",
        headers=headers, timeout=timeout,
    )
    if head.status_code != 200:
        raise NotifierError(
            f"获取默认分支 HEAD 失败：HTTP {head.status_code}", source="archive"
        )
    sha = head.json()["object"]["sha"]
    create = _request(
        "POST", f"{base_url}/repos/{repo}/git/refs",
        headers=headers,
        json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        timeout=timeout,
    )
    if create.status_code not in (201, 409):  # 409：并发下已存在，视为成功
        raise NotifierError(
            f"创建分支 {branch} 失败：HTTP {create.status_code}", source="archive"
        )
    logger.info("已创建归档分支", extra={"repo": repo, "branch": branch})


def _ensure_pages_enabled(
    repo: str, branch: str, *, headers: dict, base_url: str, timeout: float
) -> None:
    """保障 GitHub Pages 已启用且来源为目标分支根目录（409 视为已启用）。"""

    resp = _request(
        "GET", f"{base_url}/repos/{repo}/pages", headers=headers, timeout=timeout
    )
    if resp.status_code in (200, 409):
        return
    if resp.status_code != 404:
        raise NotifierError(
            f"查询 Pages 状态失败：HTTP {resp.status_code}", source="archive"
        )
    create = _request(
        "POST", f"{base_url}/repos/{repo}/pages",
        headers=headers,
        json_body={"source": {"branch": branch, "path": "/"}},
        timeout=timeout,
    )
    # 409：已启用；422：来源配置不被接受（如 Pages 已从其他分支启用）
    if create.status_code not in (201, 409):
        raise NotifierError(
            f"启用 GitHub Pages 失败：HTTP {create.status_code}", source="archive"
        )
    logger.info("已启用 GitHub Pages", extra={"repo": repo, "branch": branch})


def _put_file(
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
    *,
    headers: dict,
    base_url: str,
    timeout: float,
) -> None:
    """创建或更新单个文件（404 → 新建，否则带 sha 更新）。"""

    url = f"{base_url}/repos/{repo}/contents/{path}"
    resp = _request(
        "GET", url, headers=headers, params={"ref": branch}, timeout=timeout
    )
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if resp.status_code == 200:
        body["sha"] = resp.json()["sha"]
    elif resp.status_code != 404:
        raise NotifierError(
            f"查询文件 {path} 失败：HTTP {resp.status_code}", source="archive"
        )
    put = _request(
        "PUT", url, headers=headers, json_body=body, timeout=timeout
    )
    if put.status_code not in (200, 201):
        raise NotifierError(
            f"写入文件 {path} 失败：HTTP {put.status_code}", source="archive"
        )


# ---------------------------------------------------------------------------
# 归档索引
# ---------------------------------------------------------------------------

def _manifest_path(config: Any) -> Path:
    return Path(config["storage"]["sqlite_path"]).parent / "archive" / "manifest.json"


def _load_manifest(config: Any) -> dict[str, dict]:
    path = _manifest_path(config)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(config: Any, manifest: dict[str, dict]) -> None:
    path = _manifest_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _render_index(team_name: str, manifest: dict[str, dict]) -> str:
    """按日期倒序渲染归档索引页（纯内联样式静态页）。"""

    rows = []
    for date_iso in sorted(manifest, reverse=True):
        entry = manifest[date_iso]
        title = html_module.escape(str(entry.get("title", "")))
        done = int(entry.get("done", 0))
        total = int(entry.get("total", 0))
        progress = (
            f"{done}/{total}" if total else "—"
        )
        rows.append(
            f'<li><span class="date">{date_iso}</span>'
            f'<a href="{date_iso}.html">{title}</a>'
            f'<span class="progress">任务完成 {progress}</span></li>'
        )
    items = "\n".join(rows) or '<li class="empty">暂无归档</li>'
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html_module.escape(team_name)} · 日报归档</title>\n"
        "<style>\n"
        "body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
        "max-width:720px;margin:40px auto;padding:0 16px;color:#1f2328;"
        "background:#fff}\n"
        "h1{font-size:22px;border-bottom:2px solid #16a34a;padding-bottom:8px}\n"
        "ul{list-style:none;padding:0}\n"
        "li{display:flex;gap:12px;align-items:baseline;padding:10px 4px;"
        "border-bottom:1px solid #eef0f3}\n"
        ".date{color:#57606a;font-variant-numeric:tabular-nums;min-width:96px}\n"
        "a{color:#0969da;text-decoration:none}\n"
        "a:hover{text-decoration:underline}\n"
        ".progress{margin-left:auto;color:#57606a;font-size:14px}\n"
        ".empty{color:#57606a;justify-content:center}\n"
        "</style>\n</head>\n<body>\n"
        f"<h1>{html_module.escape(team_name)} · 日报归档</h1>\n"
        f"<ul>\n{items}\n</ul>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _throttled(storage: ReportStorage, today_iso: str) -> str:
    """判断是否应跳过本次归档；返回原因（空串表示不跳过）。"""

    if storage.get_meta(_META_LAST_DATE) == today_iso:
        return "already_archived"
    attempt = storage.get_meta(_META_ATTEMPT_AT)
    if attempt:
        try:
            last = datetime.fromisoformat(attempt)
            if datetime.now().astimezone() - last < timedelta(seconds=_RETRY_COOLDOWN):
                return "cooldown"
        except ValueError:
            pass
    return ""


def publish_report(
    config: Any,
    date_iso: str,
    title: str,
    html: str,
    *,
    done: int = 0,
    total: int = 0,
) -> bool:
    """归档一份日报（节流 + API 全流程）；成功返回 True。

    与 publish 的区别：直接接受已渲染好的 HTML 与标题，供线上补档
    / 验证使用，不依赖 DailyReport 对象、不触发任何推送。
    """

    cfg = config["archive"]
    repo = cfg["repo"]
    branch = cfg.get("branch", "gh-pages")
    base_url = "https://api.github.com"
    timeout = float(cfg.get("timeout", 15))

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        skip = _throttled(storage, date_iso)
        if skip:
            logger.info(
                "日报归档跳过", extra={"date": date_iso, "reason": skip}
            )
            return False
        storage.set_meta(_META_ATTEMPT_AT, datetime.now().astimezone().isoformat())

    headers = _headers(config)
    try:
        _ensure_branch(repo, branch, headers=headers, base_url=base_url, timeout=timeout)
        _ensure_pages_enabled(
            repo, branch, headers=headers, base_url=base_url, timeout=timeout
        )

        manifest = _load_manifest(config)
        manifest[date_iso] = {"title": title, "done": done, "total": total}
        index = _render_index(config.team_name, manifest)

        _put_file(
            repo, branch, f"{date_iso}.html", html,
            f"archive: {date_iso} 日报",
            headers=headers, base_url=base_url, timeout=timeout,
        )
        _put_file(
            repo, branch, "index.html", index,
            f"archive: 更新归档索引（{date_iso}）",
            headers=headers, base_url=base_url, timeout=timeout,
        )
        _save_manifest(config, manifest)
    except NotifierError as exc:
        logger.error("日报归档失败", extra={"date": date_iso, "error": str(exc)})
        return False

    with ReportStorage(config["storage"]["sqlite_path"]) as storage:
        storage.set_meta(_META_LAST_DATE, date_iso)
        storage.set_meta(_META_ATTEMPT_AT, "")  # 成功后清除失败冷却标记
    logger.info(
        "日报归档成功",
        extra={"date": date_iso, "repo": repo, "branch": branch},
    )
    return True


def publish(config: Any, report: DailyReport) -> bool:
    """归档管道产物日报；未启用时 no-op 返回 False。"""

    if not (config.get_path("archive", {}) or {}).get("enabled"):
        return False

    done = total = 0
    task_list = report.task_list
    if task_list and task_list.subtasks:
        total = len(task_list.subtasks)
        done = sum(1 for s in task_list.subtasks if s.status_to == "已完成")

    title = report_title(report.members, config.team_name)

    return publish_report(
        config,
        report.date.isoformat(),
        title,
        report.html,
        done=done,
        total=total,
    )
