"""Build a unit passport from immutable CUBE records and business events.

CUBE 1.1 has no top-level unit_id. PackGuard therefore carries the stable unit
identity in check detail, while accepting shipment_id as a legacy fallback.
"""
from __future__ import annotations

from typing import Any

from fulfillment_routing import decide_fulfillment_route
from orchestrator import decide_unit_next_action

STAGES = ("receiving", "prep", "pack", "returns", "recovery")
OWNED_LOCALLY = {"prep", "pack"}
STAGE_LABELS = {
	"receiving": "Receiving", "prep": "Prep", "pack": "Pack",
	"returns": "Returns", "recovery": "Recovery",
}
DONE_DECISIONS = {"prep": {"pack"}, "pack": {"seal"}}


def record_unit_id(record: dict[str, Any]) -> str | None:
	"""Read unit identity from supported extensions and legacy shipment IDs."""
	subject = record.get("subject") or {}
	value = subject.get("unit_id")
	if isinstance(value, str) and value.strip():
		return value.strip()
	for check in record.get("checks", []):
		detail = check.get("detail") or {}
		value = detail.get("unit_id") if isinstance(detail, dict) else None
		if isinstance(value, str) and value.strip():
			return value.strip()
	# Older Prep handoffs used shipment_id as the Pack unit identifier.
	value = subject.get("shipment_id")
	return value.strip() if isinstance(value, str) and value.strip() else None


def _latest(records: list[dict[str, Any]], agent: str) -> dict[str, Any] | None:
	candidates = [r for r in records if r.get("agent") == agent]
	return max(candidates, key=lambda r: r.get("captured_at", "")) if candidates else None


def _record_state(agent: str, record: dict[str, Any]) -> str:
	if record.get("status") != "complete":
		return "pending"
	decision = (record.get("outcome") or {}).get("decision")
	if agent in DONE_DECISIONS:
		return "done" if decision in DONE_DECISIONS[agent] else "attention"
	verdicts = {c.get("verdict") for c in record.get("checks", [])}
	return "attention" if verdicts & {"fail", "uncertain"} else "done"


def _event_types(events: list[dict[str, Any]]) -> set[str]:
	return {str(event.get("event_type", "")).upper() for event in events}


def build_journey(
	order_id: str,
	channel: str,
	records: list[dict[str, Any]],
	*,
	unit_id: str | None = None,
	events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	route = decide_fulfillment_route(channel)
	all_for_order = [r for r in records if (r.get("subject") or {}).get("order_id") == order_id]
	known_units = {value for record in all_for_order if (value := record_unit_id(record))}
	if unit_id:
		unit_id = unit_id.strip()
	elif len(known_units) == 1:
		unit_id = next(iter(known_units))
	elif len(known_units) > 1:
		raise ValueError("This order has multiple units. Specify unit_id to open one passport at a time.")

	if unit_id:
		scoped = [
			record for record in all_for_order
			if record_unit_id(record) == unit_id or (record_unit_id(record) is None and len(known_units) <= 1)
		]
	else:
		scoped = all_for_order

	unit_events = [event for event in (events or []) if (event.get("details") or {}).get("order_id") in {None, order_id}]
	if unit_id:
		unit_events = [event for event in unit_events if (event.get("details") or {}).get("unit_id") in {None, unit_id}]
	types = _event_types(unit_events)
	skipped = route["skipped_agent"]
	stages: list[dict[str, Any]] = []
	for name in STAGES:
		agent_record = _latest(scoped, name)
		stage: dict[str, Any] = {
			"agent": name, "label": STAGE_LABELS[name], "record_id": None,
			"decision": None, "state": "awaiting",
			"connected": name in OWNED_LOCALLY or agent_record is not None,
		}
		if agent_record is not None:
			stage.update({
				"captured_at": agent_record.get("captured_at"),
				"operator": agent_record.get("operator_label"),
				"image_count": len(agent_record.get("images", [])),
				"checks": [{"key": c["check_key"], "verdict": c["verdict"]} for c in agent_record.get("checks", [])],
				"overridden": bool(agent_record.get("overrides")),
			})
		if name == skipped:
			stage["state"] = "skipped"
		elif name == "recovery":
			# A charge may be evaluated from any relevant upstream stage evidence.
			if "RECOVERY_CHARGE_RECEIVED" in types:
				upstream = any(_latest(scoped, source) for source in ("receiving", "prep", "pack", "returns"))
				stage["state"] = "ready" if upstream else "blocked"
				stage["connected"] = False  # Recovery is not implemented by this PackGuard pod.
		elif agent_record is not None:
			stage["state"] = _record_state(name, agent_record)
			stage["record_id"] = agent_record.get("record_id")
			stage["decision"] = (agent_record.get("outcome") or {}).get("decision")
		elif name == "receiving" and "PRODUCT_RECEIVED" in types:
			stage["state"] = "ready"
		elif name == "returns":
			if "RETURN_RECEIVED" in types:
				stage["state"] = "ready"
			elif "RETURN_INITIATED" in types or "CUSTOMER_RETURN_INITIATED" in types:
				stage["state"] = "in_transit"
		latest_failure = max((
			event for event in unit_events
			if str(event.get("event_type", "")).upper() in {"AGENT_FAILED", "AGENT_TIMEOUT"}
			and str((event.get("details") or {}).get("agent", "")).casefold() == name
		), key=lambda event: str(event.get("created_at", "")), default=None)
		recorded_at = str((agent_record or {}).get("captured_at", ""))
		if name != skipped and latest_failure and str(latest_failure.get("created_at", "")) >= recorded_at:
			stage["state"] = "attention"
			stage["failure_event"] = latest_failure.get("event_type")
		stages.append(stage)

	# Prep/Pack remain the forward managers; upstream receipt gating is expressed
	# separately as next_action so legacy clients can still read next_agent.
	forward = [s for s in stages if s["agent"] in OWNED_LOCALLY and s["state"] != "skipped"]
	blocked = next((s for s in forward if s["state"] in {"attention", "pending"}), None)
	upcoming = next((s for s in forward if s["state"] == "awaiting"), None)
	next_agent = None if blocked else (upcoming["agent"] if upcoming else None)
	evidence_refs = [
		{"agent": str(record.get("agent")), "record_id": str(record.get("record_id"))}
		for record in scoped
		if record.get("agent") in {"receiving", "prep", "pack", "returns"}
	]
	return {
		"order_id": order_id,
		"unit_id": unit_id,
		"fulfillment_channel": route["fulfillment_channel"],
		"stages": stages,
		"events": sorted(unit_events, key=lambda event: event.get("created_at", "")),
		"next_agent": next_agent,
		"next_action": decide_unit_next_action(stages, unit_events, route, evidence_refs),
		"evidence_refs": evidence_refs,
		"blocked_on": blocked["agent"] if blocked else None,
		"complete": all(s["state"] == "done" for s in forward),
	}
