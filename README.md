# Codex Task Runner

[简体中文 README](README.zh-CN.md)

Codex Task Runner turns a larger goal into a queue of small, independently executable tasks. A project-local Python script reads JSON or YAML plans, runs one task at a time through Codex CLI, records progress, and resumes work across invocations. Copy the tool and its planning skill into a project; no local API or service is required.

## Quick start

You need Python 3.10+ and an installed, authenticated Codex CLI. JSON plans use only the Python standard library. YAML plans additionally require `PyYAML`.

From this repository, install the portable kit into an existing project:

```bash
python3 scripts/copy_to_project.py /path/to/project
```

You can also copy `kit/.codex-tasks/` and `kit/.agents/` directly to the target project's root. The installer does not overwrite existing files with different content or replace an existing project configuration. In the target project, create a plan in `.codex-tasks/plans/`, then run:

```bash
python3 .codex-tasks/run.py validate
python3 .codex-tasks/run.py status
python3 .codex-tasks/run.py run --max-tasks 2
python3 .codex-tasks/run.py status
```

The project skill at `.agents/skills/codex-task-planner/SKILL.md` can help Codex break down a goal; invoke `$codex-task-planner` or write a plan yourself. Example development and non-development plans are in `.codex-tasks/examples/`. Copy and adapt an example into `plans/` before running it. For a project without Git, set `skip_git_repo_check` to `true`, `auto_commit` to `false`, and `auto_push` to `false` in `.codex-tasks/project.json`.

## Workflow

```mermaid
flowchart TD
    A["Define the goal and acceptance criteria"] --> B["Split work into independent tasks"]
    B --> C["Write a plan in .codex-tasks/plans/"]
    C --> D["Validate the plan"]
    D --> E["Run the queue"]
    E --> F{"Eligible task?"}
    F -- "pending or blocked; dependencies done" --> G["Run one Codex task"]
    F -- "none" --> H["Inspect status and remaining work"]
    G --> I{"Result and checks"}
    I -- "done" --> M["Commit and push this task"]
    M --> E
    I -- "blocked" --> J["Stop; retry once on the next run"]
    I -- "failed or interrupted" --> K["Inspect logs, fix cause, then retry"]
    I -- "needs_review" --> L["Review output, then accept"]
    J --> N["Start a new run"]
    N --> E
    K --> E
    L --> E
```

Each `run` invocation executes eligible tasks serially. A previously `blocked` task has priority over independent `pending` tasks and is attempted once in the new invocation. If it is still blocked, the runner stops again. `failed` and `interrupted` tasks require an explicit `retry`; a task requiring human review needs `accept`. Editing a completed task marks that task and completed dependents as `changed`; pending dependents wait for their dependencies. Use `reconcile` when you intend to rerun the changed task and its dependents.

`run` stays in the foreground by default and streams Codex progress, command output, and check output to the terminal. Use `--foreground` to select this explicitly, or `--background` to return to the shell while a detached runner continues. The background command prints its PID and log path; follow that log with `tail -f PATH` and query task state with `status`. Press Ctrl-C to interrupt a foreground task. Only one runner can work in a project at a time.

`run` now starts each child `codex exec` with `--dangerously-bypass-approvals-and-sandbox` by default, so child tasks have no Codex approval prompts or Codex sandbox. `--dangerous` states this mode explicitly. Use `run --sandboxed` to opt into the sandbox configured in `.codex-tasks/project.json` or `project.yaml`; it also works with `--background`. The default grants child Codex full access to the host, so use this runner on trusted projects and machines. It cannot remove restrictions imposed by a parent Codex session or an external sandbox; launch the runner from your own shell if those restrictions would block the work.

## How it works

```mermaid
flowchart LR
    P["Plan files: objectives, dependencies, acceptance"] --> R["Project-local Python runner"]
    C["project.json: Codex and review settings"] --> R
    R -->|"one task prompt"| X["codex exec"]
    X -->|"structured result"| R
    R --> V["Check expected files and optional commands"]
    V --> R
    R --> G["Git commit and push per completed task"]
    R --> S[("state.sqlite: tasks and attempts")]
    R --> L["runs/: prompts, events, results, check logs"]
    Q["status / show / retry / reconcile"] <--> R
```

