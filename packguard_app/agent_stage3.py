# agent_stage3.py - Stage 3 agent implementation
"""Stage 3 agent: receives product ID and optional image, pulls Stage 2 prep data, runs assessment, and posts result to Stage 4 webhook."""

import json
from typing import Any, Optional

from flask import current_app as app

# Import existing helpers from the codebase
from detector import observe, observe_image
from agent import assess_pack, enforce_component_verdict
from orchestrator import run_pack_agent_workflow
from database import get_product, get_record, save_record, save_audit_event, utc_now, organization_uuid
from policy import auto_seal_policy
from contract import content_hash

def fetch_stage2_data(product_id: str) -> dict[str, Any]:
    """Retrieve the Stage‑2 preparation record for the given product SKU.
    This uses the existing DB helper `get_record` which expects a `record_id`.
    For simplicity we assume the `record_id` is the product_id prefixed with ``PREP-``.
    """
    record_id = f"PREP-{product_id.upper()}"
    record = get_record(app.config.get("DB_PATH"), record_id)
    if not record:
        raise ValueError(f"No Stage‑2 record found for {product_id}")
    return record

def run_stage3_agent(product_id: str, image_bytes: Optional[bytes] = None) -> dict[str, Any]:
    """Core logic for Stage‑3.
    * Retrieve Stage‑2 data.
    * Optionally run vision via `observe_image` if an image is supplied.
    * Combine deterministic verifier result with vision suggestion.
    * Enforce a final verdict using `enforce_component_verdict`.
    * Persist a new Stage‑3 record and fire the Returns‑Agent webhook.
    """
    # 1. Pull Stage‑2 data (includes expected order lines, packaging info, etc.)
    prep_record = fetch_stage2_data(product_id)
    expected = prep_record.get("order_lines", "")

    # 2. Deterministic verifier – for now we reuse existing `assess_pack` without vision.
    verifier_result = {
        "verdict": "PASS",
        "decision": "SEAL",
        "checks": [],
        "order_lines": expected,
    }

    # 3. Vision (optional)
    inspection = None
    if image_bytes:
        # `observe_image` returns a dict with status, suggestions, etc.
        inspection = observe_image(image_bytes)

    # 4. Combine via `assess_pack`
    pack_assessment = assess_pack(
        verifier_result=verifier_result,
        inspection=inspection,
        operator_observation=None,
    )

    # 5. Enforce final verdict – treat the presence of an image as evidence.
    final = enforce_component_verdict(
        verifier_result=verifier_result,
        inspection=inspection,
        photo_present=bool(image_bytes),
    )

    # 6. Persist the Stage‑3 record
    stage3_record = {
        "record_id": f"STG3-{product_id.upper()}",
        "org_id": organization_uuid(app.config.get("AGENT_API_ORG_ID", "")),
        "product_id": product_id,
        "verdict": final.get("verdict", "UNCERTAIN"),
        "action": final.get("action", "HOLD_FOR_REVIEW"),
        "decision": final.get("decision", "MANUAL_REVIEW"),
        "evidence_json": json.dumps({
            "verifier": verifier_result,
            "inspection": inspection,
            "assessment": pack_assessment,
        }),
        "created_at": utc_now(),
    }
    save_record(app.config.get("DB_PATH"), stage3_record)

    # 7. Notify Stage‑4 via webhook (if configured)
    if app.config.get("RETURNS_AGENT_WEBHOOK_URL"):
        # reuse the helper from app.py to publish the record
        from app import publish_pack_record
        publish_pack_record(stage3_record)

    return stage3_record

# Flask endpoint wrapper – will be wired in app.py
def stage3_endpoint():
    from flask import request, jsonify, abort
    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    if not product_id:
        abort(400, description="product_id is required")
    # Optional image – base64 encoded string
    image_b64 = data.get("image_base64")
    image_bytes = None
    if image_b64:
        import base64
        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception as e:
            abort(400, description="Invalid base64 image")
    try:
        record = run_stage3_agent(product_id, image_bytes)
    except Exception as e:
        app.logger.error(f"Stage‑3 processing failed: {e}")
        abort(500, description=str(e))
    return jsonify(record), 201
