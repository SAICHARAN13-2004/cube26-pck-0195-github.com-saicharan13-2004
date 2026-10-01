"""Deterministic pack-content verification.

The current build accepts structured observations from the capture form. A
vision adapter can provide the same ``observed_in_box`` format later without
changing the decision contract.
"""

from collections import defaultdict
from typing import Any


def parse_lines(value: str | None) -> dict[str, int]:
	"""Parse ``SKU:quantity`` pairs into a normalized quantity map."""
	return _parse_lines(value)[0]


def _parse_lines(value: str | None) -> tuple[dict[str, int], list[str]]:
	"""Parse lines and retain malformed fragments for safe verdicts."""
	lines: dict[str, int] = defaultdict(int)
	invalid_lines: list[str] = []
	for raw_line in (value or "").split(";"):
		raw_line = raw_line.strip()
		if not raw_line:
			continue
		try:
			sku, quantity = raw_line.rsplit(":", 1)
			quantity_value = int(quantity)
		except (ValueError, AttributeError):
			invalid_lines.append(raw_line)
			continue
		if sku.strip() and quantity_value >= 0:
			lines[sku.strip()] += quantity_value
		else:
			invalid_lines.append(raw_line)
	return dict(lines), invalid_lines


def verify_pack(order_lines: str, observed_in_box: str | None) -> dict[str, Any]:
	"""Compare expected and observed contents and return check-level evidence."""
	expected, invalid_expected = _parse_lines(order_lines)
	observed, invalid_observed = _parse_lines(observed_in_box)
	checks: list[dict[str, Any]] = []

	if invalid_expected or invalid_observed:
		invalid_inputs = []
		if invalid_expected:
			invalid_inputs.append("order lines")
		if invalid_observed:
			invalid_inputs.append("observed contents")
		return {
			"verdict": "UNCERTAIN",
			"action": "HOLD_FOR_REVIEW",
			"decision": "MANUAL_REVIEW",
			"reason": f"Malformed {' and '.join(invalid_inputs)} prevent a reliable comparison.",
			"checks": [],
			"expected": expected,
			"observed": observed,
		}
	if not expected:
		return {
			"verdict": "UNCERTAIN",
			"action": "HOLD_FOR_REVIEW",
			"decision": "MANUAL_REVIEW",
			"reason": "No valid order lines were supplied.",
			"checks": [],
			"expected": expected,
			"observed": observed,
		}
	if not (observed_in_box or "").strip():
		return {
			"verdict": "UNCERTAIN",
			"action": "HOLD_FOR_REVIEW",
			"decision": "RECAPTURE",
			"reason": "No observed contents were supplied; the box cannot be judged.",
			"checks": [],
			"expected": expected,
			"observed": observed,
		}

	for sku in sorted(set(expected) | set(observed)):
		expected_quantity = expected.get(sku, 0)
		observed_quantity = observed.get(sku, 0)
		presence_passed = expected_quantity == 0 or observed_quantity > 0
		quantity_passed = expected_quantity == observed_quantity
		checks.append(
			{
				"check": "quantity_and_identity",
				"sku": sku,
				"expected_quantity": expected_quantity,
				"observed_quantity": observed_quantity,
				"presence_status": "PASS" if presence_passed else "FAIL",
				"quantity_status": "PASS" if quantity_passed else "FAIL",
				"status": "PASS" if quantity_passed else "FAIL",
				"reason": (
					f"{sku}: expected {expected_quantity}, found {observed_quantity}."
				),
			}
		)

	passed = all(check["status"] == "PASS" for check in checks)
	return {
		"verdict": "PASS" if passed else "FAIL",
		"action": "SEAL" if passed else "STOP_AND_FIX",
		"decision": "SEAL" if passed else "FIX",
		"reason": "All expected quantities match." if passed else "Contents do not match the order.",
		"checks": checks,
		"expected": expected,
		"observed": observed,
	}


def verify_receiving_components(values: Any) -> list[dict[str, Any]]:
	"""Verify carton, packaging, variant, and component observations from the capture form."""
	checks: list[dict[str, Any]] = []
	for check_name, expected_field, observed_field, label in (
		("carton_count", "expected_carton_count", "observed_carton_count", "Carton count"),
		("units_per_carton", "expected_units_per_carton", "observed_units_per_carton", "Units per carton"),
	):
		expected_raw = (values.get(expected_field) or "").strip()
		observed_raw = (values.get(observed_field) or "").strip()
		if not expected_raw or not observed_raw:
			status, reason = "UNCERTAIN", f"{label} expected and observed values are required."
		else:
			try:
				expected, observed = int(expected_raw), int(observed_raw)
			except ValueError:
				status, reason = "UNCERTAIN", f"{label} values must be whole numbers."
			else:
				status = "PASS" if expected == observed else "FAIL"
				reason = f"{label}: expected {expected}, observed {observed}."
		checks.append({"check": check_name, "status": status, "reason": reason, "source": "operator_observation"})

	for check_name, field, label in (
		("carton_damage", "carton_condition", "Carton damage"),
		("product_damage", "product_condition", "Product-unit damage"),
	):
		condition = (values.get(field) or "").strip().upper()
		status = {"NO_DAMAGE": "PASS", "DAMAGED": "FAIL", "UNCERTAIN": "UNCERTAIN"}.get(condition, "UNCERTAIN")
		reason = {
			"PASS": f"Operator reports no {label.lower()}.",
			"FAIL": f"Operator reports {label.lower()}.",
			"UNCERTAIN": f"{label} was not confidently assessed.",
		}[status]
		checks.append({"check": check_name, "status": status, "reason": reason, "source": "operator_observation"})

	expected_variant = (values.get("expected_variant") or "").strip().casefold()
	observed_variant = (values.get("observed_variant") or "").strip().casefold()
	if not expected_variant or not observed_variant:
		status, reason = "UNCERTAIN", "Expected and observed variant/color are required."
	else:
		status = "PASS" if expected_variant == observed_variant else "FAIL"
		reason = "Variant/color matches." if status == "PASS" else "Variant/color does not match."
	checks.append({"check": "variant_match", "status": status, "reason": reason, "source": "operator_observation"})

	def component_set(field: str) -> set[str]:
		return {item.strip().casefold() for item in (values.get(field) or "").replace(",", ";").split(";") if item.strip()}

	expected_components = component_set("expected_components")
	observed_components = component_set("observed_components")
	if not expected_components or not observed_components:
		status, reason = "UNCERTAIN", "Expected and observed component lists are required."
	else:
		missing = sorted(expected_components - observed_components)
		extra = sorted(observed_components - expected_components)
		status = "PASS" if not missing and not extra else "FAIL"
		details = []
		if missing:
			details.append(f"missing: {', '.join(missing)}")
		if extra:
			details.append(f"unexpected: {', '.join(extra)}")
		reason = "All expected components are present." if status == "PASS" else "Component mismatch (" + "; ".join(details) + ")."
	checks.append({"check": "missing_components", "status": status, "reason": reason, "source": "operator_observation"})
	return checks
