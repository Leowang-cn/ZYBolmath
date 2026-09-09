from __future__ import annotations

import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import fcntl
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, session

from sync_jobs import create_job, get_current_job, get_job, launch_job
from scripts.sync_workbook import LocalAssetStore, inspect_workbook, sync_workbook


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "data/app.sqlite"))
ASSET_DIR = Path(os.getenv("ASSET_DIR", ROOT / "data/assets"))
TARGET_SHEET = os.getenv("TARGET_SHEET", "三四年级新大纲")
ACTIVE_TARGET_SHEET = TARGET_SHEET
FILTER_COLUMNS = tuple("ABCDGHLMNOP")
VISIBLE_COLUMNS = tuple("ABCDEFGH") + tuple("LMNOPQRSTU")
ALL_COLUMNS = tuple("ABCDEFGHIJKLMNOPQRSTU")
ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_DATA_IMPORT_BYTES = 256 * 1024 * 1024
MAX_DATA_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_DATA_ARCHIVE_MEMBERS = 10_000
REQUIRED_DATA_TABLES = {"sheets", "rows", "cells", "assets", "cell_images"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_app_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cell_overrides (
                sheet_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                column_name TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (sheet_id, row_number, column_name)
            );
            CREATE TABLE IF NOT EXISTS row_image_overrides (
                sheet_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                asset_hashes_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (sheet_id, row_number)
            );
            """
        )


def create_app() -> Flask:
    app = Flask(__name__, static_folder="frontend/dist", static_url_path="")
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", secrets.token_hex(32)),
        MAX_CONTENT_LENGTH=MAX_DATA_IMPORT_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    )
    initialize_app_database()

    @app.get("/api/health")
    def health() -> Response:
        try:
            with get_connection() as connection:
                data_ready = table_exists(connection, "sheets") and table_exists(connection, "rows")
            return jsonify(status="ok", dataReady=data_ready)
        except sqlite3.Error:
            return jsonify(status="error"), 503

    @app.get("/api/auth/status")
    def auth_status() -> Response:
        return jsonify(authenticated=bool(session.get("editor")))

    @app.post("/api/auth/login")
    def login() -> Response:
        password = str((request.get_json(silent=True) or {}).get("password", ""))
        expected = os.getenv("EDITOR_PASSWORD", "olmath")
        if not hmac.compare_digest(password, expected):
            return jsonify(error="密码错误"), 401
        session.clear()
        session["editor"] = True
        return jsonify(authenticated=True)

    @app.post("/api/auth/logout")
    def logout() -> Response:
        session.clear()
        return jsonify(authenticated=False)

    @app.post("/api/data/import")
    def import_data() -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        uploaded = request.files.get("archive")
        if uploaded is None or not uploaded.filename:
            return jsonify(error="请选择题库数据压缩包"), 400
        try:
            lock_path = DATABASE_PATH.parent / ".data-import.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = install_data_archive(uploaded.stream)
        except (DataImportError, tarfile.TarError, sqlite3.Error, OSError) as error:
            return jsonify(error=f"导入失败：{error}"), 400
        return jsonify(ok=True, **result)

    @app.post("/api/data/workbook/inspect")
    def inspect_data_workbook() -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        uploaded = request.files.get("workbook")
        if uploaded is None or not uploaded.filename:
            return jsonify(error="请选择 Excel 工作簿"), 400
        try:
            with tempfile.NamedTemporaryFile(dir=DATABASE_PATH.parent, suffix=".xlsx") as temporary:
                uploaded.save(temporary.name)
                sheets = inspect_workbook(Path(temporary.name))
        except (ValueError, OSError, zipfile.BadZipFile) as error:
            return jsonify(error=f"Excel 文件校验失败：{error}"), 400
        if not sheets:
            return jsonify(error="未找到表头完全匹配的工作表"), 400
        return jsonify(sheets=sheets)

    @app.post("/api/data/workbook/import")
    def import_data_workbook() -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        uploaded = request.files.get("workbook")
        selected_sheet_id = request.form.get("sheet_id") or None
        if uploaded is None or not uploaded.filename:
            return jsonify(error="请选择 Excel 工作簿"), 400
        try:
            with tempfile.NamedTemporaryFile(dir=DATABASE_PATH.parent, suffix=".xlsx") as temporary:
                uploaded.save(temporary.name)
                candidates = inspect_workbook(Path(temporary.name))
                if len(candidates) > 1 and not selected_sheet_id:
                    return jsonify(error="匹配到多个工作表，请选择一个", sheets=candidates), 409
                if selected_sheet_id and selected_sheet_id not in {str(sheet["sheet_id"]) for sheet in candidates}:
                    return jsonify(error="选择的工作表不是有效的目标工作表"), 400
                lock_path = DATABASE_PATH.parent / ".data-import.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    result = sync_workbook(Path(temporary.name), DATABASE_PATH, LocalAssetStore(ASSET_DIR), selected_sheet_id)
                    global ACTIVE_TARGET_SHEET
                    imported_sheet_id = selected_sheet_id or str(candidates[0]["sheet_id"])
                    ACTIVE_TARGET_SHEET = next(
                        sheet["name"] for sheet in candidates
                        if str(sheet["sheet_id"]) == imported_sheet_id
                    )
        except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
            return jsonify(error=f"Excel 导入失败：{error}"), 400
        return jsonify(ok=True, **result)

    @app.post("/api/data/sync")
    def start_data_sync() -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        if not os.getenv("DINGTALK_DOCUMENT_URL"):
            return jsonify(error="服务器尚未配置钉钉文档地址"), 503
        if os.getenv("ASSET_BACKEND", "local") != "local":
            return jsonify(error="钉钉自动更新当前仅支持本地图片存储，请使用离线数据包更新"), 503
        jobs_path = sync_jobs_path()
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or type(payload.get("reauthenticate", False)) is not bool:
            return jsonify(error="reauthenticate 必须为布尔值"), 400
        job, created = create_job(jobs_path)
        if not created:
            return jsonify(error="已有题库同步任务正在执行", job=job), 409
        try:
            if payload.get("reauthenticate"):
                launch_job(jobs_path, job["id"], reauthenticate=True)
            else:
                launch_job(jobs_path, job["id"])
        except OSError as error:
            from sync_jobs import update_job
            update_job(jobs_path, job["id"], status="failed", stage="failed", message="同步进程启动失败", error_code="worker_start_failed")
            return jsonify(error=f"同步进程启动失败：{error}"), 503
        return jsonify(job=get_job(jobs_path, job["id"])), 202

    @app.get("/api/data/sync/current")
    def current_data_sync() -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        return jsonify(job=get_current_job(sync_jobs_path()))

    @app.get("/api/data/sync/<job_id>")
    def data_sync_status(job_id: str) -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        job = get_job(sync_jobs_path(), job_id)
        if job is None:
            return jsonify(error="同步任务不存在"), 404
        return jsonify(job=job)

    @app.get("/api/data/sync/<job_id>/login.png")
    def data_sync_login_screenshot(job_id: str) -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        job = get_job(sync_jobs_path(), job_id)
        if job is None:
            return jsonify(error="同步任务不存在"), 404
        screenshot_path = DATABASE_PATH.parent / "sync-artifacts" / job_id / "login.png"
        if job["stage"] != "awaiting_login" or not screenshot_path.is_file():
            return jsonify(error="登录二维码尚未生成或已经失效"), 404
        response = send_file(screenshot_path, mimetype="image/png", conditional=False)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/records")
    def records() -> Response:
        with get_connection() as connection:
            if not table_exists(connection, "sheets"):
                return jsonify(error="题库数据尚未导入，请上传 data/app.sqlite 和 data/assets"), 503
            sheet_id = get_target_sheet_id(connection)
            sheet = connection.execute("SELECT sheet_id, name FROM sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
            if sheet is None:
                return jsonify(error=f"未找到工作表：{ACTIVE_TARGET_SHEET}"), 404
            headers = load_headers(connection, sheet["sheet_id"])
            result = load_records(connection, sheet["sheet_id"], headers)
        return jsonify(
            sheet={"id": sheet["sheet_id"], "name": sheet["name"]},
            headers=headers,
            filterColumns=list(FILTER_COLUMNS),
            visibleColumns=list(VISIBLE_COLUMNS),
            records=result,
        )

    @app.patch("/api/records/<int:row_number>")
    def update_record(row_number: int) -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        values = (request.get_json(silent=True) or {}).get("values")
        if not isinstance(values, dict):
            return jsonify(error="values 必须是对象"), 400
        clean_values = {column: str(value) for column, value in values.items() if column in ALL_COLUMNS}
        with get_connection() as connection:
            sheet_id = get_target_sheet_id(connection)
            if sheet_id is None or not row_exists(connection, sheet_id, row_number):
                return jsonify(error="数据不存在"), 404
            connection.executemany(
                "INSERT INTO cell_overrides(sheet_id, row_number, column_name, value, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(sheet_id, row_number, column_name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                [(sheet_id, row_number, column, value, utc_now()) for column, value in clean_values.items()],
            )
        return jsonify(ok=True)

    @app.post("/api/records/<int:row_number>/images")
    def upload_image(row_number: int) -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        uploaded = request.files.get("image")
        if uploaded is None:
            return jsonify(error="请选择或粘贴图片"), 400
        content_type = uploaded.mimetype.lower()
        extension = ALLOWED_IMAGE_TYPES.get(content_type)
        if extension is None:
            return jsonify(error="仅支持 JPG、PNG、WebP 和 GIF 图片"), 415
        payload = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if not payload or len(payload) > MAX_UPLOAD_BYTES:
            return jsonify(error="图片为空或超过 20 MB"), 413
        asset_hash = hashlib.sha256(payload).hexdigest()
        object_key = f"uploads/{asset_hash[:2]}/{asset_hash}{extension}"
        action = request.form.get("action", "append")
        try:
            image_index = int(request.form.get("index", "-1"))
        except ValueError:
            return jsonify(error="图片位置无效"), 400
        with get_connection() as connection:
            sheet_id = get_target_sheet_id(connection)
            if sheet_id is None or not row_exists(connection, sheet_id, row_number):
                return jsonify(error="数据不存在"), 404
            save_asset(connection, asset_hash, object_key, content_type, payload)
            hashes = effective_image_hashes(connection, sheet_id, row_number)
            if action == "replace":
                if image_index < 0 or image_index >= len(hashes):
                    return jsonify(error="被替换的图片不存在"), 400
                hashes[image_index] = asset_hash
            elif action == "append":
                hashes.append(asset_hash)
            else:
                return jsonify(error="不支持的上传操作"), 400
            save_image_override(connection, sheet_id, row_number, hashes)
        return jsonify(images=serialize_images(hashes))

    @app.put("/api/records/<int:row_number>/images")
    def update_images(row_number: int) -> Response:
        auth_error = require_editor()
        if auth_error:
            return auth_error
        hashes = (request.get_json(silent=True) or {}).get("assetHashes")
        if not isinstance(hashes, list) or any(not isinstance(item, str) for item in hashes):
            return jsonify(error="assetHashes 必须是字符串数组"), 400
        with get_connection() as connection:
            sheet_id = get_target_sheet_id(connection)
            if sheet_id is None or not row_exists(connection, sheet_id, row_number):
                return jsonify(error="数据不存在"), 404
            known = {
                row["sha256"]
                for row in connection.execute(
                    f"SELECT sha256 FROM assets WHERE sha256 IN ({','.join('?' for _ in hashes)})", hashes
                )
            } if hashes else set()
            if len(known) != len(set(hashes)):
                return jsonify(error="图片列表包含无效资源"), 400
            save_image_override(connection, sheet_id, row_number, hashes)
        return jsonify(images=serialize_images(hashes))

    @app.get("/api/assets/<asset_hash>")
    def asset(asset_hash: str) -> Response:
        with get_connection() as connection:
            item = connection.execute(
                "SELECT object_key, content_type FROM assets WHERE sha256 = ?", (asset_hash,)
            ).fetchone()
        if item is None:
            return jsonify(error="图片不存在"), 404
        if os.getenv("ASSET_BACKEND", "local") == "minio":
            return stream_minio_asset(item["object_key"], item["content_type"])
        asset_path = ASSET_DIR / item["object_key"]
        if not asset_path.is_file():
            return jsonify(error="图片文件不存在"), 404
        return send_file(asset_path, mimetype=item["content_type"], conditional=True, max_age=31536000)

    @app.get("/")
    @app.get("/<path:path>")
    def frontend(path: str = "") -> Response:
        static_root = ROOT / "frontend/dist"
        requested = static_root / path
        if path and requested.is_file():
            return send_from_directory(static_root, path)
        if (static_root / "index.html").is_file():
            return send_from_directory(static_root, "index.html")
        return jsonify(error="前端尚未构建，请运行 npm run build"), 503

    return app


def require_editor() -> tuple[Response, int] | None:
    if not session.get("editor"):
        return jsonify(error="需要编辑权限"), 401
    return None


def sync_jobs_path() -> Path:
    return Path(os.getenv("SYNC_JOBS_PATH", DATABASE_PATH.parent / "sync-jobs.sqlite"))


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone() is not None


def data_is_ready(connection: sqlite3.Connection) -> bool:
    return table_exists(connection, "sheets") and table_exists(connection, "rows")


class DataImportError(ValueError):
    pass


def install_data_archive(source: Any) -> dict[str, int]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="olmath-import-", dir=DATABASE_PATH.parent) as temporary_directory:
        temporary_root = Path(temporary_directory)
        archive_path = temporary_root / "upload.tar.gz"
        with archive_path.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        if archive_path.stat().st_size > MAX_DATA_IMPORT_BYTES:
            raise DataImportError("压缩包超过 256 MB")

        extract_root = temporary_root / "extracted"
        extract_root.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            validate_archive_members(members)
            archive.extractall(extract_root, members=members, filter="data")

        imported_database = extract_root / "data/app.sqlite"
        imported_assets = extract_root / "data/assets"
        if not imported_database.is_file() or not imported_assets.is_dir():
            raise DataImportError("压缩包必须包含 data/app.sqlite 和 data/assets")
        result = validate_imported_data(imported_database, imported_assets)
        if DATABASE_PATH.is_file():
            result.update(merge_existing_overrides(imported_database, imported_assets))
        replace_imported_data(imported_database, imported_assets)
        return result


def validate_archive_members(members: list[tarfile.TarInfo]) -> None:
    if len(members) > MAX_DATA_ARCHIVE_MEMBERS:
        raise DataImportError("压缩包文件数量过多")
    extracted_bytes = 0
    for member in members:
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise DataImportError("压缩包包含不安全路径")
        if not member.isreg() and not member.isdir():
            raise DataImportError("压缩包包含不支持的链接或设备文件")
        if member_path.parts[:1] != ("data",):
            raise DataImportError("压缩包只能包含 data 目录")
        extracted_bytes += member.size
        if extracted_bytes > MAX_DATA_EXTRACTED_BYTES:
            raise DataImportError("解压后数据超过 512 MB")


def validate_imported_data(database_path: Path, asset_dir: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise DataImportError("SQLite 数据库完整性校验失败")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not REQUIRED_DATA_TABLES.issubset(tables):
            raise DataImportError("SQLite 数据库缺少题库表")
        sheet = connection.execute("SELECT sheet_id FROM sheets WHERE name = ?", (TARGET_SHEET,)).fetchone()
        if sheet is None:
            raise DataImportError(f"SQLite 数据库中没有工作表：{TARGET_SHEET}")
        row_count = connection.execute(
            "SELECT COUNT(*) FROM rows WHERE sheet_id = ? AND row_number > 1", (sheet["sheet_id"],)
        ).fetchone()[0]
        if row_count == 0:
            raise DataImportError("目标工作表没有题库记录")
        assets = list(connection.execute("SELECT object_key FROM assets"))
        for asset in assets:
            object_key = Path(asset["object_key"])
            if object_key.is_absolute() or ".." in object_key.parts or not (asset_dir / object_key).is_file():
                raise DataImportError(f"缺少图片文件：{asset['object_key']}")
        return {"records": row_count, "assets": len(assets)}
    finally:
        connection.close()


def merge_existing_overrides(imported_database: Path, imported_assets: Path) -> dict[str, int]:
    source = sqlite3.connect(DATABASE_PATH)
    source.row_factory = sqlite3.Row
    destination = sqlite3.connect(imported_database)
    destination.row_factory = sqlite3.Row
    preserved_assets: set[str] = set()
    try:
        if not all(table_exists(source, table) for table in ("cell_overrides", "row_image_overrides")):
            return {"fieldOverrides": 0, "imageOverrides": 0, "preservedAssets": 0}
        initialize_override_tables(destination)
        field_overrides = list(source.execute("SELECT * FROM cell_overrides"))
        image_overrides = list(source.execute("SELECT * FROM row_image_overrides"))
        destination.executemany(
            "INSERT OR REPLACE INTO cell_overrides(sheet_id, row_number, column_name, value, updated_at) VALUES (?, ?, ?, ?, ?)",
            [tuple(row) for row in field_overrides],
        )
        destination.executemany(
            "INSERT OR REPLACE INTO row_image_overrides(sheet_id, row_number, asset_hashes_json, updated_at) VALUES (?, ?, ?, ?)",
            [tuple(row) for row in image_overrides],
        )
        referenced_hashes = {
            asset_hash
            for row in image_overrides
            for asset_hash in json.loads(row["asset_hashes_json"])
        }
        for asset_hash in referenced_hashes:
            if destination.execute("SELECT 1 FROM assets WHERE sha256 = ?", (asset_hash,)).fetchone():
                continue
            asset = source.execute("SELECT * FROM assets WHERE sha256 = ?", (asset_hash,)).fetchone()
            if asset is None:
                raise DataImportError(f"现有图片覆盖引用了无效资源：{asset_hash}")
            source_path = ASSET_DIR / asset["object_key"]
            if not source_path.is_file():
                raise DataImportError(f"现有图片覆盖缺少文件：{asset['object_key']}")
            destination_path = imported_assets / asset["object_key"]
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            destination.execute(
                "INSERT INTO assets(sha256, object_key, content_type, byte_size, created_at) VALUES (?, ?, ?, ?, ?)",
                tuple(asset),
            )
            preserved_assets.add(asset_hash)
        destination.commit()
        return {
            "fieldOverrides": len(field_overrides),
            "imageOverrides": len(image_overrides),
            "preservedAssets": len(preserved_assets),
        }
    finally:
        source.close()
        destination.close()


def initialize_override_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS cell_overrides (
            sheet_id TEXT NOT NULL, row_number INTEGER NOT NULL, column_name TEXT NOT NULL,
            value TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (sheet_id, row_number, column_name)
        );
        CREATE TABLE IF NOT EXISTS row_image_overrides (
            sheet_id TEXT NOT NULL, row_number INTEGER NOT NULL, asset_hashes_json TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (sheet_id, row_number)
        );
        """
    )


