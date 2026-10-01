"""SQLite persistence with explicit organization scoping."""

import json
import os
import re
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from schema import apply_migrations


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PACKGUARD_DB_PATH", APP_DIR / "packguard.db"))
DATABASE_URL = os.environ.get("PACKGUARD_DATABASE_URL", "").strip()
DATABASE_MIGRATION_URL = os.environ.get("PACKGUARD_DATABASE_MIGRATION_URL", "").strip()
if DATABASE_URL and not DATABASE_URL.startswith(("postgres://", "postgresql://")):
	raise RuntimeError("PACKGUARD_DATABASE_URL must use PostgreSQL.")
if os.environ.get("PACKGUARD_ENV", "development").strip().lower() == "production" and not DATABASE_URL:
	raise RuntimeError("PACKGUARD_DATABASE_URL is required in production.")
if os.environ.get("PACKGUARD_ENV", "development").strip().lower() == "production" and not DATABASE_MIGRATION_URL:
	raise RuntimeError("PACKGUARD_DATABASE_MIGRATION_URL is required in production.")
if DATABASE_MIGRATION_URL and not DATABASE_MIGRATION_URL.startswith(("postgres://", "postgresql://")):
	raise RuntimeError("PACKGUARD_DATABASE_MIGRATION_URL must use PostgreSQL.")
DATABASE_BACKEND = "postgresql" if DATABASE_URL else "sqlite"
_ORGANIZATION_CONTEXT: ContextVar[str] = ContextVar("packguard_organization_context", default="__system__")


def set_organization_context(organization_key: str):
	return _ORGANIZATION_CONTEXT.set(organization_key)


def reset_organization_context(token) -> None:
	_ORGANIZATION_CONTEXT.reset(token)


class Connection:
	def __init__(self, connection: Any, backend: str):
		self._connection = connection
		self._backend = backend
		if backend == "sqlite":
			self._connection.row_factory = sqlite3.Row

	def __enter__(self):
		if self._backend == "postgresql":
			self._connection.__enter__()
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		try:
			return self._connection.__exit__(exc_type, exc_value, traceback)
		finally:
			if self._backend == "sqlite":
				self._connection.close()

	def execute(self, sql: str, parameters: Any = ()):
		if self._backend == "postgresql":
			sql = re.sub(r"\?", "%s", sql)
		return self._connection.execute(sql, parameters)

	def set_rls_organization(self, organization_key: str) -> None:
		if self._backend != "postgresql":
			return
		from contract import organization_uuid

		self._connection.execute("SELECT set_config('app.organization_key', %s, true)", (organization_key,))
		contract_context = organization_key if organization_key.startswith("__") else organization_uuid(organization_key)
		self._connection.execute("SELECT set_config('app.contract_organization_id', %s, true)", (contract_context,))


def connect(organization_key: str | None = None, *, migration: bool = False) -> Connection:
	if DATABASE_BACKEND == "postgresql":
		import psycopg
		from psycopg.rows import dict_row
		from contract import organization_uuid

		configured_url = DATABASE_MIGRATION_URL if migration and DATABASE_MIGRATION_URL else DATABASE_URL
		url = configured_url.replace("postgres://", "postgresql://", 1)
		context = organization_key or _ORGANIZATION_CONTEXT.get()
		contract_context = context if context.startswith("__") else organization_uuid(context)
		connection = psycopg.connect(url, row_factory=dict_row)
		connection.execute("SELECT set_config('app.organization_key', %s, false)", (context,))
		connection.execute("SELECT set_config('app.contract_organization_id', %s, false)", (contract_context,))
		connection.commit()
		return Connection(connection, "postgresql")
	connection = sqlite3.connect(DB_PATH)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	return Connection(connection, "sqlite")


def init_db() -> None:
	with connect("__system__", migration=True) as connection:
		apply_migrations(connection, DATABASE_BACKEND)


def list_products(org_id: str) -> list[dict[str, Any]]:
	with connect(org_id) as connection:
		rows = connection.execute(
			"SELECT * FROM products WHERE org_id = ? ORDER BY product_name, sku",
			(org_id,),
		).fetchall()
	return [dict(row) for row in rows]


