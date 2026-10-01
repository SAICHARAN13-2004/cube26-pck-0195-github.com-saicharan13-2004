"""Production decision gates for calibrated vision automation."""

import os
from typing import Any


def require_operator_confirmation(result: dict[str, Any], operator_verdict: str | None) -> dict[str, Any]:
    """Prevent a suggested seal from becoming final without an operator's seal verdict."""
    if result.get("decision") == "SEAL" and (operator_verdict or "").strip().lower() != "seal":
        result = dict(result)
        result["decision"] = "MANUAL_REVIEW"
        result["action"] = "MANUAL_REVIEW"
        result["reason"] = "An operator must explicitly confirm SEAL; this result remains pending review."
    return result


def auto_seal_policy(evaluation: dict[str, Any] | None, vision_result: dict[str, Any] | None) -> dict[str, Any]:
    """Return whether vision is allowed to authorize SEAL.

    The default is always disabled. Enabling requires an explicit deployment
    flag, a complete double-labeled evaluation, and a measured false-SEAL rate.
    """
    evaluation = evaluation or {}
    vision_result = vision_result or {}
    threshold = float(os.environ.get("PACKGUARD_MAX_FALSE_SEAL_RATE", "0"))
    reasons: list[str] = []
    if os.environ.get("PACKGUARD_AUTO_SEAL_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        reasons.append("Automatic sealing is disabled by configuration.")
    if evaluation.get("calibration_status") != "READY":
        reasons.append("Vision calibration is not READY.")
    if evaluation.get("held_out_count", 0) < 50:
        reasons.append("At least 50 held-out units are required.")
    false_seal_rate = evaluation.get("false_seal_rate")
    if false_seal_rate is None or false_seal_rate > threshold:
        reasons.append(f"False-SEAL rate must be measurable and <= {threshold:.2%}.")
    if vision_result.get("calibration_status") != "VALIDATED":
        reasons.append("This vision result is not calibrated.")
    if vision_result.get("candidate_decision") != "SEAL":
        reasons.append("Vision did not produce a SEAL candidate.")
    return {
        "auto_seal_allowed": not reasons,
        "threshold": threshold,
        "reasons": reasons,
        "status": "ALLOWED" if not reasons else "BLOCKED",
    }