def replace_imported_data(imported_database: Path, imported_assets: Path) -> None:
    backup_database = DATABASE_PATH.with_suffix(".sqlite.import-backup")
    backup_assets = ASSET_DIR.with_name(f"{ASSET_DIR.name}.import-backup")
    for backup in (backup_database, backup_assets):
        if backup.is_dir():
            shutil.rmtree(backup)
        elif backup.exists():
            backup.unlink()
    try:
        if ASSET_DIR.exists():
            ASSET_DIR.replace(backup_assets)
        imported_assets.replace(ASSET_DIR)
        if DATABASE_PATH.exists():
            DATABASE_PATH.replace(backup_database)
        imported_database.replace(DATABASE_PATH)
    except OSError:
        if DATABASE_PATH.exists():
            DATABASE_PATH.unlink()
        if backup_database.exists():
            backup_database.replace(DATABASE_PATH)
        if ASSET_DIR.exists():
            shutil.rmtree(ASSET_DIR)
        if backup_assets.exists():
            backup_assets.replace(ASSET_DIR)
        raise
    finally:
        if backup_database.exists():
            backup_database.unlink()
        if backup_assets.exists():
            shutil.rmtree(backup_assets)


def get_target_sheet_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT sheet_id FROM sheets WHERE name = ?", (ACTIVE_TARGET_SHEET,)).fetchone()
    if row is None and ACTIVE_TARGET_SHEET != TARGET_SHEET:
        row = connection.execute("SELECT sheet_id FROM sheets WHERE name = ?", (TARGET_SHEET,)).fetchone()
    return row["sheet_id"] if row else None


