from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed")
DEFAULT_TIMEOUT_SECONDS = 30 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_jobs(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                error_code TEXT,
                result_json TEXT,
                pid INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def create_job(database_path: Path) -> tuple[dict[str, Any], bool]:
    initialize_jobs(database_path)
    now = utc_now()
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=_timeout_seconds())).isoformat()
    with closing(sqlite3.connect(database_path, timeout=10)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE sync_jobs SET status = 'failed', stage = 'failed', message = ?, error_code = 'worker_timeout', updated_at = ? "
            "WHERE status IN ('queued', 'running') AND updated_at < ?",
            ("同步任务超时或执行进程已退出", now, stale_before),
        )
        active = connection.execute(
            "SELECT * FROM sync_jobs WHERE status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if active is not None:
            connection.row_factory = sqlite3.Row
            active = connection.execute("SELECT * FROM sync_jobs WHERE id = ?", (active[0],)).fetchone()
            return serialize_job(active), False
        job_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO sync_jobs(id, status, stage, message, created_at, updated_at) VALUES (?, 'queued', 'queued', ?, ?, ?)",
            (job_id, "等待同步任务启动", now, now),
        )
    return get_job(database_path, job_id), True


def get_job(database_path: Path, job_id: str) -> dict[str, Any] | None:
    initialize_jobs(database_path)
    expire_jobs(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
    return serialize_job(row) if row else None


def get_current_job(database_path: Path) -> dict[str, Any] | None:
    initialize_jobs(database_path)
    expire_jobs(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM sync_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    return serialize_job(row) if row else None


def expire_jobs(database_path: Path) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_timeout_seconds())).isoformat()
    with closing(sqlite3.connect(database_path, timeout=10)) as connection, connection:
        connection.execute(
            "UPDATE sync_jobs SET status='failed', stage='failed', error_code='worker_timeout', message=?, updated_at=? "
            "WHERE status IN ('queued', 'running') AND updated_at < ?",
            ("同步任务超时或执行进程已退出", utc_now(), cutoff),
        )


def update_job(database_path: Path, job_id: str, **changes: Any) -> None:
    allowed = {"status", "stage", "message", "error_code", "result_json", "pid"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if "result_json" in values and not isinstance(values["result_json"], str):
        values["result_json"] = json.dumps(values["result_json"], ensure_ascii=False, sort_keys=True)
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            f"UPDATE sync_jobs SET {assignments} WHERE id = ?",
            (*values.values(), job_id),
        )


def launch_job(database_path: Path, job_id: str, reauthenticate: bool = False) -> int:
    log_directory = database_path.parent / "sync-logs"
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(log_directory / f"{job_id}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "scripts/run_dingtalk_sync.py"), job_id],
            cwd=Path(__file__).parent,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={**os.environ, "SYNC_JOBS_PATH": str(database_path), "DINGTALK_FORCE_LOGIN": "1" if reauthenticate else "0"},
        )
    update_job(database_path, job_id, pid=process.pid)
    return process.pid


def serialize_job(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {
        "id": row["id"],
        "status": row["status"],
        "stage": row["stage"],
        "message": row["message"],
        "errorCode": row["error_code"],
        "result": result,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _timeout_seconds() -> int:
    try:
        return max(60, int(os.getenv("SYNC_JOB_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS