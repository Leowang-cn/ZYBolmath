#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from scripts.dingtalk_export import DingTalkExportError, export_workbook
from scripts.sync_workbook import LocalAssetStore, create_data_archive, sync_workbook
from sync_jobs import update_job


def run(job_id: str) -> int:
    jobs_path = Path(os.getenv("SYNC_JOBS_PATH", app.DATABASE_PATH.parent / "sync-jobs.sqlite"))
    artifact_dir = app.DATABASE_PATH.parent / "sync-artifacts" / job_id
    lock_path = app.DATABASE_PATH.parent / ".dingtalk-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("w") as task_lock:
            if not _try_lock(task_lock):
                raise DingTalkExportError("sync_busy", "已有题库同步任务正在执行")
            update_job(jobs_path, job_id, status="running", stage="launching_browser", message="正在启动浏览器")
            with tempfile.TemporaryDirectory(prefix="olmath-sync-", dir=app.DATABASE_PATH.parent) as temporary_directory:
                temporary_root = Path(temporary_directory)
                workbook_path = temporary_root / "dingtalk-export.xlsx"
                update_job(jobs_path, job_id, stage="opening_document", message="正在检查钉钉登录和文档权限")
                export_workbook(
                    workbook_path,
                    on_login_screenshot=lambda payload: _save_login_screenshot(jobs_path, job_id, artifact_dir, payload),
                )
                update_job(jobs_path, job_id, stage="processing", message="正在解析表格和图片")

                staged_database = temporary_root / "data/app.sqlite"
                staged_assets = temporary_root / "data/assets"
                staged_assets.mkdir(parents=True)
                stats = sync_workbook(workbook_path, staged_database, LocalAssetStore(staged_assets))
                archive_path = temporary_root / "data.tar.gz"
                create_data_archive(staged_database, staged_assets, archive_path)

                update_job(jobs_path, job_id, stage="installing", message="正在校验并安装新版题库")
                import_lock_path = app.DATABASE_PATH.parent / ".data-import.lock"
                with import_lock_path.open("w") as import_lock:
                    fcntl.flock(import_lock, fcntl.LOCK_EX)
                    with archive_path.open("rb") as archive:
                        result = app.install_data_archive(archive)
                result.update(stats)
                update_job(
                    jobs_path,
                    job_id,
                    status="completed",
                    stage="completed",
                    message="题库更新完成",
                    result_json=result,
                )
        return 0
    except DingTalkExportError as error:
        update_job(jobs_path, job_id, status="failed", stage="failed", message=str(error), error_code=error.code)
    except Exception as error:
        update_job(jobs_path, job_id, status="failed", stage="failed", message=f"题库同步失败：{error}", error_code="sync_failed")
    finally:
        shutil.rmtree(artifact_dir, ignore_errors=True)
    return 1


def _save_login_screenshot(jobs_path: Path, job_id: str, artifact_dir: Path, payload: bytes) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = artifact_dir / "login.tmp"
    screenshot_path = artifact_dir / "login.png"
    temporary_path.write_bytes(payload)
    os.replace(temporary_path, screenshot_path)
    update_job(jobs_path, job_id, stage="awaiting_login", message="请使用钉钉扫描二维码完成登录")


def _try_lock(lock: object) -> bool:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: run_dingtalk_sync.py JOB_ID", file=sys.stderr)
        return 2
    return run(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())