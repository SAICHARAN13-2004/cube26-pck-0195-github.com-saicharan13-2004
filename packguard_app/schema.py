"""Portable, versioned schema migrations for SQLite and PostgreSQL."""

from datetime import datetime, timezone
from typing import Any


_INITIAL_SCHEMA = (
	"""CREATE TABLE IF NOT EXISTS pack_records (
		record_id TEXT PRIMARY KEY,
		org_id TEXT NOT NULL,
		unit_id TEXT NOT NULL,
		order_id TEXT NOT NULL,
		channel TEXT NOT NULL,
		order_lines TEXT NOT NULL,
		observed_in_box TEXT,
		operator_verdict TEXT,
		operator_id TEXT NOT NULL,
		photo_ref TEXT,
		captured_at TEXT NOT NULL,
		verdict TEXT NOT NULL,
		action TEXT NOT NULL,
		reason TEXT NOT NULL,
		evidence_json TEXT NOT NULL
	)""",
	"""CREATE TABLE IF NOT EXISTS products (
		product_id TEXT PRIMARY KEY,
		org_id TEXT NOT NULL,
		sku TEXT NOT NULL,
		product_name TEXT NOT NULL,
		brand TEXT,
		external_id TEXT,
		barcode TEXT,
		attributes_json TEXT NOT NULL DEFAULT '{}',
		reference_image_ref TEXT,
		created_at TEXT NOT NULL,
		UNIQUE (org_id, sku)
	)""",
	"""CREATE TABLE IF NOT EXISTS users (
		username TEXT PRIMARY KEY,
		password_hash TEXT NOT NULL,
		org_id TEXT NOT NULL,
		role TEXT NOT NULL DEFAULT 'operator',
		active INTEGER NOT NULL DEFAULT 1,
		created_at TEXT NOT NULL
	)""",
	"""CREATE TABLE IF NOT EXISTS audit_events (
		event_id TEXT PRIMARY KEY,
		record_id TEXT NOT NULL,
		org_id TEXT NOT NULL,
		event_type TEXT NOT NULL,
		reason TEXT NOT NULL,
		actor_id TEXT NOT NULL,
		created_at TEXT NOT NULL,
		details_json TEXT NOT NULL
	)""",
	"""CREATE TABLE IF NOT EXISTS support_requests (
		ticket_id TEXT PRIMARY KEY,
		org_id TEXT NOT NULL,
		requester_name TEXT NOT NULL,
		requester_email TEXT NOT NULL,
		subject TEXT NOT NULL,
		message TEXT NOT NULL,
		status TEXT NOT NULL DEFAULT 'OPEN',
		created_at TEXT NOT NULL
	)""",
	"CREATE INDEX IF NOT EXISTS idx_pack_records_org ON pack_records (org_id, captured_at DESC)",
	"CREATE INDEX IF NOT EXISTS idx_products_org ON products (org_id, product_name)",
	"CREATE INDEX IF NOT EXISTS idx_audit_events_record ON audit_events (org_id, record_id, created_at DESC)",
	"CREATE INDEX IF NOT EXISTS idx_support_requests_org ON support_requests (org_id, created_at DESC)",
)

_PACK_RECORD_COLUMNS = (
	("supplier_name", "TEXT"),
	("origin_address", "TEXT"),
	("delivery_address", "TEXT"),
	("video_ref", "TEXT"),
	("workflow_state", "TEXT NOT NULL DEFAULT 'REVIEW'"),
	("session_id", "TEXT"),
	("parent_record_id", "TEXT"),
	("attempt_number", "INTEGER NOT NULL DEFAULT 1"),
)


def _columns(connection: Any, backend: str, table: str) -> set[str]:
	if backend == "sqlite":
		return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
	rows = connection.execute(
		"SELECT column_name AS name FROM information_schema.columns "
		"WHERE table_schema = current_schema() AND table_name = ?",
		(table,),
	).fetchall()
	return {row["name"] for row in rows}


