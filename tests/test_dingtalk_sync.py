from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app
from scripts import run_dingtalk_sync
from sync_jobs import create_job, get_job
from tests.test_sync_workbook import write_fixture


class DingTalkSyncWorkerTest(unittest.TestCase):
    def test_saves_login_screenshot_and_updates_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs_path = root / "sync-jobs.sqlite"
            job, _ = create_job(jobs_path)
            artifact_dir = root / "sync-artifacts" / job["id"]

            run_dingtalk_sync._save_login_screenshot(jobs_path, job["id"], artifact_dir, b"fake-png")

            waiting = get_job(jobs_path, job["id"])
            self.assertEqual(waiting["stage"], "awaiting_login")
            self.assertIn("扫描二维码", waiting["message"])
            self.assertEqual((artifact_dir / "login.png").read_bytes(), b"fake-png")

    def test_installs_exported_workbook_and_completes_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original_database = app.DATABASE_PATH
            original_assets = app.ASSET_DIR
            original_sheet = app.TARGET_SHEET
            app.DATABASE_PATH = root / "app.sqlite"
            app.ASSET_DIR = root / "assets"
            app.TARGET_SHEET = "课程"
            jobs_path = root / "sync-jobs.sqlite"
            os.environ["SYNC_JOBS_PATH"] = str(jobs_path)
            job, created = create_job(jobs_path)
            self.assertTrue(created)

            def fake_export(destination: Path, on_login_screenshot=None) -> Path:
                write_fixture(destination, "年级", include_second_row=True)
                return destination

            try:
                with patch.object(run_dingtalk_sync, "export_workbook", side_effect=fake_export):
                    exit_code = run_dingtalk_sync.run(job["id"])

                self.assertEqual(exit_code, 0)
                completed = get_job(jobs_path, job["id"])
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["result"]["rows_changed"], 2)
                with closing(sqlite3.connect(app.DATABASE_PATH)) as connection:
                    self.assertEqual(connection.execute("SELECT value FROM cells WHERE cell_ref = 'A2'").fetchone()[0], "第二行")
                    object_key = connection.execute("SELECT object_key FROM assets").fetchone()[0]
                self.assertTrue((app.ASSET_DIR / object_key).is_file())
            finally:
                app.DATABASE_PATH = original_database
                app.ASSET_DIR = original_assets
                app.TARGET_SHEET = original_sheet
                os.environ.pop("SYNC_JOBS_PATH", None)


if __name__ == "__main__":
    unittest.main()