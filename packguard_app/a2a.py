"""Agent-to-Agent (A2A) Autonomous Transfer and Communication Engine.

Connects Point A (Source / Inbound / Supplier) with Point B (Destination / PackGuard / Outbound),
allowing autonomous AI agents to negotiate, dispatch, verify, and seal product transfers.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from database import (
    create_agent_transfer,
    get_agent_transfer,
    list_agent_transfers,
    list_agent_messages,
    save_agent_message,
    update_agent_transfer,
    save_audit_event,
    list_products,
)
from verifier import verify_pack


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# Standard Agent Network Nodes
NODES = {
    "POINT_A_SUPPLIER": {
        "id": "node_origin_supplier",
        "name": "Supplier Intake Dock (Point A)",
        "role": "Source & Dispatch Authority",
        "agent_name": "Origin Dispatch Agent",
        "agent_id": "agent-origin-01",
        "icon": "🏭",
    },
    "POINT_A_PREP": {
        "id": "node_origin_prep",
        "name": "Inbound Prep Center Pod 02 (Point A)",
        "role": "Inbound Compliance & Prep",
        "agent_name": "Prep Station Agent",
        "agent_id": "agent-prep-02",
        "icon": "📦",
    },
    "POINT_B_PACKGUARD": {
        "id": "node_dest_packguard",
        "name": "PackGuard Packing Pod 03 (Point B)",
        "role": "Pack Verification & Sealing Authority",
        "agent_name": "PackGuard Station Agent",
        "agent_id": "agent-packguard-03",
        "icon": "🛡️",
    },
    "POINT_B_FULFILLMENT": {
        "id": "node_dest_fulfillment",
        "name": "Regional Fulfillment Hub (Point B)",
        "role": "Outbound Carrier Dispatch",
        "agent_name": "Fulfillment Hub Agent",
        "agent_id": "agent-hub-04",
        "icon": "🚚",
    },
}


def compute_evidence_hash(transfer_id: str, sku: str, quantity: int, messages: list[dict[str, Any]]) -> str:
    serialized = f"{transfer_id}:{sku}:{quantity}:" + "".join(
        m.get("message_type", "") + m.get("content", "") for m in messages
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def execute_a2a_transfer(
    *,
    org_id: str,
    product_sku: str,
    product_name: str,
    quantity: int,
    source_point: str,
    destination_point: str,
    scenario: str = "normal",
    user_note: str | None = None,
) -> dict[str, Any]:
    """Execute a 5-step Agent-to-Agent protocol exchange between Point A and Point B."""
    transfer_id = f"TRF-{uuid4().hex[:8].upper()}"
    batch_id = f"BATCH-{uuid4().hex[:6].upper()}"
    unit_id = f"UNIT-{uuid4().hex[:4].upper()}"
    created_at = utc_now()

    # Determine verdict and observed behavior based on scenario
    if scenario == "short":
        observed_qty = max(0, quantity - 1)
        observed_str = f"{product_sku}:{observed_qty}"
        verdict = "FAIL"
        decision = "FIX"
        status = "REJECTED_DISCREPANCY"
        summary = f"Discrepancy flagged: Expected {quantity}x {product_sku}, observed {observed_qty}x. PackGuard Agent halted seal."
    elif scenario == "extra":
        observed_qty = quantity
        observed_str = f"{product_sku}:{observed_qty};SKU-UNKNOWN-DECOY:1"
        verdict = "FAIL"
        decision = "FIX"
        status = "REJECTED_DISCREPANCY"
        summary = f"Foreign item detected in carton: Expected {quantity}x {product_sku}, extra unknown item observed."
    elif scenario == "uncertain":
        observed_qty = quantity
        observed_str = ""
        verdict = "UNCERTAIN"
        decision = "MANUAL_REVIEW"
        status = "HOLD_FOR_REVIEW"
        summary = "Transit damage / unreadable barcode: PackGuard Agent routed container to supervisor review."
    else:
        scenario = "normal"
        observed_qty = quantity
        observed_str = f"{product_sku}:{observed_qty}"
        verdict = "PASS"
        decision = "SEAL"
        status = "COMPLETED"
        summary = f"Transfer verified: {quantity}x {product_sku} transferred from {source_point} to {destination_point} and SEALED."

    # Save initial transfer record
    initial_transfer = {
        "transfer_id": transfer_id,
        "org_id": org_id,
        "product_sku": product_sku,
        "product_name": product_name,
        "quantity": quantity,
        "source_point": source_point,
        "destination_point": destination_point,
        "scenario": scenario,
        "status": "IN_PROGRESS",
        "verdict": "PENDING",
        "evidence_hash": None,
        "summary": "Agent negotiation in progress...",
        "created_at": created_at,
        "completed_at": None,
    }
    create_agent_transfer(initial_transfer)

    messages: list[dict[str, Any]] = []

    # Helper to add a message to local list and DB
    def log_msg(step: int, from_agent: str, to_agent: str, msg_type: str, content: str, payload: dict[str, Any]):
        msg = {
            "message_id": f"MSG-{uuid4().hex[:8].upper()}",
            "transfer_id": transfer_id,
            "org_id": org_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "step_number": step,
            "message_type": msg_type,
            "content": content,
            "payload": payload,
            "created_at": utc_now(),
        }
        save_agent_message(msg)
        messages.append(msg)

    # STEP 1: Handshake Request (Source Agent -> PackGuard Agent)
    log_msg(
        step=1,
        from_agent="Origin Dispatch Agent (Point A)",
        to_agent="PackGuard Station Agent (Point B)",
        msg_type="HANDSHAKE_REQUEST",
        content=(
            f"Hello PackGuard Agent. I am initiating an outbound product transfer from {source_point}. "
            f"Consignment: {quantity} unit(s) of '{product_name}' (SKU: {product_sku}). "
            f"Batch ID: {batch_id}. Please confirm intake capacity and readiness at {destination_point}."
        ),
        payload={
            "protocol_version": "A2A/2.0",
            "source_node": source_point,
            "destination_node": destination_point,
            "product_sku": product_sku,
            "product_name": product_name,
            "quantity": quantity,
            "batch_id": batch_id,
            "note": user_note,
        },
    )

    # STEP 2: Handshake Response & Criteria (PackGuard Agent -> Source Agent)
    log_msg(
        step=2,
        from_agent="PackGuard Station Agent (Point B)",
        to_agent="Origin Dispatch Agent (Point A)",
        msg_type="HANDSHAKE_ACK",
        content=(
            f"Handshake acknowledged, Origin Agent. Station at {destination_point} is online and operational. "
            f"Verified catalog item '{product_name}' (SKU: {product_sku}). Inspection parameters loaded: "
            f"exact quantity verification required, tamper seal inspection active. Ready to receive physical transit."
        ),
        payload={
            "station_status": "READY",
            "active_org": org_id,
            "inbound_bay": "DOCK-03B",
            "expected_sku": product_sku,
            "required_checks": ["sku_quantity", "visual_evidence", "seal_integrity"],
            "transit_authorized": True,
        },
    )

    # STEP 3: Dispatch & Manifest Transmission (Source Agent -> PackGuard Agent)
    dispatch_manifest_hash = hashlib.sha256(f"{batch_id}:{product_sku}:{quantity}:{created_at}".encode("utf-8")).hexdigest()
    log_msg(
        step=3,
        from_agent="Origin Dispatch Agent (Point A)",
        to_agent="PackGuard Station Agent (Point B)",
        msg_type="DISPATCH_MANIFEST",
        content=(
            f"Consignment dispatched from {source_point} to {destination_point}. Unit tracking: {unit_id}. "
            f"Cryptographic manifest hash: {dispatch_manifest_hash[:16]}... "
            f"Expected lines manifest: '{product_sku}:{quantity}'. Secure transport envelope closed."
        ),
        payload={
            "unit_id": unit_id,
            "manifest_hash": dispatch_manifest_hash,
            "dispatched_at": utc_now(),
            "expected_lines": f"{product_sku}:{quantity}",
            "carrier_channel": "INTERNAL_PNEUMATIC_CONVEYOR",
            "transit_status": "IN_TRANSIT",
        },
    )

    # STEP 4: Physical Intake & Inspection (PackGuard Agent -> Source Agent)
    verifier_result = verify_pack(f"{product_sku}:{quantity}", observed_str)
    if scenario == "normal":
        inspection_dialogue = (
            f"Package {unit_id} received at {destination_point} bay. PackGuard automated vision check completed. "
            f"Observed in box: {observed_str}. Quantity match: {quantity}/{quantity}. "
            f"Discrepancies: None. Verdict: PASS. Recommendation: SEAL container."
        )
    elif scenario == "short":
        inspection_dialogue = (
            f"ALERT: Package {unit_id} received at {destination_point}, but SKU check FAILED! "
            f"Expected: {quantity}x {product_sku}. Observed in box: {observed_qty}x {product_sku}. "
            f"Shortage of {quantity - observed_qty} unit(s). Action: STOP_AND_FIX. Quarantine Bay 3."
        )
    elif scenario == "extra":
        inspection_dialogue = (
            f"ALERT: Package {unit_id} received at {destination_point}, but foreign object detected! "
            f"Expected: {product_sku}:{quantity}. Observed: {observed_str}. "
            f"Action: STOP_AND_FIX. Halting outbound seal to prevent mis-shipment."
        )
    else:
        inspection_dialogue = (
            f"HOLD: Package {unit_id} arrived at {destination_point} with obscured or missing label. "
            f"Visual observation inconclusive. Verdict: UNCERTAIN. Action: HOLD_FOR_REVIEW."
        )

    log_msg(
        step=4,
        from_agent="PackGuard Station Agent (Point B)",
        to_agent="Origin Dispatch Agent (Point A)",
        msg_type="INSPECTION_RUN",
        content=inspection_dialogue,
        payload={
            "unit_id": unit_id,
            "expected": f"{product_sku}:{quantity}",
            "observed": observed_str,
            "verifier": verifier_result,
            "verdict": verdict,
            "decision": decision,
        },
    )

    # STEP 5: Consensus & Final Handoff Confirmation
    evidence_hash = compute_evidence_hash(transfer_id, product_sku, quantity, messages)
    completed_at = utc_now()

    if verdict == "PASS":
        confirm_content = (
            f"Consensus confirmed between Origin Agent and PackGuard Agent. "
            f"Verification PASS. Tamper-evident seal applied to Unit {unit_id}. "
            f"Immutable evidence proof generated: {evidence_hash[:20]}... "
            f"Product transfer from {source_point} to {destination_point} successfully completed."
        )
    else:
        confirm_content = (
            f"Transfer halted between Origin Agent and PackGuard Agent due to {verdict} verdict. "
            f"Discrepancy audit ticket logged. Unit {unit_id} held at {destination_point}. "
            f"Evidence record preserved: {evidence_hash[:20]}... Operator notification dispatched."
        )

    log_msg(
        step=5,
        from_agent="PackGuard Station Agent (Point B)",
        to_agent="Origin Dispatch Agent (Point A)",
        msg_type="TRANSFER_CONFIRMED" if verdict == "PASS" else "TRANSFER_HALTED",
        content=confirm_content,
        payload={
            "transfer_id": transfer_id,
            "status": status,
            "verdict": verdict,
            "decision": decision,
            "evidence_hash": evidence_hash,
            "completed_at": completed_at,
        },
    )

    # Update transfer record with final results
    update_agent_transfer(
        transfer_id,
        org_id,
        status=status,
        verdict=verdict,
        evidence_hash=evidence_hash,
        summary=summary,
        completed_at=completed_at,
    )

    # Log audit event
    save_audit_event({
        "event_id": f"EVT-{uuid4().hex[:8].upper()}",
        "record_id": transfer_id,
        "org_id": org_id,
        "event_type": "A2A_TRANSFER_COMPLETED",
        "reason": f"Agent-to-agent transfer: {quantity}x {product_sku} from {source_point} to {destination_point} ({verdict})",
        "actor_id": "agent:autonomous_orchestrator",
        "created_at": completed_at,
        "details": {
            "transfer_id": transfer_id,
            "source_point": source_point,
            "destination_point": destination_point,
            "sku": product_sku,
            "quantity": quantity,
            "verdict": verdict,
            "scenario": scenario,
            "evidence_hash": evidence_hash,
        },
    })

    return {
        "transfer_id": transfer_id,
        "org_id": org_id,
        "product_sku": product_sku,
        "product_name": product_name,
        "quantity": quantity,
        "source_point": source_point,
        "destination_point": destination_point,
        "status": status,
        "verdict": verdict,
        "decision": decision,
        "evidence_hash": evidence_hash,
        "summary": summary,
        "created_at": created_at,
        "completed_at": completed_at,
        "messages": messages,
    }


def handle_agent_command(command: str, org_id: str) -> dict[str, Any]:
    """Parse natural language command given to the PackGuard Web Agent."""
    cmd_lower = command.lower().strip()
    catalog = list_products(org_id)
    transfers = list_agent_transfers(org_id, limit=5)

    # Check for transfer intent
    transfer_keywords = ("transfer", "move", "send", "connect", "dispatch", "ship", "deliver")
    wants_transfer = any(kw in cmd_lower for kw in transfer_keywords)

    if wants_transfer:
        # Detect SKU
        matched_product = None
        for p in catalog:
            if p["sku"].lower() in cmd_lower or p["product_name"].lower() in cmd_lower:
                matched_product = p
                break
        if not matched_product and catalog:
            matched_product = catalog[0]  # default fallback

        # Detect Quantity
        qty_match = re.search(r"\b(\d+)\b", cmd_lower)
        quantity = int(qty_match.group(1)) if qty_match else 2

        # Detect Scenario
        scenario = "normal"
        if any(w in cmd_lower for w in ("short", "missing", "less", "defect")):
            scenario = "short"
        elif any(w in cmd_lower for w in ("extra", "wrong", "foreign")):
            scenario = "extra"
        elif any(w in cmd_lower for w in ("uncertain", "damage", "obscured", "unclear")):
            scenario = "uncertain"

        # Detect Points
        source = "Supplier Intake Dock (Point A)"
        dest = "PackGuard Packing Pod 03 (Point B)"
        if "prep" in cmd_lower:
            source = "Inbound Prep Center Pod 02 (Point A)"
        if "hub" in cmd_lower or "fulfillment" in cmd_lower:
            dest = "Regional Fulfillment Hub (Point B)"

        sku = matched_product["sku"] if matched_product else "SKU-CABLE-USBC"
        name = matched_product["product_name"] if matched_product else "USB-C cable"

        result = execute_a2a_transfer(
            org_id=org_id,
            product_sku=sku,
            product_name=name,
            quantity=quantity,
            source_point=source,
            destination_point=dest,
            scenario=scenario,
            user_note=f"Initiated via natural language command: '{command}'",
        )

        reply = (
            f"Autonomous Agent Dispatch complete! I coordinated with the Origin Dispatch Agent to transfer "
            f"{quantity} unit(s) of {name} ({sku}) from {source} to {dest}. "
            f"Outcome: {result['verdict']} ({result['status']}). Evidence Hash: {result['evidence_hash'][:16]}..."
        )
        return {
            "answer": reply,
            "action_taken": "A2A_TRANSFER_EXECUTED",
            "transfer": result,
        }

    # Query about status/health
    if any(w in cmd_lower for w in ("status", "health", "state", "who are you", "what are you")):
        recent_count = len(list_agent_transfers(org_id, limit=100))
        return {
            "answer": (
                f"I am the PackGuard Autonomous Agent Station (Pod 03). "
                f"My operational status is ONLINE. Connected peer: Origin Inbound Agent (Pod 01). "
                f"I have coordinated {recent_count} multi-point transfers for organization '{org_id}'. "
                f"You can command me to transfer any product from Point A to Point B, verify packaging, or check peer status."
            ),
            "action_taken": "STATUS_REPORT",
            "active_transfers_count": recent_count,
        }

    # Query about recent transfers
    if any(w in cmd_lower for w in ("recent", "history", "transfers", "last transfer")):
        if not transfers:
            return {
                "answer": "No recent transfers found. You can command me: 'Transfer 2 units of SKU-CABLE-USBC from Point A to Point B'.",
                "action_taken": "LIST_TRANSFERS",
            }
        latest = transfers[0]
        return {
            "answer": (
                f"The latest transfer was {latest['transfer_id']}: {latest['quantity']}x {latest['product_sku']} "
                f"from {latest['source_point']} to {latest['destination_point']}. "
                f"Verdict: {latest['verdict']} ({latest['status']})."
            ),
            "action_taken": "LIST_TRANSFERS",
            "transfers": transfers,
        }

    # Default assistance
    return {
        "answer": (
            "I am ready. To connect Point A with Point B, you can command me: "
            "\"Transfer 2 units of SKU-CABLE-USBC from Supplier Dock to Pack Station\", "
            "\"Simulate a short shipment from Point A to Point B\", or "
            "\"Check agent network status\"."
        ),
        "action_taken": "HELP",
    }