def apply_migrations(connection: Any, backend: str) -> None:
	if backend == "postgresql":
		connection.execute("SELECT pg_advisory_xact_lock(73190420260929)")
	connection.execute(
		"CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
	)
	rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
	applied = {row["version"] for row in rows}
	if 1 not in applied:
		for statement in _INITIAL_SCHEMA:
			connection.execute(statement)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
			(1, datetime.now(timezone.utc).isoformat()),
		)
	if 2 not in applied:
		columns = _columns(connection, backend, "pack_records")
		for name, definition in _PACK_RECORD_COLUMNS:
			if name not in columns:
				connection.execute(f"ALTER TABLE pack_records ADD COLUMN {name} {definition}")
		connection.execute("UPDATE pack_records SET session_id = record_id WHERE session_id IS NULL")
		connection.execute(
			"CREATE INDEX IF NOT EXISTS idx_pack_records_session "
			"ON pack_records (org_id, session_id, attempt_number)"
		)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
			(2, datetime.now(timezone.utc).isoformat()),
		)
	if 3 not in applied:
		connection.execute(
			"""CREATE TABLE IF NOT EXISTS login_attempts (
				bucket_key TEXT PRIMARY KEY,
				window_started_at INTEGER NOT NULL,
				attempts INTEGER NOT NULL,
				blocked_until INTEGER NOT NULL DEFAULT 0
			)"""
		)
		connection.execute(
			"CREATE INDEX IF NOT EXISTS idx_login_attempts_window ON login_attempts (window_started_at)"
		)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
			(3, datetime.now(timezone.utc).isoformat()),
		)
	if 4 not in applied:
		user_columns = _columns(connection, backend, "users")
		if "oidc_issuer" not in user_columns:
			connection.execute("ALTER TABLE users ADD COLUMN oidc_issuer TEXT")
		if "oidc_subject" not in user_columns:
			connection.execute("ALTER TABLE users ADD COLUMN oidc_subject TEXT")
		connection.execute(
			"CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oidc_identity "
			"ON users (oidc_issuer, oidc_subject) WHERE oidc_subject IS NOT NULL"
		)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
			(4, datetime.now(timezone.utc).isoformat()),
		)
	if 5 not in applied:
		connection.execute(
			"""CREATE TABLE IF NOT EXISTS contract_captures (
				capture_id TEXT PRIMARY KEY,
				organization_id TEXT NOT NULL,
				owner_username TEXT NOT NULL,
				client_id TEXT,
				agent TEXT NOT NULL,
				subject_json TEXT NOT NULL,
				operator_label TEXT NOT NULL,
				image_slots_json TEXT NOT NULL,
				input_json TEXT NOT NULL,
				status TEXT NOT NULL,
				created_at TEXT NOT NULL,
				record_id TEXT
			)"""
		)
		connection.execute(
			"""CREATE TABLE IF NOT EXISTS contract_records (
				record_id TEXT PRIMARY KEY,
				organization_id TEXT NOT NULL,
				agent TEXT NOT NULL,
				captured_at TEXT NOT NULL,
				status TEXT NOT NULL,
				record_json TEXT NOT NULL
			)"""
		)
		connection.execute(
			"""CREATE TABLE IF NOT EXISTS contract_overrides (
				override_id TEXT PRIMARY KEY,
				organization_id TEXT NOT NULL,
				record_id TEXT NOT NULL,
				check_key TEXT NOT NULL,
				from_verdict TEXT NOT NULL,
				to_verdict TEXT NOT NULL,
				reason TEXT NOT NULL,
				by TEXT NOT NULL,
				at TEXT NOT NULL
			)"""
		)
		connection.execute("CREATE INDEX IF NOT EXISTS idx_contract_records_org_capture ON contract_records (organization_id, captured_at DESC, record_id)")
		connection.execute("CREATE INDEX IF NOT EXISTS idx_contract_captures_org ON contract_captures (organization_id, created_at DESC)")
		connection.execute("CREATE INDEX IF NOT EXISTS idx_contract_overrides_record ON contract_overrides (organization_id, record_id, at)")
		tenant_tables = (
			"pack_records", "products", "users", "audit_events", "support_requests",
			"login_attempts", "schema_migrations", "contract_captures", "contract_records", "contract_overrides",
		)
		for table in tenant_tables:
			columns = _columns(connection, backend, table)
			if "organization_id" not in columns:
				connection.execute(f"ALTER TABLE {table} ADD COLUMN organization_id TEXT")
			if "org_id" in columns:
				connection.execute(f"UPDATE {table} SET organization_id = org_id WHERE organization_id IS NULL")
			else:
				connection.execute(f"UPDATE {table} SET organization_id = '__system__' WHERE organization_id IS NULL")
		if backend == "postgresql":
			for table in tenant_tables:
				if table in {"contract_captures", "contract_records", "contract_overrides"}:
					setting = "app.contract_organization_id"
				elif table in {"schema_migrations", "login_attempts"}:
					setting = "app.organization_key"
				else:
					setting = "app.organization_key"
				expression = f"organization_id = current_setting('{setting}', true)"
				if table in {"contract_captures", "contract_records", "contract_overrides"}:
					expression = f"({expression} OR current_setting('app.organization_key', true) = '__public__')"
				if table == "users":
					expression = f"({expression} OR current_setting('app.organization_key', true) = '__auth__')"
				connection.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
				connection.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
				connection.execute(f"DROP POLICY IF EXISTS tenant_scope ON {table}")
				connection.execute(
					f"CREATE POLICY tenant_scope ON {table} USING ({expression}) WITH CHECK (organization_id = current_setting('{setting}', true))"
				)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at, organization_id) VALUES (?, ?, '__system__')",
			(5, datetime.now(timezone.utc).isoformat()),
		)