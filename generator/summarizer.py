"""AI 日报摘要（generator/summarizer.py）。

在「采集 → 聚合」之后加一个可选的「总结」环节：把当天结构化数据
（提交、沟通、任务清单、问题、明日计划）交给 OpenAI 兼容的大模型接口
（DeepSeek / 通义 / GLM 均可），生成一段自然语言「今日总结」附在日报头部。

设计要点（优雅降级）：
- 未启用 / 未配置 API Key / 接口失败 / 超时 → 返回空串，日报照常生成；
- 指纹缓存：看板服务每 60 秒重建一轮日报，摘要按当日数据指纹缓存，
  数据不变不重复调用，变化才重新生成（内存缓存，重启后重调一次可接受）。
"""

from __future__ import annotations

import hashlib
import os
from datetime import date

import httpx

from shared.logger import get_logger
from shared.models import IssueItem, MemberReport, PlanItem, TaskRecord

logger = get_logger(__name__)

# 模块级缓存：(日期, 数据指纹) -> 摘要文本
_CACHE: dict[tuple[str, str], str] = {}


def data_digest(
    members: list[MemberReport],
    task_list: TaskRecord | None,
    tomorrow_plans: list[PlanItem],
    issues: list[IssueItem],
    failed_sources: list[str] | None = None,
) -> str:
    """把结构化数据压缩为喂给模型的纯文本摘要（同时作为指纹的输入）。"""

    lines: list[str] = []
    for member in members:
        if member.commits:
            lines.append(f"{member.name}的代码提交：")
            for c in member.commits:
                lines.append(f"- {c.repo} | {c.message} | +{c.additions}/-{c.deletions}")
        if member.messages:
            lines.append(f"{member.name}的协作沟通：")
            for m in member.messages:
                lines.append(f"- {m.content}")

    if task_list is not None:
        done, total = task_list.progress
        lines.append(f"{task_list.title}（已完成 {done}/{total}）：")
        for sub in task_list.subtasks:
            # 状态用明文中文标注，比 [x] 记号更利于模型转述「已完成 xxx」
            state = "已完成" if sub.status_to == "已完成" else "进行中"
            lines.append(f"- {state}：{sub.title}")

    if issues:
        lines.append("问题与解决：")
        for i in issues:
            solution = i.solution or "方案待定"
            lines.append(f"- {i.problem} → {solution}（{i.status}）")

    if tomorrow_plans:
        lines.append("明日计划：")
        for p in tomorrow_plans:
            lines.append(f"- {p.title}")

    if failed_sources:
        lines.append(f"以下数据源今日采集失败（相关数据可能缺失）：{'、'.join(failed_sources)}")

    return "\n".join(lines) if lines else "今日暂无任何工作数据。"


def data_fingerprint(digest: str) -> str:
    """数据指纹：内容哈希，用于摘要缓存去重。"""

    return hashlib.md5(digest.encode("utf-8")).hexdigest()


def build_prompt(digest: str) -> str:
    """构造摘要请求的 prompt。"""

    return (
        "你是一名研发团队的日报助理。请根据下面的今日工作数据，用简体中文"
        "写一段简洁的日报总结，分三小节输出，每节标题独占一行：\n"
        "今日亮点：\n风险提示：\n明日关注：\n"
        "每节写 1-2 条，用「-」开头的短句，直接陈述事实；该节没有可写内容时"
        "在标题下写「- 无」。只输出这三节内容，不要任何开场白和客套。\n"
        "硬性要求：数据里标注「已完成」的条目写进今日亮点时，每条必须以"
        "「已完成」三字开头并复述条目内容（数据里写「已完成：xxx」，亮点就写"
        "「- 已完成 xxx」）；标注「进行中」的条目严禁写进今日亮点，只能放进"
        "「明日关注」。\n"
        "输出示例（模仿此格式）：\n"
        "今日亮点：\n- 已完成 登录模块开发\n- 已完成 修复支付回调bug\n"
        "风险提示：\n- 无\n明日关注：\n- 继续推进联调测试\n\n"
        f"今日数据：\n{digest}"
    )


def _call_api(prompt: str, ai_cfg: dict) -> str:
    """调用 OpenAI 兼容 /chat/completions 接口，返回模型输出文本。

    网络错误、超时、非 2xx、响应结构异常统一抛出 RuntimeError（由
    generate_summary 捕获降级）。
    """

    url = ai_cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": ai_cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": int(ai_cfg.get("max_tokens", 800)),
    }
    headers = {
        "Authorization": f"Bearer {os.environ.get(ai_cfg.get('api_key_env', 'DEEPSEEK_API_KEY'), '')}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, json=payload, headers=headers, timeout=ai_cfg.get("timeout", 60))
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError("响应 content 不是字符串")
    return content.strip()


def generate_summary(
    report_date: date,
    members: list[MemberReport],
    task_list: TaskRecord | None,
    tomorrow_plans: list[PlanItem],
    issues: list[IssueItem],
    ai_cfg: dict,
    failed_sources: list[str] | None = None,
) -> str:
    """生成当日 AI 摘要；任何不可用场景均返回空串（不阻断日报生成）。

    前置校验（启用开关、base_url/model、API Key）不通过直接返回；
    指纹命中缓存不重复调用；调用失败记日志并返回空串。
    """

    if not ai_cfg.get("enabled"):
        return ""
    if not ai_cfg.get("base_url") or not ai_cfg.get("model"):
        logger.warning("AI 摘要已启用但缺少 base_url/model 配置，跳过生成")
        return ""
    api_key = os.environ.get(ai_cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "")
    if not api_key:
        logger.info(
            "AI 摘要未配置 API Key（环境变量缺失），跳过生成",
            extra={"key_env": ai_cfg.get("api_key_env", "DEEPSEEK_API_KEY")},
        )
        return ""

    digest = data_digest(members, task_list, tomorrow_plans, issues, failed_sources)
    fingerprint = data_fingerprint(digest)
    cache_key = (report_date.isoformat(), fingerprint)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        summary = _call_api(build_prompt(digest), ai_cfg).strip()
    except Exception as exc:  # 网络/超时/解析失败统一降级
        logger.error("AI 摘要生成失败，日报按无摘要继续", extra={"error": str(exc)})
        return ""
    if not summary:
        logger.warning("AI 摘要返回空内容，日报按无摘要继续")
        return ""

    _CACHE[cache_key] = summary
    logger.info("AI 摘要生成成功", extra={"date": report_date.isoformat()})
    return summary


def clear_cache() -> None:
    """清空摘要缓存（测试用）。"""

    _CACHE.clear()
