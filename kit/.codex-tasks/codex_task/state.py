"""SQLite state and project run lock."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .plan import PlanError, Task


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, root: Path):
        self.root = root
        self.conn = sqlite3.connect(root / ".codex-tasks" / "state.sqlite")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL REFERENCES tasks(task_key),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT,
                exit_code INTEGER,
                summary TEXT NOT NULL DEFAULT '',
                run_dir TEXT NOT NULL,
                result_json TEXT,
                checks_json TEXT,
                session_id TEXT
            );
        """)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(attempts)")}
        if "session_id" not in columns:
            self.conn.execute("ALTER TABLE attempts ADD COLUMN session_id TEXT")

    def close(self) -> None:
        self.conn.close()

    def sync(self, tasks: list[Task], *, recover_running: bool = False) -> None:
        with self.conn:
            if recover_running:
                interrupted_at = now()
                self.conn.execute("""UPDATE attempts SET ended_at=?, status='interrupted', summary='previous run ended unexpectedly'
                    WHERE ended_at IS NULL""", (interrupted_at,))
                self.conn.execute("""UPDATE tasks SET status='interrupted', updated_at=?, summary='previous run ended unexpectedly'
                    WHERE status='running'""", (interrupted_at,))
            for task in tasks:
                row = self.conn.execute("SELECT fingerprint, status FROM tasks WHERE task_key=?", (task.key,)).fetchone()
                if row is None:
                    self.conn.execute("INSERT INTO tasks VALUES (?, ?, 'pending', ?, '')", (task.key, task.fingerprint, now()))
                elif row["fingerprint"] != task.fingerprint and row["status"] != "changed":
                    self.conn.execute("UPDATE tasks SET status='changed', updated_at=?, summary='task definition changed' WHERE task_key=?", (now(), task.key))
            changed = {row["task_key"] for row in self.conn.execute("SELECT task_key FROM tasks WHERE status='changed'")}
            previous_size = -1
            while previous_size != len(changed):
                previous_size = len(changed)
                for task in tasks:
                    if any(f"{task.plan_id}/{dep}" in changed for dep in task.depends_on):
                        changed.add(task.key)
            for key in changed:
                self.conn.execute("""UPDATE tasks SET status='changed', updated_at=?, summary='upstream task changed'
                    WHERE task_key=? AND status NOT IN ('pending', 'changed', 'running')""", (now(), key))

    def rows(self) -> dict[str, dict]:
        return {row["task_key"]: dict(row) for row in self.conn.execute("SELECT * FROM tasks")}

    def attempt_rows(self, key: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM attempts WHERE task_key=? ORDER BY id DESC", (key,))]

    def latest_attempt(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM attempts WHERE task_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return dict(row) if row else None

    def start(self, task: Task, run_dir: Path) -> int:
        with self.conn:
            self.conn.execute("UPDATE tasks SET status='running', updated_at=?, summary='' WHERE task_key=?", (now(), task.key))
            cur = self.conn.execute("INSERT INTO attempts(task_key, started_at, run_dir) VALUES (?, ?, ?)", (task.key, now(), str(run_dir)))
            return int(cur.lastrowid)

    def finish(self, attempt_id: int, task: Task, status: str, summary: str, exit_code: int | None,
               result: dict | None, checks: list[dict], session_id: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE tasks SET status=?, updated_at=?, summary=? WHERE task_key=?", (status, now(), summary, task.key))
            self.conn.execute("""UPDATE attempts SET ended_at=?, status=?, exit_code=?, summary=?, result_json=?, checks_json=?, session_id=?
                WHERE id=?""", (now(), status, exit_code, summary, json.dumps(result, ensure_ascii=False) if result is not None else None,
                              json.dumps(checks, ensure_ascii=False), session_id, attempt_id))

    def retry(self, key: str) -> None:
        row = self.rows().get(key)
        if row is None:
            raise PlanError(f"unknown task: {key}")
        if row["status"] not in ("failed", "blocked", "interrupted", "needs_review"):
            raise PlanError(f"{key}: cannot retry from {row['status']}")
        self._set_pending([key])

    def accept(self, key: str) -> None:
        row = self.rows().get(key)
        if row is None or row["status"] != "needs_review":
            raise PlanError(f"{key}: only needs_review tasks can be accepted")
        with self.conn:
            self.conn.execute("UPDATE tasks SET status='done', updated_at=?, summary='accepted after review' WHERE task_key=?", (now(), key))

    def reconcile(self, key: str, tasks: list[Task]) -> list[str]:
        by_key = {task.key: task for task in tasks}
        if key not in by_key:
            raise PlanError(f"unknown task: {key}")
        affected = {key}
        changed = True
        while changed:
            before = len(affected)
            for task in tasks:
                if any(f"{task.plan_id}/{dep}" in affected for dep in task.depends_on):
                    affected.add(task.key)
            changed = len(affected) != before
        with self.conn:
            for task_key in affected:
                self.conn.execute("UPDATE tasks SET fingerprint=?, status='pending', updated_at=?, summary='reconciled' WHERE task_key=?",
                                  (by_key[task_key].fingerprint, now(), task_key))
        return sorted(affected)

    def _set_pending(self, keys: list[str]) -> None:
        with self.conn:
            for key in keys:
                self.conn.execute("UPDATE tasks SET status='pending', updated_at=?, summary='' WHERE task_key=?", (now(), key))


@contextmanager
def run_lock(root: Path):
    path = root / ".codex-tasks" / "run.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PlanError("another codex-task process is running in this project") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