def get_product(org_id: str, sku: str) -> dict[str, Any] | None:
	with connect(org_id) as connection:
		row = connection.execute(
			"SELECT * FROM products WHERE org_id = ? AND sku = ?",
			(org_id, sku),
		).fetchone()
	return dict(row) if row else None


def save_product(product: dict[str, Any]) -> None:
	with connect(product["org_id"]) as connection:
		connection.execute(
			"""
			INSERT INTO products (
				product_id, org_id, organization_id, sku, product_name, brand, external_id,
				barcode, attributes_json, reference_image_ref, created_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT (org_id, sku) DO UPDATE SET
				product_name = excluded.product_name,
				brand = excluded.brand,
				external_id = excluded.external_id,
				barcode = excluded.barcode,
				attributes_json = excluded.attributes_json,
				reference_image_ref = COALESCE(excluded.reference_image_ref, products.reference_image_ref)
			""",
			(
				product["product_id"], product["org_id"], product["org_id"], product["sku"],
				product["product_name"], product.get("brand"), product.get("external_id"),
				product.get("barcode"), json.dumps(product.get("attributes", {})),
				product.get("reference_image_ref"), product["created_at"],
			),
		)


def save_user(user: dict[str, Any]) -> None:
	with connect(user["org_id"]) as connection:
		connection.execute(
			"""
			INSERT INTO users (username, password_hash, org_id, organization_id, role, active, created_at)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT (username) DO UPDATE SET
				password_hash = excluded.password_hash,
				org_id = excluded.org_id,
				role = excluded.role,
				active = excluded.active
			""",
			(
				user["username"], user["password_hash"], user["org_id"], user["org_id"],
				user.get("role", "operator"), int(user.get("active", True)),
				user["created_at"],
			),
		)


def get_user(username: str) -> dict[str, Any] | None:
	with connect("__auth__") as connection:
		row = connection.execute(
			"SELECT * FROM users WHERE username = ? AND active = 1",
			(username,),
		).fetchone()
	return dict(row) if row else None


def get_oidc_user(issuer: str, subject: str, email: str) -> dict[str, Any] | None:
	with connect("__auth__") as connection:
		row = connection.execute(
			"SELECT * FROM users WHERE oidc_issuer = ? AND oidc_subject = ? AND active = 1",
			(issuer, subject),
		).fetchone()
		if not row and email:
			row = connection.execute(
				"SELECT * FROM users WHERE lower(username) = ? AND oidc_subject IS NULL AND active = 1",
				(email.casefold(),),
			).fetchone()
	return dict(row) if row else None


def link_oidc_identity(username: str, issuer: str, subject: str, organization_key: str) -> bool:
	with connect(organization_key) as connection:
		cursor = connection.execute(
			"""UPDATE users SET oidc_issuer = ?, oidc_subject = ?
			WHERE username = ? AND active = 1
			AND (oidc_subject IS NULL OR (oidc_issuer = ? AND oidc_subject = ?))""",
			(issuer, subject, username, issuer, subject),
		)
		return cursor.rowcount == 1


def user_exists(username: str) -> bool:
	with connect("__auth__") as connection:
		return connection.execute(
			"SELECT 1 FROM users WHERE username = ?",
			(username,),
		).fetchone() is not None


def login_blocked_until(bucket_keys: list[str], now: int) -> int:
	if not bucket_keys:
		return 0
	placeholders = ", ".join("?" for _ in bucket_keys)
	with connect("__system__") as connection:
		rows = connection.execute(
			f"SELECT blocked_until FROM login_attempts WHERE bucket_key IN ({placeholders})",
			tuple(bucket_keys),
		).fetchall()
	return max((int(row["blocked_until"]) for row in rows if row["blocked_until"] > now), default=0)


