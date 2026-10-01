"""Evaluation summaries for PackGuard decision records."""

from collections import Counter
from typing import Any, Iterable


UNCERTAIN_RATE_TARGET = 0.10
PENDING_RATE_TARGET = 0.05
PENDING_VISION_STATUSES = {
	"MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_NOT_INSTALLED", "MODEL_ERROR",
	"INVALID_MODEL_RESPONSE", "NOT_PROVIDED", "IMAGE_NOT_FOUND", "INVALID_IMAGE",
}


def summarize_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
	"""Summarize verdict coverage and human/system agreement."""
	record_list = list(records)
	verdicts = Counter(record.get("verdict", "UNKNOWN") for record in record_list)
	decisions = Counter(
		(record.get("evidence", {}) or {}).get("decision", "NOT_AVAILABLE")
		for record in record_list
	)
	agreements = Counter(
		(record.get("evidence", {}) or {}).get("operator_agreement", "NOT_RECORDED")
		for record in record_list
	)
	total = len(record_list)
	uncertain_count = verdicts.get("UNCERTAIN", 0)
	pending_count = sum(
		1 for record in record_list
		if ((record.get("evidence", {}) or {}).get("source", {}) or {}).get("inspection", {}).get("status") in PENDING_VISION_STATUSES
	)
	presence = Counter()
	quantities = Counter()
	for record in record_list:
		checks = ((record.get("evidence", {}) or {}).get("checks", []) or [])
		identity_checks = [check for check in checks if check.get("check") == "quantity_and_identity"]
		for check in identity_checks:
			presence[check.get("presence_status", "UNCERTAIN")] += 1
			quantities[check.get("quantity_status", check.get("status", "UNCERTAIN"))] += 1
	decision_values = ("SEAL", "FIX", "RECAPTURE", "MANUAL_REVIEW")
	measured_decisions = sum(decisions.get(decision, 0) for decision in decision_values)
	decision_counts = {decision: decisions.get(decision, 0) for decision in decision_values}
	decision_counts["NOT_AVAILABLE"] = decisions.get("NOT_AVAILABLE", 0)
	return {
		"total_records": total,
		"verdicts": {
			"PASS": verdicts.get("PASS", 0),
			"FAIL": verdicts.get("FAIL", 0),
			"UNCERTAIN": verdicts.get("UNCERTAIN", 0),
		},
		"uncertain_rate": round(uncertain_count / total, 4) if total else 0.0,
		"uncertain_rate_target": UNCERTAIN_RATE_TARGET,
		"uncertain_rate_status": "NOT_MEASURED" if not total else "WITHIN_TARGET" if uncertain_count / total <= UNCERTAIN_RATE_TARGET else "ABOVE_TARGET",
		"pending_rate": round(pending_count / total, 4) if total else 0.0,
		"pending_rate_target": PENDING_RATE_TARGET,
		"pending_rate_status": "NOT_MEASURED" if not total else "WITHIN_TARGET" if pending_count / total <= PENDING_RATE_TARGET else "ABOVE_TARGET",
		"check_metrics": {
			"all_items_present": _metric_rates(presence),
			"quantities_correct": _metric_rates(quantities),
		},
		"product_decisions": decision_counts,
		"product_decision_rates": {
			decision: round(decisions.get(decision, 0) / measured_decisions, 4)
			if measured_decisions else 0.0
			for decision in decision_values
		},
		"operator_agreement": {
			"AGREES": agreements.get("AGREES", 0),
			"DISAGREES": agreements.get("DISAGREES", 0),
			"NOT_RECORDED": agreements.get("NOT_RECORDED", 0),
		},
		"scope": "logged_in_organization",
		"vision_accuracy": "not_measured_until_a_vision_provider_is_configured",
		"false_seal_rate": "not_measured_until_held_out_predictions_exist",
	}


def _metric_rates(counts: Counter[str]) -> dict[str, Any]:
	"""Return separate presence/count outcomes so recognition is not hidden by quantity."""
	total = sum(counts.values())
	return {
		"pass": counts.get("PASS", 0),
		"fail": counts.get("FAIL", 0),
		"uncertain": counts.get("UNCERTAIN", 0),
		"total": total,
		"pass_rate": round(counts.get("PASS", 0) / total, 4) if total else None,
	}