Plans are definitions, not a progress ledger. The runner selects tasks whose dependencies are complete, asks Codex to work in the target project, checks its structured completion result, verifies listed artifacts and optional check commands, then saves the attempt and status locally in SQLite. SQLite is part of Python and needs no database server. Each attempt gets its own directory under `.codex-tasks/runs/` with the prompt, invocation and permission mode (`invocation.json`), raw Codex events (`events.jsonl`), stderr, structured result, check output, and Git finalization output (`git.log`). Runner exceptions also leave a `runner-error.log` traceback. `show PLAN_ID/TASK_ID` lists every attempt directory and its files, including earlier retries. Background runs additionally save the terminal progress stream under `.codex-tasks/background/`. These logs and the state database are ignored by Git. `status --json` exposes progress to other scripts.

`acceptance` tells Codex and reviewers what success means; it is not automatically proven by the runner. Use `artifacts` and `checks` for outcomes that can be verified mechanically. After a task passes, the runner creates one Git commit and pushes it by default. It requires a clean worktree before starting so unrelated changes are not included. Configure `auto_commit`, `auto_push`, `git_remote`, `git_branch`, and `commit_prefix` in `project.json` or `project.yaml`; set both automatic options to `false` for non-Git projects. Deployment and other external writes remain outside the runner.

The main project settings are:

| Setting | Default | Meaning |
| --- | --- | --- |
| `auto_commit` | `true` | Create one commit after each completed task. |
| `auto_push` | `true` | Push each task commit immediately. Requires `auto_commit`. |
| `git_remote` | `origin` | Remote used for automatic pushes. |
| `git_branch` | `null` | Push to the current branch; set a name to override it. |
| `commit_prefix` | `task` | Prefix for generated commit subjects. |
| `sandbox` | `workspace-write` | Sandbox used only when `run --sandboxed` is selected. |

## Plan format

A plan is one `.json`, `.yaml`, or `.yml` file in `.codex-tasks/plans/`. This minimal JSON plan works without extra Python packages:

```json
{
  "version": 1,
  "plan": {"id": "onboarding", "title": "Write onboarding documentation"},
  "tasks": [
    {
      "id": "draft",
      "title": "Draft the guide",
      "objective": "Create docs/onboarding.md for new contributors",
      "acceptance": ["The guide explains setup and the first validation command"],
      "artifacts": ["docs/onboarding.md"]
    },
    {
      "id": "review",
      "title": "Review the guide",
      "objective": "Verify the guide against the project and correct inaccuracies",
      "depends_on": ["draft"],
      "acceptance": ["Commands and paths match the current project"],
      "artifacts": ["docs/onboarding.md"]
    }
  ]
}
```

Task keys have the form `plan-id/task-id`. A task may also list existing project-relative `context` files and `checks`, such as `{"argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]}`. Dependencies refer to task IDs in the same plan. See the [integration and task standard](docs/项目集成流程与任务标准.md) for the full workflow and field rules.

## Useful commands

Run these from the target project's root:

| Command | Purpose |
| --- | --- |
| `python3 .codex-tasks/run.py validate` | Validate configuration, plans, paths, and dependencies. |
| `python3 .codex-tasks/run.py status --json` | Query task status in a machine-readable form. |
| `python3 .codex-tasks/run.py run` | Run in the foreground with live output until no task is runnable or one needs attention. |
| `python3 .codex-tasks/run.py run --foreground` | Explicitly select foreground execution. |
| `python3 .codex-tasks/run.py run --background` | Run detached and unattended; print PID and `.codex-tasks/background/*.log` path. |
| `python3 .codex-tasks/run.py run --sandboxed` | Use the configured Codex sandbox instead of the default full-access mode. |
| `python3 .codex-tasks/run.py run --max-tasks 2` | Run at most two tasks in this invocation. |
| `python3 .codex-tasks/run.py run --task onboarding/draft` | Run one eligible task. |
| `python3 .codex-tasks/run.py show onboarding/draft` | Show status, attempts, and log locations. |
| `python3 .codex-tasks/run.py retry onboarding/draft` | Reset a failed or interrupted task for another run. |
| `python3 .codex-tasks/run.py accept onboarding/draft` | Accept a task waiting for human review. |
| `python3 .codex-tasks/run.py reconcile onboarding/draft` | Reset a changed task and downstream tasks. |

## Repository development

The maintained implementation is in `src/codex_task/`. Run `python3 scripts/build_kit.py` after changing it to refresh the copyable `kit/`. To run the repository tests, install the optional development package and then use:

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

The Python package entry point remains available for development. Copying the kit is the recommended integration path for other projects.
