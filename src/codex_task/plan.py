"""Load and validate portable task plans."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # JSON plans work with the Python standard library alone.
    yaml = None


ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
DEFAULT_CONFIG = {
    "version": 1,
    "codex_bin": "codex",
    "model": None,
    "sandbox": "workspace-write",
    "timeout_seconds": 3600,
    "check_timeout_seconds": 300,
    "allow_shell_checks": False,
    "require_review_without_checks": False,
    "skip_git_repo_check": False,
    "auto_commit": True,
    "auto_push": True,
    "git_remote": "origin",
    "git_branch": None,
    "commit_prefix": "task",
}


class PlanError(ValueError):
    pass


if yaml is not None:
    class UniqueLoader(yaml.SafeLoader):
        pass

    def _mapping(loader: UniqueLoader, node: yaml.MappingNode) -> dict:
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            try:
                if key in result:
                    raise PlanError(f"duplicate YAML key: {key}")
                result[key] = loader.construct_object(value_node)
            except TypeError as exc:
                raise PlanError("YAML mapping keys must be strings or numbers") from exc
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_plan_file(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            value = json.loads(content, object_pairs_hook=_unique_json_pairs)
        else:
            if yaml is None:
                raise PlanError(f"{path}: YAML requires PyYAML; install it or use .json files")
            value = yaml.load(content, Loader=UniqueLoader)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PlanError(f"{path}: {exc}") from exc
    except Exception as exc:
        if yaml is not None and isinstance(exc, yaml.YAMLError):
            raise PlanError(f"{path}: {exc}") from exc
        raise
    if not isinstance(value, dict):
        raise PlanError(f"{path}: expected a mapping")
    return value


def project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if any((directory / ".codex-tasks" / f"project.{ext}").is_file() for ext in ("yaml", "json")):
            return directory
    raise PlanError("project not found; run 'codex-task init' or pass --project")


def _inside(root: Path, name: str, *, must_exist: bool = False) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        raise PlanError(f"absolute path is not portable: {name}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise PlanError(f"path escapes project: {name}")
    if must_exist and not resolved.exists():
        raise PlanError(f"path does not exist: {name}")
    return resolved


def load_config(root: Path) -> dict:
    path = root / ".codex-tasks" / "project.yaml"
    if not path.is_file():
        path = root / ".codex-tasks" / "project.json"
    if not path.is_file():
        raise PlanError(f"missing {path}; run 'codex-task init'")
    supplied = read_plan_file(path)
    if any(not isinstance(key, str) for key in supplied):
        raise PlanError(f"{path}: setting names must be strings")
    unknown = set(supplied) - set(DEFAULT_CONFIG)
    if unknown:
        raise PlanError(f"{path}: unknown settings: {', '.join(sorted(unknown))}")
    config = {**DEFAULT_CONFIG, **supplied}
    if type(config["version"]) is not int or config["version"] != 1:
        raise PlanError("project config version must be 1")
    if config["sandbox"] not in ("workspace-write", "read-only"):
        raise PlanError("sandbox must be workspace-write or read-only")
    for key in ("timeout_seconds", "check_timeout_seconds"):
        if type(config[key]) is not int or config[key] <= 0:
            raise PlanError(f"{key} must be a positive integer")
    for key in ("allow_shell_checks", "require_review_without_checks", "skip_git_repo_check", "auto_commit", "auto_push"):
        if type(config[key]) is not bool:
            raise PlanError(f"{key} must be true or false")
    if config["auto_push"] and not config["auto_commit"]:
        raise PlanError("auto_push requires auto_commit")
    if not isinstance(config["codex_bin"], str) or not config["codex_bin"].strip():
        raise PlanError("codex_bin must be a command path")
    if config["model"] is not None and (not isinstance(config["model"], str) or not config["model"].strip()):
        raise PlanError("model must be a nonempty string or null")
    for key in ("git_remote", "commit_prefix"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise PlanError(f"{key} must be a nonempty string")
    if config["git_branch"] is not None and (not isinstance(config["git_branch"], str) or not config["git_branch"].strip()):
        raise PlanError("git_branch must be a nonempty string or null")
    return config


@dataclass(frozen=True)
class Task:
    key: str
    plan_id: str
    id: str
    title: str
    objective: str
    context: tuple[str, ...]
    acceptance: tuple[str, ...]
    artifacts: tuple[str, ...]
    depends_on: tuple[str, ...]
    checks: tuple[dict, ...]
    instructions: str
    fingerprint: str


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise PlanError(f"{label} must be a list of nonempty strings")
    return tuple(value)


def load_tasks(root: Path, config: dict) -> list[Task]:
    plan_dir = root / ".codex-tasks" / "plans"
    if not plan_dir.is_dir():
        raise PlanError(f"missing plans directory: {plan_dir}")
    paths = sorted([*plan_dir.glob("*.yaml"), *plan_dir.glob("*.yml"), *plan_dir.glob("*.json")])
    tasks: list[Task] = []
    plan_ids: set[str] = set()
    for path in paths:
        data = read_plan_file(path)
        if type(data.get("version")) is not int or data["version"] != 1 or set(data) - {"version", "plan", "tasks"}:
            raise PlanError(f"{path}: invalid version or unknown top-level fields")
        plan = data.get("plan")
        if not isinstance(plan, dict) or set(plan) - {"id", "title", "defaults"}:
            raise PlanError(f"{path}: invalid plan")
        plan_id = plan.get("id")
        if not isinstance(plan_id, str) or not ID_RE.fullmatch(plan_id) or plan_id in plan_ids:
            raise PlanError(f"{path}: invalid or duplicate plan id: {plan_id}")
        plan_ids.add(plan_id)
        if not isinstance(plan.get("title"), str) or not plan["title"].strip():
            raise PlanError(f"{path}: plan title must be nonempty")
        defaults = plan.get("defaults", {})
        if not isinstance(defaults, dict) or set(defaults) - {"instructions"}:
            raise PlanError(f"{path}: invalid plan defaults")
        instructions = defaults.get("instructions", "")
        if not isinstance(instructions, str):
            raise PlanError(f"{path}: instructions must be a string")
        rows = data.get("tasks")
        if not isinstance(rows, list) or not rows:
            raise PlanError(f"{path}: tasks must be a nonempty list")
        seen_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - {"id", "title", "objective", "context", "acceptance", "artifacts", "depends_on", "checks"}:
                raise PlanError(f"{path}: invalid task fields")
            task_id = row.get("id")
            if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id) or task_id in seen_ids:
                raise PlanError(f"{path}: invalid or duplicate task id: {task_id}")
            seen_ids.add(task_id)
            for field in ("title", "objective"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise PlanError(f"{path}: {task_id}.{field} must be nonempty")
            context = _strings(row.get("context", []), f"{task_id}.context")
            artifacts = _strings(row.get("artifacts", []), f"{task_id}.artifacts")
            acceptance = _strings(row.get("acceptance", []), f"{task_id}.acceptance")
            deps = _strings(row.get("depends_on", []), f"{task_id}.depends_on")
            if not acceptance:
                raise PlanError(f"{path}: {task_id} needs acceptance criteria")
            for name in context:
                _inside(root, name, must_exist=True)
            for name in artifacts:
                _inside(root, name)
            checks = row.get("checks", [])
            if not isinstance(checks, list):
                raise PlanError(f"{path}: {task_id}.checks must be a list")
            for check in checks:
                if not isinstance(check, dict) or len(check) != 1:
                    raise PlanError(f"{path}: invalid check in {task_id}")
                if "argv" in check:
                    argv = check["argv"]
                    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
                        raise PlanError(f"{path}: invalid argv check in {task_id}")
                elif "shell" in check:
                    if not config["allow_shell_checks"] or not isinstance(check["shell"], str) or not check["shell"].strip():
                        raise PlanError(f"{path}: shell check not enabled or invalid in {task_id}")
                else:
                    raise PlanError(f"{path}: check must use argv or shell")
            payload = {"task": row, "instructions": instructions}
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            tasks.append(Task(f"{plan_id}/{task_id}", plan_id, task_id, row["title"], row["objective"], context,
                              acceptance, artifacts, deps, tuple(checks), instructions, fingerprint))
    by_key = {task.key: task for task in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if f"{task.plan_id}/{dep}" not in by_key:
                raise PlanError(f"{task.key}: missing dependency {dep}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise PlanError(f"dependency cycle at {key}")
        if key in visited:
            return
        visiting.add(key)
        for dep in by_key[key].depends_on:
            visit(f"{by_key[key].plan_id}/{dep}")
        visiting.remove(key)
        visited.add(key)

    for task in tasks:
        visit(task.key)
    return tasks


def artifact_path(root: Path, name: str) -> Path:
    return _inside(root, name)
