"""Execute one task with Codex and deterministic checks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable

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
        "Do not commit or push. The outer task runner owns Git operations after validation.",
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


def _codex_progress(line: str) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        print(line, flush=True)
        return
    if not isinstance(event, dict):
        print(line, flush=True)
        return
    event_type = event.get("type", "event")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    kind = item.get("type")
    if event_type == "thread.started":
        print(f"[codex] session {event.get('thread_id', '')}", flush=True)
    elif event_type == "turn.started":
        print("[codex] working...", flush=True)
    elif event_type == "item.started" and kind == "command_execution":
        print(f"[codex] $ {item.get('command', '')}", flush=True)
    elif event_type == "item.completed" and kind == "command_execution":
        output = item.get("aggregated_output")
        if isinstance(output, str) and output.strip():
            print(output.rstrip(), flush=True)
        print(f"[codex] command exit {item.get('exit_code', '?')}", flush=True)
    elif event_type == "item.completed" and kind == "agent_message":
        message = item.get("text")
        if isinstance(message, str) and message.strip():
            print(message.rstrip(), flush=True)
    elif event_type in ("turn.completed", "turn.failed", "error"):
        detail = event.get("message")
        print(f"[codex] {event_type}" + (f": {detail}" if isinstance(detail, str) else ""), flush=True)
    elif event_type in ("item.started", "item.completed") and kind:
        print(f"[codex] {kind}: {event_type.split('.')[1]}", flush=True)


def _invoke(argv: list[str], *, cwd: Path, input_text: str | None, timeout: int,
            stdout_path: Path, stderr_path: Path, shell: bool = False,
            on_stdout: Callable[[str], None] | None = None,
            on_stderr: Callable[[str], None] | None = None) -> tuple[int | None, str | None]:
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(argv if not shell else argv[0], cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                                       bufsize=1, start_new_session=True, shell=shell)
        except OSError as exc:
            stderr.write(str(exc) + "\n")
            return None, str(exc)

        def copy_lines(source, target, callback: Callable[[str], None] | None) -> None:
            for line in source:
                target.write(line)
                target.flush()
                if callback:
                    callback(line.rstrip("\r\n"))

        readers = [threading.Thread(target=copy_lines, args=(process.stdout, stdout, on_stdout)),
                   threading.Thread(target=copy_lines, args=(process.stderr, stderr, on_stderr))]
        for reader in readers:
            reader.start()
        try:
            if input_text is not None:
                try:
                    process.stdin.write(input_text)
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return process.returncode, f"timed out after {timeout}s"
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait()
            raise
        finally:
            for reader in readers:
                reader.join()
            process.stdout.close()
            process.stderr.close()
        return process.returncode, None


def execute(root: Path, task: Task, config: dict, run_dir: Path,
            *, stream_output: bool = False, dangerous: bool = True) -> tuple[str, str, int | None, dict | None, list[dict]]:
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt = prompt_for(task)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (run_dir / "schema.json").write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
    argv = [config["codex_bin"], "exec", "--json", "--output-schema", str(run_dir / "schema.json"),
            "--output-last-message", str(run_dir / "result.json"), "--cd", str(root)]
    if dangerous:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv.extend(["--sandbox", config["sandbox"]])
    if config["model"]:
        argv.extend(["--model", config["model"]])
    if config["skip_git_repo_check"]:
        argv.append("--skip-git-repo-check")
    argv.append("-")
    (run_dir / "invocation.json").write_text(json.dumps({
        "permissions": "dangerous" if dangerous else config["sandbox"],
        "codex_argv": argv,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    exit_code, error = _invoke(argv, cwd=root, input_text=prompt, timeout=config["timeout_seconds"],
                               stdout_path=run_dir / "events.jsonl", stderr_path=run_dir / "stderr.log",
                               on_stdout=_codex_progress if stream_output else None,
                               on_stderr=(lambda line: print(f"[codex stderr] {line}", flush=True)) if stream_output else None)
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
                                          stdout_path=run_dir / f"check-{index}.out", stderr_path=run_dir / f"check-{index}.err", shell=is_shell,
                                          on_stdout=(lambda line: print(f"[check {index}] {line}", flush=True)) if stream_output else None,
                                          on_stderr=(lambda line: print(f"[check {index} stderr] {line}", flush=True)) if stream_output else None)
        checks.append({"command": command, "exit_code": check_code, "error": check_error})
        if check_error or check_code != 0:
            return "failed", f"check {index} failed; see check-{index}.out and check-{index}.err", exit_code, result, checks
    missing = [name for name in task.artifacts if not artifact_path(root, name).exists()]
    if missing:
        return "failed", f"missing artifacts: {', '.join(missing)}", exit_code, result, checks
    if config["require_review_without_checks"] and not task.checks:
        return "needs_review", result["summary"], exit_code, result, checks
    return "done", result["summary"], exit_code, result, checks