def row_exists(connection: sqlite3.Connection, sheet_id: str, row_number: int) -> bool:
    return connection.execute(
        "SELECT 1 FROM rows WHERE sheet_id = ? AND row_number = ?", (sheet_id, row_number)
    ).fetchone() is not None


def load_headers(connection: sqlite3.Connection, sheet_id: str) -> dict[str, str]:
    headers = {column: column for column in ALL_COLUMNS}
    for row in connection.execute("SELECT cell_ref, value FROM cells WHERE sheet_id = ? AND row_number = 1", (sheet_id,)):
        column = "".join(character for character in row["cell_ref"] if character.isalpha())
        if column in headers:
            headers[column] = row["value"]
    return headers


def load_records(connection: sqlite3.Connection, sheet_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT row_number FROM rows WHERE sheet_id = ? AND row_number > 1 ORDER BY row_number", (sheet_id,)
    ):
        records[row["row_number"]] = {"rowNumber": row["row_number"], "values": {}}
    for row in connection.execute(
        "SELECT row_number, cell_ref, value FROM cells WHERE sheet_id = ? AND row_number > 1", (sheet_id,)
    ):
        column = "".join(character for character in row["cell_ref"] if character.isalpha())
        if column in ALL_COLUMNS:
            records[row["row_number"]]["values"][column] = row["value"]
    for row in connection.execute(
        "SELECT row_number, column_name, value FROM cell_overrides WHERE sheet_id = ?", (sheet_id,)
    ):
        if row["row_number"] in records:
            records[row["row_number"]]["values"][row["column_name"]] = row["value"]
    image_overrides = {
        row["row_number"]: json.loads(row["asset_hashes_json"])
        for row in connection.execute("SELECT row_number, asset_hashes_json FROM row_image_overrides WHERE sheet_id = ?", (sheet_id,))
    }
    source_images: dict[int, list[str]] = {}
    for row in connection.execute(
        "SELECT row_number, asset_sha256 FROM cell_images WHERE sheet_id = ? AND column_number = 11 ORDER BY row_number, ordinal",
        (sheet_id,),
    ):
        source_images.setdefault(row["row_number"], []).append(row["asset_sha256"])
    result = []
    for row_number, record in records.items():
        values = {column: record["values"].get(column, "") for column in ALL_COLUMNS}
        hashes = image_overrides.get(row_number, source_images.get(row_number, []))
        result.append(
            {
                "rowNumber": row_number,
                "values": values,
                "images": serialize_images(hashes),
                "title": values.get("D") or values.get("I") or f"第 {row_number} 行",
                "searchText": " ".join(values.get(column, "") for column in FILTER_COLUMNS),
            }
        )
    return result


