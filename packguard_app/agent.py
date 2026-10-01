"""PackGuard packing-agent assessment and next-action policy."""

from typing import Any


DECISION_ACTIONS = {
    "SEAL": "Release to the sealing step after operator confirmation.",
    "FIX": "Correct the contents, then run the pack check again.",
    "RECAPTURE": "Retake a clear photo of the complete open box.",
    "MANUAL_REVIEW": "Have an operator or supervisor inspect the package.",
}

def enforce_component_verdict(
    verifier_result: dict[str, Any],
    inspection: dict[str, Any] | None,
    photo_present: bool,
) -> dict[str, Any]:
    """Prevent overall PASS whenever pack evidence is failed or unknown."""
    result = dict(verifier_result)
    checks = [dict(check) for check in verifier_result.get("checks", [])]
    inspection = inspection or {}

    if not photo_present:
        checks.append({
            "check": "visual_evidence", "status": "UNCERTAIN",
            "reason": "No open-box photo was provided, so visual pack checks were not run.",
        })
    elif inspection.get("status") != "SUGGESTIONS_READY_UNCALIBRATED":
        checks.append({
            "check": "visual_evidence", "status": "UNCERTAIN",
            "reason": f"Visual pack assessment unavailable ({inspection.get('status', 'UNKNOWN')}).",
        })
    else:
        checks.append({
            "check": "visual_evidence", "status": "UNCERTAIN",
            "reason": "Vision suggestion is uncalibrated and cannot count as passed pack evidence.",
        })

    statuses = [str(check.get("status", "UNCERTAIN")).upper() for check in checks]
    result["checks"] = checks
    if "FAIL" in statuses:
        result.update({
            "verdict": "FAIL", "action": "STOP_AND_FIX", "decision": "FIX",
            "reason": "At least one required component check failed; other results cannot override it.",
        })
    elif "UNCERTAIN" in statuses:
        poor_image = str((inspection.get("image_quality") or {}).get("status", "")).upper() == "POOR"
        missing_photo = not photo_present
        decision = "RECAPTURE" if poor_image or missing_photo else "MANUAL_REVIEW"
        result.update({
            "verdict": "UNCERTAIN", "action": "HOLD_FOR_REVIEW", "decision": decision,
            "reason": "Pack evidence is uncertain or not implemented; overall PASS is blocked.",
        })
    return result


def assess_pack(
    *,
    verifier_result: dict[str, Any],
    inspection: dict[str, Any] | None,
    operator_observation: str | None,
) -> dict[str, Any]:
    """Combine verifier and optional vision evidence into a transparent recommendation."""
    inspection = inspection or {}
    operator_observation = (operator_observation or "").strip()
    decision = str(verifier_result.get("decision", "MANUAL_REVIEW")).upper()
    vision_ready = inspection.get("status") == "SUGGESTIONS_READY_UNCALIBRATED"
    suggested_contents = str(inspection.get("suggested_observed_contents", "")).strip()
    quality = str((inspection.get("image_quality") or {}).get("status", "UNKNOWN")).upper()
    uncertainties = inspection.get("uncertainties") or []

    vision_failed = inspection.get("status") in {
        "MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_NOT_INSTALLED", "MODEL_ERROR",
        "INVALID_MODEL_RESPONSE", "INVALID_IMAGE", "IMAGE_NOT_FOUND", "IMAGE_TOO_LARGE",
        "MULTIPLE_CANDIDATES_UNSUPPORTED",
    }
    manual_result_authoritative = bool(operator_observation) and verifier_result.get("verdict") in {"PASS", "FAIL"}
    if manual_result_authoritative:
        recommendation = decision
        rationale = "The deterministic SKU and quantity comparison uses the operator-entered observation; vision is advisory."
    elif vision_failed:
        recommendation = "MANUAL_REVIEW"
        rationale = f"Vision could not provide a reliable assessment ({inspection.get('status')})."
    elif vision_ready and quality == "POOR":
        recommendation = "RECAPTURE"
        rationale = "The vision model flags image quality as poor."
    elif vision_ready and (not suggested_contents or quality != "GOOD" or uncertainties):
        recommendation = "MANUAL_REVIEW"
        rationale = "The image suggestion is incomplete or uncertain; a person must verify contents."
    elif vision_ready:
        recommendation = str(inspection.get("candidate_decision", "MANUAL_REVIEW")).upper()
        rationale = "A candidate comparison was made from the image, but its output is uncalibrated."
    else:
        recommendation = decision
        rationale = "Recommendation is based on the deterministic verifier and available operator observation."

    next_action = DECISION_ACTIONS.get(recommendation, DECISION_ACTIONS["MANUAL_REVIEW"])
    if vision_ready and not manual_result_authoritative:
        next_action += " Human confirmation is required; this vision result cannot authorize SEAL."
    elif vision_failed and not manual_result_authoritative:
        next_action = "Enter observed contents manually or route the package to a human reviewer."

    return {
        "recommendation": recommendation,
        "rationale": rationale,
        "next_action": next_action,
        "observation_source": "operator_and_vision_suggestion" if operator_observation and vision_ready else (
            "vision_suggestion_only" if vision_ready else "operator_and_deterministic_verifier" if operator_observation else "insufficient_observation"
        ),
        "suggested_observed_contents": suggested_contents or None,
        "requires_human_confirmation": True,
        "vision_calibration": inspection.get("calibration_status", "NOT_AVAILABLE"),
        "authoritative_verdict": verifier_result.get("verdict", "UNCERTAIN"),
        "authoritative_decision": decision,
    }
