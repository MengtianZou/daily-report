# AGENTS.md — AI 协作规则

任何 AI 工具在本仓库工作前必须遵守本文件。规则来自项目真实踩坑记录，违反即事故。

## 项目概览

智能日报生成器：采集（GitHub 提交 + 飞书任务/群消息）→ 聚合生成日报 → 三端推送（邮件 / 飞书机器人 / GitHub Pages 归档）+ 群消息实时回执 + `--status` 命令行速查。无 Web 前端。SDD（规范驱动）项目：动手改代码前先读 `specs/` 下最新版规范（当前为 no-dashboard.md）；新功能先补规范再实现。

## 目录结构

- `main.py` — 后台服务入口（15s 自动采集 + 定时推送 + 群回执 + --status/--view/--check）+ 管道 run_pipeline + 消息分类 classify_message
- `collector/` — 采集层（github / lark_task / lark_msg / _lark_client）
- `generator/` — 生成层（formatter / template / diffview / summarizer）
- `notifier/` — 推送层（email / lark_bot / archive）
- `shared/` — 基础层（storage / models / config / serializer / logger / errors / envfile）
- `specs/` — SDD 规范文档；`tests/` — pytest 测试

## 常用命令

```powershell
# 全量测试（Windows；本机代理 127.0.0.1:7897 会拦截 localhost，必须设 NO_PROXY）
$env:NO_PROXY='localhost,127.0.0.1'; .\.venv\Scripts\python.exe -m pytest -q

# 启动后台服务（单实例文件锁 data/service.lock，第二实例自动退出；
# 15 秒一轮自动采集 + 每天 10:35 定时推送 + 群回执）
python main.py

# 查看今日捕获摘要（无网，全读 SQLite）
python main.py --status

# 浏览器打开最新日报 HTML（直读 reports 表 html 字段）
python main.py --view
python main.py --view 2026-10-11

# 一次性采集+生成+推送（非工作日默认跳过；测试时临时把日期加进 config.yaml
# schedule.workdays，用完必须还原）
python main.py --run
python main.py --run --dry-run     # 只采集生成，不推送（采集轮也发回执）
python main.py --run --date 2026-10-11  # 指定日期（测试历史日报用）
```

## 硬约束

1. **密钥只能走 `.env` 环境变量**，禁止写入 config.yaml、代码或文档（飞书凭证、SMTP 授权码、GitHub Token、AI API Key）。
2. **全源采集失败不生成空日报；单源失败优雅降级**继续生成。
3. **LLM 摘要可降级**：未启用 / 无 key / 调用失败 / 超时 → 跳过摘要，绝不阻断主管道。
4. **日期时间禁止硬编码**：不得写死时区偏移（如 +8）或固定时刻字符串。CI 在 UTC 环境曾抓出 3 处此类 bug。期望时刻必须按运行环境动态计算（本机时区）。
5. **修改 config.yaml 后**：必须用 `python -c "import yaml; ..."` 验证解析结果；成员/数据源配置变更后必须重启后台服务（旧进程持旧配置）。
6. **bat 文件纯 ASCII**：中文注释在 GBK 控制台会乱码并被当作命令执行。
7. **config.yaml 用 Edit 写入后**必须经 Shell 侧验证再信任（曾出现编辑器写入成功但进程读到旧内容）。
8. **群回执失败不阻断主管道**：飞书机器人回执是附加环节，任何异常或被拒绝都只记 warning 日志，日报必须照常生成推送。回执文案必须含「【智能日报】」前缀以过 webhook 自定义关键词安全校验。

## 日报格式（三端必须一致）

- 邮件主题：`研一+姓名+YYYY-MM-DD+智能日报`；单人日报标题「[成员名]的智能日报」，多人「团队名 智能日报」。
- 板块顺序：github代码提交情况 → 今日任务清单 → 飞书SDD测试群协作沟通情况 → 问题与解决 → 明日计划。
- 「问题与解决」格式「✅ 问题 → 方案」、不显示负责人；「明日计划」格式「📌 标题」、不显示负责人。
- 当日新建任务（status_from="(无)"）只渲染标题；真实状态转移保留标注。
- 改格式必须三端同步：formatter（markdown）/ template（HTML 邮件 + Pages 归档）；提交内联展示共用 `generator/diffview.inline_groups` 判定。

## 消息任务闭环（classify_message 规则）

- plan 触发词：明天/明日/计划/待办（消息开头，或「触发词：」冒号强信号）；编号列表自动拆分。
- done 触发词：完成/做完/搞定（开头或尾部宣告，可连剥，如「…PPT已完成」）；done 优先于 plan；done 消息反向跳过 plan 触发词开头。
- done 匹配梯度：双侧归一化精确 → 双向包含 → 唯一模糊兜底（字符集 Jaccard ≥ 0.7）；多命中全部放弃。
- 解决特征词 `_RESOLVED_HINTS`（已解决/已修复/修复为/修好了/解决方案：/原因是 等）命中 → 是解决总结不是新问题，自动挂为同负责人最近无方案条目的 solution。
- **群回执时机**：每轮自动采集（15s）完成闭环后即发回执，不等定时推送；回执条目只覆盖「本轮新捕获」，已入库的旧消息不重复回执。

## 飞书 API 约定

- open_id 按应用隔离：必须用本项目应用自身凭证查询，成员 lark 字段只接受 `ou_` 开头。
- Task v2：更新任务 body = `{"task": {…}, "update_fields": [字段名]}`；完成 = PATCH completed_at 为毫秒时间戳；创建挂清单用 body 内 tasklists；子任务走 `/subtasks` 接口（列表接口不返回子任务，需递归）。
- API 权限需开通且发布版本后才生效；机器人必须已在目标群内。
- 非团队成员的未完成任务不进入明日计划自动条目。
- webhook 自定义关键词安全校验：回执文案（日报卡片 / 纯文本回执）必须含预设关键词（当前为「智能日报」），缺失会被机器人静默拒发。

## GitHub 采集约定

- 仓库配置用短格式 `owner/repo`。
- 搜索 API 的时间参数必须百分号编码（`+` → `%2B`），否则返回 200 + 空列表的静默失败。
- 提交条目始终提供「查看提交」链接（html_url，缺失时由 repo+sha 拼接）。
- Pages 归档走 gh-pages 孤儿分支，禁止放 main 分支（会被 GitHub 采集器自我污染）。

## 测试与交付纪律

- 测试覆盖四场景：正常 / 单源降级 / 空数据 / 全源失败。
- **交付前必须跑全量 pytest 并给出数字证据**（如"250 passed"），不允许"应该没问题"。
- 新功能必须带测试；改 classify_message / done 匹配必须把真实用户消息加为回归用例。
- CI：`.github/workflows/ci.yml`（Windows + Python 3.14），每次 push 自动跑全量；时区相关测试的期望值必须动态计算（教训见硬约束 4）。
- 日志采用 JSON Lines 格式。