def effective_image_hashes(connection: sqlite3.Connection, sheet_id: str, row_number: int) -> list[str]:
    override = connection.execute(
        "SELECT asset_hashes_json FROM row_image_overrides WHERE sheet_id = ? AND row_number = ?",
        (sheet_id, row_number),
    ).fetchone()
    if override:
        return list(json.loads(override["asset_hashes_json"]))
    return [
        row["asset_sha256"]
        for row in connection.execute(
            "SELECT asset_sha256 FROM cell_images WHERE sheet_id = ? AND row_number = ? AND column_number = 11 ORDER BY ordinal",
            (sheet_id, row_number),
        )
    ]


def save_image_override(connection: sqlite3.Connection, sheet_id: str, row_number: int, hashes: list[str]) -> None:
    connection.execute(
        "INSERT INTO row_image_overrides(sheet_id, row_number, asset_hashes_json, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(sheet_id, row_number) DO UPDATE SET asset_hashes_json=excluded.asset_hashes_json, updated_at=excluded.updated_at",
        (sheet_id, row_number, json.dumps(hashes), utc_now()),
    )


def serialize_images(hashes: list[str]) -> list[dict[str, str]]:
    return [{"hash": asset_hash, "url": f"/api/assets/{asset_hash}"} for asset_hash in hashes]