def record_login_failure(bucket_key: str, now: int, limit: int, window_seconds: int, block_seconds: int) -> None:
	with connect("__system__") as connection:
		connection.execute(
			"""INSERT INTO login_attempts (bucket_key, window_started_at, attempts, blocked_until, organization_id)
			VALUES (?, ?, 1, 0, '__system__')
			ON CONFLICT (bucket_key) DO UPDATE SET
				window_started_at = CASE
					WHEN excluded.window_started_at >= login_attempts.window_started_at + ?
					THEN excluded.window_started_at ELSE login_attempts.window_started_at END,
				attempts = CASE
					WHEN excluded.window_started_at >= login_attempts.window_started_at + ?
					THEN 1 ELSE login_attempts.attempts + 1 END,
				blocked_until = CASE
					WHEN excluded.window_started_at >= login_attempts.window_started_at + ? THEN 0
					WHEN login_attempts.attempts + 1 >= ? THEN excluded.window_started_at + ?
					ELSE login_attempts.blocked_until END""",
			(bucket_key, now, window_seconds, window_seconds, window_seconds, limit, block_seconds),
		)
		connection.execute("DELETE FROM login_attempts WHERE window_started_at < ?", (now - window_seconds * 2,))


def clear_login_attempts(bucket_keys: list[str]) -> None:
	if not bucket_keys:
		return
	placeholders = ", ".join("?" for _ in bucket_keys)
	with connect("__system__") as connection:
		connection.execute(
			f"DELETE FROM login_attempts WHERE bucket_key IN ({placeholders})",
			tuple(bucket_keys),
		)


def count_records(org_id: str) -> int:
	with connect(org_id) as connection:
		row = connection.execute(
			"SELECT COUNT(*) AS count FROM pack_records WHERE org_id = ?",
			(org_id,),
		).fetchone()
	return int(row["count"])


def list_records(org_id: str, limit: int = 100) -> list[dict[str, Any]]:
	with connect(org_id) as connection:
		rows = connection.execute(
			"""
			SELECT * FROM pack_records
			WHERE org_id = ?
			ORDER BY captured_at DESC
			LIMIT ?
			""",
			(org_id, limit),
		).fetchall()
	return [dict(row) for row in rows]


def get_record(record_id: str, org_id: str) -> dict[str, Any] | None:
	with connect(org_id) as connection:
		row = connection.execute(
			"SELECT * FROM pack_records WHERE record_id = ? AND org_id = ?",
			(record_id, org_id),
		).fetchone()
	return dict(row) if row else None


def list_session_records(session_id: str, org_id: str) -> list[dict[str, Any]]:
	with connect(org_id) as connection:
		rows = connection.execute(
			"SELECT * FROM pack_records WHERE session_id = ? AND org_id = ? ORDER BY attempt_number, captured_at",
			(session_id, org_id),
		).fetchall()
	return [dict(row) for row in rows]


def update_workflow_state(record_id: str, org_id: str, workflow_state: str) -> None:
	with connect(org_id) as connection:
		connection.execute(
			"UPDATE pack_records SET workflow_state = ? WHERE record_id = ? AND org_id = ?",
			(workflow_state, record_id, org_id),
		)


def save_record(record: dict[str, Any]) -> None:
	with connect(record["org_id"]) as connection:
		connection.execute(
			"""
			INSERT INTO pack_records (
				record_id, org_id, organization_id, unit_id, order_id, supplier_name, channel, origin_address, delivery_address, order_lines,
				observed_in_box, operator_verdict, operator_id, photo_ref, video_ref,
				captured_at, verdict, action, workflow_state, session_id, parent_record_id, attempt_number, reason, evidence_json
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				record["record_id"], record["org_id"], record["org_id"], record["unit_id"],
				record["order_id"], record.get("supplier_name"), record["channel"], record.get("origin_address"),
				record.get("delivery_address"), record["order_lines"],
				record.get("observed_in_box"), record.get("operator_verdict"),
				record["operator_id"], record.get("photo_ref"), record.get("video_ref"),
				record["captured_at"], record["verdict"], record["action"],
				record.get("workflow_state", "REVIEW"), record.get("session_id", record["record_id"]),
				record.get("parent_record_id"), record.get("attempt_number", 1), record["reason"],
				json.dumps(record["evidence"]),
			),
		)


def save_audit_event(event: dict[str, Any]) -> None:
	with connect(event["org_id"]) as connection:
		connection.execute(
			"""
			INSERT INTO audit_events (
				event_id, record_id, org_id, organization_id, event_type, reason,
				actor_id, created_at, details_json
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				event["event_id"], event["record_id"], event["org_id"], event["org_id"],
				event["event_type"], event["reason"], event["actor_id"],
				event["created_at"], json.dumps(event.get("details", {})),
			),
		)


