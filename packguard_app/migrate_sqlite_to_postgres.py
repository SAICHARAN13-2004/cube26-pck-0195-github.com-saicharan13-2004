"""Copy an existing SQLite deployment and its media into PostgreSQL and private S3 storage."""

import argparse
from io import BytesIO
import mimetypes
import os
from pathlib import Path
import sqlite3
from typing import Any

from database import APP_DIR, DATABASE_BACKEND, connect, init_db
from storage import create_media_storage


_TABLES = ("products", "users", "pack_records", "audit_events", "support_requests")
_MEDIA_COLUMNS = (
	("pack_records", "org_id", "photo_ref"),
	("pack_records", "org_id", "video_ref"),
	("products", "org_id", "reference_image_ref"),
)


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
	return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def _target_columns(connection: Any, table: str) -> set[str]:
	if getattr(connection, "_backend", None) == "sqlite":
		return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
	rows = connection.execute(
		"SELECT column_name AS name FROM information_schema.columns "
		"WHERE table_schema = current_schema() AND table_name = ?",
		(table,),
	).fetchall()
	return {row["name"] for row in rows}


def _assert_target_empty(target: Any) -> None:
	for table in _TABLES:
		if target.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]:
			raise RuntimeError(f"Target table {table} is not empty; refusing a potentially duplicating import.")


def copy_rows(source: sqlite3.Connection, target: Any) -> dict[str, int]:
	"""Copy all application tables into an initialized, empty target database."""
	_assert_target_empty(target)
	counts: dict[str, int] = {}
	for table in _TABLES:
		source_columns = _sqlite_columns(source, table)
		target_columns = _target_columns(target, table)
		columns = [column for column in source_columns if column in target_columns]
		backfill_organization_id = "organization_id" in target_columns and "organization_id" not in source_columns and "org_id" in source_columns
		if backfill_organization_id:
			columns.append("organization_id")
		if not columns:
			counts[table] = 0
			continue
		column_sql = ", ".join(columns)
		placeholders = ", ".join("?" for _ in columns)
		rows = source.execute(f"SELECT {column_sql} FROM {table}")
		count = 0
		for row in rows:
			organization_key = row["org_id"] if "org_id" in source_columns else "__system__"
			target.set_rls_organization(organization_key)
			values = [
				(row[column] or organization_key) if column == "organization_id" else row[column]
				for column in columns
			]
			target.execute(
				f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
				tuple(values),
			)
			count += 1
		counts[table] = count
	return counts


def _copy_media(source: sqlite3.Connection, storage: Any, upload_root: Path) -> int:
	copied: set[str] = set()
	for table, org_column, media_column in _MEDIA_COLUMNS:
		columns = set(_sqlite_columns(source, table))
		if media_column not in columns:
			continue
		for row in source.execute(f"SELECT {org_column}, {media_column} FROM {table} WHERE {media_column} IS NOT NULL"):
			org_id, key = row[org_column], row[media_column]
			if key.startswith("fixtures/") or not key.startswith(f"{org_id}/") or key in copied:
				continue
			parts = Path(*key.split("/"))
			path = (upload_root / parts).resolve()
			if upload_root.resolve() not in path.parents or not path.is_file():
				raise RuntimeError(f"Referenced media is missing or unsafe: {key}")
			storage.save(key, BytesIO(path.read_bytes()), mimetypes.guess_type(path.name)[0])
			copied.add(key)
	return len(copied)


def migrate(source_path: str | Path) -> tuple[dict[str, int], int]:
	if DATABASE_BACKEND != "postgresql":
		raise RuntimeError("Set PACKGUARD_DATABASE_URL to a PostgreSQL DSN before running this migration.")
	if not Path(source_path).is_file():
		raise FileNotFoundError(f"SQLite source database does not exist: {source_path}")
	storage = create_media_storage(os.environ.get("PACKGUARD_UPLOAD_DIR", APP_DIR / "uploads"))
	if storage.backend != "s3":
		raise RuntimeError("Set PACKGUARD_OBJECT_STORAGE_URL to a private s3:// bucket before migrating media.")
	storage.health_check()
	init_db()
	source = sqlite3.connect(source_path)
	source.row_factory = sqlite3.Row
	try:
		with connect() as target:
			_assert_target_empty(target)
			media_count = _copy_media(
				source,
				storage,
				Path(os.environ.get("PACKGUARD_UPLOAD_DIR", APP_DIR / "uploads")),
			)
			counts = copy_rows(source, target)
			target.execute("UPDATE pack_records SET session_id = record_id WHERE session_id IS NULL")
		return counts, media_count
	finally:
		source.close()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("sqlite_path", help="Path to the source packguard.db file")
	arguments = parser.parse_args()
	rows, media = migrate(arguments.sqlite_path)
	print(f"Copied rows: {rows}")
	print(f"Copied private media objects: {media}")
