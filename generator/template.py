"""日报 HTML 模板（design.md §2 generator/template.py）。

使用 Jinja2 将结构化的日报数据渲染为带内联样式的独立 HTML 页面，
可直接作为 HTML 邮件正文。
"""

from __future__ import annotations

from datetime import date

from jinja2 import Environment, BaseLoader

from generator.diffview import inline_groups as diff_inline
from shared.models import MemberReport

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{{ report_title }} {{ report_date }}</title>
</head>
<body style="margin:0;padding:24px;background:#f5f6f8;font-family:'Segoe UI','Microsoft YaHei',sans-serif;color:#2b2f36;">
  <div style="max-width:820px;margin:0 auto;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.06);">
    <div style="padding:20px 28px;background:#2f6fed;color:#ffffff;">
      <h1 style="margin:0;font-size:20px;">{{ report_title }}</h1>
      <div style="margin-top:4px;font-size:13px;opacity:.85;">{{ report_date }}</div>
    </div>

    {% if failed_sources %}
    <div style="margin:16px 28px 0;padding:10px 14px;background:#fff4e5;border:1px solid #ffcc80;border-radius:6px;font-size:13px;color:#a15c00;">
      ⚠️ 以下数据源获取失败：{{ failed_sources | join('、') }}
    </div>
    {% endif %}

    {% if summary %}
    <section style="margin:16px 28px 0;padding:14px 18px;background:#f0f6ff;border:1px solid #c7dcff;border-radius:8px;">
      <h2 style="margin:0 0 8px;font-size:14px;color:#2f6fed;">🤖 今日总结 <span style="font-weight:400;font-size:12px;color:#7a8aa0;">AI 生成</span></h2>
      <div style="font-size:13px;line-height:1.9;color:#374151;white-space:pre-wrap;">{{ summary }}</div>
    </section>
    {% endif %}

    {% for member in members %}
    <section style="padding:18px 28px;border-top:1px solid #eef0f3;">
      {% if show_member_names %}<h2 style="margin:0 0 10px;font-size:16px;color:#2f6fed;">{{ member.name }}</h2>{% endif %}

      <h3 style="margin:10px 0 6px;font-size:13px;color:#6b7280;">github代码提交情况</h3>
      {% if 'github' in failed_sources %}<p style="margin:0;font-size:13px;color:#c0392b;">⚠️ 数据获取失败</p>
      {% elif member.commits %}<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for c in member.commits %}
        <li><code style="background:#f1f3f5;padding:1px 5px;border-radius:4px;">{{ c.repo }}</code>
            {% if c.html_url %}<a href="{{ c.html_url }}" style="color:#2f6fed;text-decoration:none;">{{ c.message }}</a> <a href="{{ c.html_url }}" style="font-size:12px;color:#2f6fed;">查看提交 ↗</a>
            <div style="margin:2px 0 4px;font-size:11px;color:#9aa0a6;word-break:break-all;">邮箱内无法打开 GitHub 时，长按复制链接到浏览器：{{ c.html_url }}</div>
            {% else %}{{ c.message }}{% endif %}
            <span style="color:#16a34a;">+{{ c.additions }}</span>/<span style="color:#dc2626;">-{{ c.deletions }}</span>
            {% set groups = diff_inline(c) %}
            {% if groups %}
            <div style="margin:6px 0 8px;">
              {% for f, added in groups %}
              <div style="font-size:12px;font-weight:600;color:#374151;">📄 {% if f.blob_url %}<a href="{{ f.blob_url }}" style="color:#2f6fed;">{{ f.filename }}</a>{% else %}{{ f.filename }}{% endif %}（<span style="color:#16a34a;">+{{ f.additions }}</span>/<span style="color:#dc2626;">-{{ f.deletions }}</span>）</div>
              <div style="margin:2px 0 8px;padding:6px 10px;background:#f8fafc;border:1px solid #eef0f3;border-radius:6px;font-family:Consolas,Menlo,monospace;font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-all;">{% for ln in added %}<span style="color:#16a34a;">+</span> {{ ln }}
{% endfor -%}</div>
              {% endfor %}
            </div>
            {% elif c.files %}
            <div style="margin:4px 0 8px;font-size:12px;color:#6b7280;">改动较大，查看文件：{% for f in c.files %}{% if f.blob_url %}<a href="{{ f.blob_url }}" style="color:#2f6fed;">{{ f.filename }}</a>{% else %}{{ f.filename }}{% endif %}{% if not loop.last %} · {% endif %}{% endfor %}（或上方「查看提交」查看完整差异）</div>
            {% endif %}
        </li>
        {% endfor %}
      </ul>{% else %}<p style="margin:0;font-size:13px;color:#9aa0a6;">今日无记录</p>{% endif %}

      <h3 style="margin:10px 0 6px;font-size:13px;color:#6b7280;">飞书SDD测试群协作沟通情况</h3>
      {% if 'lark_message' in failed_sources %}<p style="margin:0;font-size:13px;color:#c0392b;">⚠️ 数据获取失败</p>
      {% elif member.messages %}<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for m in member.messages %}
        <li>沟通记录{{ loop.index }}：{{ m.content }}</li>
        {% endfor %}
      </ul>{% else %}<p style="margin:0;font-size:13px;color:#9aa0a6;">今日无记录</p>{% endif %}
    </section>
    {% endfor %}

    {% if 'lark_task' in failed_sources and not task_list %}
    <section style="padding:18px 28px;border-top:1px solid #eef0f3;">
      <h2 style="margin:0 0 10px;font-size:16px;color:#2f6fed;">今日任务清单</h2>
      <p style="margin:0;font-size:13px;color:#c0392b;">⚠️ 数据获取失败</p>
    </section>
    {% elif task_list %}
    <section style="padding:18px 28px;border-top:1px solid #eef0f3;">
      <h2 style="margin:0 0 10px;font-size:16px;color:#2f6fed;">今日任务清单</h2>
      <div style="font-size:13px;font-weight:600;color:#374151;">📋 {{ task_list.title }}
        {% if task_list.progress[1] > 0 %}
        <span style="margin-left:6px;padding:1px 8px;border-radius:10px;background:#e8f0fe;color:#2f6fed;font-size:12px;">已完成 {{ task_list.progress[0] }}/{{ task_list.progress[1] }}</span>
        {% endif %}
      </div>
      {% if task_list.subtasks %}
      <ul style="margin:8px 0 0;padding-left:18px;font-size:13px;line-height:1.9;">
        {% for s in task_list.subtasks %}
        <li>{% if s.status_to == '已完成' %}✅{% else %}⬜{% endif %} {{ s.title }}</li>
        {% endfor %}
      </ul>
      {% endif %}
    </section>
    {% endif %}

    {% if issues %}
    <section style="padding:18px 28px;border-top:1px solid #eef0f3;">
      <h2 style="margin:0 0 10px;font-size:16px;color:#2f6fed;">问题与解决</h2>
      <ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for i in issues %}
        <li>{% if i.status == '已解决' %}<span style="color:#16a34a;">✅</span>{% else %}<span style="color:#d97706;">⏳</span>{% endif %}
            {{ i.problem }} → {{ i.solution if i.solution else '方案待定' }}</li>
        {% endfor %}
      </ul>
    </section>
    {% endif %}

    {% if tomorrow_plans %}
    <section style="padding:18px 28px;border-top:1px solid #eef0f3;">
      <h2 style="margin:0 0 10px;font-size:16px;color:#2f6fed;">明日计划</h2>
      <ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for p in tomorrow_plans %}
        <li>📌 {{ p.title }}</li>
        {% endfor %}
      </ul>
    </section>
    {% endif %}
  </div>
</body>
</html>
"""

_ENV = Environment(loader=BaseLoader(), autoescape=True, trim_blocks=True, lstrip_blocks=True)


def render_report_html(
    team_name: str,
    report_date: date,
    members: list[MemberReport],
    failed_sources: list[str] | None = None,
    tomorrow_plans: list | None = None,
    issues: list | None = None,
    task_list=None,
    report_title: str | None = None,
    show_member_names: bool = True,
    summary: str = "",
) -> str:
    """渲染独立 HTML 文档。"""

    template = _ENV.from_string(_HTML_TEMPLATE)
    return template.render(
        team_name=team_name,
        report_title=report_title or f"{team_name} · 智能日报",
        report_date=report_date.isoformat(),
        members=members,
        failed_sources=failed_sources or [],
        tomorrow_plans=tomorrow_plans or [],
        issues=issues or [],
        task_list=task_list,
        show_member_names=show_member_names,
        summary=summary,
        diff_inline=diff_inline,
    )
