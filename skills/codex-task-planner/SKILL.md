---
name: codex-task-planner
description: Integrate and maintain a project-local Codex task queue when a user wants a multi-step goal split into independently executable tasks with progress tracking.
---

# Project task queue

Use this skill for queue setup, task decomposition, execution, and recovery in the current project. The project-local command is `python3 .codex-tasks/run.py`; run it from the project root. Read the project's `AGENTS.md` and relevant requirements first. If the tool is missing, copy the portable kit into the project (see the source kit's README) before creating plans. Do not replace existing project configuration, plans, or task history.

Write one plan per goal in `.codex-tasks/plans/`. JSON works with Python alone; YAML also works if PyYAML is installed. Use the examples in `.codex-tasks/examples/` and the integration standard in `.codex-tasks/WORKFLOW.md`. Give each task a concrete, bounded outcome that one Codex run can finish; name its acceptance criteria, relevant context paths, expected artifacts, and dependencies within the same plan. Prefer `checks: [{argv: [...]}]` when an objective result is available. For work that needs human judgment, set `require_review_without_checks` in project config and review the output before accepting. Do not encode commits, pushes, external writes, or permission bypasses unless the user has authorized them.

Validate a plan with `python3 .codex-tasks/run.py validate`. Check the queue with `status` before changing it or running it. Run `run --max-tasks N` when a bounded batch is useful; `run` continues until no runnable task remains or one needs attention. It runs in the foreground with live Codex and check output by default; `run --foreground` selects that explicitly, while `run --background` starts a detached runner and prints its PID and log path. Use `tail -f` on that log and `status` to follow background work. A new `run` automatically retries a previously `blocked` task once, then stops again if it remains blocked. Inspect `show PLAN_ID/TASK_ID` and the recorded run directory to diagnose it. Failed or interrupted tasks still require explicit `retry` after fixing the cause. If a completed task definition changes, `reconcile PLAN_ID/TASK_ID` resets it and downstream work. Report completed, failed, blocked, and pending work from `status`; the YAML/JSON plan is a definition, not a status ledger.
