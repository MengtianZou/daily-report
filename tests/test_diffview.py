"""generator/diffview.py 单元测试：新增行提取与内联阈值判定。"""

from __future__ import annotations

from datetime import datetime, timezone

from generator.diffview import (
    MAX_INLINE_ADDED_LINES,
    extract_added_lines,
    inline_groups,
)
from shared.models import CommitFile, CommitRecord

_T = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def test_extract_added_lines_skips_headers_and_context() -> None:
    patch = (
        "@@ -1,2 +1,3 @@\n"
        " context\n"
        "-removed\n"
        "+added1\n"
        "+++ b/new.py\n"
        "+added2\n"
    )
    assert extract_added_lines(patch) == ["added1", "added2"]


def _commit(file: CommitFile) -> CommitRecord:
    return CommitRecord(
        author="zhangsan",
        message="m",
        timestamp=_T,
        repo="org/repo",
        additions=file.additions,
        deletions=file.deletions,
        files_changed=1,
        files=[file],
    )


def test_inline_groups_returns_small_diff() -> None:
    small = CommitFile(
        filename="a.py", additions=1, patch="@@ -0,0 +1 @@\n+x\n"
    )
    groups = inline_groups(_commit(small))
    assert groups is not None
    assert groups[0][0].filename == "a.py"
    assert groups[0][1] == ["x"]


def test_inline_groups_none_when_too_many_lines() -> None:
    patch = "@@ -0,0 +1 @@\n" + "\n".join(
        f"+line{i}" for i in range(MAX_INLINE_ADDED_LINES + 1)
    ) + "\n"
    big = CommitFile(filename="a.py", additions=MAX_INLINE_ADDED_LINES + 1, patch=patch)
    assert inline_groups(_commit(big)) is None


def test_inline_groups_none_for_deletion_only() -> None:
    deletion = CommitFile(
        filename="a.py", additions=0, deletions=3, patch="@@ -1,3 +0,0 @@\n-x\n"
    )
    assert inline_groups(_commit(deletion)) is None
