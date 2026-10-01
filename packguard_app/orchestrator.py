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

    # Verifier has priority for confirmed discrepancies. Uncalibrated visual
    # evidence can request review, but it can never turn an uncertain into pass.
    if operator_result["verdict"] == "FAIL":
        action, decision = "open_fix_workflow", "fix"
    elif visual_status in FAILED_VISION_STATUSES:
        action, decision = "route_manual_review", "manual_review"
    elif quality == "POOR":
        action, decision = "request_recapture", "recapture"
    elif conflict:
        action, decision = "route_manual_review", "manual_review"
    elif operator_result["verdict"] == "PASS" and vision_result_check and vision_result_check["verdict"] == "PASS":
        action, decision = "request_operator_confirmation", "manual_review"
    elif operator_result["verdict"] == "UNCERTAIN" and vision_result_check:
        action, decision = "route_manual_review", "manual_review"
    else:
        action, decision = "route_manual_review", "manual_review"

    handoffs = [
        {"from": "vision_agent", "to": "evidence_verifier", "status": visual_status},
        {"from": "evidence_verifier", "to": "workflow_coordinator", "operator_verdict": operator_result["verdict"],
         "vision_candidate_verdict": vision_result_check["verdict"] if vision_result_check else "uncertain"},
        {"from": "workflow_coordinator", "to": "action_executor", "action": action},
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
            "operator_vision_conflict": conflict,
            "vision_is_calibrated": False,
            "operator_confirmation_required_for_seal": True,
        },
    }
    return {
        "decision": decision,
        "action": action,
        "trace": trace,
        "operator_verification": operator_result,
        "vision_verification": vision_result_check,
        "vision_observation": vision_observation,
        "observed": observed,
    }
