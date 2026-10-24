# 智能日报生成器 · 去看板化（spec）

> 状态：**待用户审批**
> 日期：2026-10-24
> 方法论：SDD 六阶段 · proposal + design + tasks 合订本（单功能迭代）
> 前置：归档已交付（archive.md），250 测试通过

## 1. Proposal：背景与目标

### 1.1 背景

老师明确不需要 Web 前端：日报的消费端只保留**邮件 + 飞书机器人**（外加已上线的 GitHub Pages 归档页）。当前 `python main.py` 默认启动本地看板 HTTP 服务（webapp/：登录鉴权、/api/reports、手动 plans/issues 表单），这些能力失去存在意义，但看板服务进程同时承载着两个**与 HTTP 无关的后台调度**——15 秒一轮的自动采集（AutoCollector）与每天 10:35 的定时推送（ScheduledPusher），必须保留并迁移。

去掉看板后用户失去两个能力，需要替代方案：

1. **实时确认群消息捕捉结果**（原来靠看板刷新）→ 改为**飞书群回执**：每轮采集新捕捉到条目时，机器人直接在群里发一条确认消息；
2. **随手查看今日捕获概况**（原来打开看板）→ 新增 `python main.py --status` 命令行速查；历史日报回看走 Pages 归档站（免登录）。

### 1.2 目标

1. 删除 webapp/ 全部代码与配置（server/auth/dashboard.html/export），仓库不再含 HTTP 服务；
2. AutoCollector / ScheduledPusher 挪入 main.py，`python main.py` 默认动作改为：抢单实例锁 → 起两个后台线程 → 主线程阻塞；功能参数（采集间隔、push_time、单实例锁、跨进程推送防重）全部保持；
3. 飞书回执：每轮采集对 classify_message 闭环的新捕获条目（明日计划收录 / 完成打勾 / 问题记录 / 方案挂载）即发群回执，防重、失败不阻断；
4. `python main.py --status`：命令行打印今日捕获摘要（提交数 / 消息数 / 任务清单进度 / 明日计划 / 问题统计 / 最近推送日期）；
5. `--view` 保留为调试出口：改为直接打开 reports 表存储的当日 HTML，不再经过看板模板。

### 1.3 非目标（明确不做）

- 任何形式的 Web 页面 / HTTP 接口替代品；
- 看板手动 plans/issues 表单的替代品（手动条目功能随看板下线，群消息关键词是唯一入口）；
- Pages 归档（已上线，不受影响，本次零改动）。

## 2. Design：技术设计

### 2.1 调度类迁移

- `AutoCollector` / `ScheduledPusher` 原样搬入 main.py（含跨进程推送标记 meta `last_push_date`、CHECK_INTERVAL=20s、异常不中断语义）；
- 采集间隔配置从 `dashboard.auto_refresh.interval_seconds` 迁至 **`schedule.collect_interval_seconds`**（默认 15，下限 10 钳制），读取函数 `collect_interval(config) -> int`；
- main() 默认动作（含 `--serve` 兼容旧习惯）：`_acquire_service_lock` → 起 AutoCollector + ScheduledPusher → 主线程 `Event.wait()` 阻塞，KeyboardInterrupt 优雅退出；抽出 `run_service(config, *, stop_event=None)` 便于测试注入立即停止；
- 启动打印：采集间隔、定时推送时间、归档状态，不打开浏览器。

### 2.2 飞书回执

- **捕获点回报**：`_collect_plan_messages` / `_collect_done_messages` / `_collect_issues` 增加可选参数 `receipts: list[str] | None = None`（默认 None，现有测试签名零破坏），仅对**本轮新捕获**追加回执文案：
  - 计划收录：`📌 已收录为明日计划：{title}`（仅 `plan_msg_key_exists` 为假的新条目）；
  - 完成打勾：`✅ 已完成：{title}`（仅飞书打勾成功分支，对账分支不回执）；
  - 问题记录：`⚠️ 已记录问题：{problem}` / 方案挂载：`✅ 问题已更新方案：{problem}`；
