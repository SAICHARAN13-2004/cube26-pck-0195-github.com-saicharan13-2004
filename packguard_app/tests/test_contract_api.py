import json
from io import BytesIO
from threading import Event
from uuid import uuid4

import app as packguard_app
from contract import build_record, organization_uuid
from database import connect, get_contract_record, list_audit_events, save_contract_record


class FakePrivateStorage:
	backend = "s3"

	def __init__(self):
		self.objects = {}

	def presign_put(self, key, content_type, expires=900):
		return f"https://private-storage.test/{key}?signed=token"

	def read(self, key):
		return self.objects.get(key)

	def save(self, key, upload, _content_type=None):
		self.objects[key] = upload.read()
		return key


def _login(client, username="alpha.operator", password="alpha-demo"):
	return client.post("/login", data={"username": username, "password": password})


def _payload():
	return {
		"agent": "pack",
		"client_id": None,
		"subject": {
			"type": "order", "asin": None, "sku": "SKU-CABLE-USBC", "order_id": "ORD-CUBE-1",
			"po_line_id": None, "shipment_id": "SHIP-CUBE-1", "quantity_expected": 1,
			"quantity_observed": 1,
		},
		"image_slots": [
			{"slot": "overview", "content_type": "image/jpeg"},
			{"slot": "labels", "content_type": "image/jpeg"},
		],
		"expected_lines": "SKU-CABLE-USBC:1",
		"observed_contents": "SKU-CABLE-USBC:1",
	}


def _save_prep_record():
	record = build_record(
		organization_key="org_demo_alpha",
		client_id=None,
		agent="prep",
		subject={
			"type": "order", "asin": None, "sku": "SKU-CABLE-USBC", "order_id": "ORD-CUBE-1",
			"po_line_id": None, "shipment_id": "SHIP-CUBE-1", "quantity_expected": 1,
			"quantity_observed": 1,
		},
		operator_label="prep-agent",
		images=[],
		checks=[{
			"check_key": "label_match", "verdict": "pass", "confidence": None,
			"detail": {
				"reason": "Label matches the prep work order.",
				"logistics": {
					"supplier_name": "Stage Two Supplier",
					"origin_address": "12 Prep Source Way, Reno, NV",
					"ordered_for": "North Shop",
					"delivery_address": "34 Pack Destination Rd, Sacramento, CA",
				},
			},
			"model_version": "test-prep-v1", "latency_ms": 0,
		}],
		decision="pack",
		decided_by="agent",
		status="complete",
	)
	with connect("org_demo_alpha") as connection:
		connection.execute(
			"INSERT INTO contract_records (record_id, organization_id, agent, captured_at, status, record_json) VALUES (?, ?, ?, ?, ?, ?)",
			(record["record_id"], organization_uuid("org_demo_alpha"), "prep", record["captured_at"], record["status"], json.dumps(record)),
		)
	return record


