# 项目内任务队列标准

## 集成

复制本工具包的 `.codex-tasks/` 和 `.agents/` 到项目根目录。项目已有 `AGENTS.md` 时沿用，不覆盖。`project.json` 是项目配置；非 Git 项目把 `skip_git_repo_check` 设为 `true`。`plans/` 存放可提交的任务定义；`state.sqlite`、`runs/` 和 `background/` 是本地运行记录，由 `.gitignore` 排除。升级工具时保留项目配置、计划和状态；先在副本中验证新版工具。

## 从目标到计划

1. 先读需求、`AGENTS.md`、相关源码或资料，明确可交付结果与约束。
2. 按可独立交付和验证的边界拆任务。一个任务应能在一次 Codex 运行中完成；过大的任务继续拆，已有结果用 `depends_on` 串起来。依赖仅能引用同一计划内的 ID。
3. 每个任务写清 `objective`（结果）、`acceptance`（可判断的完成条件）。`context` 只列必需且已存在的项目相对路径；`artifacts` 列预期生成的文件。可自动验证的任务写 `checks`，使用 `argv` 参数数组。
4. 任务保持项目中立：开发、文档、研究整理、本地数据处理均用同一结构。检查命令和内容由目标项目决定。不可把密钥写进计划，也不可把提交、推送或外部写入设为默认动作。

文件名使用 `.json` 时只需 Python 标准库；`.yaml` 或 `.yml` 需要 PyYAML。计划示例见 `examples/`。`version: 1`，计划 ID 与任务 ID 使用小写字母、数字和连字符，任务键为 `计划ID/任务ID`。各计划的 `plan.id` 必须唯一。计划是定义文件，任务状态由运行器记录，不在计划中手工改状态。

## 执行和交接

```bash
python3 .codex-tasks/run.py validate
python3 .codex-tasks/run.py status
python3 .codex-tasks/run.py run --max-tasks 2
python3 .codex-tasks/run.py status --json
python3 .codex-tasks/run.py show plan-id/task-id
```

不设 `--max-tasks` 时串行运行所有当前可执行任务；`--task plan-id/task-id` 只运行指定任务。每个任务由一次独立的 `codex exec` 执行，工作目录是项目根目录。成功还需满足产物存在和检查通过。任务返回 `blocked` 时本次执行停止；下次启动 `run` 会优先重试该任务一次，仍阻塞就再次停止并保留状态。其他独立任务要等后续一次启动再继续。`failed` 或 `interrupted` 仍需查看 `.codex-tasks/runs/` 日志、修复原因，并用 `retry plan-id/task-id` 重置。没有可自动检查的任务若需人工验收，可在项目配置中设 `require_review_without_checks: true`，审阅后运行 `accept plan-id/task-id`。已完成任务的定义变化会使它及下游任务进入 `changed`，确认需要重跑后执行 `reconcile plan-id/task-id`。

`run` 默认前台实时显示 Codex 和检查输出；`--foreground` 可显式指定前台，`--background` 则脱离终端运行并打印 PID、日志路径。用 `tail -f 日志路径` 跟踪后台输出，用 `status` 查询状态。前台按 Ctrl-C 可中断当前任务。同一项目同一时间只允许一个执行器运行。

交接时报告：已完成/待执行/失败或阻塞的任务键、验证证据、需要人工判断的事项，以及下一条可运行命令。不要仅凭 Codex 的文字总结宣称项目完成；以任务验收和最终项目检查为准。
