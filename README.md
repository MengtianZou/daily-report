# 智能日报生成器

[![CI](https://github.com/chenpingan6158-a11y/IDNG.github.io/actions/workflows/ci.yml/badge.svg)](https://github.com/chenpingan6158-a11y/IDNG.github.io/actions/workflows/ci.yml)

自动汇总团队成员的 GitHub 提交、飞书任务进展、飞书群聊关键词消息，生成结构化日报，支持邮件 / 飞书机器人推送与浏览器看板展示。

## 功能特性

- **多源采集**：GitHub 提交记录、飞书任务清单状态流转、飞书群聊关键词消息
- **自动生成**：按成员聚合为「代码提交 / 任务进展 / 协作沟通」三板块日报
- **多种推送**：邮件（SMTP）、飞书自定义机器人（群卡片）
- **浏览器看板**：本地只读 HTTP 服务，左侧日期导航，查看历史日报
- **自动刷新**：看板服务后台定时采集，新消息自动出现在页面
- **定时推送**：服务运行期间每天定点自动执行完整流程并推送

## 快速开始

### 环境要求

- Python 3.11+
- 依赖：`httpx`、`pyyaml`、`jinja2`、`pytest`（开发）、`chinesecalendar`（节假日识别，可选）

```bash
pip install httpx pyyaml jinja2 chinesecalendar
```

### 配置

1. 复制 `config.yaml` 并按注释填写：
   - `members`：成员姓名、GitHub 用户名、飞书 open_id 映射
   - `github`：仓库列表与令牌环境变量名
   - `lark_task` / `lark_message`：飞书应用凭证（从环境变量读取）、任务清单 project_id、群 chat_id
   - `notify.email` / `notify.lark_bot`：邮件与飞书机器人配置
   - `schedule.push_time`：每日定时推送时间（如 `"14:25"`）
2. 在项目根目录创建 `.env` 写入密钥（已在 `.gitignore` 中，不会提交）：

```env
GITHUB_TOKEN=ghp_xxx
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=xxx
EMAIL_PASSWORD=xxx
```

### 使用

| 命令 | 作用 |
|---|---|
| `python main.py` | 启动浏览器看板（自动刷新 + 定时推送） |
| `python main.py --run` | 立即执行完整流程：采集 + 生成 + 推送 |
| `python main.py --run --dry-run` | 只采集生成，不推送 |
| `python main.py --view [日期]` | 导出静态看板页面并在浏览器打开 |
| `python main.py --serve` | 显式启动看板服务（与默认相同） |
| `python main.py --check` | 检查配置与外部连通性 |

Windows 下也可直接双击 `启动看板.bat`。

### 看板

- 服务启动后自动打开浏览器，地址形如 `http://127.0.0.1:<端口>/`
- 每 60 秒自动采集一次当日数据（dry-run，不推送），刷新页面即可看到最新内容
- 到达 `schedule.push_time` 时自动执行完整流程并推送（同一天只推一次）

## 项目结构

```
├── main.py              # 入口与流程编排
├── collector/           # 采集器：github / lark_task / lark_msg
├── generator/           # 日报聚合与 Markdown 渲染
├── notifier/            # 推送：email / lark_bot
├── shared/              # 配置、日志、存储（SQLite）、序列化、错误
├── webapp/              # 看板：dashboard.html / export.py / server.py
├── specs/               # SDD 规范文档（proposal / design / tasks）
└── tests/               # pytest 单元测试（108 个）
```

## 开发

```bash
python -m pytest tests/ -q   # 运行全部测试
```

本项目按 SDD（规范驱动开发）流程构建：需求 → proposal → design → tasks → 代码 → 验证，规范文档见 `specs/`。

## 注意事项

- 飞书机器人只能读取其所在群聊的消息、所在任务清单的任务
- 群聊消息按关键词采集：`上线`、`bug`、`需求`、`联调`、`发布`、`阻塞`、`review`
- 非工作日（周末、法定节假日）默认跳过，可用 `--force` 强制执行
- `data/` 下的 SQLite 数据库与导出页面为本地数据，已加入 `.gitignore`