def test_contract_capture_completes_one_batched_call_and_emits_exact_record(monkeypatch):
	storage = FakePrivateStorage()
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	monkeypatch.setattr(packguard_app, "RETURNS_AGENT_WEBHOOK_URL", "https://returns-agent.test/v1/records")
	monkeypatch.setattr(packguard_app, "RETURNS_AGENT_WEBHOOK_TOKEN", "r" * 48)
	monkeypatch.setattr(packguard_app, "AGENT_API_ORG_ID", "org_demo_alpha")
	prep_record = _save_prep_record()
	vision_called = Event()
	webhook_called = Event()
	captured = {}
	delivered = {}

	def fake_returns_webhook(webhook_request, timeout=10):
		delivered["url"] = webhook_request.full_url
		delivered["headers"] = dict(webhook_request.header_items())
		delivered["record"] = json.loads(webhook_request.data.decode("utf-8"))
		webhook_called.set()
		return BytesIO(b"{}")

	monkeypatch.setattr(packguard_app, "urlopen", fake_returns_webhook)

	def fake_vision(photo_bytes, **kwargs):
		captured["bytes"] = [photo_bytes, *[item[0] for item in kwargs["additional_images"]]]
		captured["context"] = kwargs["capture_context"]
		vision_called.set()
		return {
			"status": "SUGGESTIONS_READY_UNCALIBRATED", "provider": "test-model:1",
			"inference_ms": 17, "token_usage": {"prompt": 90, "completion": 20},
			"detected_items": [{"sku": "SKU-CABLE-USBC", "quantity": 1, "confidence": 0.96, "variant_match": True}], "extra_items": [],
			"image_quality": {"status": "GOOD", "score": 0.9}, "occlusion": {"status": "CLEAR"},
			"uncertainties": [], "confidence": 0.96,
		}
	monkeypatch.setattr(packguard_app, "inspect_image", fake_vision)
	record_updated = Event()
	original_update = packguard_app.update_contract_record
	def update_record_and_signal(record, organization_key):
		updated = original_update(record, organization_key)
		record_updated.set()
		return updated
	monkeypatch.setattr(packguard_app, "update_contract_record", update_record_and_signal)
	client = packguard_app.app.test_client()
	_login(client)
	alerts_page = client.get("/alerts")
	assert alerts_page.status_code == 200
	assert prep_record["record_id"].encode() in alerts_page.data
	assert b"Start pack capture" in alerts_page.data
	capture_page = client.get(f"/capture/contract?prep_record_id={prep_record['record_id']}")
	assert capture_page.status_code == 200
	assert b'value="ORD-CUBE-1"' in capture_page.data
	assert b'value="SKU-CABLE-USBC"' in capture_page.data
	assert prep_record["record_id"].encode() in capture_page.data
	payload = _payload()
	payload["source_record_id"] = prep_record["record_id"]
	tampered_payload = _payload()
	tampered_payload["source_record_id"] = prep_record["record_id"]
	tampered_payload["subject"]["quantity_expected"] = 2
	tampered_payload["expected_lines"] = "SKU-CABLE-USBC:2"
	assert client.post("/v1/captures", json=tampered_payload).status_code == 400
	bravo_client = packguard_app.app.test_client()
	_login(bravo_client, "bravo.operator", "bravo-demo")
	assert bravo_client.post("/v1/captures", json=payload).status_code == 400
	created = client.post("/v1/captures", json=payload)
	assert created.status_code == 201
	response = created.get_json()
	assert len(response["upload_urls"]) == 2
	for item in response["upload_urls"]:
		storage.objects[item["key"]] = b"\xff\xd8\xff" + item["slot"].encode("ascii")
	completed = client.post(
		f"/v1/captures/{response['capture_id']}/complete",
		json={"images": [{"key": item["key"], "taken_at": "2026-09-30T10:00:00Z"} for item in response["upload_urls"]]},
	)
	assert completed.status_code == 201
	assert completed.get_json()["status"] == "pending"
	assert vision_called.wait(2)
	assert record_updated.wait(2)
	assert webhook_called.wait(2)
	record_id = completed.get_json()["record_id"]
	public_record = packguard_app.app.test_client().get(f"/v1/records/{record_id}")
	assert public_record.status_code == 200
	record = public_record.get_json()
	assert set(record) == {
		"record_id", "schema_version", "organization_id", "client_id", "agent", "subject",
		"captured_at", "operator_label", "images", "checks", "outcome", "overrides", "status", "content_hash",
	}
	assert record["schema_version"] == "1.1"
	assert record["status"] == "complete"
	assert record["agent"] == "pack"
	assert delivered["url"] == "https://returns-agent.test/v1/records"
	assert delivered["headers"]["Authorization"] == f"Bearer {'r' * 48}"
	assert delivered["headers"]["Idempotency-key"] == record_id
	assert delivered["record"] == record
	assert len(record["images"]) == 2
	pack_alerts = client.get("/alerts")
	assert record_id.encode() in pack_alerts.data
	assert f'href="/v1/records/{record_id}"'.encode() in pack_alerts.data
	sku_check = next(check for check in record["checks"] if check["check_key"] == "sku_quantity")
	assert sku_check["detail"]["source_prep_record_id"] == prep_record["record_id"]
	pack_records = client.get("/v1/records?agent=pack").get_json()["records"]
	assert any(item["record_id"] == record_id for item in pack_records)
	handoff_events = list_audit_events(record_id, "org_demo_alpha")
	assert any(event["event_type"] == "RETURNS_AGENT_DELIVERED" for event in handoff_events)
	assert len(captured["bytes"]) == 2
	assert captured["context"]["check_keys"] == ["sku_quantity", "visual_evidence"]
	visual_check = next(check for check in record["checks"] if check["check_key"] == "visual_evidence")
	assert visual_check["detail"]["token_usage"] == {"prompt": 90, "completion": 20}
	assert visual_check["latency_ms"] == 17
	assert visual_check["detail"]["candidate_decision"] == "SEAL"
	assert visual_check["detail"]["automatic_seal_authorized"] is False
	assert delivered["record"]["checks"][1]["detail"]["candidate_decision"] == "SEAL"
	image_response = packguard_app.app.test_client().get(f"/v1/records/{record_id}/images/{record['images'][0]['key']}")
	assert image_response.status_code == 200
	assert image_response.data.startswith(b"\xff\xd8\xff")
	vision_called.clear()
	record_updated.clear()
	second_payload = _payload()
	second_payload["subject"]["order_id"] = "ORD-CUBE-2"
	second_capture = client.post("/v1/captures", json=second_payload).get_json()
	for item in second_capture["upload_urls"]:
		storage.objects[item["key"]] = b"\xff\xd8\xff" + item["slot"].encode("ascii")
	second_record = client.post(
		f"/v1/captures/{second_capture['capture_id']}/complete",
		json={"images": [{"key": item["key"], "taken_at": "2026-09-30T10:00:00Z"} for item in second_capture["upload_urls"]]},
	).get_json()
	assert record_updated.wait(2)
	first_page = client.get("/v1/records?agent=pack&limit=1").get_json()
	assert len(first_page["records"]) == 1
	assert first_page["next_cursor"]
	second_page = client.get(f"/v1/records?agent=pack&limit=1&cursor={first_page['next_cursor']}").get_json()
	assert len(second_page["records"]) == 1
	assert first_page["records"][0]["record_id"] != second_page["records"][0]["record_id"]


