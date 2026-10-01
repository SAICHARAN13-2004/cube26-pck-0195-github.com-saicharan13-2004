from threading import Event

import app as packguard_app


class FakePrivateStorage:
	backend = "s3"

	def __init__(self):
		self.objects = {}

	def presign_put(self, key, content_type, expires=900):
		return f"https://private-storage.test/{key}?signed=token"

	def read(self, key):
		return self.objects.get(key)


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


def test_contract_capture_completes_one_batched_call_and_emits_exact_record(monkeypatch):
	storage = FakePrivateStorage()
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	vision_called = Event()
	captured = {}

	def fake_vision(photo_bytes, **kwargs):
		captured["bytes"] = [photo_bytes, *[item[0] for item in kwargs["additional_images"]]]
		captured["context"] = kwargs["capture_context"]
		vision_called.set()
		return {
			"status": "SUGGESTIONS_READY_UNCALIBRATED", "provider": "test-model:1",
			"inference_ms": 17, "token_usage": {"prompt": 90, "completion": 20},
			"detected_items": [{"sku": "SKU-CABLE-USBC", "quantity": 1}], "extra_items": [],
			"image_quality": {"status": "GOOD", "score": 0.9}, "occlusion": {"status": "CLEAR"},
			"uncertainties": [], "confidence": 0.8,
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
	created = client.post("/v1/captures", json=_payload())
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
	assert len(record["images"]) == 2
	assert len(captured["bytes"]) == 2
	assert captured["context"]["check_keys"] == ["sku_quantity", "visual_evidence"]
	visual_check = next(check for check in record["checks"] if check["check_key"] == "visual_evidence")
	assert visual_check["detail"]["token_usage"] == {"prompt": 90, "completion": 20}
	assert visual_check["latency_ms"] == 17
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
