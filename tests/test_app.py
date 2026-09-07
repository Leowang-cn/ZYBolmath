from __future__ import annotations

import io
import os
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import app as app_module


class AppApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.original_database = app_module.DATABASE_PATH
        self.original_asset_dir = app_module.ASSET_DIR
        app_module.DATABASE_PATH = root / "app.sqlite"
        app_module.ASSET_DIR = root / "assets"
        os.environ["EDITOR_PASSWORD"] = "test-password"
        self.seed_database()
        application = app_module.create_app()
        application.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = application.test_client()

    def tearDown(self) -> None:
        app_module.DATABASE_PATH = self.original_database
        app_module.ASSET_DIR = self.original_asset_dir
        os.environ.pop("EDITOR_PASSWORD", None)
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