def test_contract_reads_and_images_are_tenant_scoped_and_overrides_append(monkeypatch):
	storage = FakePrivateStorage()
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	monkeypatch.setattr(packguard_app, "inspect_image", lambda *_args, **_kwargs: {
		"status": "SUGGESTIONS_READY_UNCALIBRATED", "provider": "test", "inference_ms": 1,
		"token_usage": {"prompt": 1, "completion": 1}, "detected_items": [], "extra_items": [],
		"image_quality": {"status": "GOOD"}, "occlusion": {"status": "CLEAR"}, "uncertainties": [],
	})
	alpha = packguard_app.app.test_client()
	bravo = packguard_app.app.test_client()
	_login(alpha)
	created = alpha.post("/v1/captures", json=_payload()).get_json()
	for item in created["upload_urls"]:
		storage.objects[item["key"]] = b"\xff\xd8\xff" + item["slot"].encode("ascii")
	completed = alpha.post(
		f"/v1/captures/{created['capture_id']}/complete",
		json={"images": [{"key": item["key"], "taken_at": "2026-09-30T10:00:00Z"} for item in created["upload_urls"]]},
	).get_json()
	assert packguard_app.app.test_client().get(f"/v1/records/{completed['record_id']}").status_code == 200
	_login(bravo, "bravo.operator", "bravo-demo")
	assert bravo.get(f"/v1/records/{completed['record_id']}").status_code == 404
	assert bravo.get("/v1/records?agent=pack").get_json()["records"] == []
	assert bravo.get(f"/v1/records/{completed['record_id']}/images/{created['upload_urls'][0]['key']}").status_code == 404
	response = alpha.post(
		f"/v1/records/{completed['record_id']}/overrides",
		json={"check_key": "sku_quantity", "to_verdict": "uncertain", "reason": "Operator requested a second check."},
	)
	assert response.status_code == 201
	updated = packguard_app.app.test_client().get(f"/v1/records/{completed['record_id']}").get_json()
	assert len(updated["overrides"]) == 1
	assert updated["outcome"]["decided_by"] == "operator"
	assert updated["outcome"]["decision"] == "manual_review"
	assert updated["checks"][0]["verdict"] == "pass"


