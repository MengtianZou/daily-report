"""AI 日报摘要单元测试（summarizer + 三端渲染 + 序列化）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from generator import formatter, summarizer
from shared.models import (
    CommitRecord,
    IssueItem,
    MemberReport,
    MessageRecord,
    PlanItem,
    TaskRecord,
)
from shared.serializer import report_to_dict

_T0800 = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
_DAY = datetime(2026, 10, 15).date()

_AI_CFG = {
    "enabled": True,
    "base_url": "https://api.test/v1",
    "model": "test-model",
    "api_key_env": "TEST_AI_KEY",
    "timeout": 1,
    "max_tokens": 100,
}


def _sample_members() -> list[MemberReport]:
    return [
        MemberReport(
            name="张三",
            github_username="zhangsan",
            commits=[
                CommitRecord(
                    author="zhangsan",
                    message="fix: 修复登录问题",
                    timestamp=_T0800,
                    repo="org/repo-a",
                    additions=10,
                    deletions=2,
                    files_changed=2,
                )
            ],
            messages=[
                MessageRecord(
                    sender="ou_zhangsan",
                    content="登录问题已定位",
                    timestamp=_T0800,
                    chat_name="SDD测试",
                )
            ],
        )
    ]


@pytest.fixture(autouse=True)
def _clear_cache():
    summarizer.clear_cache()
    yield
    summarizer.clear_cache()


# ---------------------------------------------------------------------------
# digest / fingerprint / prompt
# ---------------------------------------------------------------------------

def test_digest_contains_all_sources():
    digest = summarizer.data_digest(
        _sample_members(),
        TaskRecord(assignee="", title="10月15日任务清单", status_from="", status_to="",
                   updated_at=_T0800,
                   subtasks=[TaskRecord(assignee="", title="写周报", status_from="",
                                        status_to="已完成", updated_at=_T0800)]),
        [PlanItem(title="明天写日报", owner="张三")],
        [IssueItem(problem="登录报错", solution="配置写错", status="已解决", owner="张三")],
        failed_sources=["github"],
    )
    assert "fix: 修复登录问题" in digest
    assert "登录问题已定位" in digest
    assert "10月15日任务清单" in digest
    assert "- 已完成：写周报" in digest
    assert "登录报错" in digest
    assert "明天写日报" in digest
    assert "github" in digest  # 失败数据源提示


def test_fingerprint_changes_with_data():
    d1 = summarizer.data_digest(_sample_members(), None, [], [])
    d2 = summarizer.data_digest(_sample_members(), None, [PlanItem(title="新计划")], [])
    assert summarizer.data_fingerprint(d1) == summarizer.data_fingerprint(d1)
    assert summarizer.data_fingerprint(d1) != summarizer.data_fingerprint(d2)


def test_prompt_requires_three_sections():
    prompt = summarizer.build_prompt("今日数据文本")
    for section in ("今日亮点", "风险提示", "明日关注"):
        assert section in prompt
    assert "今日数据文本" in prompt


# ---------------------------------------------------------------------------
# generate_summary：降级与缓存
# ---------------------------------------------------------------------------

def test_disabled_config_skips_api(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    called = []

    def _fail(*args, **kwargs):
        called.append(1)
        raise AssertionError("不应调用 API")

    monkeypatch.setattr(summarizer, "_call_api", _fail)
    cfg = dict(_AI_CFG, enabled=False)
    assert summarizer.generate_summary(_DAY, [], None, [], [], cfg) == ""
    assert not called


def test_missing_api_key_skips_api(monkeypatch):
    monkeypatch.delenv("TEST_AI_KEY", raising=False)
    monkeypatch.setattr(
        summarizer, "_call_api",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 API")),
    )
    assert summarizer.generate_summary(_DAY, [], None, [], [], _AI_CFG) == ""


def test_api_failure_degrades_to_empty(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")

    def _boom(prompt, cfg):
        raise RuntimeError("连接超时")

    monkeypatch.setattr(summarizer, "_call_api", _boom)
    assert summarizer.generate_summary(_DAY, [], None, [], [], _AI_CFG) == ""


def test_empty_model_output_degrades_to_empty(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    monkeypatch.setattr(summarizer, "_call_api", lambda *a, **k: "   ")
    assert summarizer.generate_summary(_DAY, [], None, [], [], _AI_CFG) == ""


def test_success_returns_summary(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    monkeypatch.setattr(
        summarizer, "_call_api",
        lambda prompt, cfg: "今日亮点：\n- 已完成 修复登录问题\n风险提示：\n- 无\n明日关注：\n- 修复登录问题",
    )
    summary = summarizer.generate_summary(
        _DAY, _sample_members(), None, [], [], _AI_CFG
    )
    assert summary.startswith("今日亮点")


def test_cache_hit_does_not_recall_api(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    calls = []

    def _fake(prompt, cfg):
        calls.append(prompt)
        return "今日亮点：\n- 已完成 修复登录问题"

    monkeypatch.setattr(summarizer, "_call_api", _fake)
    first = summarizer.generate_summary(
        _DAY, _sample_members(), None, [], [], _AI_CFG
    )
    second = summarizer.generate_summary(
        _DAY, _sample_members(), None, [], [], _AI_CFG
    )
    assert first == second
    assert len(calls) == 1  # 数据不变，指纹命中缓存不二次调用


def test_cache_miss_on_data_change(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "sk-test")
    calls = []

    def _fake(prompt, cfg):
        calls.append(prompt)
        return "今日亮点：\n- 已完成 修复登录问题"

    monkeypatch.setattr(summarizer, "_call_api", _fake)
    summarizer.generate_summary(_DAY, _sample_members(), None, [], [], _AI_CFG)
    summarizer.generate_summary(
        _DAY, _sample_members(), None, [PlanItem(title="新计划")], [], _AI_CFG
    )
    assert len(calls) == 2  # 数据变化重新生成


# ---------------------------------------------------------------------------
# 出处校验（防幻觉）：模型把 few-shot 示例句抄进日报的真实回归
# ---------------------------------------------------------------------------

def test_grounding_sources_extracts_all_digest_segments():
    digest = summarizer.data_digest(
        _sample_members(),
        TaskRecord(assignee="", title="10月15日任务清单", status_from="", status_to="",
                   updated_at=_T0800,
                   subtasks=[TaskRecord(assignee="", title="写周报", status_from="",
                                        status_to="已完成", updated_at=_T0800)]),
        [PlanItem(title="明天写日报", owner="张三")],
        [IssueItem(problem="登录报错", solution="配置写错", status="已解决", owner="张三")],
    )
    sources = summarizer._grounding_sources(digest)
    assert "写周报" in sources          # 任务行剥「已完成：」前缀
    assert "fix: 修复登录问题" in sources  # 提交行 message 段
    assert "登录问题已定位" in sources   # 沟通行
    assert "登录报错" in sources        # 问题行 → 左侧
    assert "配置写错" in sources        # 问题行 → 右侧（状态括号已剥）
    assert "明天写日报" in sources      # 计划行


def test_strip_ungrounded_drops_example_copy():
    # 真实回归：模型把 prompt 示例句「登录模块开发」「修复支付回调bug」抄进亮点，
    # 而当天数据里只有「修复支付功能」相关条目
    digest = "10月20日任务清单（已完成 1/2）：\n- 已完成：修复支付功能\n- 进行中：python网课"
    summary = (
        "今日亮点：\n- 已完成 登录模块开发\n- 已完成 修复支付回调bug\n"
        "风险提示：\n- 无\n明日关注：\n- 继续推进python网课"
    )
    assert summarizer.strip_ungrounded(summary, digest) == ""  # 亮点全无出处 → 整篇弃用


def test_strip_ungrounded_keeps_faithful_entries():
    digest = "10月20日任务清单（已完成 1/1）：\n- 已完成：修复支付功能"
    summary = "今日亮点：\n- 已完成 修复支付功能\n风险提示：\n- 无\n明日关注：\n- 无"
    assert summarizer.strip_ungrounded(summary, digest) == summary


def test_strip_ungrounded_filters_only_hallucinated_line():
    digest = "10月20日任务清单（已完成 1/2）：\n- 已完成：修复支付功能\n- 进行中：python网课"
    summary = (
        "今日亮点：\n- 已完成 修复支付功能\n- 已完成 登录模块开发\n"
        "风险提示：\n- 无\n明日关注：\n- 继续推进python网课"
    )
    filtered = summarizer.strip_ungrounded(summary, digest)
    assert "- 已完成 修复支付功能" in filtered       # 忠实条目保留
    assert "登录模块开发" not in filtered            # 抄示例的剔除
    assert "- 继续推进python网课" in filtered        # 有出处（双向包含）保留


def test_grounded_allows_mild_paraphrase():
    # 语序微调但字符集一致 / 轻度润色不应误杀
    sources = ["英语PPT发言稿读熟"]
    assert summarizer._grounded("已完成 读熟英语PPT发言稿", sources)
    assert summarizer._grounded("无", sources)
    assert not summarizer._grounded("已完成 修复支付回调bug", ["修复支付功能"])


def test_prompt_forbids_copying_examples():
    prompt = summarizer.build_prompt("数据")
    assert "严禁把示例中的具体条目" in prompt
    assert "出处" in prompt


# ---------------------------------------------------------------------------
# 三端渲染与序列化
# ---------------------------------------------------------------------------

def test_markdown_contains_summary_block():
    members = _sample_members()
    md = formatter.build_markdown(
        "研发一组", _DAY, members,
        summary="今日亮点：\n- 修复登录问题", tomorrow_plans=[], issues=[],
    )
    assert "## 今日总结" in md
    assert "今日亮点：\n- 修复登录问题" in md
    # 摘要在正文板块之前
    assert md.index("## 今日总结") < md.index("### github代码提交情况")


def test_markdown_without_summary_has_no_section():
    md = formatter.build_markdown("研发一组", _DAY, _sample_members())
    assert "## 今日总结" not in md


def test_generate_carries_summary_and_html_renders_it():
    report = formatter.generate(
        _sample_members(), _DAY, "研发一组",
        summary="今日亮点：\n- 一切顺利",
    )
    assert report.summary == "今日亮点：\n- 一切顺利"
    assert "今日总结" in report.markdown
    assert "今日总结" in report.html
    assert "一切顺利" in report.html


def test_serializer_includes_summary():
    report = formatter.generate(_sample_members(), _DAY, "研发一组", summary="摘要文本")
    data = report_to_dict(report)
    assert data["summary"] == "摘要文本"
    empty = report_to_dict(formatter.generate(_sample_members(), _DAY, "研发一组"))
    assert empty["summary"] == ""
