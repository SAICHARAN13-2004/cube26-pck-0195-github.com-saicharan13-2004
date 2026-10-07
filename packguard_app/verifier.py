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


def diagnose_pack_contents(
	order_lines: str,
	observed_in_box: str | None,
	catalog_names: dict[str, str] | None = None,
) -> dict[str, Any]:
	"""Diagnose packing discrepancies with detailed item-level analysis.
	
	Identifies:
	  - Items present
	  - Missing items
	  - Incorrect / wrong items (replaced items)
	  - Incorrect quantities
	  - Unexpected extra items
	Produces final operational decision: SEAL, STOP & FIX, or UNCERTAIN.
	"""
	catalog_names = catalog_names or {}
	verification = verify_pack(order_lines, observed_in_box)

	expected = verification.get("expected", {})
	observed = verification.get("observed", {})
	verdict = verification.get("verdict", "UNCERTAIN")

	if verdict == "UNCERTAIN":
		return {
			"status": "HOLD FOR REVIEW",
			"decision": "UNCERTAIN",
			"operational_verdict": "UNCERTAIN",
			"is_wrong_package": False,
			"issues": [verification.get("reason", "Ambiguous evidence or unreadable packaging prevents reliable comparison.")],
			"issue_summary": verification.get("reason", "Ambiguous evidence."),
			"items_present": [],
			"missing_items": [],
			"wrong_items": [],
			"extra_items": [],
			"quantity_mismatches": [],
			"verification": verification,
		}

	items_present = []
	missing_items = []
	extra_items = []
	quantity_mismatches = []

	for sku, exp_qty in expected.items():
		obs_qty = observed.get(sku, 0)
		name = catalog_names.get(sku, sku)
		if obs_qty == 0:
			missing_items.append({"sku": sku, "name": name, "expected": exp_qty, "observed": 0})
		elif obs_qty == exp_qty:
			items_present.append({"sku": sku, "name": name, "quantity": exp_qty})
		else:
			items_present.append({"sku": sku, "name": name, "quantity": obs_qty})
			quantity_mismatches.append({
				"sku": sku,
				"name": name,
				"expected": exp_qty,
				"observed": obs_qty,
				"difference": obs_qty - exp_qty,
			})

	for sku, obs_qty in observed.items():
		if sku not in expected and obs_qty > 0:
			name = catalog_names.get(sku, sku)
			extra_items.append({"sku": sku, "name": name, "observed": obs_qty})

	# Pair missing items with extra items to detect Replaced / Wrong items
	wrong_items = []
	unpaired_missing = list(missing_items)
	unpaired_extra = list(extra_items)

	while unpaired_missing and unpaired_extra:
		m = unpaired_missing.pop(0)
		e = unpaired_extra.pop(0)
		wrong_items.append({
			"expected_sku": m["sku"],
			"expected_name": m["name"],
			"expected_quantity": m["expected"],
			"detected_sku": e["sku"],
			"detected_name": e["name"],
			"detected_quantity": e["observed"],
		})

	issues = []
	for w in wrong_items:
		issues.append(f"Expected {w['expected_name']}, Detected {w['detected_name']}")
	for m in unpaired_missing:
		issues.append(f"Missing item: {m['expected']} × {m['name']}")
	for e in unpaired_extra:
		issues.append(f"Unexpected extra item: {e['observed']} × {e['name']}")
	for q in quantity_mismatches:
		diff_str = f"+{q['difference']}" if q['difference'] > 0 else str(q['difference'])
		issues.append(f"Incorrect quantity for {q['name']}: expected {q['expected']}, detected {q['observed']} ({diff_str})")

	is_wrong = bool(wrong_items or unpaired_missing or unpaired_extra or quantity_mismatches)

	if not is_wrong and verdict == "PASS":
		status_text = "SEAL"
		op_decision = "SEAL"
		issue_summary = "All items verified: exact quantities matched, no discrepancies."
	else:
		status_text = "STOP & FIX"
		op_decision = "STOP & FIX"
		issue_summary = "; ".join(issues) if issues else "Contents do not match the expected order."

	return {
		"status": status_text,
		"decision": op_decision,
		"operational_verdict": "SEAL" if not is_wrong else "STOP & FIX",
		"is_wrong_package": is_wrong,
		"issues": issues,
		"issue_summary": issue_summary,
		"items_present": items_present,
		"missing_items": missing_items,
		"wrong_items": wrong_items,
		"extra_items": extra_items,
		"quantity_mismatches": quantity_mismatches,
		"verification": verification,
	}