def list_audit_events(record_id: str, org_id: str) -> list[dict[str, Any]]:
	with connect(org_id) as connection:
		rows = connection.execute(
			"""
			SELECT * FROM audit_events
			WHERE record_id = ? AND org_id = ?
			ORDER BY created_at DESC
			""",
			(record_id, org_id),
		).fetchall()
	events = []
	for row in rows:
		event = dict(row)
		event["details"] = json.loads(event.pop("details_json"))
		events.append(event)
	return events


def save_support_request(ticket: dict[str, Any]) -> None:
	with connect(ticket["org_id"]) as connection:
		connection.execute(
			"""
			INSERT INTO support_requests (
				ticket_id, org_id, organization_id, requester_name, requester_email,
				subject, message, status, created_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				ticket["ticket_id"], ticket["org_id"], ticket["org_id"], ticket["requester_name"],
				ticket["requester_email"], ticket["subject"], ticket["message"],
				ticket.get("status", "OPEN"), ticket["created_at"],
			),
		)


def list_support_requests(org_id: str) -> list[dict[str, Any]]:
	with connect(org_id) as connection:
		rows = connection.execute(
			"SELECT * FROM support_requests WHERE org_id = ? ORDER BY created_at DESC",
			(org_id,),
		).fetchall()
	return [dict(row) for row in rows]


def create_contract_capture(capture: dict[str, Any]) -> None:
	with connect(capture["organization_key"]) as connection:
		connection.execute(
			"""INSERT INTO contract_captures (
				capture_id, organization_id, owner_username, client_id, agent,
				subject_json, operator_label, image_slots_json, input_json, status, created_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				capture["capture_id"], capture["organization_id"], capture["owner_username"],
				capture.get("client_id"), capture["agent"], json.dumps(capture["subject"]),
				capture["operator_label"], json.dumps(capture["image_slots"]),
				json.dumps(capture["input"]), "pending", capture["created_at"],
			),
		)


def get_contract_capture(capture_id: str, organization_key: str) -> dict[str, Any] | None:
	from contract import organization_uuid

	with connect(organization_key) as connection:
		row = connection.execute(
			"SELECT * FROM contract_captures WHERE capture_id = ? AND organization_id = ?",
			(capture_id, organization_uuid(organization_key)),
		).fetchone()
	return dict(row) if row else None


def complete_contract_capture(capture_id: str, organization_key: str, record: dict[str, Any]) -> bool:
	from contract import organization_uuid

	with connect(organization_key) as connection:
		updated = connection.execute(
			"UPDATE contract_captures SET status = 'processing' WHERE capture_id = ? AND organization_id = ? AND status = 'pending'",
			(capture_id, organization_uuid(organization_key)),
		)
		if updated.rowcount != 1:
			return False
		connection.execute(
			"""INSERT INTO contract_records (record_id, organization_id, agent, captured_at, status, record_json)
			VALUES (?, ?, ?, ?, ?, ?)""",
			(
				record["record_id"], record["organization_id"], record["agent"],
				record["captured_at"], record["status"], json.dumps(record, separators=(",", ":"), ensure_ascii=False),
			),
		)
		connection.execute(
			"UPDATE contract_captures SET status = 'complete', record_id = ? WHERE capture_id = ? AND organization_id = ? AND status = 'processing'",
			(record["record_id"], capture_id, organization_uuid(organization_key)),
		)
	return True


