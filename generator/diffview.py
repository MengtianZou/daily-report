"""提交差异的展示辅助（小改动内联展示，大改动仅给跳转链接）。

同一份判定逻辑供 Markdown、HTML 邮件与看板序列化共用，保证三端一致。
"""

from __future__ import annotations

from shared.models import CommitFile, CommitRecord

# 新增行数不超过该阈值时，在日报中内联展示新增内容；否则仅提供跳转链接
MAX_INLINE_ADDED_LINES = 20
# 单行内容展示上限，避免超长行撑爆邮件/看板
MAX_INLINE_LINE_CHARS = 300


def extract_added_lines(patch: str) -> list[str]:
    """从 GitHub unified diff 中提取新增行（去掉行首 '+'）。

    跳过文件头 '+++'、hunk 头 '@@' 与上下文行。
    """

    lines: list[str] = []
    for raw in patch.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        content = raw[1:]
        if len(content) > MAX_INLINE_LINE_CHARS:
            content = content[:MAX_INLINE_LINE_CHARS] + " …"
        lines.append(content)
    return lines


def inline_groups(
    commit: CommitRecord,
) -> list[tuple[CommitFile, list[str]]] | None:
    """返回可内联展示的 (文件, 新增行) 列表；改动过大或无新增时返回 None。

    None 表示调用方只展示文件/提交的跳转链接。
    """

    groups: list[tuple[CommitFile, list[str]]] = []
    for f in commit.files:
        if f.additions <= 0:
            continue
        added = extract_added_lines(f.patch)
        if added:
            groups.append((f, added))

    if not groups:
        return None
    total = sum(len(lines) for _, lines in groups)
    if total > MAX_INLINE_ADDED_LINES:
        return None
    return groups
