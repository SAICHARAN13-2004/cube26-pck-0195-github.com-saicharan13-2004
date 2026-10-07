"""Bounded, auditable multi-agent workflow for Pack evidence.

Vision remains one batched model call per capture. The other agents are
deterministic workers that hand its findings to the verifier and then invoke
one of a small set of persisted next-step actions.
"""

from __future__ import annotations

from typing import Any

from verifier import parse_lines, verify_pack


MAX_AGENT_STEPS = 3
FAILED_VISION_STATUSES = {
    "MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_NOT_INSTALLED", "MODEL_ERROR",
    "INVALID_MODEL_RESPONSE", "INVALID_IMAGE", "IMAGE_NOT_FOUND", "IMAGE_TOO_LARGE",
    "MULTIPLE_CANDIDATES_UNSUPPORTED",
}


def _vision_observation(vision_result: dict[str, Any]) -> str:
    suggested = str(vision_result.get("suggested_observed_contents") or "").strip()
    if suggested:
        return suggested
    items = vision_result.get("detected_items") or []
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sku, quantity = item.get("sku"), item.get("quantity")
        if isinstance(sku, str) and sku.strip() and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0:
            parts.append(f"{sku.strip()}:{quantity}")
    return ";".join(parts)


def run_pack_agent_workflow(
    vision_result: dict[str, Any], *, expected_lines: str, observed_contents: str,
) -> dict[str, Any]:
    """Hand off one capture through bounded specialist agents and an action tool."""
    visual_status = str(vision_result.get("status", "MODEL_ERROR"))
    vision_observation = _vision_observation(vision_result)
    operator_result = verify_pack(expected_lines, observed_contents)
    vision_result_check = verify_pack(expected_lines, vision_observation) if vision_observation else None
    observed = parse_lines(observed_contents)
    vision_observed = parse_lines(vision_observation)
    conflict = bool(observed_contents.strip() and vision_observation and observed != vision_observed)
    image_quality = vision_result.get("image_quality")
    quality = str(image_quality.get("status", "UNKNOWN") if isinstance(image_quality, dict) else "UNKNOWN").upper()
    occlusion = vision_result.get("occlusion")
    occlusion_status = str(occlusion.get("status", "UNCERTAIN") if isinstance(occlusion, dict) else "UNCERTAIN").upper()
    uncertainties = vision_result.get("uncertainties") or []
    confidence = vision_result.get("confidence")
    detected_items = vision_result.get("detected_items") or []
    extra_items = vision_result.get("extra_items") or []
    wrong_variant_items = [
		item for item in detected_items
		if isinstance(item, dict) and item.get("variant_match") is False
	]
    confident_extras = [
		item for item in extra_items
		if isinstance(item, dict)
		and isinstance(item.get("confidence"), (int, float))
		and item["confidence"] >= 0.85
	]
    detections_are_clear = bool(detected_items) and all(
		isinstance(item, dict)
		and isinstance(item.get("sku"), str)
		and isinstance(item.get("confidence"), (int, float))
		and item["confidence"] >= 0.85
		for item in detected_items
	)
    vision_is_clear = (
        visual_status == "SUGGESTIONS_READY_UNCALIBRATED"
        and quality == "GOOD"
        and occlusion_status == "CLEAR"
        and not uncertainties
        and isinstance(confidence, (int, float))
        and confidence >= 0.85
        and detections_are_clear
    )
    if visual_status in FAILED_VISION_STATUSES:
        candidate_decision = "MANUAL_REVIEW"
        candidate_reason = f"Vision could not provide a usable assessment ({visual_status})."
    elif quality == "POOR":
        candidate_decision = "RECAPTURE"
        candidate_reason = str((image_quality or {}).get("reason") or "The pack image is not clear enough to inspect.")
    elif not vision_is_clear:
        candidate_decision = "MANUAL_REVIEW"
        candidate_reason = str((uncertainties or ["Image quality, SKU identity, quantity, or confidence is not clear enough for an autonomous recommendation."])[0])
    elif confident_extras or wrong_variant_items:
        candidate_decision = "FIX"
        candidate_reason = "An unexpected item or wrong product variant is visible in the pack."
    elif vision_result_check and vision_result_check["verdict"] == "FAIL":
        candidate_decision = "FIX"
        candidate_reason = vision_result_check["reason"]
    elif vision_result_check and vision_result_check["verdict"] == "PASS":
        candidate_decision = "SEAL"
        candidate_reason = "The image suggests all expected SKU quantities are present and no extra item was detected."
    else:
        candidate_decision = "MANUAL_REVIEW"
        candidate_reason = "Image evidence did not produce a complete SKU and quantity comparison."

    # Clear image mismatches can safely hold a package; a seal remains gated.
    if operator_result["verdict"] == "FAIL":
        action, decision = "open_fix_workflow", "fix"
    elif visual_status in FAILED_VISION_STATUSES:
        action, decision = "route_manual_review", "manual_review"
    elif quality == "POOR":
        action, decision = "request_recapture", "recapture"
    elif conflict:
        action, decision = "route_manual_review", "manual_review"
    elif vision_is_clear and candidate_decision == "FIX":
        action, decision = "open_fix_workflow", "fix"
    elif vision_is_clear and candidate_decision == "SEAL":
        action, decision = "request_operator_confirmation", "manual_review"
    elif operator_result["verdict"] == "PASS" and vision_result_check and vision_result_check["verdict"] == "PASS":
        action, decision = "request_operator_confirmation", "manual_review"
    elif operator_result["verdict"] == "UNCERTAIN" and vision_result_check:
        action, decision = "route_manual_review", "manual_review"
    else:
        action, decision = "route_manual_review", "manual_review"

    handoffs = [
        {"from": "vision_agent", "to": "evidence_verifier", "status": visual_status},
        {"from": "evidence_verifier", "to": "workflow_coordinator", "operator_verdict": operator_result["verdict"],
         "vision_candidate_verdict": vision_result_check["verdict"] if vision_result_check else "uncertain",
         "candidate_decision": candidate_decision},
        {"from": "workflow_coordinator", "to": "action_executor", "action": action, "decision": decision},
    ]
    trace = {
        "workflow": "pack_evidence_v1",
        "status": "completed" if visual_status not in FAILED_VISION_STATUSES else "waiting_for_operator",
        "max_steps": MAX_AGENT_STEPS,
        "steps_used": min(len(handoffs), MAX_AGENT_STEPS),
        "agents": ["vision_agent", "evidence_verifier", "workflow_coordinator"],
        "handoffs": handoffs,
        "tool_call": {"name": action, "result": decision},
        "decision_basis": {
            "operator_verdict": operator_result["verdict"],
            "vision_candidate_verdict": vision_result_check["verdict"] if vision_result_check else None,
            "vision_candidate_decision": candidate_decision,
            "operator_vision_conflict": conflict,
            "vision_meets_clear_candidate_gate": vision_is_clear,
            "vision_is_calibrated": False,
            "automatic_seal_authorized": False,
            "operator_confirmation_required_for_seal": candidate_decision == "SEAL",
        },
    }
    return {
        "decision": decision,
        "action": action,
        "candidate_decision": candidate_decision,
        "candidate_reason": candidate_reason,
        "trace": trace,
        "operator_verification": operator_result,
        "vision_verification": vision_result_check,
        "vision_observation": vision_observation,
        "observed": observed,
    }
