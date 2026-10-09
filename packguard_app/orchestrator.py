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

RETURN_START_EVENTS = {"RETURN_INITIATED", "CUSTOMER_RETURN_INITIATED"}
AGENT_FAILURE_EVENTS = {"AGENT_FAILED", "AGENT_TIMEOUT"}


def decide_unit_next_action(
    stages: list[dict[str, Any]], events: list[dict[str, Any]],
    route: dict[str, str], evidence_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Recommend a safe next step from recorded unit events and evidence.

    This router never invokes a manager or mutates evidence records.
    """
    by_agent = {str(stage.get("agent")): stage for stage in stages}
    event_types = {str(event.get("event_type", "")).upper() for event in events}
    evidence = evidence_refs or []

    def review(agent: str, reason: str) -> dict[str, Any]:
        return {"kind": "human_review", "agent": agent, "reason": reason}

    def manager_action(agent: str, reason: str) -> dict[str, Any]:
        if not by_agent.get(agent, {}).get("connected"):
            return {"kind": "handoff_required", "agent": agent,
                    "reason": f"{reason} The {agent.title()} manager is not connected; no manager was invoked."}
        return {"kind": "operator_action", "agent": agent,
                "reason": f"{reason} Open the {agent.title()} manager to continue."}

    def recent_failure(agent: str) -> dict[str, Any] | None:
        captured_at = str(by_agent.get(agent, {}).get("captured_at") or "")
        failures = [event for event in events
                    if str(event.get("event_type", "")).upper() in AGENT_FAILURE_EVENTS
                    and str((event.get("details") or {}).get("agent", "")).lower() == agent
                    and str(event.get("created_at", "")) >= captured_at]
        if not failures:
            return None
        failure = max(failures, key=lambda item: str(item.get("created_at", "")))
        details = failure.get("details") or {}
        attempt = details.get("attempt_number", 1)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            attempt = 1
        if attempt < 2:
            return {"kind": "retry_required", "agent": agent, "attempt_number": attempt + 1,
                    "reason": f"{agent.title()} reported {failure['event_type']}; request one retry and retain the failure event."}
        return review(agent, f"{agent.title()} failed after a retry. Keep the unit in review.")

    if "PRODUCT_RECEIVED" not in event_types:
        return {"kind": "wait_for_event", "event": "PRODUCT_RECEIVED", "reason": "The unit has not been marked received."}
    receiving = by_agent.get("receiving", {"state": "awaiting", "connected": False})
    failure = recent_failure("receiving")
    if failure:
        return failure
    if receiving.get("state") in {"attention", "pending"}:
        return review("receiving", "Receiving evidence is incomplete, failed, or needs review.")
    if receiving.get("state") != "done":
        return manager_action("receiving", "The receipt event is recorded; Receiving is next.")

    forward_agent = route.get("next_agent")
    if forward_agent not in {"prep", "pack"}:
        return review("orchestrator", "No valid Prep or Pack route is available for this fulfillment channel.")
    stage = by_agent.get(forward_agent, {"state": "awaiting", "connected": False})
    failure = recent_failure(forward_agent)
    if failure:
        return failure
    if stage.get("state") in {"attention", "pending", "blocked"}:
        return review(forward_agent, f"{forward_agent.title()} evidence needs attention before the unit can continue.")
    if stage.get("state") != "done":
        return manager_action(forward_agent, f"The route selects {forward_agent.title()} and skips {route.get('skipped_agent', 'the other manager')}.")

    if not event_types.intersection(RETURN_START_EVENTS):
        return {"kind": "wait_for_event", "event": "RETURN_INITIATED", "reason": "Forward fulfillment is complete; wait until a customer return is initiated."}
    if "RETURN_RECEIVED" not in event_types:
        return {"kind": "wait_for_event", "event": "RETURN_RECEIVED", "reason": "The return is in transit; inspect it after physical arrival."}
    returns = by_agent.get("returns", {"state": "awaiting", "connected": False})
    failure = recent_failure("returns")
    if failure:
        return failure
    if returns.get("state") in {"attention", "pending", "blocked"}:
        return review("returns", "Return evidence is incomplete or needs human review.")
    if returns.get("state") != "done":
        return manager_action("returns", "The physical return arrived; inspect identity, completeness, and condition.")

    if "RECOVERY_CHARGE_RECEIVED" not in event_types:
        return {"kind": "wait_for_event", "event": "RECOVERY_CHARGE_RECEIVED", "reason": "Returns is complete; Recovery runs only when a charge event arrives."}
    recovery = by_agent.get("recovery", {"state": "ready", "connected": False})
    failure = recent_failure("recovery")
    if failure:
        return failure
    if recovery.get("state") in {"attention", "pending", "blocked"} or not evidence:
        return review("recovery", "A charge arrived, but usable upstream evidence is missing or needs review.")
    if recovery.get("state") != "done":
        action = manager_action("recovery", "A charge arrived; evaluate it against available unit evidence.")
        action["evidence_refs"] = evidence
        return action
    return {"kind": "journey_complete", "reason": "All applicable stages have a recorded outcome."}


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
