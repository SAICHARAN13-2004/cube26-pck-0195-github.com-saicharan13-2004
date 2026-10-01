import sqlite3

import pytest

from database import Connection
from migrate_sqlite_to_postgres import copy_rows
from schema import apply_migrations


def _create_database(path):
	connection = Connection(sqlite3.connect(path), "sqlite")
	with connection:
		apply_migrations(connection, "sqlite")


def test_data_transfer_copies_application_rows(tmp_path):
	source_path = tmp_path / "source.db"
	target_path = tmp_path / "target.db"
	_create_database(source_path)
	_create_database(target_path)
	with sqlite3.connect(source_path) as source:
		source.execute(
			"""INSERT INTO products
			(product_id, org_id, sku, product_name, attributes_json, created_at)
			VALUES (?, ?, ?, ?, ?, ?)""",
			("PRD-1", "org-a", "SKU-A", "Sample", "{}", "2026-01-01T00:00:00+00:00"),
		)
		source.execute(
			"""INSERT INTO pack_records
			(record_id, org_id, unit_id, order_id, channel, order_lines, operator_id,
			 captured_at, verdict, action, reason, evidence_json)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			("PCK-1", "org-a", "UNIT-1", "ORD-1", "mfn", "SKU-A:1", "operator",
			 "2026-01-01T00:00:00+00:00", "PASS", "SEAL", "match", "{}"),
		)

	source = sqlite3.connect(source_path)
	source.row_factory = sqlite3.Row
	target = Connection(sqlite3.connect(target_path), "sqlite")
	with target:
		counts = copy_rows(source, target)
		target.execute("UPDATE pack_records SET session_id = record_id WHERE session_id IS NULL")
	source.close()

	with sqlite3.connect(target_path) as target_check:
		target_check.row_factory = sqlite3.Row
		product = target_check.execute("SELECT sku, organization_id FROM products WHERE product_id = ?", ("PRD-1",)).fetchone()
		record = target_check.execute("SELECT session_id FROM pack_records WHERE record_id = ?", ("PCK-1",)).fetchone()

	assert counts["products"] == 1
	assert counts["pack_records"] == 1
	assert product["sku"] == "SKU-A"
	assert product["organization_id"] == "org-a"
	assert record["session_id"] == "PCK-1"


def test_data_transfer_refuses_nonempty_target(tmp_path):
	source_path = tmp_path / "source.db"
	target_path = tmp_path / "target.db"
	_create_database(source_path)
	_create_database(target_path)
	with sqlite3.connect(target_path) as target:
		target.execute(
			"INSERT INTO products (product_id, org_id, sku, product_name, created_at) VALUES (?, ?, ?, ?, ?)",
			("existing", "org-a", "SKU-A", "Existing", "2026-01-01"),
		)

	source = sqlite3.connect(source_path)
	source.row_factory = sqlite3.Row
	target = Connection(sqlite3.connect(target_path), "sqlite")
	with target, pytest.raises(RuntimeError, match="not empty"):
		copy_rows(source, target)
	source.close()
