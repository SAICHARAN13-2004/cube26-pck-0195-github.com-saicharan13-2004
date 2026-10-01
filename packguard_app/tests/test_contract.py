import copy

import pytest

from contract import build_record, validate_record


def _record():
	return build_record(
		organization_key="org_demo_alpha",
		client_id=None,
		agent="pack",
		subject={
			"type": "order",
			"asin": None,
			"sku": "SKU-A",
			"order_id": "ORD-A",
			"po_line_id": None,
			"shipment_id": "SHIP-A",
			"quantity_expected": 1,
			"quantity_observed": 1,
		},
		operator_label="operator-a",
		images=[{"key": "org/record/shot-1.jpg", "sha256": "a" * 64, "bytes": 10, "taken_at": "2026-09-30T00:00:00Z"}],
		checks=[{"check_key": "sku_quantity", "verdict": "pass", "confidence": None, "detail": {"sku": "SKU-A"}, "model_version": "deterministic-packguard-v1", "latency_ms": 0}],
		decision="seal",
		decided_by="operator",
		status="complete",
	)


def test_record_matches_frozen_contract_and_content_hash():
	record = _record()
	validate_record(record)
	assert record["schema_version"] == "1.1"
	assert record["agent"] == "pack"
	assert set(record) == {
		"record_id", "schema_version", "organization_id", "client_id", "agent",
		"subject", "captured_at", "operator_label", "images", "checks", "outcome",
		"overrides", "status", "content_hash",
	}
	assert len(record["organization_id"]) == 36


def test_record_rejects_extra_fields_outside_contract_extension_point():
	record = _record()
	record["internal_notes"] = "not allowed"
	with pytest.raises(ValueError, match="exactly match"):
		validate_record(record)


def test_record_rejects_wrong_content_hash():
	record = _record()
	record["checks"][0]["detail"] = {"sku": "SKU-B"}
	with pytest.raises(ValueError, match="content_hash"):
		validate_record(record)


def test_record_detail_is_the_only_check_extension_point():
	record = _record()
	invalid = copy.deepcopy(record)
	invalid["checks"][0]["sku"] = "SKU-A"
	with pytest.raises(ValueError, match="check fields"):
		validate_record(invalid)
