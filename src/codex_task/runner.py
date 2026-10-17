"""Execute one task with Codex and deterministic checks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from .plan import Task, artifact_path


RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "evidence"],
}


def prompt_for(task: Task) -> str:
    lines = [
        f"Task: {task.key} — {task.title}",
        f"Objective: {task.objective}",
        "Work independently within the project. Read AGENTS.md if present. Do not launch another Codex instance.",
        "If blocked, report the blocker. Do not claim completion without doing the work.",
    ]
    if task.instructions:
        lines.extend(["Project instructions:", task.instructions])
    for label, items in (("Context files", task.context), ("Acceptance criteria", task.acceptance), ("Expected artifacts", task.artifacts)):
        if items:
            lines.append(label + ":")
            lines.extend(f"- {item}" for item in items)
    lines.append("Return a concise status, summary, and concrete evidence in the required JSON response.")
    return "\n".join(lines) + "\n"


def _invoke(argv: list[str], *, cwd: Path, input_text: str | None, timeout: int,
            stdout_path: Path, stderr_path: Path, shell: bool = False) -> tuple[int | None, str | None]:
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(argv if not shell else argv[0], cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, text=True, start_new_session=True, shell=shell)
        except OSError as exc:
            stderr.write(str(exc) + "\n")
            return None, str(exc)
        try:
            process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            return process.returncode, f"timed out after {timeout}s"
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate()
            raise
        return process.returncode, None


def execute(root: Path, task: Task, config: dict, run_dir: Path) -> tuple[str, str, int | None, dict | None, list[dict]]:
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt = prompt_for(task)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (run_dir / "schema.json").write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
    argv = [config["codex_bin"], "exec", "--json", "--sandbox", config["sandbox"], "--output-schema", str(run_dir / "schema.json"),
            "--output-last-message", str(run_dir / "result.json"), "--cd", str(root)]
    if config["model"]:
        argv.extend(["--model", config["model"]])
    if config["skip_git_repo_check"]:
        argv.append("--skip-git-repo-check")
    argv.append("-")
    exit_code, error = _invoke(argv, cwd=root, input_text=prompt, timeout=config["timeout_seconds"],
                               stdout_path=run_dir / "events.jsonl", stderr_path=run_dir / "stderr.log")
    if error:
        return "failed", error, exit_code, None, []
    if exit_code != 0:
        stderr_lines = (run_dir / "stderr.log").read_text(encoding="utf-8", errors="replace").splitlines()
        detail = next((line.strip() for line in reversed(stderr_lines) if line.strip()), "see stderr.log")
        return "failed", f"Codex exited with {exit_code}: {detail[:300]}", exit_code, None, []
    try:
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict) or result.get("status") not in ("completed", "blocked") or not isinstance(result.get("summary"), str) or not isinstance(result.get("evidence"), list) or any(not isinstance(v, str) for v in result["evidence"]):
            raise ValueError("invalid result shape")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "failed", f"invalid Codex result: {exc}", exit_code, None, []
    if result["status"] == "blocked":
        return "blocked", result["summary"], exit_code, result, []
    checks: list[dict] = []
    for index, check in enumerate(task.checks, start=1):
        is_shell = "shell" in check
        command = [check["shell"]] if is_shell else check["argv"]
        check_code, check_error = _invoke(command, cwd=root, input_text=None, timeout=config["check_timeout_seconds"],
                                          stdout_path=run_dir / f"check-{index}.out", stderr_path=run_dir / f"check-{index}.err", shell=is_shell)
        checks.append({"command": command, "exit_code": check_code, "error": check_error})
        if check_error or check_code != 0:
            return "failed", f"check {index} failed; see check-{index}.out and check-{index}.err", exit_code, result, checks
    missing = [name for name in task.artifacts if not artifact_path(root, name).exists()]
    if missing:
        return "failed", f"missing artifacts: {', '.join(missing)}", exit_code, result, checks
    if config["require_review_without_checks"] and not task.checks:
        return "needs_review", result["summary"], exit_code, result, checks
    return "done", result["summary"], exit_code, result, checks
