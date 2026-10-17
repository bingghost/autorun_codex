# 项目内 Codex 任务队列

将本目录中的 `.codex-tasks/` 和 `.agents/` 复制到目标项目根目录，然后在该项目运行：

```bash
python3 .codex-tasks/run.py validate
python3 .codex-tasks/run.py status
```

从工具源仓库集成时，也可以运行 `python3 scripts/copy_to_project.py /path/to/project`；已有不同内容的文件不会被覆盖。需要 Python 3.10+ 和已安装、已登录的 Codex CLI。JSON 计划无需安装 Python 包；如需 YAML 计划，安装 `PyYAML` 即可。没有本地 API、服务或后台进程。

把 `.codex-tasks/examples/` 中的示例复制并改写到 `.codex-tasks/plans/`，然后运行 `validate`。完整流程与任务标准见 [.codex-tasks/WORKFLOW.md](.codex-tasks/WORKFLOW.md)。项目级 skill 位于 `.agents/skills/codex-task-planner/SKILL.md`，可在 Codex 中使用 `$codex-task-planner`。
