"""Install the portable kit into a target project without replacing local work."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


KIT = Path(__file__).resolve().parents[1] / "kit"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="existing target project directory")
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        parser.error(f"project directory does not exist: {project}")
    conflicts = []
    files = [p for folder in (KIT / ".codex-tasks", KIT / ".agents")
             for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts
             and "runs" not in p.relative_to(KIT).parts and "background" not in p.relative_to(KIT).parts
             and p.suffix != ".pyc"
             and not p.name.startswith("state.sqlite") and p.name != "run.lock"]
    for source in files:
        dest = project / source.relative_to(KIT)
        if source.name == "project.json" and (dest.exists() or (project / ".codex-tasks/project.yaml").exists()):
            continue
        if dest.exists() and dest.read_bytes() != source.read_bytes():
            conflicts.append(str(dest))
    if conflicts:
        parser.error("existing files differ; resolve them before copying:\n" + "\n".join(conflicts))
    for source in files:
        dest = project / source.relative_to(KIT)
        if source.name == "project.json" and (dest.exists() or (project / ".codex-tasks/project.yaml").exists()):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(source, dest)
    print(f"installed project-local task runner and skill in {project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
