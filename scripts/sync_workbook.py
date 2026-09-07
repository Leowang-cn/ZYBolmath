#!/usr/bin/env python3
"""Incrementally import a DingTalk spreadsheet export into SQLite and object storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import posixpath
import sqlite3
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable
from xml.etree import ElementTree as ET


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "docRel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgRel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
FORWARD_FILL_COLUMNS = tuple("ABCDEF")
SYNC_RULES_VERSION = 2


@dataclass(frozen=True)
class ImageRef:
    row_number: int
    column_number: int
    archive_path: str


@dataclass(frozen=True)
class ParsedSheet:
    sheet_id: str
    name: str
    position: int
    rows: dict[int, dict[str, str]]
    images: list[ImageRef]


class AssetStore:
    def put(self, object_key: str, source: BinaryIO, size: int, content_type: str) -> bool:
        raise NotImplementedError


class LocalAssetStore(AssetStore):
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, object_key: str, source: BinaryIO, size: int, content_type: str) -> bool:
        del size, content_type
        destination = self.root / object_key
        if destination.exists():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            while chunk := source.read(1024 * 1024):
                temporary.write(chunk)
        temporary_path.replace(destination)
        return True


class MinioAssetStore(AssetStore):
    def __init__(self) -> None:
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError("MinIO backend requires: pip install -r requirements.txt") from exc

        endpoint = os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9010")
        self.bucket = os.getenv("MINIO_BUCKET_RAW", "vf-dev-raw")
        self.client = Minio(
            endpoint.removeprefix("http://").removeprefix("https://"),
            access_key=os.environ["MINIO_ACCESS_KEY"],
            secret_key=os.environ["MINIO_SECRET_KEY"],
            secure=endpoint.startswith("https://"),
        )

    def put(self, object_key: str, source: BinaryIO, size: int, content_type: str) -> bool:
        try:
            self.client.stat_object(self.bucket, object_key)
            return False
        except Exception as exc:
            if getattr(exc, "code", None) not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
        self.client.put_object(self.bucket, object_key, source, size, content_type=content_type)
        return True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_stream(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def column_name(column_number: int) -> str:
    result = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def resolve_archive_path(base_path: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target))


def read_relationships(workbook: zipfile.ZipFile, path: str) -> dict[str, str]:
    try:
        root = ET.fromstring(workbook.read(path))
    except KeyError:
        return {}
    return {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in root.findall("pkgRel:Relationship", NS)
    }


def read_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.text or "" for node in item.findall(".//main:t", NS)) for item in root]


def cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", NS))
    value = cell.findtext("main:v", default="", namespaces=NS)
    if cell_type == "s" and value:
        return shared_strings[int(value)]
    if cell_type == "b":
        return "true" if value == "1" else "false"
    return value


def parse_drawing(workbook: zipfile.ZipFile, sheet_path: str) -> list[ImageRef]:
    rels_path = posixpath.join(posixpath.dirname(sheet_path), "_rels", posixpath.basename(sheet_path) + ".rels")
    sheet_relationships = read_relationships(workbook, rels_path)
    sheet_root = ET.fromstring(workbook.read(sheet_path))
    drawing = sheet_root.find("main:drawing", NS)
    if drawing is None:
        return []
    drawing_target = sheet_relationships.get(drawing.attrib[f"{{{NS['docRel']}}}id"])
    if not drawing_target:
        return []
    drawing_path = resolve_archive_path(sheet_path, drawing_target)
    drawing_relationships = read_relationships(
        workbook,
        posixpath.join(posixpath.dirname(drawing_path), "_rels", posixpath.basename(drawing_path) + ".rels"),
    )
    drawing_root = ET.fromstring(workbook.read(drawing_path))
    images: list[ImageRef] = []
    anchors: Iterable[ET.Element] = list(drawing_root.findall("xdr:oneCellAnchor", NS)) + list(
        drawing_root.findall("xdr:twoCellAnchor", NS)
    )
    for anchor in anchors:
        start = anchor.find("xdr:from", NS)
        blip = anchor.find(".//a:blip", NS)
        if start is None or blip is None:
            continue
        relationship_id = blip.attrib.get(f"{{{NS['docRel']}}}embed")
        media_target = drawing_relationships.get(relationship_id or "")
        if not media_target:
            continue
        images.append(
            ImageRef(
                row_number=int(start.findtext("xdr:row", default="0", namespaces=NS)) + 1,
                column_number=int(start.findtext("xdr:col", default="0", namespaces=NS)) + 1,
                archive_path=resolve_archive_path(drawing_path, media_target),
            )
        )
    return images


def forward_fill_rows(
    rows: dict[int, dict[str, str]], row_numbers: Iterable[int]
) -> dict[int, dict[str, str]]:
    filled_rows = {row_number: dict(values) for row_number, values in rows.items()}
    previous_values: dict[str, str] = {}
    for row_number in sorted(number for number in row_numbers if number > 1):
        values = filled_rows.setdefault(row_number, {})
        for column in FORWARD_FILL_COLUMNS:
            reference = f"{column}{row_number}"
            if value := values.get(reference):
                previous_values[column] = value
            elif column in previous_values:
                values[reference] = previous_values[column]
    return filled_rows


def parse_workbook(workbook: zipfile.ZipFile) -> list[ParsedSheet]:
    shared_strings = read_shared_strings(workbook)
    root = ET.fromstring(workbook.read("xl/workbook.xml"))
    relationships = read_relationships(workbook, "xl/_rels/workbook.xml.rels")
    sheets: list[ParsedSheet] = []
    for position, sheet in enumerate(root.findall("main:sheets/main:sheet", NS), start=1):
        relationship_id = sheet.attrib[f"{{{NS['docRel']}}}id"]
        sheet_path = resolve_archive_path("xl/workbook.xml", relationships[relationship_id])
        sheet_root = ET.fromstring(workbook.read(sheet_path))
        rows: dict[int, dict[str, str]] = {}
        for row in sheet_root.findall("main:sheetData/main:row", NS):
            values = {
                cell.attrib["r"]: cell_value(cell, shared_strings)
                for cell in row.findall("main:c", NS)
                if cell_value(cell, shared_strings) != ""
            }
            if values:
                rows[int(row.attrib["r"])] = values
        images = parse_drawing(workbook, sheet_path)
        business_rows = set(rows) | {image.row_number for image in images}
        rows = forward_fill_rows(rows, business_rows)
        sheets.append(
            ParsedSheet(
                sheet_id=sheet.attrib.get("sheetId", str(position)),
                name=sheet.attrib["name"],
                position=position,
                rows=rows,
                images=images,
            )
        )
    return sheets


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            stats_json TEXT
        );
        CREATE TABLE IF NOT EXISTS sheets (
            sheet_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rows (
            sheet_id TEXT NOT NULL REFERENCES sheets(sheet_id) ON DELETE CASCADE,
            row_number INTEGER NOT NULL,
            row_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (sheet_id, row_number)
        );
        CREATE TABLE IF NOT EXISTS cells (
            sheet_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            cell_ref TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (sheet_id, cell_ref),
            FOREIGN KEY (sheet_id, row_number) REFERENCES rows(sheet_id, row_number) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS assets (
            sha256 TEXT PRIMARY KEY,
            object_key TEXT NOT NULL UNIQUE,
            content_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cell_images (
            sheet_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            column_number INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            asset_sha256 TEXT NOT NULL REFERENCES assets(sha256),
            PRIMARY KEY (sheet_id, row_number, column_number, ordinal),
            FOREIGN KEY (sheet_id, row_number) REFERENCES rows(sheet_id, row_number) ON DELETE CASCADE
        );
        """
    )


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def sync_workbook(source_path: Path, database_path: Path, asset_store: AssetStore) -> dict[str, int | bool | str]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source:
        source_hash = sha256_stream(source)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    previous = connection.execute(
        "SELECT stats_json FROM sync_runs WHERE source_sha256 = ? AND status = 'completed' ORDER BY id DESC LIMIT 1",
        (source_hash,),
    ).fetchone()
    previous_stats = json.loads(previous["stats_json"] or "{}") if previous else {}
    if previous and previous_stats.get("sync_rules_version") == SYNC_RULES_VERSION:
        connection.close()
        return {"skipped": True, "reason": "source_unchanged", "source_sha256": source_hash, "rows_changed": 0, "assets_uploaded": 0}

    now = utc_now()
    cursor = connection.execute(
        "INSERT INTO sync_runs(source_path, source_sha256, started_at, status) VALUES (?, ?, ?, 'running')",
        (str(source_path), source_hash, now),
    )
    run_id = cursor.lastrowid
    stats: dict[str, int | bool | str] = {
        "skipped": False,
        "sync_rules_version": SYNC_RULES_VERSION,
        "source_sha256": source_hash,
        "sheets_changed": 0,
        "rows_changed": 0,
        "rows_deleted": 0,
        "assets_uploaded": 0,
    }
    try:
        with zipfile.ZipFile(source_path) as workbook:
            sheets = parse_workbook(workbook)
            active_sheet_ids = {sheet.sheet_id for sheet in sheets}
            for sheet in sheets:
                image_metadata: dict[int, list[tuple[int, int, str]]] = {}
                ordinals: dict[tuple[int, int], int] = {}
                for image in sheet.images:
                    with workbook.open(image.archive_path) as media:
                        asset_hash = sha256_stream(media)
                    extension = PurePosixPath(image.archive_path).suffix.lower() or ".bin"
                    object_key = f"dingtalk/assets/{asset_hash[:2]}/{asset_hash}{extension}"
                    content_type = mimetypes.guess_type(image.archive_path)[0] or "application/octet-stream"
                    info = workbook.getinfo(image.archive_path)
                    if connection.execute("SELECT 1 FROM assets WHERE sha256 = ?", (asset_hash,)).fetchone() is None:
                        with workbook.open(image.archive_path) as media:
                            uploaded = asset_store.put(object_key, media, info.file_size, content_type)
                        stats["assets_uploaded"] = int(stats["assets_uploaded"]) + int(uploaded)
                        connection.execute(
                            "INSERT INTO assets(sha256, object_key, content_type, byte_size, created_at) VALUES (?, ?, ?, ?, ?)",
                            (asset_hash, object_key, content_type, info.file_size, now),
                        )
                    key = (image.row_number, image.column_number)
                    ordinals[key] = ordinals.get(key, 0) + 1
                    image_metadata.setdefault(image.row_number, []).append((image.column_number, ordinals[key], asset_hash))

                sheet_hash = stable_hash({"rows": sheet.rows, "images": image_metadata})
                old_sheet = connection.execute("SELECT content_hash FROM sheets WHERE sheet_id = ?", (sheet.sheet_id,)).fetchone()
                if old_sheet is None or old_sheet["content_hash"] != sheet_hash:
                    stats["sheets_changed"] = int(stats["sheets_changed"]) + 1
                connection.execute(
                    "INSERT INTO sheets(sheet_id, name, position, content_hash, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(sheet_id) DO UPDATE SET name=excluded.name, position=excluded.position, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                    (sheet.sheet_id, sheet.name, sheet.position, sheet_hash, now),
                )

                current_rows = set(sheet.rows) | set(image_metadata)
                existing_rows = {
                    row["row_number"]: row["row_hash"]
                    for row in connection.execute("SELECT row_number, row_hash FROM rows WHERE sheet_id = ?", (sheet.sheet_id,))
                }
                for row_number in sorted(current_rows):
                    values = sheet.rows.get(row_number, {})
                    images = sorted(image_metadata.get(row_number, []))
                    row_hash = stable_hash({"cells": values, "images": images})
                    if existing_rows.get(row_number) == row_hash:
                        continue
                    stats["rows_changed"] = int(stats["rows_changed"]) + 1
                    connection.execute(
                        "INSERT INTO rows(sheet_id, row_number, row_hash, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(sheet_id, row_number) DO UPDATE SET row_hash=excluded.row_hash, updated_at=excluded.updated_at",
                        (sheet.sheet_id, row_number, row_hash, now),
                    )
                    connection.execute("DELETE FROM cells WHERE sheet_id = ? AND row_number = ?", (sheet.sheet_id, row_number))
                    connection.executemany(
                        "INSERT INTO cells(sheet_id, row_number, cell_ref, value) VALUES (?, ?, ?, ?)",
                        [(sheet.sheet_id, row_number, reference, value) for reference, value in values.items()],
                    )
                    connection.execute("DELETE FROM cell_images WHERE sheet_id = ? AND row_number = ?", (sheet.sheet_id, row_number))
                    connection.executemany(
                        "INSERT INTO cell_images(sheet_id, row_number, column_number, ordinal, asset_sha256) VALUES (?, ?, ?, ?, ?)",
                        [(sheet.sheet_id, row_number, column, ordinal, asset_hash) for column, ordinal, asset_hash in images],
                    )

                deleted_rows = set(existing_rows) - current_rows
                stats["rows_deleted"] = int(stats["rows_deleted"]) + len(deleted_rows)
                connection.executemany(
                    "DELETE FROM rows WHERE sheet_id = ? AND row_number = ?",
                    [(sheet.sheet_id, row_number) for row_number in deleted_rows],
                )

            existing_sheet_ids = {row["sheet_id"] for row in connection.execute("SELECT sheet_id FROM sheets")}
            connection.executemany("DELETE FROM sheets WHERE sheet_id = ?", [(sheet_id,) for sheet_id in existing_sheet_ids - active_sheet_ids])

        connection.execute(
            "UPDATE sync_runs SET finished_at = ?, status = 'completed', stats_json = ? WHERE id = ?",
            (utc_now(), json.dumps(stats, ensure_ascii=False, sort_keys=True), run_id),
        )
        connection.commit()
        return stats
    except Exception:
        connection.rollback()
        connection.execute(
            "UPDATE sync_runs SET finished_at = ?, status = 'failed' WHERE id = ?", (utc_now(), run_id)
        )
        connection.commit()
        raise
    finally:
        connection.close()


