"""Command line interface for portable Codex task plans."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from importlib.resources import files
from pathlib import Path

from .plan import DEFAULT_CONFIG, PlanError, Task, load_config, load_tasks, project_root, yaml
from .runner import execute
from .state import State, now, run_lock


def init(root: Path) -> None:
    root = root.resolve()
    if not root.is_dir():
        raise PlanError(f"project directory does not exist: {root}")
    folder = root / ".codex-tasks"
    plans = folder / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    config_path = folder / ("project.yaml" if yaml is not None else "project.json")
    if not config_path.exists() and not (folder / "project.json").exists() and not (folder / "project.yaml").exists():
        content = (yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True) if yaml is not None
                   else json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n")
        config_path.write_text(content, encoding="utf-8")
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("state.sqlite*\nrun.lock\nruns/\nbackground/\n", encoding="utf-8")
    keep = plans / ".gitkeep"
    if not keep.exists():
        keep.touch()
    print(f"initialized {folder}")


def install_skill(target: Path | None, *, force: bool = False) -> None:
    skills_dir = (target or Path.home() / ".codex" / "skills").expanduser().resolve()
    source = files("codex_task").joinpath("skill", "SKILL.md")
    destination = skills_dir / "codex-task-planner" / "SKILL.md"
    content = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() == content:
            print(f"skill already current: {destination}")
            return
        if not force:
            raise PlanError(f"skill already exists with different content: {destination}; use --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    print(f"installed skill: {destination}")


def _root(args: argparse.Namespace) -> Path:
    if args.project:
        root = Path(args.project).resolve()
        if not any((root / ".codex-tasks" / f"project.{ext}").is_file() for ext in ("yaml", "json")):
            raise PlanError(f"not an initialized project: {root}")
        return root
    return project_root()


def _load(root: Path) -> tuple[dict, list[Task]]:
    config = load_config(root)
    return config, load_tasks(root, config)


def _state(root: Path, tasks: list[Task], *, recover: bool = False) -> State:
    state = State(root)
    state.sync(tasks, recover_running=recover)
    return state


def _status_data(tasks: list[Task], state: State) -> dict:
    rows = state.rows()
    visible = []
    for task in tasks:
        row = rows[task.key]
        dependencies = [f"{task.plan_id}/{dep}" for dep in task.depends_on]
        blocked_by = [dep for dep in dependencies if rows[dep]["status"] != "done"]
        item = {"key": task.key, "title": task.title, "status": row["status"], "summary": row["summary"],
                "depends_on": dependencies, "blocked_by": blocked_by,
                "runnable": row["status"] in ("pending", "blocked") and not blocked_by}
        if row["status"] == "running":
            attempt = state.latest_attempt(task.key)
            if attempt:
                run_dir = Path(attempt["run_dir"])
                item["current_attempt"] = {"id": attempt["id"], "started_at": attempt["started_at"],
                                           "run_dir": attempt["run_dir"], "session_id": _session_id(run_dir),
                                           "last_event_type": _last_event_type(run_dir)}
        visible.append(item)
    counts = {}
    for task in visible:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {"schema_version": 1, "total": len(visible), "counts": counts, "tasks": visible}


def _print_status(data: dict) -> None:
    print(f"{data['total']} tasks: " + ", ".join(f"{key}={value}" for key, value in sorted(data["counts"].items())))
    for task in data["tasks"]:
        marker = " ready" if task["runnable"] else ""
        detail = f" — {task['summary']}" if task["summary"] else ""
        if task["status"] == "pending" and task["blocked_by"]:
            detail = " — waiting for " + ", ".join(task["blocked_by"])
        if "current_attempt" in task:
            attempt = task["current_attempt"]
            event = f"; event={attempt['last_event_type']}" if attempt["last_event_type"] else ""
            detail = f" — started {attempt['started_at']}{event}"
        print(f"{task['key']}: {task['status']}{marker}{detail}")


def _session_id(run_dir: Path) -> str | None:
    events = run_dir / "events.jsonl"
    if not events.is_file():
        return None
    with events.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                return event["thread_id"]
    return None


def _last_event_type(run_dir: Path) -> str | None:
    events = run_dir / "events.jsonl"
    if not events.is_file():
        return None
    with events.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 16384))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            return event["type"]
    return None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PlanError(f"git {' '.join(args)} failed: {detail}")
    return result


def _prepare_git(root: Path, config: dict) -> None:
    if not config["auto_commit"]:
        return
    _git(root, "rev-parse", "--show-toplevel")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=all").stdout.strip()
    if dirty:
        raise PlanError("automatic commits require a clean worktree before run; commit or stash existing changes")


def _push(root: Path, config: dict) -> str:
    branch = config["git_branch"] or _git(root, "branch", "--show-current").stdout.strip()
    if not branch:
        raise PlanError("cannot push detached HEAD; set git_branch in project config")
    result = _git(root, "push", config["git_remote"], f"HEAD:{branch}")
    return result.stderr.strip() or result.stdout.strip()


def _commit_task(root: Path, task: Task, summary: str, config: dict, run_dir: Path) -> tuple[str | None, str | None]:
    if not config["auto_commit"]:
        return None, None
    _git(root, "add", "-A")
    subject = f"{config['commit_prefix']}: [{task.key}] {task.title}"
    subject = " ".join(subject.splitlines())[:72].rstrip()
    body = f"{summary.strip()}\n\nTask-ID: {task.key}"
    commit = _git(root, "commit", "--allow-empty", "-m", subject, "-m", body)
    commit_hash = _git(root, "rev-parse", "HEAD").stdout.strip()
    lines = [commit.stdout.strip(), f"commit: {commit_hash}"]
    push_error = None
    if config["auto_push"]:
        try:
            lines.append("push: " + _push(root, config))
        except PlanError as exc:
            push_error = str(exc)
            lines.append("push error: " + push_error)
    (run_dir / "git.log").write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")
    print(f"committed {task.key}: {commit_hash[:12]}", flush=True)
    if config["auto_push"] and push_error is None:
        print(f"pushed {task.key} to {config['git_remote']}", flush=True)
    return commit_hash, push_error


def _run(root: Path, tasks: list[Task], config: dict, max_tasks: int | None, only: str | None,
         ready_path: Path | None = None, dangerous: bool = True) -> int:
    if max_tasks is not None and max_tasks <= 0:
        raise PlanError("--max-tasks must be positive")
    by_key = {task.key: task for task in tasks}
    if only and only not in by_key:
        raise PlanError(f"unknown task: {only}")
    with run_lock(root):
        _prepare_git(root, config)
        if config["auto_push"]:
            _push(root, config)
        state = _state(root, tasks, recover=True)
        try:
            if ready_path is not None:
                ready_path.touch()
            print("codex permissions: " + ("dangerous (no approvals or sandbox)" if dangerous else config["sandbox"]),
                  flush=True)
            launched = 0
            while max_tasks is None or launched < max_tasks:
                rows = state.rows()
                candidates = [by_key[only]] if only else tasks
                ready = [task for task in candidates if rows[task.key]["status"] in ("pending", "blocked") and all(
                    rows[f"{task.plan_id}/{dep}"]["status"] == "done" for dep in task.depends_on)]
                if not ready:
                    if only and rows[only]["status"] not in ("done",):
                        raise PlanError(f"{only} is {rows[only]['status']} or waiting for dependencies")
                    remaining = [task for task in candidates if rows[task.key]["status"] != "done"]
                    if remaining and launched == 0:
                        raise PlanError("no runnable tasks; inspect status")
                    break
                task = next((item for item in ready if rows[item.key]["status"] == "blocked"), ready[0])
                run_dir = root / ".codex-tasks" / "runs" / f"{now().replace(':', '-')}-{uuid.uuid4().hex[:8]}"
                run_dir.parent.mkdir(exist_ok=True)
                attempt_id = state.start(task, run_dir)
                print(f"running {task.key}", flush=True)
                try:
                    status, summary, exit_code, result, checks = execute(root, task, config, run_dir,
                                                                         stream_output=True, dangerous=dangerous)
                except KeyboardInterrupt:
                    state.finish(attempt_id, task, "interrupted", "interrupted by user", None, None, [])
                    print(f"interrupted {task.key}", file=sys.stderr)
                    return 130
                except Exception as exc:
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "runner-error.log").write_text(traceback.format_exc(), encoding="utf-8")
                    status, summary, exit_code, result, checks = "failed", f"runner error: {exc}; see runner-error.log", None, None, []
                push_error = None
                if status == "done":
                    try:
                        _, push_error = _commit_task(root, task, summary, config, run_dir)
                    except (PlanError, OSError) as exc:
                        status, summary = "failed", f"Git finalization failed: {exc}"
                state.finish(attempt_id, task, status, summary, exit_code, result, checks, _session_id(run_dir))
                print(f"{task.key}: {status} — {summary}", flush=True)
                launched += 1
                if status != "done":
                    return 1
                if push_error:
                    print(f"{task.key}: done locally but push failed — {push_error}", file=sys.stderr, flush=True)
                    return 1
                if only:
                    break
            return 0
        finally:
            state.close()


def _run_background(root: Path, tasks: list[Task], max_tasks: int | None, only: str | None,
                    dangerous: bool = True) -> int:
    if max_tasks is not None and max_tasks <= 0:
        raise PlanError("--max-tasks must be positive")
    if only and only not in {task.key for task in tasks}:
        raise PlanError(f"unknown task: {only}")
    with run_lock(root):
        pass
    folder = root / ".codex-tasks" / "background"
    folder.mkdir(parents=True, exist_ok=True)
    run_id = f"{now().replace(':', '-')}-{uuid.uuid4().hex[:8]}"
    log_path = folder / f"{run_id}.log"
    ready_path = folder / f"{run_id}.ready"
    command = [sys.executable, "-m", "codex_task.cli", "run", "--foreground", "--project", str(root),
               "--_background-ready", str(ready_path)]
    if max_tasks is not None:
        command.extend(["--max-tasks", str(max_tasks)])
    if only:
        command.extend(["--task", only])
    if dangerous:
        command.append("--dangerous")
    else:
        command.append("--sandboxed")
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (package_root, env.get("PYTHONPATH", "")) if part)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
    for _ in range(200):
        if ready_path.exists():
            ready_path.unlink(missing_ok=True)
            threading.Thread(target=process.wait, daemon=True).start()
            print(f"background run started (pid {process.pid}); log: {log_path}")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.05)
    detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
        raise PlanError(f"background runner did not become ready; inspect {log_path}")
    raise PlanError(f"background runner exited before starting: {detail or log_path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-task", description="Run JSON or YAML tasks with Codex")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "install-skill", "validate", "list", "run", "status", "show", "retry", "accept", "reconcile"):
        item = sub.add_parser(command)
        if command == "install-skill":
            item.add_argument("--target", help="Codex skills directory; defaults to ~/.codex/skills")
            item.add_argument("--force", action="store_true", help="replace a different existing skill")
            continue
        item.add_argument("--project", help="target project directory")
        if command in ("show", "retry", "accept", "reconcile"):
            item.add_argument("task_key", help="PLAN_ID/TASK_ID")
        if command == "run":
            item.add_argument("--max-tasks", type=int)
            item.add_argument("--task", help="run one PLAN_ID/TASK_ID")
            permissions = item.add_mutually_exclusive_group()
            permissions.add_argument("--dangerous", dest="dangerous", action="store_true", default=True,
                                     help="run Codex without approvals or sandboxing (default)")
            permissions.add_argument("--sandboxed", dest="dangerous", action="store_false",
                                     help="use the project's configured Codex sandbox")
            mode = item.add_mutually_exclusive_group()
            mode.add_argument("--background", action="store_true", help="run detached and write progress to a log")
            mode.add_argument("--foreground", action="store_true", help="stream progress in this terminal (default)")
            item.add_argument("--_background-ready", type=Path, help=argparse.SUPPRESS)
        if command == "status":
            item.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "init":
            init(Path(args.project) if args.project else Path.cwd())
            return 0
        if args.command == "install-skill":
            install_skill(Path(args.target) if args.target else None, force=args.force)
            return 0
        root = _root(args)
        config, tasks = _load(root)
        if args.command == "validate":
            print(f"valid: {len(tasks)} tasks in {root}")
            return 0
        if args.command == "run":
            if args.background:
                return _run_background(root, tasks, args.max_tasks, args.task, args.dangerous)
            return _run(root, tasks, config, args.max_tasks, args.task, args._background_ready, args.dangerous)
        if args.command in ("retry", "accept", "reconcile"):
            with run_lock(root):
                state = _state(root, tasks, recover=True)
                try:
                    if args.command == "retry":
                        state.retry(args.task_key)
                    elif args.command == "accept":
                        task = next((item for item in tasks if item.key == args.task_key), None)
                        if task is None:
                            raise PlanError(f"unknown task: {args.task_key}")
                        row = state.rows()[args.task_key]
                        if row["status"] != "needs_review":
                            raise PlanError(f"{args.task_key}: only needs_review tasks can be accepted")
                        attempt = state.latest_attempt(args.task_key)
                        if attempt is None:
                            raise PlanError(f"{args.task_key}: no attempt to accept")
                        _, push_error = _commit_task(root, task, "accepted after review", config,
                                                     Path(attempt["run_dir"]))
                        state.accept(args.task_key)
                        if push_error:
                            print(f"{args.task_key}: accepted locally but push failed — {push_error}",
                                  file=sys.stderr)
                            return 1
                    else:
                        print("reset: " + ", ".join(state.reconcile(args.task_key, tasks)))
                    return 0
                finally:
                    state.close()
        try:
            with run_lock(root):
                state = _state(root, tasks, recover=True)
        except PlanError as exc:
            if "another codex-task process" not in str(exc):
                raise
            state = _state(root, tasks)
        try:
            if args.command == "show":
                if args.task_key not in {task.key for task in tasks}:
                    raise PlanError(f"unknown task: {args.task_key}")
                row = state.rows()[args.task_key]
                print(f"{args.task_key}: {row['status']} — {row['summary']}")
                for attempt in state.attempt_rows(args.task_key):
                    session = f" session={attempt['session_id']}" if attempt["session_id"] else ""
                    print(f"attempt {attempt['id']}: {attempt['status'] or 'running'}{session} {attempt['run_dir']}")
                    run_dir = Path(attempt["run_dir"])
                    if run_dir.is_dir():
                        logs = sorted(path.name for path in run_dir.iterdir() if path.is_file())
                        if logs:
                            print("  files: " + ", ".join(logs))
                return 0
            data = _status_data(tasks, state)
            if args.command == "status" and args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                _print_status(data)
            return 0
        finally:
            state.close()
    except (PlanError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
