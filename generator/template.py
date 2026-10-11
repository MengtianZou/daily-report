"""日报 HTML 模板（design.md §2 generator/template.py）。

使用 Jinja2 将结构化的日报数据渲染为带内联样式的独立 HTML 页面，
可直接作为 HTML 邮件正文。
"""

from __future__ import annotations

from datetime import date

from jinja2 import Environment, BaseLoader

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

    {% for member in members %}
    <section style="padding:18px 28px;border-top:1px solid #eef0f3;">
      {% if show_member_names %}<h2 style="margin:0 0 10px;font-size:16px;color:#2f6fed;">{{ member.name }}</h2>{% endif %}

      <h3 style="margin:10px 0 6px;font-size:13px;color:#6b7280;">代码提交</h3>
      {% if 'github' in failed_sources %}<p style="margin:0;font-size:13px;color:#c0392b;">⚠️ 数据获取失败</p>
      {% elif member.commits %}<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for c in member.commits %}
        <li><code style="background:#f1f3f5;padding:1px 5px;border-radius:4px;">{{ c.repo }}</code>
            {{ c.message }} <span style="color:#16a34a;">+{{ c.additions }}</span>/<span style="color:#dc2626;">-{{ c.deletions }}</span></li>
        {% endfor %}
      </ul>{% else %}<p style="margin:0;font-size:13px;color:#9aa0a6;">今日无记录</p>{% endif %}

      <h3 style="margin:10px 0 6px;font-size:13px;color:#6b7280;">任务进展</h3>
      {% if 'lark_task' in failed_sources %}<p style="margin:0;font-size:13px;color:#c0392b;">⚠️ 数据获取失败</p>
      {% elif member.tasks %}<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for t in member.tasks %}
        <li>{% if t.status_from %}{{ t.status_from }} → {% endif %}<strong>{{ t.status_to }}</strong>：{{ t.title }}
          {% if t.subtasks %}
          <div style="margin-top:6px;padding-left:12px;border-left:2px solid #e5e7eb;">
            <div style="font-size:12px;color:#2f6fed;font-weight:600;">📋 子任务 {{ t.progress[0] }}/{{ t.progress[1] }} 已完成</div>
            <ul style="margin:4px 0 0;padding-left:0;list-style:none;">
              {% for s in t.subtasks %}
              <li style="font-size:12px;line-height:1.7;">{% if s.status_to == '已完成' %}✅{% else %}⬜{% endif %} {{ s.title }}（{{ s.status_to }}）</li>
              {% endfor %}
            </ul>
          </div>
          {% endif %}
        </li>
        {% endfor %}
      </ul>{% else %}<p style="margin:0;font-size:13px;color:#9aa0a6;">今日无记录</p>{% endif %}

      <h3 style="margin:10px 0 6px;font-size:13px;color:#6b7280;">协作沟通</h3>
      {% if 'lark_message' in failed_sources %}<p style="margin:0;font-size:13px;color:#c0392b;">⚠️ 数据获取失败</p>
      {% elif member.messages %}<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.8;">
        {% for m in member.messages %}
        <li>[{{ m.chat_name }}] {{ m.content }}</li>
        {% endfor %}
      </ul>{% else %}<p style="margin:0;font-size:13px;color:#9aa0a6;">今日无记录</p>{% endif %}
    </section>
    {% endfor %}

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
        <li>📌 {{ p.owner if p.owner else '团队' }}：{{ p.title }}</li>
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
    report_title: str | None = None,
    show_member_names: bool = True,
) -> str:
    """渲染独立 HTML 文档。"""

    template = _ENV.from_string(_HTML_TEMPLATE)
    return template.render(
        team_name=team_name,
        report_title=report_title or f"{team_name} · 工作日报",
        report_date=report_date.isoformat(),
        members=members,
        failed_sources=failed_sources or [],
        tomorrow_plans=tomorrow_plans or [],
        issues=issues or [],
        show_member_names=show_member_names,
    )
