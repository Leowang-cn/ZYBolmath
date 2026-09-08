import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import MagicMock, patch

from scripts.container_runner import export_in_container, read_result
from scripts.dingtalk_export import DingTalkExportError
from sync_jobs import create_job, get_current_job, get_job


class ContainerResultTest(unittest.TestCase):
    def test_expired_job_is_failed_on_read(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite"
            job, _ = create_job(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE sync_jobs SET updated_at='2000-01-01T00:00:00+00:00'")
            self.assertEqual(get_current_job(database)["status"], "failed")
            self.assertEqual(get_job(database, job["id"])["errorCode"], "worker_timeout")

    def test_timeout_removes_named_container(self):
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired("docker", 1), 0]
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DINGTALK_DOCUMENT_URL": "https://example.com/doc"}), \
                patch("scripts.container_runner.prepare_directory"), \
                patch("scripts.container_runner.subprocess.Popen", return_value=process) as launch, \
                patch("scripts.container_runner.subprocess.run") as cleanup, \
                patch("scripts.container_runner.time.monotonic", side_effect=[0, 10000]):
            cleanup.return_value.returncode = 0
            with self.assertRaises(DingTalkExportError) as caught:
                export_in_container(Path(directory) / "out.xlsx")
            self.assertEqual(caught.exception.code, "export_timeout")
            command = launch.call_args.args[0]
            name = command[command.index("--name") + 1]
            self.assertEqual(cleanup.call_args.args[0], ["docker", "rm", "-f", name])
            self.assertNotIn("https://example.com/doc", command)
            process.kill.assert_called_once()

    def test_container_success_and_callback_failure(self):
        for callback_fails in (False, True):
            with self.subTest(callback_fails=callback_fails), tempfile.TemporaryDirectory() as directory:
                process = MagicMock(returncode=0)
                process.poll.return_value = 0
                process.wait.side_effect = [subprocess.TimeoutExpired("docker", 1), 0, 0]

                def launch(command, **kwargs):
                    mount = next(value for value in command if value.startswith("type=bind,src=") and value.endswith(",dst=/work"))
                    work = Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/work"))
                    self.assertEqual(command[-1], "/work/export/container_export.py")
                    self.assertFalse(any("/opt/export" in value for value in command))
                    export_dir = work / "export"
                    self.assertEqual(export_dir.stat().st_mode & 0o777, 0o755)
                    for name in ("container_export.py", "dingtalk_export.py", "__init__.py"):
                        copied = export_dir / name
                        self.assertEqual(copied.stat().st_mode & 0o777, 0o644)
                        self.assertEqual(copied.read_bytes(), (Path(__file__).resolve().parents[1] / "scripts" / name).read_bytes())
                    (work / "login.png").write_bytes(b"qr")
                    (work / "result.json").write_text(json.dumps({"ok": True, "file": "dingtalk-export.xlsx"}))
                    with zipfile.ZipFile(work / "dingtalk-export.xlsx", "w") as archive:
                        archive.writestr("xl/workbook.xml", "<workbook/>")
                        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
                    return process

                callback = MagicMock(side_effect=RuntimeError("callback failed") if callback_fails else None)
                with patch.dict(os.environ, {"DINGTALK_DOCUMENT_URL": "https://example.com/doc"}), \
                        patch("scripts.container_runner.prepare_directory"), \
                        patch("scripts.container_runner.subprocess.Popen", side_effect=launch), \
                        patch("scripts.container_runner.subprocess.run") as cleanup:
                    destination = Path(directory) / "out.xlsx"
                    if callback_fails:
                        with self.assertRaises(RuntimeError):
                            export_in_container(destination, callback)
                        self.assertFalse(destination.exists())
                    else:
                        self.assertEqual(export_in_container(destination, callback), destination)
                        self.assertTrue(destination.exists())
                    callback.assert_called_once_with(b"qr")
                    cleanup.assert_called_once()

    def test_valid_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "result.json").write_text(json.dumps({"ok": True, "file": "dingtalk-export.xlsx"}))
            with zipfile.ZipFile(work / "dingtalk-export.xlsx", "w") as archive:
                archive.writestr("xl/workbook.xml", "<workbook/>")
                archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
            self.assertEqual(read_result(work, 0), work / "dingtalk-export.xlsx")

    def test_rejects_invalid_results(self):
        for result, returncode in [([], 0), ({"ok": True, "file": "../../secret"}, 0),
                                   ({"ok": True, "file": "dingtalk-export.xlsx"}, 1),
                                   ({"ok": True, "file": "dingtalk-export.xlsx"}, 0)]:
            with self.subTest(result=result, returncode=returncode), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                (work / "result.json").write_text(json.dumps(result))
                with self.assertRaises(DingTalkExportError):
                    read_result(work, returncode)

    def test_failure_redacts_url(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "result.json").write_text(json.dumps({"ok": False, "code": "export_failed", "message": "failed https://example.com/private"}))
            with self.assertRaises(DingTalkExportError) as caught:
                read_result(work, 1)
            self.assertNotIn("example.com", str(caught.exception))
