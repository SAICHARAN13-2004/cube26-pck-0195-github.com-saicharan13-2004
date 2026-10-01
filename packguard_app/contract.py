"""Strict CUBE Evidence Contract 1.1 serialization and validation."""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


SCHEMA_VERSION = "1.1"
AGENTS = {"receiving", "prep", "pack", "returns"}
VERDICTS = {"pass", "fail", "uncertain"}
STATUSES = {"complete", "pending", "failed"}
TOP_LEVEL_FIELDS = {
	"record_id", "schema_version", "organization_id", "client_id", "agent",
	"subject", "captured_at", "operator_label", "images", "checks", "outcome",
	"overrides", "status", "content_hash",
}
SUBJECT_FIELDS = {
	"type", "asin", "sku", "order_id", "po_line_id", "shipment_id",
	"quantity_expected", "quantity_observed",
}
IMAGE_FIELDS = {"key", "sha256", "bytes", "taken_at"}
CHECK_FIELDS = {"check_key", "verdict", "confidence", "detail", "model_version", "latency_ms"}
OUTCOME_FIELDS = {"decision", "decided_by", "decided_at"}
OVERRIDE_FIELDS = {"check_key", "from_verdict", "to_verdict", "reason", "by", "at"}


def utc_now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def organization_uuid(organization_key: str) -> str:
	"""Map legacy workspace keys to stable contract UUIDs without changing auth IDs."""
	try:
		return str(UUID(organization_key))
	except (ValueError, TypeError, AttributeError):
		return str(uuid5(NAMESPACE_URL, f"packguard:organization:{organization_key}"))


def content_hash(images: list[dict[str, Any]], checks: list[dict[str, Any]]) -> str:
	image_hashes = "".join(image["sha256"] for image in images)
	serialized_checks = json.dumps(checks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256((image_hashes + serialized_checks).encode("utf-8")).hexdigest()


def _require_utc(value: Any, field: str) -> None:
	if not isinstance(value, str):
		raise ValueError(f"{field} must be an ISO 8601 UTC string")
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError as error:
		raise ValueError(f"{field} must be an ISO 8601 UTC string") from error
	if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
		raise ValueError(f"{field} must be UTC")


def validate_record(record: dict[str, Any]) -> None:
	if set(record) != TOP_LEVEL_FIELDS:
		raise ValueError("Evidence record fields do not exactly match contract version 1.1")
	if record["schema_version"] != SCHEMA_VERSION:
		raise ValueError("schema_version must be 1.1")
	UUID(record["record_id"])
	UUID(record["organization_id"])
	if record["client_id"] is not None:
		UUID(record["client_id"])
	if record["agent"] not in AGENTS:
		raise ValueError("Invalid evidence agent")
	subject = record["subject"]
	if not isinstance(subject, dict) or set(subject) != SUBJECT_FIELDS:
		raise ValueError("subject fields do not match contract version 1.1")
	if subject["type"] not in {"unit", "order", "carton"}:
		raise ValueError("Invalid subject type")
	if record["agent"] == "pack" and subject["type"] != "order":
		raise ValueError("Pack records must use subject.type=order")
	for field in ("asin", "sku", "order_id", "po_line_id", "shipment_id"):
		if subject[field] is not None and not isinstance(subject[field], str):
			raise ValueError(f"subject.{field} must be a string or null")
	if not isinstance(record["operator_label"], str) or not record["operator_label"].strip():
		raise ValueError("operator_label is required")
	_require_utc(record["captured_at"], "captured_at")
	for field in ("quantity_expected", "quantity_observed"):
		if subject[field] is not None and (not isinstance(subject[field], int) or isinstance(subject[field], bool)):
			raise ValueError(f"{field} must be an integer or null")
	if not isinstance(record["images"], list) or not isinstance(record["checks"], list):
		raise ValueError("images and checks must be arrays")
	for image in record["images"]:
		if not isinstance(image, dict) or set(image) != IMAGE_FIELDS:
			raise ValueError("image fields do not match contract version 1.1")
		if not isinstance(image["bytes"], int) or image["bytes"] < 0:
			raise ValueError("image bytes must be a non-negative integer")
		if not isinstance(image["key"], str) or not image["key"]:
			raise ValueError("image key is required")
		if len(image["sha256"]) != 64:
			raise ValueError("image sha256 must be a hexadecimal SHA-256 digest")
		int(image["sha256"], 16)
		_require_utc(image["taken_at"], "image.taken_at")
	for check in record["checks"]:
		if not isinstance(check, dict) or set(check) != CHECK_FIELDS:
			raise ValueError("check fields do not match contract version 1.1")
		if check["verdict"] not in VERDICTS:
			raise ValueError("Invalid check verdict")
		if not isinstance(check["check_key"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*", check["check_key"]):
			raise ValueError("check_key must be lowercase snake_case")
		if check["confidence"] is not None and not 0 <= check["confidence"] <= 1:
			raise ValueError("check confidence must be between 0 and 1 or null")
		if not isinstance(check["detail"], dict):
			raise ValueError("checks[].detail must be an object")
		if not isinstance(check["latency_ms"], int) or check["latency_ms"] < 0:
			raise ValueError("latency_ms must be a non-negative integer")
	if not isinstance(record["outcome"], dict) or set(record["outcome"]) != OUTCOME_FIELDS:
		raise ValueError("outcome fields do not match contract version 1.1")
	if record["outcome"]["decided_by"] not in {"agent", "operator"}:
		raise ValueError("outcome.decided_by must be agent or operator")
	if not isinstance(record["outcome"]["decision"], str) or not record["outcome"]["decision"]:
		raise ValueError("outcome.decision is required")
	_require_utc(record["outcome"]["decided_at"], "outcome.decided_at")
	if not isinstance(record["overrides"], list):
		raise ValueError("overrides must be an array")
	for override in record["overrides"]:
		if not isinstance(override, dict) or set(override) != OVERRIDE_FIELDS:
			raise ValueError("override fields do not match contract version 1.1")
		if override["from_verdict"] not in VERDICTS or override["to_verdict"] not in VERDICTS:
			raise ValueError("Invalid override verdict")
		if not override["reason"].strip() or not override["by"].strip():
			raise ValueError("Override reason and operator are required")
		_require_utc(override["at"], "override.at")
	if record["status"] not in STATUSES:
		raise ValueError("Invalid evidence status")
	if record["content_hash"] != content_hash(record["images"], record["checks"]):
		raise ValueError("content_hash does not match images and checks")


def build_record(
	*,
	organization_key: str,
	client_id: str | None,
	agent: str,
	subject: dict[str, Any],
	operator_label: str,
	images: list[dict[str, Any]],
	checks: list[dict[str, Any]],
	decision: str,
	decided_by: str,
	status: str,
	record_id: str | None = None,
	captured_at: str | None = None,
	overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	record = {
		"record_id": record_id or str(uuid4()),
		"schema_version": SCHEMA_VERSION,
		"organization_id": organization_uuid(organization_key),
		"client_id": client_id,
		"agent": agent,
		"subject": {field: subject.get(field) for field in SUBJECT_FIELDS},
		"captured_at": captured_at or utc_now(),
		"operator_label": operator_label,
		"images": images,
		"checks": checks,
		"outcome": {"decision": decision, "decided_by": decided_by, "decided_at": utc_now()},
		"overrides": overrides or [],
		"status": status,
	}
	record["content_hash"] = content_hash(images, checks)
	validate_record(record)
	return record