def create_data_archive(database_path: Path, asset_dir: Path, output_path: Path) -> dict[str, int | str]:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")
    if not asset_dir.is_dir():
        raise FileNotFoundError(f"Asset directory not found: {asset_dir}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".tar.gz", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with tarfile.open(temporary_path, "w:gz") as archive:
            archive.add(database_path, arcname="data/app.sqlite")
            archive.add(asset_dir, arcname="data/assets")
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    with output_path.open("rb") as source:
        archive_hash = sha256_stream(source)
    return {
        "package": str(output_path),
        "package_bytes": output_path.stat().st_size,
        "package_sha256": archive_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", type=Path, help="DingTalk .xlsx export to import")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("DATABASE_PATH", "data/app.sqlite")),
        help="SQLite database path (default: data/app.sqlite)",
    )
    parser.add_argument(
        "--asset-backend",
        choices=("local", "minio"),
        default=os.getenv("ASSET_BACKEND", "local"),
        help="Image storage backend (default: local)",
    )
    parser.add_argument(
        "--asset-dir",
        type=Path,
        default=Path(os.getenv("ASSET_DIR", "data/assets")),
        help="Local asset root when --asset-backend=local",
    )
    parser.add_argument(
        "--package",
        type=Path,
        help="Create a browser-importable .tar.gz after syncing",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.xlsx.is_file():
        print(json.dumps({"error": f"XLSX not found: {args.xlsx}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    store: AssetStore = LocalAssetStore(args.asset_dir) if args.asset_backend == "local" else MinioAssetStore()
    stats = sync_workbook(args.xlsx, args.database, store)
    if args.package:
        if args.asset_backend != "local":
            print(json.dumps({"error": "--package requires --asset-backend local"}), file=sys.stderr)
            return 2
        stats.update(create_data_archive(args.database, args.asset_dir, args.package))
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())