def update_contract_record(record: dict[str, Any], organization_key: str) -> bool:
	from contract import organization_uuid

	with connect(organization_key) as connection:
		cursor = connection.execute(
			"UPDATE contract_records SET status = ?, record_json = ? "
			"WHERE record_id = ? AND organization_id = ? AND status = 'pending'",
			(
				record["status"], json.dumps(record, separators=(",", ":"), ensure_ascii=False),
				record["record_id"], organization_uuid(organization_key),
			),
		)
		return cursor.rowcount == 1


def _contract_record(row: Any, connection: Connection) -> dict[str, Any]:
	record = json.loads(row["record_json"])
	rows = connection.execute(
		"SELECT check_key, from_verdict, to_verdict, reason, by, at FROM contract_overrides "
		"WHERE organization_id = ? AND record_id = ? ORDER BY at, override_id",
		(record["organization_id"], record["record_id"]),
	).fetchall()
	record["overrides"] = [dict(item) for item in rows]
	if record["overrides"]:
		latest_by_check = {}
		for override in record["overrides"]:
			latest_by_check[override["check_key"]] = override
		verdicts = [latest_by_check[check["check_key"]]["to_verdict"] if check["check_key"] in latest_by_check else check["verdict"] for check in record["checks"]]
		decision = "fix" if "fail" in verdicts else "manual_review" if "uncertain" in verdicts else "seal"
		latest = max(record["overrides"], key=lambda item: item["at"])
		record["outcome"] = {"decision": decision, "decided_by": "operator", "decided_at": latest["at"]}
	return record


def get_contract_record(record_id: str, organization_key: str | None = None, *, public: bool = False) -> dict[str, Any] | None:
	from contract import organization_uuid

	context = "__public__" if public else organization_key
	if not context:
		return None
	with connect(context) as connection:
		if public:
			row = connection.execute("SELECT * FROM contract_records WHERE record_id = ?", (record_id,)).fetchone()
		else:
			row = connection.execute(
				"SELECT * FROM contract_records WHERE record_id = ? AND organization_id = ?",
				(record_id, organization_uuid(context)),
			).fetchone()
		return _contract_record(row, connection) if row else None


def list_contract_records(
	organization_key: str,
	*,
	since: str | None,
	agent: str | None,
	cursor: tuple[str, str] | None,
	limit: int,
) -> list[dict[str, Any]]:
	from contract import organization_uuid

	conditions = ["organization_id = ?"]
	parameters: list[Any] = [organization_uuid(organization_key)]
	if since:
		conditions.append("captured_at >= ?")
		parameters.append(since)
	if agent:
		conditions.append("agent = ?")
		parameters.append(agent)
	if cursor:
		conditions.append("(captured_at < ? OR (captured_at = ? AND record_id < ?))")
		parameters.extend((cursor[0], cursor[0], cursor[1]))
	parameters.append(limit)
	with connect(organization_key) as connection:
		rows = connection.execute(
			f"SELECT * FROM contract_records WHERE {' AND '.join(conditions)} "
			"ORDER BY captured_at DESC, record_id DESC LIMIT ?",
			tuple(parameters),
		).fetchall()
		return [_contract_record(row, connection) for row in rows]


def append_contract_override(record_id: str, organization_key: str, override: dict[str, Any]) -> bool:
	from contract import organization_uuid

	organization_id = organization_uuid(organization_key)
	with connect(organization_key) as connection:
		row = connection.execute(
			"SELECT record_json FROM contract_records WHERE record_id = ? AND organization_id = ?",
			(record_id, organization_id),
		).fetchone()
		if not row:
			return False
		record = json.loads(row["record_json"])
		if not any(check["check_key"] == override["check_key"] and check["verdict"] == override["from_verdict"] for check in record["checks"]):
			return False
		connection.execute(
			"""INSERT INTO contract_overrides (
				override_id, organization_id, record_id, check_key, from_verdict,
				to_verdict, reason, by, at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
			(
				override["override_id"], organization_id, record_id, override["check_key"],
				override["from_verdict"], override["to_verdict"], override["reason"],
				override["by"], override["at"],
			),
		)
	return True