- **防重**：plans 靠 plans 表 msg_key 天然跨进程去重；done 靠飞书完成态；issues/方案挂载无持久态，用 main.py 进程内 `_RECEIPT_SENT: set[str]`（msg_key）兜底——重启后当日问题条目最多重发一次，可接受；
- **发送**：`notifier/lark_bot.py` 新增 `send_text(webhook, text, *, timeout, http_client=None) -> bool`，`msg_type=text` 纯文本；多条回执合并为一条消息（换行连接）；每条前缀「【智能日报】」保证过 webhook 自定义关键词安全校验（与日报卡片标题同词）；
- **挂点**：`run_pipeline` 在消息消费完成后、生成报告前统一发送；dry_run（采集轮）同样发送——实时确认正是采集轮的意义；lark_bot 未启用或发送失败仅记日志。

### 2.3 --status 命令

`python main.py --status` 打印（全部读 SQLite，不触网）：

- 今日报告是否存在及生成时间；GitHub 提交数、协作消息数（成员小节聚合）；
- 今日任务清单进度 `已完成 N/M` + 未完成标题；
- 明日计划待完成条目数与标题（plans 表 plan_date=明天）；
- 问题与解决条数（处理中/已解决分列）；
- 最近推送日期（meta `last_push_date`）与最近归档日期（meta `archive_last_date`）；
- 今日无报告时打印引导语（服务在跑则 15 秒内会有首轮采集）。

### 2.4 --view 改造

`view_report` 改为：`storage.get_report(date, team_name)`（无日期时 `list_reports(1)` 取最新）取 `html` 字段写临时文件 `data/report-{date}.html` 并浏览器打开；删除 `from webapp.export import render_dashboard`。

### 2.5 配置与文件清理

- config.yaml / config.yaml.example：删 `dashboard:` 整段（auto_refresh + member_accounts），`schedule:` 下加 `collect_interval_seconds: 15`；改后按硬约束 5 用 Shell 侧 yaml 解析验证；
- config.py 无需改动（dashboard 从未进 `_REQUIRED`）；
- 删除 `webapp/` 目录与 `tests/test_server.py` / `test_auth.py` / `test_export.py`；保留 `tests/dashboard_helpers.py`（test_serializer / test_storage_migration 仍用 sample_report）；
- `启动看板.bat` → `启动服务.bat`、`停止看板.bat` → `停止服务.bat`，内容纯 ASCII；桌面两个 .lnk 快捷方式重建指向新 bat；
- AGENTS.md：目录结构删 webapp、常用命令与硬约束中看板表述改为后台服务、新增回执与 --status 约定。

### 2.6 测试调整

- `test_auto_refresh.py`：import 改自 main；`auto_refresh_settings` 用例改为 `collect_interval` 用例；删 build_server 端到端用例；AutoCollector/ScheduledPusher 用例原样保留；
- `test_main.py`：serve 两个用例改为「默认动作启动两个调度线程」（mock 类 + 预置 stop_event）；锁冲突用例保留；view 用例改为 html 落盘断言；
- 新增回执用例：新计划/打勾/问题/方案各产生一条回执、往轮已入库消息不回执、多命中放弃不回执、发送失败不阻断管道、dry_run 也发回执；
- 新增 --status 用例：有报告 / 无报告两态。

## 3. Tasks：实施清单

1. 本规范落盘 specs/no-dashboard.md；
2. main.py：搬入两个调度类 + collect_interval + run_service + main() 默认动作改写 + 回执收集与发送 + --status + view_report 改造；
3. notifier/lark_bot.py：新增 send_text；
4. 删 webapp/、改 config.yaml / config.yaml.example、bat 改名重写、AGENTS.md 更新；
5. 测试删改 + 新增用例，全量 pytest 跑绿给数字；
6. 重启服务实测：群里发「明天：xxx」收回执、--status 输出、定时线程存活；
7. commit + push（走代理）+ CI 确认；重建桌面快捷方式。
