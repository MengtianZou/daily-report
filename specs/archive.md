# 智能日报生成器 · 日报网页归档（spec）

> 状态：**待用户审批**
> 日期：2026-10-20
> 方法论：SDD 六阶段 · proposal + design + tasks 合订本（单功能迭代）
> 前置：迭代一~三与任务闭环、AI 摘要、任务趋势、CI 均已交付，234 测试通过

## 1. Proposal：背景与目标

### 1.1 背景

日报目前推送到邮件 / 飞书机器人 / 本地看板三端，但老师查看日报依赖转发或用户本机开服务，无法随时回看历史某天的日报。用户需求：**每天日报生成后，自动把当天日报网页归档到公开网址，老师点开固定链接即可按日期浏览全部历史日报**。

用户的 GitHub 仓库 `chenpingan6158-a11y/IDNG.github.io` 即日常推送提交的仓库，具备 GitHub Pages 的天然载体。

### 1.2 目标

1. 每天日报推送轮次结束后，自动将当天日报 HTML 归档发布到 GitHub Pages；
2. 归档首页（index）按日期倒序列出所有归档日报及任务完成进度，免登录可访问；
3. 同一天只归档一次，失败不阻断日报主管道，可自愈重试；
4. 看板 15 秒一轮的后台重建**不触发**归档（只有真实推送轮次才归档）。

### 1.3 非目标（明确不做）

- 归档与推送成败的解耦治理、开机自启（用户明确暂不做）；
- 归档访问权限控制（按公开归档实施，用户未反对；结束时可提醒）；
- 本地备份多副本、归档删除/回滚 UI。

## 2. Design：技术设计

### 2.1 归档载体与分支策略

- 目标仓库：`config.archive.repo`（默认 `chenpingan6158-a11y/IDNG.github.io`）；
- 目标分支：**`gh-pages` 孤儿分支**根目录，文件为 `index.html` + `YYYY-MM-DD.html`；
- **禁止放 `main` 或 `main/docs`**：main 分支的归档提交会被本项目自家 GitHub 采集器当作工作提交捕进日报（自我污染）；
- Pages 来源：`GET /repos/{repo}/pages` 检查现状，未启用则 `POST /repos/{repo}/pages`（`{"source":{"branch":"gh-pages","path":"/"}}`），HTTP 409 视为已启用；
- gh-pages 不存在时创建：`GET /repos/{repo}` 拿 `default_branch` → 取其 HEAD sha → `POST /repos/{repo}/git/refs`（`refs/heads/gh-pages`）。

### 2.2 GitHub API 交互（`notifier/archive.py`）

- 用 httpx 直连 REST API（与采集层 `collector/github.py` 同模式），token 走 `token_env` 环境变量（默认 `GITHUB_TOKEN`），需 **contents:write** 权限；
- 写文件用 Contents API：先 `GET /repos/{repo}/contents/{path}?ref={branch}` 拿 sha（404 视为新建），再 `PUT /repos/{repo}/contents/{path}`（body 含 message/content(base64)/branch/sha）；
- 请求异常 / 非 2xx（409 除外）→ 抛 `NotifierError`，上层捕获降级。

### 2.3 归档内容

- 当日页 `YYYY-MM-DD.html`：直接复用 `report.html`（`formatter.generate` 已调用 `template.render_report_html` 渲染的完整独立 HTML，三端同源）；
- 索引页 `index.html`：依据本地清单 `data/archive/manifest.json`（`{日期: {title, done, total}}`）构建，按日期倒序渲染简单静态列表（每行：日期链接 + 日报标题 + 任务完成 `done/total`），纯内联样式 UTF-8；
- manifest 先更新内存再写回本地，index 每次全量重渲染。

### 2.4 管道挂点与节流

- 挂点：`main.run_pipeline` 推送轮次内（`dry_run=False`，即 10:35 定时推送或 `--run`）在 `_push_all` 之后调用；AutoCollector（`dry_run=True`）不触发；
- 节流（storage meta 表，跨进程）：
  - `archive_last_date` == 当天 → 同日已成功，跳过；
  - `archive_attempt_at` 距今 < 600 秒 → 失败冷却期，跳过；
  - 成功后写 `archive_last_date`；失败写 `archive_attempt_at`；
- **优雅降级**：`archive.publish` 内部捕获全部异常记日志返回 False，绝不阻断主管道（硬约束 2/3 同源原则）。

### 2.5 配置

```yaml
archive:
  enabled: true
  repo: chenpingan6158-a11y/IDNG.github.io
  branch: gh-pages
  token_env: GITHUB_TOKEN
  timeout: 15
```

- 非必填 section（不进 `_REQUIRED`），缺省或 `enabled: false` 时完全 no-op，老配置零改动兼容。

### 2.6 对外入口

- `archive.publish(config, report) -> bool`：从 `DailyReport` 提取 date/title/html/task_list 进度后调 `publish_report`（管道用）；
- `archive.publish_report(config, date_iso, title, html, done, total) -> bool`：节流 + API 全流程（线上补档/验证脚本用，不触发邮件）。

## 3. Tasks：实施清单

1. `notifier/archive.py`：API 交互、index/manifest、节流、双入口；
2. `config.yaml` / `config.yaml.example`：新增 archive 段（改后按硬约束 5 用 Shell 侧 yaml 解析验证）；
3. `main.py`：`run_pipeline` 挂点；
4. `tests/test_archive.py`：mock httpx，用例覆盖——首日新建 / 同文件更新带 sha / index 更新与 manifest 回写 / 同日节流跳过 / 失败冷却 / disabled no-op / gh-pages 自动建分支 / Pages 自动启用；
5. 全量 pytest 跑绿（`NO_PROXY` 已知）；
6. 线上验证：用 `publish_report` 补档当天日报（避免重发邮件）→ 开启 Pages → 确认 `https://chenpingan6158-a11y.github.io/IDNG.github.io/` 可访问；
7. commit + push（走代理）+ CI 全绿。
