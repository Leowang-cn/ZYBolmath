from __future__ import annotations

import io
import os
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app as app_module
from sync_jobs import create_job, update_job


class AppApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.original_database = app_module.DATABASE_PATH
        self.original_asset_dir = app_module.ASSET_DIR
        app_module.DATABASE_PATH = root / "app.sqlite"
        app_module.ASSET_DIR = root / "assets"
        os.environ["EDITOR_PASSWORD"] = "test-password"
        os.environ.pop("DINGTALK_DOCUMENT_URL", None)
        os.environ["SYNC_JOBS_PATH"] = str(root / "sync-jobs.sqlite")
        self.seed_database()
        application = app_module.create_app()
        application.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = application.test_client()

    def tearDown(self) -> None:
        app_module.DATABASE_PATH = self.original_database
        app_module.ASSET_DIR = self.original_asset_dir
        os.environ.pop("EDITOR_PASSWORD", None)
        os.environ.pop("DINGTALK_DOCUMENT_URL", None)
        os.environ.pop("SYNC_JOBS_PATH", None)
        self.temporary_directory.cleanup()

    def seed_database(self) -> None:
        with closing(sqlite3.connect(app_module.DATABASE_PATH)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE sheets (sheet_id TEXT PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE rows (sheet_id TEXT NOT NULL, row_number INTEGER NOT NULL, row_hash TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (sheet_id, row_number));
                CREATE TABLE cells (sheet_id TEXT NOT NULL, row_number INTEGER NOT NULL, cell_ref TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (sheet_id, cell_ref));
                CREATE TABLE assets (sha256 TEXT PRIMARY KEY, object_key TEXT NOT NULL UNIQUE, content_type TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE cell_images (sheet_id TEXT NOT NULL, row_number INTEGER NOT NULL, column_number INTEGER NOT NULL, ordinal INTEGER NOT NULL, asset_sha256 TEXT NOT NULL, PRIMARY KEY (sheet_id, row_number, column_number, ordinal));
                """
            )
            connection.execute("INSERT INTO sheets VALUES ('sheet-1', ?, 0, 'hash', 'now')", (app_module.TARGET_SHEET,))
            connection.executemany("INSERT INTO rows VALUES ('sheet-1', ?, 'hash', 'now')", [(1,), (2,)])
            connection.executemany(
                "INSERT INTO cells VALUES ('sheet-1', ?, ?, ?)",
                [(1, "A1", "年级"), (1, "D1", "讲次名"), (2, "A2", "三年级"), (2, "D2", "巧求周长")],
            )

    def login(self) -> None:
        response = self.client.post("/api/auth/login", json={"password": "test-password"})
        self.assertEqual(response.status_code, 200)

    def test_health_records_and_authentication(self) -> None:
        self.assertEqual(self.client.get("/api/health").get_json(), {"dataReady": True, "status": "ok"})
        records = self.client.get("/api/records").get_json()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "巧求周长")
        self.assertEqual(self.client.patch("/api/records/2", json={"values": {"A": "四年级"}}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "wrong"}).status_code, 401)

    def test_sync_requires_authentication_and_configuration(self) -> None:
        self.assertEqual(self.client.post("/api/data/sync").status_code, 401)
        self.login()
        response = self.client.post("/api/data/sync")
        self.assertEqual(response.status_code, 503)
        self.assertIn("尚未配置", response.get_json()["error"])

    def test_sync_starts_one_persistent_job(self) -> None:
        self.login()
        os.environ["DINGTALK_DOCUMENT_URL"] = "https://alidocs.dingtalk.com/example"
        with patch("app.launch_job", return_value=12345) as launch:
            first = self.client.post("/api/data/sync")
            second = self.client.post("/api/data/sync")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(launch.call_count, 1)
        job = first.get_json()["job"]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.client.get(f"/api/data/sync/{job['id']}").get_json()["job"]["id"], job["id"])
        self.assertEqual(self.client.get("/api/data/sync/current").get_json()["job"]["id"], job["id"])

    def test_login_screenshot_requires_authentication_and_disables_cache(self) -> None:
        jobs_path = Path(os.environ["SYNC_JOBS_PATH"])
        job, _ = create_job(jobs_path)
        screenshot_path = app_module.DATABASE_PATH.parent / "sync-artifacts" / job["id"] / "login.png"
        screenshot_path.parent.mkdir(parents=True)
        screenshot_path.write_bytes(b"fake-png")
        update_job(jobs_path, job["id"], status="running", stage="awaiting_login", message="请扫码")

        self.assertEqual(self.client.get(f"/api/data/sync/{job['id']}/login.png").status_code, 401)
        self.login()
        response = self.client.get(f"/api/data/sync/{job['id']}/login.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"fake-png")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()

    def test_reports_missing_seed_data(self) -> None:
        app_module.DATABASE_PATH.unlink()
        application = app_module.create_app()
        application.config.update(TESTING=True, SECRET_KEY="test-secret")
        client = application.test_client()

        self.assertEqual(client.get("/api/health").get_json(), {"dataReady": False, "status": "ok"})
        response = client.get("/api/records")
        self.assertEqual(response.status_code, 503)
        self.assertIn("题库数据尚未导入", response.get_json()["error"])

    def test_imports_seed_data_from_archive(self) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            bundle.add(app_module.DATABASE_PATH, arcname="data/app.sqlite")
            empty_assets = tarfile.TarInfo("data/assets/")
            empty_assets.type = tarfile.DIRTYPE
            bundle.addfile(empty_assets)
        archive.seek(0)

        app_module.DATABASE_PATH.unlink()
        application = app_module.create_app()
        application.config.update(TESTING=True, SECRET_KEY="test-secret")
        client = application.test_client()
        self.assertEqual(client.post("/api/auth/login", json={"password": "test-password"}).status_code, 200)
        response = client.post(
            "/api/data/import",
            data={"archive": (archive, "data.tar.gz")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["records"], 1)
        self.assertTrue(client.get("/api/health").get_json()["dataReady"])
        self.assertEqual(len(client.get("/api/records").get_json()["records"]), 1)

    def test_rejects_unsafe_data_archive(self) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            payload = b"unsafe"
            member = tarfile.TarInfo("../outside.txt")
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        archive.seek(0)

        app_module.DATABASE_PATH.unlink()
        application = app_module.create_app()
        application.config.update(TESTING=True, SECRET_KEY="test-secret")
        client = application.test_client()
        self.assertEqual(client.post("/api/auth/login", json={"password": "test-password"}).status_code, 200)
        response = client.post(
            "/api/data/import",
            data={"archive": (archive, "unsafe.tar.gz")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("不安全路径", response.get_json()["error"])

    def test_updates_seed_data_and_preserves_overrides(self) -> None:
        self.login()
        self.assertEqual(self.client.patch("/api/records/2", json={"values": {"A": "网页覆盖"}}).status_code, 200)
        image_upload = self.client.post(
            "/api/records/2/images",
            data={"image": (io.BytesIO(b"server-image"), "server.png"), "action": "append"},
            content_type="multipart/form-data",
        )
        image_hash = image_upload.get_json()["images"][0]["hash"]

        updated_database = Path(self.temporary_directory.name) / "updated.sqlite"
        original_database = app_module.DATABASE_PATH
        app_module.DATABASE_PATH = updated_database
        try:
            self.seed_database()
            with closing(sqlite3.connect(updated_database)) as connection, connection:
                connection.execute("UPDATE cells SET value = '新版标题' WHERE cell_ref = 'D2'")
        finally:
            app_module.DATABASE_PATH = original_database
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            bundle.add(updated_database, arcname="data/app.sqlite")
            empty_assets = tarfile.TarInfo("data/assets/")
            empty_assets.type = tarfile.DIRTYPE
            bundle.addfile(empty_assets)
        archive.seek(0)

        response = self.client.post(
            "/api/data/import",
            data={"archive": (archive, "update.tar.gz")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["fieldOverrides"], 1)
        self.assertEqual(response.get_json()["imageOverrides"], 1)
        self.assertEqual(response.get_json()["preservedAssets"], 1)
        record = self.client.get("/api/records").get_json()["records"][0]
        self.assertEqual(record["title"], "新版标题")
        self.assertEqual(record["values"]["A"], "网页覆盖")
        self.assertEqual(record["images"][0]["hash"], image_hash)
        image_response = self.client.get(f"/api/assets/{image_hash}")
        self.assertEqual(image_response.data, b"server-image")
        image_response.close()

    def test_field_and_image_overrides(self) -> None:
        self.login()
        self.assertEqual(self.client.patch("/api/records/2", json={"values": {"A": "四年级"}}).status_code, 200)
        upload = self.client.post(
            "/api/records/2/images",
            data={"image": (io.BytesIO(b"test-image"), "example.png"), "action": "append"},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 200)
        asset_hash = upload.get_json()["images"][0]["hash"]
        asset_response = self.client.get(f"/api/assets/{asset_hash}")
        self.assertEqual(asset_response.data, b"test-image")
        asset_response.close()
        self.assertEqual(self.client.put("/api/records/2/images", json={"assetHashes": []}).status_code, 200)

        record = self.client.get("/api/records").get_json()["records"][0]
        self.assertEqual(record["values"]["A"], "四年级")
        self.assertEqual(record["images"], [])


if __name__ == "__main__":
    unittest.main()