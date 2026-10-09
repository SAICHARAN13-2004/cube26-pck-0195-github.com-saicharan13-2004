from fulfillment_routing import decide_fulfillment_route
from orchestrator import decide_unit_next_action
from unit_journey import build_journey


def test_fulfillment_routes_skip_only_the_other_forward_manager():
	assert decide_fulfillment_route("Amazon FBA")["next_agent"] == "prep"
	assert decide_fulfillment_route("Amazon FBA")["skipped_agent"] == "pack"
	assert decide_fulfillment_route("merchant fulfilled")["next_agent"] == "pack"
	assert decide_fulfillment_route("3PL")["skipped_agent"] == "prep"


def test_unknown_fulfillment_channel_is_rejected():
	try:
		decide_fulfillment_route("unknown")
	except ValueError as error:
		assert "FBA, MFN, or 3PL" in str(error)
	else:
		raise AssertionError("unknown fulfillment route should not be accepted")


def test_router_waits_for_product_received_event():
	result = decide_unit_next_action([], [], decide_fulfillment_route("MFN"))
	assert result["kind"] == "wait_for_event"
	assert result["event"] == "PRODUCT_RECEIVED"


def test_return_inspection_waits_for_physical_arrival():
	stages = [
		{"agent": "receiving", "state": "done", "connected": True},
		{"agent": "prep", "state": "skipped", "connected": True},
		{"agent": "pack", "state": "done", "connected": True},
	]
	route = decide_fulfillment_route("MFN")
	result = decide_unit_next_action(
		stages, [{"event_type": "PRODUCT_RECEIVED"}, {"event_type": "RETURN_INITIATED"}],
		route, [{"agent": "pack", "record_id": "PACK-1"}],
	)
	assert result["kind"] == "wait_for_event"
	assert result["event"] == "RETURN_RECEIVED"


def test_unit_passport_scopes_records_by_order_and_stable_unit():
	prep_record = {
		"record_id": "PREP-1", "agent": "prep", "status": "complete", "captured_at": "2026-10-01T10:00:00Z",
		"subject": {"order_id": "ORD-1", "shipment_id": "UNIT-1"},
		"outcome": {"decision": "pack"}, "checks": [], "images": [], "overrides": [],
	}
	journey = build_journey("ORD-1", "FBA", [prep_record], events=[{"event_type": "PRODUCT_RECEIVED"}])
	assert journey["unit_id"] == "UNIT-1"
	assert journey["fulfillment_channel"] == "FBA"
	assert next(stage for stage in journey["stages"] if stage["agent"] == "pack")["state"] == "skipped"
	assert journey["next_action"]["kind"] == "handoff_required"
	assert journey["next_action"]["agent"] == "receiving"


def test_unit_passport_page_is_available_to_signed_in_operators():
	import app as packguard_app
	client = packguard_app.app.test_client()
	client.post("/login", data={"username": "alpha.operator", "password": "alpha-demo"})
	page = client.get("/unit-passport")
	assert page.status_code == 200
	assert b"Unit passport" in page.data
	assert b"Open passport" in page.data

	missing_order = client.get("/api/unit-passport")
	assert missing_order.status_code == 400


def test_machine_unit_event_ingest_is_authenticated_and_retry_safe(monkeypatch):
	import app as packguard_app

	monkeypatch.setattr(packguard_app, "UNIT_EVENT_API_TOKEN", "a" * 40)
	monkeypatch.setattr(packguard_app, "AGENT_API_ORG_ID", "org_demo_alpha")
	client = packguard_app.app.test_client()
	payload = {
		"event_id": "evt-product-received-1", "unit_id": "UNIT-1", "order_id": "ORD-1",
		"event_type": "PRODUCT_RECEIVED", "details": {"dock": "A1"},
	}
	assert client.post("/v1/agent/events", json=payload).status_code == 401
	headers = {"Authorization": f"Bearer {'a' * 40}"}
	created = client.post("/v1/agent/events", json=payload, headers=headers)
	assert created.status_code == 201
	assert client.post("/v1/agent/events", json=payload, headers=headers).get_json()["status"] == "duplicate"
	changed = {**payload, "details": {"dock": "B2"}}
	assert client.post("/v1/agent/events", json=changed, headers=headers).status_code == 409
	operator_client = packguard_app.app.test_client()
	operator_client.post("/login", data={"username": "alpha.operator", "password": "alpha-demo"})
	passport = operator_client.get("/api/unit-passport?order_id=ORD-1&unit_id=UNIT-1&channel=MFN")
	assert passport.status_code == 200
	assert any(event["event_type"] == "PRODUCT_RECEIVED" for event in passport.get_json()["events"])
