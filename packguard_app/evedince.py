"""Evidence contract for interoperable Pack Manager records."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def build_evidence(
	*,
	record_id: str,
	org_id: str,
	unit_id: str,
	order_lines: str,
	supplier_name: str | None = None,
	observed_in_box: str | None,
	result: dict[str, Any],
	photo_ref: str | None,
	image_observation: dict[str, Any] | None = None,
	vision_result: dict[str, Any] | None = None,
	operator_verdict: str | None = None,
	agreement: str = "NOT_RECORDED",
	video_ref: str | None = None,
) -> dict[str, Any]:
	"""Create a portable, append-only decision payload."""
	inspection = vision_result or {"status": "NOT_PROVIDED"}
	return {
		"evidence_id": f"EVD-{uuid4().hex[:12].upper()}",
		"contract_version": "pack-manager.v1",
		"record_id": record_id,
		"org_id": org_id,
		"unit_id": unit_id,
		"supplier_name": supplier_name,
		"captured_at": datetime.now(timezone.utc).isoformat(),
		"source": {
			"type": (image_observation or {}).get("source", "operator_structured_observation"),
			"photo_ref": photo_ref,
			"video_ref": video_ref,
			"media_present": bool(photo_ref or video_ref),
			"observation_present": bool((observed_in_box or "").strip()),
			"vision_status": inspection.get("status", "NOT_PROVIDED"),
			"confidence": (image_observation or {}).get("confidence"),
			"inspection": inspection,
		},
		"expected": result["expected"],
		"observed": result["observed"],
		"checks": result["checks"],
		"verdict": result["verdict"],
		"action": result["action"],
		"decision": result.get("decision", "MANUAL_REVIEW"),
		"confidence": (vision_result or {}).get("confidence"),
		"reason": result["reason"],
			"operator_verdict": operator_verdict or "",
			"operator_agreement": agreement,
		"input_snapshot": {
			"order_lines": order_lines,
			"observed_in_box": observed_in_box,
		},
	}
