# Codex 项目内任务队列

把一个较大的目标拆成独立任务，逐个交给 Codex CLI 执行，并随时查询进度。工具和 skill 随项目走：不需要安装本项目的 Python 包，也不需要本地 API 或服务。

## 集成到其他项目

从本仓库运行：

```bash
python3 scripts/copy_to_project.py /path/to/project
```

或者直接将 [kit](kit/README.md) 中的 `.codex-tasks/`、`.agents/` 两个目录复制到目标项目根目录。已有项目文件不会由安装脚本覆盖。目标项目需要 Python 3.10+ 和可用的 Codex CLI；JSON 格式无需其他 Python 依赖，YAML 格式需要 `PyYAML`。

在目标项目根目录运行：

```bash
python3 .codex-tasks/run.py validate
python3 .codex-tasks/run.py status
python3 .codex-tasks/run.py run --max-tasks 2
```

计划放在 `.codex-tasks/plans/*.json` 或 `*.yaml`；可先参考 `.codex-tasks/examples/`，也可让 Codex 用项目内 `$codex-task-planner` skill 拆解目标。只有复制 skill 而未复制运行器时，Codex 可以参照流程规划，但不能执行队列或记录进度。完整约定见 [项目集成流程与任务标准](docs/项目集成流程与任务标准.md)。

`status --json` 可供其他脚本读取；`show PLAN_ID/TASK_ID` 查看任务与日志位置。下次运行 `run` 会自动重试上次 `blocked` 的任务一次，仍阻塞则退出；`failed`、`interrupted` 仍需手动 `retry PLAN_ID/TASK_ID`。修改已完成任务定义后用 `reconcile PLAN_ID/TASK_ID`。运行器串行调用 `codex exec`，不自行启动服务，也不自动提交或推送。

## 本仓库开发

源代码位于 `src/codex_task/`，`python3 scripts/build_kit.py` 会同步可复制工具包。运行测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

可选的 Python 包入口仍保留，用于本仓库开发；跨项目集成以复制工具包为准。