def test_image_only_single_sku_capture_uses_presence_agent_without_manual_contents(monkeypatch):
	storage = FakePrivateStorage()
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	vision_called = Event()
	record_updated = Event()
	captured = {}

	def fake_vision(_photo_bytes, **kwargs):
		captured.update(kwargs)
		vision_called.set()
		return {
			"status": "SUGGESTIONS_READY_UNCALIBRATED", "provider": "ollama:moondream:1.8b",
			"presence_hint": "MATCH", "inference_ms": 800,
			"detected_items": [{"sku": "SKU-CABLE-USBC", "quantity": None}],
			"extra_items": [], "image_quality": {"status": "UNKNOWN"},
			"uncertainties": ["Presence identified; quantity is not measured."],
			"confidence": None,
		}

	monkeypatch.setattr(packguard_app, "inspect_image", fake_vision)
	original_update = packguard_app.update_contract_record
	def update_record_and_signal(record, organization_key):
		updated = original_update(record, organization_key)
		record_updated.set()
		return updated
	monkeypatch.setattr(packguard_app, "update_contract_record", update_record_and_signal)
	client = packguard_app.app.test_client()
	_login(client)
	payload = _payload()
	payload["observed_contents"] = ""
	payload["subject"]["quantity_observed"] = None
	created = client.post("/v1/captures", json=payload).get_json()
	for item in created["upload_urls"]:
		storage.objects[item["key"]] = b"\xff\xd8\xff" + item["slot"].encode("ascii")
	completed = client.post(
		f"/v1/captures/{created['capture_id']}/complete",
		json={"images": [{"key": item["key"], "taken_at": "2026-10-02T10:00:00Z"} for item in created["upload_urls"]]},
	)
	assert completed.status_code == 201
	assert vision_called.wait(2)
	assert record_updated.wait(2)
	assert captured["model_override"] == "moondream:1.8b"
	assert captured["force_structured"] is False
	assert [product["sku"] for product in captured["catalog_products"]] == ["SKU-CABLE-USBC"]
	record = packguard_app.app.test_client().get(f"/v1/records/{completed.get_json()['record_id']}").get_json()
	visual_check = next(check for check in record["checks"] if check["check_key"] == "visual_evidence")
	assert visual_check["detail"]["presence_hint"] == "MATCH"
	assert visual_check["detail"]["detected_items"][0]["quantity"] is None
	assert visual_check["detail"]["candidate_decision"] == "MANUAL_REVIEW"
	assert visual_check["detail"]["automatic_seal_authorized"] is False
	assert record["outcome"]["decision"] == "manual_review"


def test_contract_capture_surface_requires_login_and_direct_storage(monkeypatch):
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", type("LocalStorage", (), {"backend": "local"})())
	anonymous = packguard_app.app.test_client()
	assert anonymous.get("/capture/contract").status_code == 302
	client = packguard_app.app.test_client()
	_login(client)
	page = client.get("/capture/contract")
	assert page.status_code == 200
	assert b"Shot 1" in page.data
	assert b"Shot 2" in page.data
	assert b"private S3-compatible storage is configured" in page.data
	assert b'id="contract-submit" class="button button-primary button-wide" type="submit" disabled' in page.data


def test_new_check_prefills_and_persists_stage2_route_from_prep_record(monkeypatch):
	storage = FakePrivateStorage()
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	monkeypatch.setattr(packguard_app, "AGENT_API_ORG_ID", "org_demo_alpha")
	monkeypatch.setattr(packguard_app, "RETURNS_AGENT_WEBHOOK_URL", "https://returns-agent.test/v1/records")
	monkeypatch.setattr(packguard_app, "RETURNS_AGENT_WEBHOOK_TOKEN", "r" * 48)
	monkeypatch.setattr(packguard_app, "PACK_FEED_AGENT_API_TOKEN", "f" * 48)
	prep_record = _save_prep_record()
	monkeypatch.setattr(packguard_app, "inspect_image", lambda *_args, **_kwargs: {
		"status": "SUGGESTIONS_READY_UNCALIBRATED", "provider": "test:moondream",
		"presence_hint": "MATCH", "detected_items": [{"sku": "SKU-CABLE-USBC", "quantity": None}],
		"extra_items": [], "image_quality": {"status": "UNKNOWN"},
		"uncertainties": ["Presence only; quantity is not measured."],
	})
	deliveries = []
	def send_to_returns(webhook_request, timeout=10):
		deliveries.append(json.loads(webhook_request.data.decode("utf-8")))
		return BytesIO(b"{}")
	monkeypatch.setattr(packguard_app, "urlopen", send_to_returns)
	client = packguard_app.app.test_client()
	_login(client)
	page = client.get(f"/capture?prep_record_id={prep_record['record_id']}")
	assert page.status_code == 200
	assert prep_record["record_id"].encode() in page.data
	assert b"ORD-CUBE-1" in page.data
	assert b"SKU-CABLE-USBC:1" in page.data
	assert b"12 Prep Source Way, Reno, NV" in page.data
	assert b"North Shop" in page.data
	response = client.post(
		"/capture",
		data={
			"source_prep_record_id": prep_record["record_id"],
			"operator_id": "alpha.operator",
			"observed_in_box": "",
			"photo": (BytesIO(b"\xff\xd8\xfftest-image"), "open-box.jpg"),
		},
		content_type="multipart/form-data",
	)
	assert response.status_code == 302
	record_id = response.headers["Location"].rsplit("/", 1)[-1].split("?", 1)[0]
	saved = next(item for item in client.get("/api/records").get_json()["records"] if item["record_id"] == record_id)
	assert saved["order_id"] == "ORD-CUBE-1"
	assert saved["unit_id"] == "SHIP-CUBE-1"
	assert saved["origin_address"] == "12 Prep Source Way, Reno, NV"
	assert saved["delivery_address"] == "34 Pack Destination Rd, Sacramento, CA"
	assert saved["evidence"]["source"]["source_prep_record_id"] == prep_record["record_id"]
	assert saved["evidence"]["source"]["product_routes"][0]["ordered_for"] == "North Shop"
	pack_contract_id = saved["evidence"]["source"]["stage3_contract_record_id"]
	pack_contract = get_contract_record(pack_contract_id, "org_demo_alpha")
	assert pack_contract["agent"] == "pack"
	assert pack_contract["outcome"]["decision"] == "manual_review"
	sku_check = next(check for check in pack_contract["checks"] if check["check_key"] == "sku_quantity")
	assert sku_check["detail"]["source_prep_record_id"] == prep_record["record_id"]
	feed_response = client.get("/v1/agent/records?agent=pack", headers={"Authorization": f"Bearer {'f' * 48}"})
	assert feed_response.status_code == 200
	assert any(item["record_id"] == pack_contract_id for item in feed_response.get_json()["records"])
	assert deliveries[0]["record_id"] == pack_contract_id

	tampered = client.post(
		"/capture",
		data={
			"source_prep_record_id": prep_record["record_id"],
			"order_lines": "SKU-CABLE-USBC:9",
			"observed_in_box": "",
		},
	)
	assert tampered.status_code == 400


