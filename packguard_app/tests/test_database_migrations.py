import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from database import Connection
from migrate_sqlite_to_postgres import copy_rows
from schema import apply_migrations


def _migrate(path):
	connection = Connection(sqlite3.connect(path), "sqlite")
	with connection:
		apply_migrations(connection, "sqlite")


def test_fresh_database_migrations_are_versioned_and_idempotent(tmp_path):
	database_path = tmp_path / "fresh.db"

	_migrate(database_path)
	_migrate(database_path)

	with sqlite3.connect(database_path) as connection:
		versions = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
		columns = {row[1] for row in connection.execute("PRAGMA table_info(pack_records)")}
		login_attempts = connection.execute("PRAGMA table_info(login_attempts)").fetchall()

	assert versions == [(1,), (2,), (3,), (4,), (5,)]
	assert {"video_ref", "session_id", "parent_record_id", "attempt_number"} <= columns
	assert login_attempts
	with sqlite3.connect(database_path) as connection:
		assert {"organization_id"} <= {row[1] for row in connection.execute("PRAGMA table_info(contract_records)")}


def test_migration_upgrades_legacy_records_and_preserves_session_identity(tmp_path):
	database_path = tmp_path / "legacy.db"
	with sqlite3.connect(database_path) as connection:
		connection.execute(
			"""CREATE TABLE pack_records (
				record_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, unit_id TEXT NOT NULL,
				order_id TEXT NOT NULL, channel TEXT NOT NULL, order_lines TEXT NOT NULL,
				observed_in_box TEXT, operator_verdict TEXT, operator_id TEXT NOT NULL,
				photo_ref TEXT, captured_at TEXT NOT NULL, verdict TEXT NOT NULL,
				action TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL
			)"""
		)
		connection.execute(
			"INSERT INTO pack_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
			("PCK-LEGACY", "org-a", "unit-a", "order-a", "mfn", "SKU-A:1", "SKU-A:1", "seal", "op-a", None, "2026-01-01", "PASS", "SEAL", "ok", "{}"),
		)

	_migrate(database_path)

	with sqlite3.connect(database_path) as connection:
		connection.row_factory = sqlite3.Row
		record = connection.execute("SELECT session_id, attempt_number FROM pack_records WHERE record_id = ?", ("PCK-LEGACY",)).fetchone()
		versions = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
		user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}

	assert dict(record) == {"session_id": "PCK-LEGACY", "attempt_number": 1}
	assert versions == 5
	assert {"oidc_issuer", "oidc_subject"} <= user_columns


def test_postgresql_migrations_when_test_service_is_configured():
	dsn = os.environ.get("PACKGUARD_TEST_POSTGRES_URL")
	if not dsn:
		pytest.skip("Set PACKGUARD_TEST_POSTGRES_URL to run PostgreSQL integration migrations.")
	psycopg = pytest.importorskip("psycopg")
	from psycopg import sql
	from psycopg.rows import dict_row

	schema_name = f"packguard_migration_test_{uuid4().hex}"
	admin = psycopg.connect(dsn, autocommit=True)
	source = sqlite3.connect(":memory:")
	source.row_factory = sqlite3.Row
	try:
		apply_migrations(Connection(source, "sqlite"), "sqlite")
		source.execute(
			"INSERT INTO products (product_id, org_id, sku, product_name, created_at) VALUES (?, ?, ?, ?, ?)",
			("PRD-PG-TEST", "org-pg-test", "SKU-PG-TEST", "PG migration test", "2026-01-01"),
		)
		source.commit()
		admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
		admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
		connection = psycopg.connect(dsn, options=f"-csearch_path={schema_name}", row_factory=dict_row)
		adapter = Connection(connection, "postgresql")
		with adapter:
			apply_migrations(adapter, "postgresql")
			apply_migrations(adapter, "postgresql")
			versions = adapter.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
			counts = copy_rows(source, adapter)
			product = adapter.execute("SELECT sku FROM products WHERE product_id = ?", ("PRD-PG-TEST",)).fetchone()
			contract_org = "d415e9d0-a208-5d35-b7bb-b02b0d035902"
			other_org = "ca2b4e57-e8a2-5cf5-9c87-40cbcbf5dc0a"
			record_id = str(uuid4())
			adapter.set_rls_organization("org-pg-test")
			adapter.execute(
				"INSERT INTO contract_records (record_id, organization_id, agent, captured_at, status, record_json) VALUES (?, ?, ?, ?, ?, ?)",
				(record_id, contract_org, "pack", "2026-01-01T00:00:00Z", "pending", "{}"),
			)
			adapter.set_rls_organization("org-pg-other")
			isolated_count = adapter.execute("SELECT COUNT(*) AS count FROM contract_records WHERE record_id = ?", (record_id,)).fetchone()["count"]
			rlspolicy = adapter.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'contract_records'").fetchone()
		assert [row["version"] for row in versions] == [1, 2, 3, 4, 5]
		assert counts["products"] == 1
		assert product["sku"] == "SKU-PG-TEST"
		assert isolated_count == 0
		assert rlspolicy["relrowsecurity"] is True
		assert rlspolicy["relforcerowsecurity"] is True
	finally:
		admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
		admin.close()
		source.close()


def test_production_database_configuration_cannot_fall_back_to_sqlite():
	environment = os.environ.copy()
	environment["PACKGUARD_ENV"] = "production"
	environment.pop("PACKGUARD_DATABASE_URL", None)
	result = subprocess.run(
		[sys.executable, "-c", "import database"],
		capture_output=True,
		text=True,
		env=environment,
		check=False,
	)

	assert result.returncode != 0
	assert "PACKGUARD_DATABASE_URL is required in production" in result.stderr


def test_production_storage_configuration_cannot_fall_back_to_local_disk():
	environment = os.environ.copy()
	environment["PACKGUARD_ENV"] = "production"
	environment["PACKGUARD_DATABASE_URL"] = "postgresql://user:password@localhost/packguard"
	environment["PACKGUARD_DATABASE_MIGRATION_URL"] = "postgresql://user:password@localhost/packguard"
	environment["PACKGUARD_SECRET_KEY"] = "x" * 40
	environment.pop("PACKGUARD_OBJECT_STORAGE_URL", None)
	result = subprocess.run(
		[sys.executable, "-c", "import app"],
		capture_output=True,
		text=True,
		env=environment,
		check=False,
	)

	assert result.returncode != 0
	assert "PACKGUARD_OBJECT_STORAGE_URL must configure private S3-compatible storage" in result.stderr
