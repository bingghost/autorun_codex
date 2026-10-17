"""Build the directory that can be copied into any target project."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "kit"
TOOL = KIT / ".codex-tasks"


def main() -> None:
    for path in TOOL.glob("state.sqlite*"):
        path.unlink()
    (TOOL / "run.lock").unlink(missing_ok=True)
    if (TOOL / "runs").exists():
        shutil.rmtree(TOOL / "runs")
    package = TOOL / "codex_task"
    package.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "cli.py", "plan.py", "runner.py", "state.py"):
        shutil.copyfile(ROOT / "src" / "codex_task" / name, package / name)
    (KIT / ".agents" / "skills" / "codex-task-planner").mkdir(parents=True, exist_ok=True)
    (package / "skill").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "skills" / "codex-task-planner" / "SKILL.md",
                    KIT / ".agents" / "skills" / "codex-task-planner" / "SKILL.md")
    shutil.copyfile(ROOT / "skills" / "codex-task-planner" / "SKILL.md",
                    package / "skill" / "SKILL.md")
    shutil.copyfile(ROOT / "skills" / "codex-task-planner" / "SKILL.md",
                    ROOT / "src" / "codex_task" / "skill" / "SKILL.md")
    (TOOL / "run.py").write_text(
        '"""Project-local Codex task runner. Run from the project root."""\n'
        'from codex_task.cli import main\n\n'
        'if __name__ == "__main__":\n    raise SystemExit(main())\n', encoding="utf-8")
    config = {
        "version": 1, "codex_bin": "codex", "model": None, "sandbox": "workspace-write",
        "timeout_seconds": 3600, "check_timeout_seconds": 300,
        "allow_shell_checks": False, "require_review_without_checks": False,
        "skip_git_repo_check": False,
    }
    (TOOL / "project.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (TOOL / ".gitignore").write_text("state.sqlite*\nrun.lock\nruns/\n__pycache__/\n", encoding="utf-8")
    (TOOL / "plans").mkdir(exist_ok=True)
    (TOOL / "plans" / ".gitkeep").touch()


if __name__ == "__main__":
    main()