def test_assistant_uses_loaded_stage2_prep_context_for_route_questions():
	client = packguard_app.app.test_client()
	_login(client)
	prep_record = _save_prep_record()
	response = client.post(
		"/api/assistant",
		json={
			"question": "Where did this product come from and where is it going?",
			"page_context": {
				"current_page": {"title": "New check", "path": "/capture"},
				"recent_pages": [{"title": "New check", "path": "/capture"}],
				"source_prep_record_id": prep_record["record_id"],
			},
		},
	)
	assert response.status_code == 200
	answer = response.get_json()["answer"]
	assert "12 Prep Source Way, Reno, NV" in answer
	assert "34 Pack Destination Rd, Sacramento, CA" in answer
	assert "North Shop" in answer


def test_machine_agent_api_ingests_prep_and_serves_only_pack_records(monkeypatch):
	prep_token = "p" * 48
	feed_token = "f" * 48
	monkeypatch.setattr(packguard_app, "PREP_AGENT_API_TOKEN", prep_token)
	monkeypatch.setattr(packguard_app, "PACK_FEED_AGENT_API_TOKEN", feed_token)
	monkeypatch.setattr(packguard_app, "AGENT_API_ORG_ID", "org_demo_alpha")
	prep_record = _save_prep_record()
	prep_record["record_id"] = str(uuid4())
	client = packguard_app.app.test_client()

	assert client.get("/v1/agent/records").status_code == 401
	assert client.post("/v1/agent/records", json=prep_record).status_code == 401
	response = client.post(
		"/v1/agent/records",
		json=prep_record,
		headers={"Authorization": f"Bearer {prep_token}"},
	)
	assert response.status_code == 201
	assert response.get_json()["status"] == "created"
	duplicate = client.post(
		"/v1/agent/records",
		json=prep_record,
		headers={"Authorization": f"Bearer {prep_token}"},
	)
	assert duplicate.status_code == 200
	assert duplicate.get_json()["status"] == "duplicate"

	feed = client.get(
		"/v1/agent/records?agent=pack",
		headers={"Authorization": f"Bearer {feed_token}"},
	)
	assert feed.status_code == 200
	assert all(record["agent"] == "pack" for record in feed.get_json()["records"])
	assert client.get(
		"/v1/agent/records?agent=prep",
		headers={"Authorization": f"Bearer {feed_token}"},
	).status_code == 400
	assert client.post(
		"/v1/agent/records",
		json=prep_record,
		headers={"Authorization": f"Bearer {feed_token}"},
	).status_code == 401

	other_org_record = build_record(
		organization_key="org_demo_bravo",
		client_id=None,
		agent="prep",
		subject=prep_record["subject"],
		operator_label="prep-agent",
		images=[],
		checks=prep_record["checks"],
		decision="pack",
		decided_by="agent",
		status="complete",
	)
	assert client.post(
		"/v1/agent/records",
		json=other_org_record,
		headers={"Authorization": f"Bearer {prep_token}"},
	).status_code == 403