def save_asset(
    connection: sqlite3.Connection, asset_hash: str, object_key: str, content_type: str, payload: bytes
) -> None:
    if connection.execute("SELECT 1 FROM assets WHERE sha256 = ?", (asset_hash,)).fetchone():
        return
    if os.getenv("ASSET_BACKEND", "local") == "minio":
        from minio import Minio

        endpoint = os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9010")
        bucket = os.getenv("MINIO_BUCKET_RAW", "vf-dev-raw")
        client = Minio(
            endpoint.removeprefix("http://").removeprefix("https://"),
            access_key=os.environ["MINIO_ACCESS_KEY"],
            secret_key=os.environ["MINIO_SECRET_KEY"],
            secure=endpoint.startswith("https://"),
        )
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.put_object(bucket, object_key, io.BytesIO(payload), len(payload), content_type=content_type)
    else:
        destination = ASSET_DIR / object_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    connection.execute(
        "INSERT INTO assets(sha256, object_key, content_type, byte_size, created_at) VALUES (?, ?, ?, ?, ?)",
        (asset_hash, object_key, content_type, len(payload), utc_now()),
    )


def stream_minio_asset(object_key: str, content_type: str) -> Response:
    from minio import Minio

    endpoint = os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9010")
    client = Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=endpoint.startswith("https://"),
    )
    source = client.get_object(os.getenv("MINIO_BUCKET_RAW", "vf-dev-raw"), object_key)

    def generate() -> Any:
        try:
            yield from source.stream(64 * 1024)
        finally:
            source.close()
            source.release_conn()

    return Response(generate(), mimetype=content_type, headers={"Cache-Control": "public, max-age=31536000, immutable"})


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8910")), debug=os.getenv("FLASK_DEBUG") == "1")