"""collector/github.py 单元测试（Mock HTTP）。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from collector import github
from shared.models import CommitRecord


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, headers=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeHTTP:
    """按 URL 子串分派预置结果；可统计调用次数。"""

    def __init__(self, routes: list[tuple[str, object]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        for fragment, result in self.routes:
            if fragment in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected url: {url}")


_SINCE = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
_UNTIL = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)

_LIST = [
    {
        "sha": "abc123",
        "author": {"login": "zhangsan"},
        "commit": {
            "message": "fix: 修复登录问题",
            "author": {"name": "张三", "date": "2026-09-25T08:00:00Z"},
        },
    }
]

_DETAIL = {
    "html_url": "https://github.com/org/repo/commit/abc123",
    "stats": {"additions": 10, "deletions": 2, "total": 12},
    "files": [
        {
            "filename": "a.py",
            "status": "added",
            "additions": 10,
            "deletions": 0,
            "patch": "@@ -0,0 +1,10 @@\n+print('hi')\n",
            "blob_url": "https://github.com/org/repo/blob/abc123/a.py",
        },
        {
            "filename": "b.py",
            "status": "modified",
            "additions": 0,
            "deletions": 2,
            "patch": "@@ -1,2 +1,0 @@\n-old\n",
            "blob_url": "https://github.com/org/repo/blob/abc123/b.py",
        },
    ],
}


def _noop_sleep(_seconds: float) -> None:
    return None


def test_collect_returns_commit_records_with_all_fields() -> None:
    fake = FakeHTTP(
        [
            ("/commits?", FakeResponse(payload=_LIST)),
            ("/commits/abc123", FakeResponse(payload=_DETAIL)),
        ]
    )
    records = github.collect(
        ["org/repo"], _SINCE, _UNTIL, http_client=fake, sleeper=_noop_sleep
    )

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, CommitRecord)
    assert record.author == "zhangsan"
    assert record.message == "fix: 修复登录问题"
    assert record.timestamp == datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    assert record.repo == "org/repo"
    assert record.additions == 10
    assert record.deletions == 2
    assert record.files_changed == 2
    assert record.html_url == "https://github.com/org/repo/commit/abc123"
    assert len(record.files) == 2
    added_file = record.files[0]
    assert added_file.filename == "a.py"
    assert added_file.status == "added"
    assert added_file.additions == 10
    assert "+print('hi')" in added_file.patch
    assert added_file.blob_url == "https://github.com/org/repo/blob/abc123/a.py"


def test_html_url_falls_back_to_constructed_commit_url() -> None:
    detail = dict(_DETAIL)
    detail.pop("html_url")
    fake = FakeHTTP(
        [
            ("/commits?", FakeResponse(payload=_LIST)),
            ("/commits/abc123", FakeResponse(payload=detail)),
        ]
    )
    records = github.collect(
        ["org/repo"], _SINCE, _UNTIL, http_client=fake, sleeper=_noop_sleep
    )
    assert records[0].html_url == "https://github.com/org/repo/commit/abc123"


def test_pagination_follows_next_link() -> None:
    first = FakeResponse(
        payload=_LIST,
        headers={
            "Link": '<https://api.github.com/repos/org/repo/commits?page=2>; rel="next"'
        },
    )
    second = FakeResponse(payload=[])
    fake = FakeHTTP(
        [
            ("page=1", first),
            ("page=2", second),
            ("/commits/abc123", FakeResponse(payload=_DETAIL)),
        ]
    )

    records = github.collect(
        ["org/repo"], _SINCE, _UNTIL, http_client=fake, sleeper=_noop_sleep
    )
    assert len(records) == 1
    assert any("page=2" in call for call in fake.calls)


def test_timeout_retries_three_times_and_skips_repo() -> None:
    error = httpx.ConnectError("timeout")
    fake = FakeHTTP([("/commits?", error)])

    records = github.collect(
        ["org/repo"], _SINCE, _UNTIL, http_client=fake, sleeper=_noop_sleep
    )
    assert records == []
    list_calls = [c for c in fake.calls if "/commits?" in c]
    assert len(list_calls) == 3


def test_rate_limit_waits_for_reset_then_retries() -> None:
    waited: list[float] = []
    limited = FakeResponse(
        status_code=403,
        payload={"message": "rate limited"},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"},
    )

    call_count = {"n": 0}

    class SwitchHTTP(FakeHTTP):
        def get(self, url: str):
            if "/commits?" in url:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return limited
                return FakeResponse(payload=_LIST)
            return FakeResponse(payload=_DETAIL)

    fake = SwitchHTTP([])
    records = github.collect(
        ["org/repo"],
        _SINCE,
        _UNTIL,
        http_client=fake,
        sleeper=lambda s: waited.append(s),
    )
    assert len(records) == 1
    assert waited  # 确实发生了限流等待


def test_failed_repo_does_not_block_other_repos() -> None:
    def dispatch(url: str):
        if "repo-bad" in url and "/commits?" in url:
            return FakeResponse(status_code=404, payload={"message": "Not Found"})
        if "/commits?" in url:
            return FakeResponse(payload=_LIST)
        return FakeResponse(payload=_DETAIL)

    fake = FakeHTTP([])
    fake.get = dispatch  # type: ignore[method-assign]

    records = github.collect(
        ["org/repo-bad", "org/repo-good"],
        _SINCE,
        _UNTIL,
        http_client=fake,
        sleeper=_noop_sleep,
    )
    assert len(records) == 1
    assert records[0].repo == "org/repo-good"


def test_author_fallback_to_commit_name() -> None:
    list_item = dict(_LIST[0])
    list_item["author"] = None
    fake = FakeHTTP(
        [
            ("/commits?", FakeResponse(payload=[list_item])),
            ("/commits/abc123", FakeResponse(payload=_DETAIL)),
        ]
    )
    records = github.collect(
        ["org/repo"], _SINCE, _UNTIL, http_client=fake, sleeper=_noop_sleep
    )
    assert records[0].author == "张三"


def test_timezone_offset_is_percent_encoded() -> None:
    """回归：'+08:00' 中的 '+' 不能裸拼进查询串，否则被解释为空格、静默漏采。"""

    fake = FakeHTTP(
        [
            ("/commits?", FakeResponse(payload=_LIST)),
            ("/commits/abc123", FakeResponse(payload=_DETAIL)),
        ]
    )
    since_local = datetime(2026, 9, 26, 0, 0).astimezone()
    until_local = datetime(2026, 9, 26, 18, 0).astimezone()
    github.collect(
        ["org/repo"], since_local, until_local, http_client=fake, sleeper=_noop_sleep
    )

    list_url = next(c for c in fake.calls if "/commits?" in c)
    query = list_url.split("?", 1)[1]
    assert "%2B" in query
    assert "+" not in query