def test_agent_workflow_shows_delivery_failure_and_retries_to_returns(monkeypatch):
	monkeypatch.setattr(packguard_app, "AGENT_API_ORG_ID", "org_demo_alpha")
	monkeypatch.setattr(packguard_app, "RETURNS_AGENT_WEBHOOK_URL", "https://returns-agent.test/v1/records")
	monkeypatch.setattr(packguard_app, "RETURNS_AGENT_WEBHOOK_TOKEN", "r" * 48)
	record = build_record(
		organization_key="org_demo_alpha",
		client_id=None,
		agent="pack",
		subject={
			"type": "order", "asin": None, "sku": "SKU-CABLE-USBC", "order_id": "ORD-RETRY-1",
			"po_line_id": None, "shipment_id": "SHIP-RETRY-1", "quantity_expected": 1,
			"quantity_observed": None,
		},
		operator_label="packguard",
		images=[],
		checks=[{
			"check_key": "sku_quantity", "verdict": "uncertain", "confidence": None,
			"detail": {"sku_checks": [], "source_prep_record_id": None},
			"model_version": "test-pack-v1", "latency_ms": 0,
		}, {
			"check_key": "visual_evidence", "verdict": "uncertain", "confidence": None,
			"detail": {"status": "MODEL_TIMEOUT", "uncertainties": ["Vision timed out."]},
			"model_version": "test-vision-v1", "latency_ms": 0,
		}],
		decision="manual_review",
		decided_by="agent",
		status="pending",
	)
	assert save_contract_record(record, "org_demo_alpha") == "created"
	delivery_attempts = []

	def fail_then_deliver(webhook_request, timeout=10):
		delivery_attempts.append(webhook_request)
		if len(delivery_attempts) == 1:
			raise OSError("returns service unavailable")
		return BytesIO(b"{}")

	monkeypatch.setattr(packguard_app, "urlopen", fail_then_deliver)
	assert packguard_app.publish_pack_record(record) is False
	client = packguard_app.app.test_client()
	_login(client)
	failed_page = client.get("/agent-network")
	assert b"DELIVERY FAILED" in failed_page.data
	assert b"Retry delivery" in failed_page.data
	retry = client.post(f"/v1/agent/records/{record['record_id']}/retry")
	assert retry.status_code == 302
	assert len(delivery_attempts) == 2
	assert delivery_attempts[1].headers["Authorization"] == f"Bearer {'r' * 48}"
	events = list_audit_events(record["record_id"], "org_demo_alpha")
	assert [event["event_type"] for event in events[:2]] == [
		"RETURNS_AGENT_DELIVERED", "RETURNS_AGENT_FAILED",
	]
	delivered_page = client.get("/agent-network")
	assert b"DELIVERED" in delivered_page.data
	assert b"Retry delivery" not in delivered_page.data


def test_model_timeout_fails_open_with_pending_canonical_record(monkeypatch):
	storage = FakePrivateStorage()
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	updated_event = Event()
	original_update = packguard_app.update_contract_record
	monkeypatch.setattr(packguard_app, "inspect_image", lambda *_args, **_kwargs: {
		"status": "MODEL_TIMEOUT", "provider": "ollama:gemma3:4b", "inference_ms": 2100,
		"token_usage": None,
	})
	def update_record_and_signal(record, organization_key):
		updated = original_update(record, organization_key)
		updated_event.set()
		return updated
	monkeypatch.setattr(packguard_app, "update_contract_record", update_record_and_signal)
	client = packguard_app.app.test_client()
	_login(client)
	created = client.post("/v1/captures", json=_payload()).get_json()
	for item in created["upload_urls"]:
		storage.objects[item["key"]] = b"\xff\xd8\xff" + item["slot"].encode("ascii")
	completed = client.post(
		f"/v1/captures/{created['capture_id']}/complete",
		json={"images": [{"key": item["key"], "taken_at": "2026-09-30T10:00:00Z"} for item in created["upload_urls"]]},
	)
	assert completed.status_code == 201
	assert completed.get_json()["status"] == "pending"
	assert updated_event.wait(2)
	record = packguard_app.app.test_client().get(f"/v1/records/{completed.get_json()['record_id']}").get_json()
	assert record["status"] == "pending"
	assert record["checks"][1]["detail"]["status"] == "MODEL_TIMEOUT"
