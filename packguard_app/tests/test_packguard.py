import csv
import json
import re
from io import BytesIO
from unittest.mock import patch
from PIL import Image

import app as packguard_app
from app import app
from database import get_record
from evaluate_vision import evaluate_predictions, validate_double_labeled_holdout
from verifier import verify_pack, verify_receiving_components
from validate_fixtures import inspect_manifest
from vision import inspect_image
from policy import auto_seal_policy, require_operator_confirmation
from production import readiness_report
from agent import assess_pack, enforce_component_verdict
from orchestrator import run_pack_agent_workflow
from evaluation import summarize_records


def login(client, username="alpha.operator", password="alpha-demo"):
    return client.post("/login", data={"username": username, "password": password})


def test_vision_catalog_prioritizes_expected_skus_without_dropping_decoys(monkeypatch):
    products = [
        {"sku": "SKU-DECOY", "product_name": "Decoy item"},
        {"sku": "SKU-EXPECTED", "product_name": "Expected item"},
    ]
    monkeypatch.setattr(packguard_app, "catalog_products", lambda _org_id: products)

    result = packguard_app.vision_catalog_products("org_demo_alpha", {"SKU-EXPECTED"})

    assert [product["sku"] for product in result] == ["SKU-EXPECTED", "SKU-DECOY"]


def test_verifier_returns_three_way_decision():
    assert verify_pack("SKU-A:1", "SKU-A:1")["verdict"] == "PASS"
    assert verify_pack("SKU-A:1", "SKU-A:1")["decision"] == "SEAL"
    assert verify_pack("SKU-A:1", "SKU-A:2")["verdict"] == "FAIL"
    assert verify_pack("SKU-A:1", "SKU-A:2")["decision"] == "FIX"
    assert verify_pack("SKU-A:1", "SKU-A:1;not-a-line")["verdict"] == "UNCERTAIN"
    assert verify_pack("SKU-A:1", "SKU-A:1;not-a-line")["decision"] == "MANUAL_REVIEW"
    assert verify_pack("SKU-A:1;broken", "SKU-A:1")["verdict"] == "UNCERTAIN"
    assert verify_pack("", "SKU-A:1")["decision"] == "MANUAL_REVIEW"


def test_verifier_separates_presence_from_quantity():
    checks = verify_pack("SKU-A:2", "SKU-A:1")["checks"]

    assert checks[0]["presence_status"] == "PASS"
    assert checks[0]["quantity_status"] == "FAIL"
    assert checks[0]["status"] == "FAIL"


def test_evaluation_reports_uncertain_pending_and_split_metrics():
    summary = summarize_records([
        {
            "verdict": "UNCERTAIN",
            "evidence": {
                "decision": "MANUAL_REVIEW",
                "checks": [{"check": "quantity_and_identity", "presence_status": "PASS", "quantity_status": "FAIL"}],
                "source": {"inspection": {"status": "MODEL_TIMEOUT"}},
            },
        }
    ])

    assert summary["uncertain_rate"] == 1.0
    assert summary["uncertain_rate_status"] == "ABOVE_TARGET"
    assert summary["pending_rate"] == 1.0
    assert summary["pending_rate_status"] == "ABOVE_TARGET"
    assert summary["check_metrics"]["all_items_present"]["pass_rate"] == 1.0
    assert summary["check_metrics"]["quantities_correct"]["pass_rate"] == 0.0
    assert verify_pack("SKU-A:1", "SKU-A:1;SKU-B:1")["verdict"] == "FAIL"
    assert verify_pack("SKU-A:1", "")["verdict"] == "UNCERTAIN"
    assert verify_pack("SKU-A:1", "")["decision"] == "RECAPTURE"


def test_receiving_component_checks_compare_counts_conditions_and_contents():
    checks = verify_receiving_components({
        "expected_carton_count": "2", "observed_carton_count": "2",
        "expected_units_per_carton": "6", "observed_units_per_carton": "5",
        "carton_condition": "NO_DAMAGE", "product_condition": "DAMAGED",
        "expected_variant": "Blue", "observed_variant": "Blue",
        "expected_components": "cable;adapter;manual",
        "observed_components": "cable;manual",
    })
    results = {check["check"]: check["status"] for check in checks}
    assert results == {
        "carton_count": "PASS",
        "units_per_carton": "FAIL",
        "carton_damage": "PASS",
        "product_damage": "FAIL",
        "variant_match": "PASS",
        "missing_components": "FAIL",
    }


def test_missing_receiving_component_inputs_are_uncertain():
    checks = verify_receiving_components({})
    assert len(checks) == 6
    assert all(check["status"] == "UNCERTAIN" for check in checks)


def test_tenant_cannot_read_other_tenant_record():
    alpha_client = app.test_client()
    bravo_client = app.test_client()
    login(alpha_client)
    login(bravo_client, "bravo.operator", "bravo-demo")
    alpha = alpha_client.get("/api/records?org_id=org_demo_bravo", headers={"X-Org-Id": "org_demo_bravo"})
    bravo = bravo_client.get("/api/records?org_id=org_demo_alpha", headers={"X-Org-Id": "org_demo_alpha"})
    alpha_records = alpha.get_json()["records"]
    bravo_records = bravo.get_json()["records"]
    assert alpha.status_code == 200
    assert bravo.status_code == 200
    assert alpha_records and bravo_records
    assert {record["org_id"] for record in alpha_records} == {"org_demo_alpha"}
    assert {record["org_id"] for record in bravo_records} == {"org_demo_bravo"}
    record_id = alpha_records[0]["record_id"]
    assert bravo_client.get(f"/records/{record_id}").status_code == 404


def test_product_catalog_is_tenant_scoped_and_stores_variants_and_reference_images():
    alpha_client = app.test_client()
    bravo_client = app.test_client()
    login(alpha_client)
    login(bravo_client, "bravo.operator", "bravo-demo")

    response = alpha_client.post(
        "/catalog",
        data={
            "sku": "SKU-CATALOG-TEST",
            "product_name": "Demo bottle",
            "brand": "Demo brand",
            "external_id": "EXT-100",
            "barcode": "00012345678905",
            "supplier_name": "Demo supplier",
            "origin_address": "10 Supplier Way, Reno, NV",
            "ordered_for": "North Shop",
            "delivery_address": "20 Retail Road, Sacramento, CA",
            "attributes": '{"color":"blue","size":"750ml"}',
            "reference_image": (BytesIO(b"fake-image"), "bottle.png"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    alpha_products = alpha_client.get("/api/products").get_json()["products"]
    bravo_products = bravo_client.get("/api/products").get_json()["products"]
    created = next(product for product in alpha_products if product["sku"] == "SKU-CATALOG-TEST")
    assert created["attributes"] == {"color": "blue", "size": "750ml"}
    assert created["reference_image_ref"].startswith("org_demo_alpha/catalog/")
    assert created["supplier_name"] == "Demo supplier"
    assert created["origin_address"] == "10 Supplier Way, Reno, NV"
    assert created["ordered_for"] == "North Shop"
    assert created["delivery_address"] == "20 Retail Road, Sacramento, CA"
    assert "SKU-CATALOG-TEST" not in {product["sku"] for product in bravo_products}

    prefills = alpha_client.get(f"/capture?product_id={created['product_id']}")
    assert prefills.status_code == 200
    assert created["product_id"].encode() in prefills.data
    assert b"SKU-CATALOG-TEST:1" in prefills.data
    assert b"10 Supplier Way, Reno, NV" in prefills.data
    assert b"20 Retail Road, Sacramento, CA" in prefills.data
    name_prefill = alpha_client.get("/capture?product=Demo%20bottle")
    assert name_prefill.status_code == 200
    assert created["product_id"].encode() in name_prefill.data
    assert b"SKU-CATALOG-TEST:1" in name_prefill.data
    detail_page = alpha_client.get("/catalog/product/SKU-CATALOG-TEST")
    assert b"Demo supplier" in detail_page.data
    assert b"North Shop" in detail_page.data

    check_response = alpha_client.post(
        "/capture",
        data={
            "unit_id": "UNIT-CATALOG-CHECK",
            "order_id": "ORD-CATALOG-CHECK",
            "operator_id": "alpha.operator",
            "selected_product_id": created["product_id"],
            "order_lines": "SKU-CATALOG-TEST:1",
            "observed_in_box": "SKU-CATALOG-TEST:1",
        },
    )
    assert check_response.status_code == 302
    check_record = alpha_client.get(check_response.headers["Location"])
    assert b"Demo bottle" in check_record.data
    check_api = alpha_client.get("/api/records").get_json()["records"]
    created_check = next(item for item in check_api if item["unit_id"] == "UNIT-CATALOG-CHECK")
    assert created_check["evidence"]["checks"][0]["product"]["name"] == "Demo bottle"
    assert created_check["evidence"]["checks"][0]["product"]["attributes"] == {"color": "blue", "size": "750ml"}
    assert created_check["origin_address"] == "10 Supplier Way, Reno, NV"
    assert created_check["delivery_address"] == "20 Retail Road, Sacramento, CA"
    assert created_check["evidence"]["source"]["product_routes"][0]["ordered_for"] == "North Shop"
    assert b"10 Supplier Way, Reno, NV" in check_record.data
    assert b"20 Retail Road, Sacramento, CA" in check_record.data


def test_single_sku_image_capture_uses_fast_presence_agent_without_manual_observation(monkeypatch):
    client = app.test_client()
    login(client)
    product = next(
        item for item in client.get("/api/products").get_json()["products"]
        if item["sku"] == "SKU-CABLE-USBC"
    )
    captured = {}

    def fake_inspection(_photo, **kwargs):
        captured.update(kwargs)
        return {
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "provider": "test:moondream",
            "presence_hint": "MATCH",
            "detected_items": [{"sku": "SKU-CABLE-USBC", "quantity": None}],
            "extra_items": [],
            "image_quality": {"status": "UNKNOWN"},
            "uncertainties": ["Presence only; quantity is unknown."],
        }

    monkeypatch.setattr("app.inspect_image", fake_inspection)
    monkeypatch.delenv("PACKGUARD_VISION_MODEL", raising=False)
    image = BytesIO()
    Image.new("RGB", (24, 24), "white").save(image, format="JPEG")
    response = client.post(
        "/capture",
        data={
            "selected_product_id": product["product_id"],
            "observed_in_box": "",
            "photo": (image, "open-box.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert captured["model_override"] == "moondream:1.8b"
    assert captured["force_structured"] is False
    assert [candidate["sku"] for candidate in captured["catalog_products"]] == ["SKU-CABLE-USBC"]
    record_id = response.headers["Location"].rstrip("/").split("/")[-1].split("?")[0]
    saved = next(item for item in client.get("/api/records").get_json()["records"] if item["record_id"] == record_id)
    assert saved["order_lines"] == "SKU-CABLE-USBC:1"
    assert saved["order_id"].startswith("ORD-LOCAL-")
    assert saved["unit_id"].startswith("UNIT-")
    record_page = client.get(response.headers["Location"])
    assert b"Product presence" in record_page.data
    assert b"quantity not determined" in record_page.data
    assert b"Agent candidate" in record_page.data


def test_new_check_routes_clear_wrong_sku_image_to_fix_without_typed_observation(monkeypatch):
    client = app.test_client()
    login(client)
    monkeypatch.setattr("app.inspect_image", lambda *_args, **_kwargs: {
        "status": "SUGGESTIONS_READY_UNCALIBRATED",
        "provider": "test:structured-vision",
        "detected_items": [{"sku": "SKU-BOTTLE-750", "quantity": 1, "confidence": 0.98, "variant_match": True}],
        "extra_items": [],
        "image_quality": {"status": "GOOD", "score": 0.98},
        "occlusion": {"status": "CLEAR"},
        "uncertainties": [],
        "confidence": 0.98,
    })
    photo = BytesIO()
    Image.new("RGB", (24, 24), "white").save(photo, format="JPEG")
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-AUTO-WRONG-001",
            "order_id": "ORD-AUTO-WRONG-001",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "",
            "photo": (photo, "open-box.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    record_id = response.headers["Location"].rstrip("/").split("/")[-1].split("?")[0]
    saved = next(item for item in client.get("/api/records").get_json()["records"] if item["record_id"] == record_id)
    assert saved["verdict"] == "FAIL"
    assert saved["action"] == "STOP_AND_FIX"
    assert saved["evidence"]["decision"] == "FIX"
    assert saved["evidence"]["source"]["inspection"]["candidate_decision"] == "FIX"


def test_catalog_names_are_distinct_from_sku_and_dashboard_search_is_scoped():
    alpha_client = app.test_client()
    bravo_client = app.test_client()
    login(alpha_client)
    login(bravo_client, "bravo.operator", "bravo-demo")

    alpha_search = alpha_client.get("/api/search?q=SKU-CABLE-USBC").get_json()["results"]
    cable = next(result for result in alpha_search if result["type"] == "Product")
    assert cable["title"] == "USB-C cable"
    assert "Product ID PRD-" in cable["subtitle"]
    assert "SKU SKU-CABLE-USBC" in cable["subtitle"]
    assert cable["url"] == "/catalog/product/SKU-CABLE-USBC"
    name_search = alpha_client.get("/api/search?q=USB-C").get_json()["results"]
    assert any(result["type"] == "Product" and result["title"] == "USB-C cable" for result in name_search)
    detail = alpha_client.get(cable["url"])
    assert detail.status_code == 200
    assert b"USB-C cable" in detail.data
    assert b"SKU-CABLE-USBC" in detail.data
    assert b"Reference image" in detail.data
    assert b'href="/catalog/product/SKU-CABLE-USBC"' in alpha_client.get("/catalog").data
    assert alpha_client.get("/api/search?q=ORD-DUMMY-50006").get_json()["results"] == []
    assert bravo_client.get("/catalog/product/SKU-CATALOG-TEST").status_code == 404
    assert any(
        result["type"] == "Product" and result["title"] == "6 ft leash"
        for result in bravo_client.get("/api/search?q=SKU-LEASH-6FT").get_json()["results"]
    )
    assert app.test_client().get("/api/search?q=SKU-CABLE-USBC").status_code == 401


def test_seeded_catalog_serves_matching_fixture_reference_photo():
    client = app.test_client()
    login(client)
    response = client.get("/api/products")
    products = response.get_json()["products"]
    cable = next(product for product in products if product["sku"] == "SKU-CABLE-USBC")

    assert cable["reference_image_ref"] == "fixtures/vision/SKU-CABLE-USBC_001.jpg"
    image_response = client.get(f"/media/{cable['reference_image_ref']}")
    assert image_response.status_code == 200
    assert image_response.mimetype == "image/jpeg"


def test_capture_uses_catalog_picker_and_phone_rear_camera():
    client = app.test_client()
    login(client)
    response = client.get("/capture")

    assert response.status_code == 200
    assert b'id="product-lookup"' in response.data
    assert b"Product name or Product ID" in response.data
    assert b'<textarea name="observed_in_box"' not in response.data
    assert b"demo-preset" not in response.data
    assert b'capture="environment"' in response.data
    assert b"camera-capture" in response.data
    assert b"agent starts automatically" in response.data


def test_capture_persists_evidence():
    client = app.test_client()
    login(client, "bravo.operator", "bravo-demo")
    response = client.post(
        "/capture?org_id=org_demo_alpha",
        data={
            "unit_id": "UNIT-TEST",
            "order_id": "ORD-TEST",
            "operator_id": "op_test",
            "operator_verdict": "seal",
            "order_lines": "SKU-A:1;SKU-B:2",
            "observed_in_box": "SKU-A:1;SKU-B:1",
        },
    )
    assert response.status_code == 302
    record = client.get(response.headers["Location"])
    assert record.status_code == 200
    assert b"FAIL" in record.data
    assert b"Product decision" in record.data
    assert b"FIX" in record.data
    api = client.get("/api/records?org_id=org_demo_alpha").get_json()
    created = next(item for item in api["records"] if item["unit_id"] == "UNIT-TEST")
    assert created["evidence"]["contract_version"] == "pack-manager.v1"
    assert created["evidence"]["decision"] == "FIX"
    assert created["evidence"]["operator_agreement"] == "DISAGREES"
    assert created["org_id"] == "org_demo_bravo"


def test_record_without_order_media_shows_catalog_reference_as_non_evidence():
    client = app.test_client()
    login(client)
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-CATALOG-REF-ONLY",
            "order_id": "ORD-CATALOG-REF-ONLY",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-BOTTLE-750:1",
            "observed_in_box": "SKU-BOTTLE-750:1",
        },
    )

    record = client.get(response.headers["Location"])

    assert record.status_code == 200
    assert b"Catalog reference" in record.data
    assert b"not captured pack evidence" in record.data
    assert b"No packing photo or video was captured" in record.data


def test_protected_routes_require_login():
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/api/records").status_code == 401
    assert client.get("/reports/records.csv").status_code == 302
    assert client.get("/reports/summary.json").status_code == 302
    assert client.get("/reports/summary").status_code == 302
    assert client.get("/assistant").status_code == 302
    health = client.get("/health")
    assert health.status_code == 200
    content_policy = health.headers["Content-Security-Policy"]
    assert "https://fonts.googleapis.com" in content_policy
    assert "https://fonts.gstatic.com" in content_policy


def test_production_login_blocks_demo_accounts_but_accepts_provisioned_operator(monkeypatch):
    from werkzeug.security import generate_password_hash
    from database import save_user

    monkeypatch.setattr("app.IS_PRODUCTION", True)
    client = app.test_client()

    demo_response = client.post("/login", data={"username": "alpha.operator", "password": "alpha-demo"})
    assert demo_response.status_code == 401
    assert b"Local demo accounts" not in demo_response.data

    save_user({
        "username": "production.operator",
        "password_hash": generate_password_hash("correct horse battery staple 2026"),
        "org_id": "org_production_test",
        "role": "operator",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    operator_response = client.post(
        "/login",
        data={"username": "production.operator", "password": "correct horse battery staple 2026"},
    )

    assert operator_response.status_code == 302
    assert operator_response.headers["Location"] == "/"


def test_production_state_changes_require_csrf_token(monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    response = app.test_client().post(
        "/login",
        data={"username": "alpha.operator", "password": "alpha-demo"},
    )

    assert response.status_code == 400
    assert b"CSRF" in response.data


def test_csrf_token_allows_valid_login_form_submission(monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    client = app.test_client()
    login_page = client.get("/login")
    csrf_token = re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data).group(1).decode()

    response = client.post(
        "/login",
        data={"csrf_token": csrf_token, "username": "alpha.operator", "password": "alpha-demo"},
    )

    assert response.status_code == 302


def test_login_throttle_blocks_repeated_failures_and_returns_retry_after():
    client = app.test_client()
    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": "throttle.test.operator", "password": "wrong"},
        )
        assert response.status_code == 401

    blocked = client.post(
        "/login",
        data={"username": "throttle.test.operator", "password": "wrong"},
    )

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_create_operator_cli_stores_a_hashed_password(monkeypatch):
    from database import get_user

    monkeypatch.setattr("builtins.input", lambda _prompt: "cli.operator" if "username" in _prompt.lower() else "org_cli")
    monkeypatch.setattr("app.getpass.getpass", lambda _prompt: "a-long-test-password-2026")
    result = app.test_cli_runner().invoke(args=["create-operator"])

    user = get_user("cli.operator")
    assert result.exit_code == 0
    assert user["org_id"] == "org_cli"
    assert user["password_hash"] != "a-long-test-password-2026"


def test_csv_report_is_scoped_to_logged_in_organization():
    client = app.test_client()
    login(client)
    response = client.get("/reports/records.csv?org_id=org_demo_bravo")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "org_demo_alpha" in response.text
    assert "org_demo_bravo" not in response.text


def test_evaluation_summary_is_scoped_and_reports_uncertainty():
    client = app.test_client()
    login(client)
    response = client.get("/reports/summary.json?org_id=org_demo_bravo")

    assert response.status_code == 200
    body = response.get_json()
    assert body["org_id"] == "org_demo_alpha"
    assert body["summary"]["scope"] == "logged_in_organization"
    assert set(body["summary"]["verdicts"]) == {"PASS", "FAIL", "UNCERTAIN"}
    assert set(body["summary"]["product_decisions"]) == {"SEAL", "FIX", "RECAPTURE", "MANUAL_REVIEW", "NOT_AVAILABLE"}
    assert body["summary"]["false_seal_rate"] == "not_measured_until_held_out_predictions_exist"
    assert body["summary"]["vision_accuracy"] == "not_measured_until_a_vision_provider_is_configured"
    assert client.get("/reports/summary").status_code == 200


def test_assistant_answers_from_logged_in_organization():
    client = app.test_client()
    login(client)
    response = client.post("/api/assistant", data={"question": "How many alerts do we have?"})

    assert response.status_code == 200
    assert "active alerts" in response.get_json()["answer"]


def test_assistant_agent_network_answer_uses_live_workflow_counts():
    client = app.test_client()
    login(client)
    response = client.post(
        "/api/assistant",
        json={"question": "What is the agent network status?"},
    )

    assert response.status_code == 200
    answer = response.get_json()["answer"]
    assert "Stage-2 prep record(s)" in answer
    assert "Stage-3 pack record(s)" in answer
    assert "Stage-4 webhook is" in answer
    assert "Dispatch complete" not in answer


def test_assistant_answers_product_questions_from_tenant_catalog():
    client = app.test_client()
    login(client)
    client.post(
        "/catalog",
        data={
            "sku": "SKU-CABLE-USBC",
            "product_name": "USB-C cable",
            "supplier_name": "Cable supplier",
            "origin_address": "12 Factory Lane, Reno, NV",
            "ordered_for": "Online retail",
            "delivery_address": "34 Store Road, Sacramento, CA",
            "attributes": "{}",
        },
    )
    response = client.post(
        "/api/assistant",
        json={"question": "What is SKU-CABLE-USBC?"},
    )

    assert response.status_code == 200
    answer = response.get_json()["answer"]
    assert "USB-C cable" in answer
    assert "Product ID PRD-" in answer
    assert "Cable supplier" in answer
    assert "12 Factory Lane, Reno, NV" in answer
    assert "Online retail" in answer
    assert "34 Store Road, Sacramento, CA" in answer
    assert "not a visual confirmation" in answer

    product = next(
        item for item in client.get("/api/products").get_json()["products"]
        if item["sku"] == "SKU-CABLE-USBC"
    )
    current_page_response = client.post(
        "/api/assistant",
        json={
            "question": "Where should this go?",
            "page_context": {
                "current_page": {"path": "/capture", "title": "New check"},
                "form_fields": {"selected_product_id": product["product_id"], "password": "must not be shared"},
            },
        },
    )
    assert current_page_response.status_code == 200
    assert "34 Store Road, Sacramento, CA" in current_page_response.get_json()["answer"]

    def test_support_request_is_persisted_and_tenant_scoped():
        alpha_client = app.test_client()
        bravo_client = app.test_client()
        login(alpha_client)
        login(bravo_client, "bravo.operator", "bravo-demo")

        response = alpha_client.post(
            "/support",
            data={
                "name": "Demo Operator",
                "email": "operator@example.com",
                "subject": "Question about order",
                "message": "Please review ORD-DEMO-001.",
            },
        )

        assert response.status_code == 200
        assert b"Support request saved" in response.data
        alpha_requests = alpha_client.get("/api/support/requests").get_json()["requests"]
        bravo_requests = bravo_client.get("/api/support/requests").get_json()["requests"]
        assert len(alpha_requests) == 1
        assert alpha_requests[0]["subject"] == "Question about order"
        assert alpha_requests[0]["status"] == "OPEN"
        assert bravo_requests == []

def test_assistant_is_available_on_separate_page_not_dashboard():
    client = app.test_client()
    login(client)
    dashboard = client.get("/")
    assistant_page = client.get("/assistant")

    assert dashboard.status_code == 200
    assert b'id="global-assistant-chat"' in dashboard.data
    assert b'id="assistant-chat"' not in dashboard.data
    assert b'aria-label="Open assistant"' in dashboard.data
    assert b'href="/assistant"' not in dashboard.data
    assert assistant_page.status_code == 200
    assert b"assistant-chat" in assistant_page.data


def test_assistant_answers_common_packguard_questions():
    client = app.test_client()
    login(client)
    for question, expected in (
        ("What does PASS mean in PackGuard?", "PASS / SEAL"),
        ("How do I use SKU quantities?", "SKU:quantity"),
        ("What are the PackGuard APIs?", "/api/records"),
        ("What are the PackGuard demo credentials?", "alpha.operator"),
    ):
        response = client.post("/api/assistant", data={"question": question})
        assert response.status_code == 200
        assert expected in response.get_json()["answer"]


def test_assistant_packguard_workflow_answer_is_consistent():
    client = app.test_client()
    login(client)
    questions = (
        "Explain the PackGuard workflow.",
        "What is PackGuard's workflow?",
        "Describe the workflow for PackGuard.",
        "Can you explain the workflowof packguard?",
    )
    with patch("app.urlopen", side_effect=AssertionError("workflow FAQ must not call Ollama")):
        answers = [
            client.post("/api/assistant", json={"question": question}).get_json()["answer"]
            for question in questions
        ]
        detailed_followup = client.post(
            "/api/assistant",
            json={
                "question": "Can you explain indetaill?",
                "history": [
                    {"role": "user", "content": questions[-1]},
                    {"role": "assistant", "content": answers[-1]},
                ],
            },
        ).get_json()["answer"]

    assert answers[0] == answers[1] == answers[2]
    assert answers[2] == answers[3] == detailed_followup
    assert "PASS / SEAL" in answers[0]
    assert "FAIL / STOP_AND_FIX" in answers[0]
    assert "UNCERTAIN / HOLD_FOR_REVIEW" in answers[0]
    assert "threat" not in answers[0].lower()


def test_assistant_answers_general_questions_with_local_model():
    client = app.test_client()
    login(client)

    def fake_ollama(request, timeout=60):
        return BytesIO(json.dumps({"message": {"content": "Paris is the capital of France."}}).encode("utf-8"))

    with patch("app.urlopen", side_effect=fake_ollama):
        response = client.post("/api/assistant", json={"question": "Where is Paris?"})

    assert response.status_code == 200
    assert response.get_json()["answer"] == "Paris is the capital of France."


def test_assistant_passes_conversation_history_to_local_model():
    client = app.test_client()
    login(client)
    captured_payload = {}

    def fake_ollama(request, timeout=60):
        captured_payload.update(json.loads(request.data.decode("utf-8")))
        return BytesIO(json.dumps({"message": {"content": "Here is the follow-up answer."}}).encode("utf-8"))

    with patch("app.urlopen", side_effect=fake_ollama):
        response = client.post(
            "/api/assistant",
            json={
                "question": "Can you explain that more?",
                "history": [
                    {"role": "user", "content": "What is a PASS decision?"},
                    {"role": "assistant", "content": "PASS means the quantities match."},
                ],
            },
        )

    assert response.status_code == 200
    assert response.get_json()["answer"] == "Here is the follow-up answer."
    assert captured_payload["messages"][-3]["content"] == "What is a PASS decision?"
    assert captured_payload["messages"][-2]["content"] == "PASS means the quantities match."
    assert captured_payload["messages"][-1]["content"] == "Can you explain that more?"


def test_assistant_answers_order_followups_from_logged_in_workspace():
    client = app.test_client()
    login(client)
    client.post(
        "/capture",
        data={
            "unit_id": "UNIT-ASSISTANT-CTX-001",
            "order_id": "ORD-ASSISTANT-CTX-001",
            "operator_id": "alpha.operator",
            "origin_address": "Reno fulfillment center",
            "delivery_address": "Sacramento, CA",
            "order_lines": "SKU-BOTTLE-750:1",
            "observed_in_box": "SKU-BOTTLE-750:1",
        },
    )

    first_question = "Tell me the status of ORD-ASSISTANT-CTX-001"
    first_answer = client.post(
        "/api/assistant", json={"question": first_question}
    ).get_json()["answer"]
    followup = client.post(
        "/api/assistant",
        json={
            "question": "Where did it arrive from?",
            "history": [
                {"role": "user", "content": first_question},
                {"role": "assistant", "content": first_answer},
            ],
        },
    )

    assert followup.status_code == 200
    assert "Reno fulfillment center" in followup.get_json()["answer"]


def test_assistant_uses_only_local_model_even_if_hosted_provider_is_selected(monkeypatch):
    client = app.test_client()
    login(client)
    captured_url = []

    def fake_ollama(request, timeout=60):
        captured_url.append(request.full_url)
        return BytesIO(json.dumps({"message": {"content": "Answered locally."}}).encode("utf-8"))

    monkeypatch.setenv("ASSISTANT_PROVIDER", "openai")
    with patch("app.urlopen", side_effect=fake_ollama):
        response = client.post("/api/assistant", json={"question": "Tell me something interesting."})

    assert response.status_code == 200
    assert response.get_json()["answer"] == "Answered locally."
    assert captured_url == ["http://127.0.0.1:11434/api/chat"]


def test_invalid_uploaded_photo_is_recorded_in_vision_evidence():
    client = app.test_client()
    login(client)
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-PHOTO",
            "order_id": "ORD-PHOTO",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-A:1",
            "observed_in_box": "SKU-A:1",
            "photo": (BytesIO(b"fake-image-content"), "open-box.png"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    record = client.get(response.headers["Location"])
    assert record.status_code == 200
    api_records = client.get("/api/records").get_json()["records"]
    created = next(item for item in api_records if item["unit_id"] == "UNIT-PHOTO")
    assert created["photo_ref"].startswith("org_demo_alpha/")
    assert created["evidence"]["source"]["type"] == "operator_structured_observation"
    assert created["evidence"]["source"]["vision_status"] == "INVALID_IMAGE"
    assert b"MULTIPLE CANDIDATES UNSUPPORTED" not in record.data
    assert b"Status:</strong> INVALID IMAGE" in record.data


def test_local_vision_sends_catalog_reference_to_ollama(tmp_path, monkeypatch):
    monkeypatch.setenv("PACKGUARD_MAX_VISION_REFERENCES", "2")
    monkeypatch.delenv("PACKGUARD_VISION_MODEL", raising=False)
    monkeypatch.delenv("PACKGUARD_VISION_TIMEOUT", raising=False)
    photo = tmp_path / "pack.jpg"
    reference = tmp_path / "catalog.jpg"
    Image.new("RGB", (24, 24), "white").save(photo)
    Image.new("RGB", (24, 24), "blue").save(reference)
    captured = {}
    model_output = {
        "image_quality": {"status": "GOOD", "score": 0.91, "reason": "Clear view."},
        "detected_items": [{"sku": "SKU-A", "quantity": 1, "confidence": 0.88, "variant_match": True, "reason": "Matches reference."}],
        "extra_items": [],
        "uncertainties": [],
        "overall_confidence": 0.87,
    }

    def fake_ollama(request, timeout=180):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return BytesIO(json.dumps({"message": {"content": json.dumps(model_output)}}).encode("utf-8"))

    with patch("vision.urlopen", side_effect=fake_ollama):
        result = inspect_image(str(photo), [{"sku": "SKU-A", "product_name": "Sample item", "reference_image_path": str(reference)}])

    assert result["status"] == "SUGGESTIONS_READY_UNCALIBRATED"
    assert result["calibration_status"] == "UNVALIDATED"
    assert captured["payload"]["model"] == "gemma3:4b"
    assert captured["timeout"] == 90
    assert len(captured["payload"]["messages"][0]["images"]) == 2
    assert "catalog reference" in captured["payload"]["messages"][0]["content"]


def test_vision_candidates_include_catalog_decoys_for_extra_item_detection(monkeypatch):
    products = [
        {"sku": "SKU-CABLE-USBC", "product_name": "USB-C cable", "attributes": {}},
        {"sku": "SKU-BOTTLE-750", "product_name": "750 mL bottle", "attributes": {}},
    ]
    monkeypatch.setattr("app.catalog_products", lambda _org_id: products)

    from app import vision_catalog_products
    candidates = vision_catalog_products("org_demo_alpha", {"SKU-CABLE-USBC"})

    assert [product["sku"] for product in candidates] == ["SKU-CABLE-USBC", "SKU-BOTTLE-750"]


def test_local_vision_accepts_private_storage_image_bytes(monkeypatch):
    monkeypatch.setenv("PACKGUARD_MAX_VISION_REFERENCES", "1")
    monkeypatch.setenv("PACKGUARD_VISION_MODEL", "gemma3:4b")
    photo = BytesIO()
    reference = BytesIO()
    Image.new("RGB", (24, 24), "white").save(photo, format="JPEG")
    Image.new("RGB", (24, 24), "blue").save(reference, format="JPEG")
    captured = {}
    model_output = {
        "image_quality": {"status": "GOOD", "score": 0.9, "reason": "Clear."},
        "occlusion": {"status": "CLEAR", "reason": "Visible."},
        "detected_items": [],
        "extra_items": [],
        "uncertainties": [],
        "overall_confidence": 0.8,
    }

    def fake_ollama(request, timeout=180):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return BytesIO(json.dumps({"message": {"content": json.dumps(model_output)}}).encode("utf-8"))

    with patch("vision.urlopen", side_effect=fake_ollama):
        result = inspect_image(
            photo.getvalue(),
            [{"sku": "SKU-A", "product_name": "Sample item", "reference_image_bytes": reference.getvalue(), "reference_image_name": "catalog.jpg"}],
            photo_name="evidence.jpg",
        )

    assert result["status"] == "SUGGESTIONS_READY_UNCALIBRATED"
    assert result["photo_path"] == "evidence.jpg"
    assert len(captured["payload"]["messages"][0]["images"]) == 2
    assert "not a pack-order photo" in captured["payload"]["messages"][0]["content"]


def test_visual_suggestion_does_not_replace_missing_manual_observation():
    client = app.test_client()
    login(client)
    vision_suggestion = {
        "status": "SUGGESTIONS_READY_UNCALIBRATED",
        "detected_items": [{"sku": "SKU-CABLE-USBC", "quantity": 1, "confidence": 0.99}],
        "extra_items": [],
        "uncertainties": [],
        "image_quality": {"status": "GOOD", "score": 0.99, "reason": "Clear."},
        "calibration_status": "UNVALIDATED",
        "confidence": 0.99,
    }
    with patch("app.inspect_image", return_value=vision_suggestion) as inspect:
        response = client.post(
            "/capture",
            data={
                "unit_id": "UNIT-VISION-REVIEW",
                "order_id": "ORD-VISION-REVIEW",
                "order_lines": "SKU-CABLE-USBC:1",
                "observed_in_box": "",
                "photo": (BytesIO(b"placeholder"), "pack.jpg"),
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 302
    inspect.assert_called_once()
    record = client.get(response.headers["Location"])
    assert record.status_code == 200
    created = next(
        item for item in client.get("/api/records").get_json()["records"]
        if item["unit_id"] == "UNIT-VISION-REVIEW"
    )
    assert created["evidence"]["source"]["inspection"]["status"] == "SUGGESTIONS_READY_UNCALIBRATED"
    assert created["evidence"]["verdict"] == "UNCERTAIN"
    assert created["evidence"]["decision"] == "MANUAL_REVIEW"
    assert b"Image assessment" in record.data


def test_poor_image_suggestion_does_not_override_manual_observation():
    client = app.test_client()
    login(client)
    suggestion = {
        "status": "SUGGESTIONS_READY_UNCALIBRATED",
        "detected_items": [],
        "extra_items": [],
        "uncertainties": [],
        "image_quality": {"status": "POOR", "score": 0.2, "reason": "Obscured."},
        "calibration_status": "UNVALIDATED",
        "confidence": 0.2,
    }
    with patch("app.inspect_image", return_value=suggestion) as inspect:
        response = client.post(
            "/capture",
            data={
                "unit_id": "UNIT-POOR-VISION",
                "order_id": "ORD-POOR-VISION",
                "order_lines": "SKU-CABLE-USBC:1",
                "observed_in_box": "SKU-CABLE-USBC:1",
                "operator_verdict": "seal",
                "photo": (BytesIO(b"placeholder"), "pack.jpg"),
            },
            content_type="multipart/form-data",
        )

    assert response.status_code == 302
    inspect.assert_called_once()
    created = next(
        item for item in client.get("/api/records").get_json()["records"]
        if item["unit_id"] == "UNIT-POOR-VISION"
    )
    assert created["evidence"]["verdict"] == "PASS"
    assert created["evidence"]["decision"] == "SEAL"
    assert created["evidence"]["source"]["inspection"]["status"] == "SUGGESTIONS_READY_UNCALIBRATED"


def test_overall_pass_never_masks_uncertain_pack_evidence():
    result = enforce_component_verdict(
        {"verdict": "PASS", "action": "SEAL", "decision": "SEAL", "checks": [{"check": "sku_quantity", "status": "PASS"}], "reason": "quantities match"},
        {"status": "SUGGESTIONS_READY_UNCALIBRATED", "calibration_status": "UNVALIDATED", "image_quality": {"status": "GOOD"}},
        photo_present=True,
    )

    assert result["verdict"] == "UNCERTAIN"
    assert result["decision"] == "MANUAL_REVIEW"
    assert all(check["status"] != "FAIL" for check in result["checks"])
    assert any(check["check"] == "visual_evidence" and check["status"] == "UNCERTAIN" for check in result["checks"])


def test_capture_uses_matching_manual_skus_without_requiring_visual_evidence():
    client = app.test_client()
    login(client)
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-PACK-INCOMPLETE",
            "order_id": "ORD-PACK-INCOMPLETE",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "SKU-CABLE-USBC:1",
        },
    )
    record_id = response.headers["Location"].split("/")[-1].split("?")[0]
    record = next(
        item for item in client.get("/api/records").get_json()["records"]
        if item["record_id"] == record_id
    )

    assert record["verdict"] == "PASS"
    assert record["evidence"]["decision"] == "MANUAL_REVIEW"
    check_status = {check["check"]: check["status"] for check in record["evidence"]["checks"]}
    assert check_status["quantity_and_identity"] == "PASS"
    assert "visual_evidence" not in check_status
    assert "carton_count" not in check_status


def test_uploaded_packing_video_is_saved_as_tenant_scoped_evidence():
    client = app.test_client()
    login(client)
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-VIDEO",
            "order_id": "ORD-VIDEO",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-A:1",
            "observed_in_box": "SKU-A:1",
            "packing_video": (BytesIO(b"fake-video-content"), "packing.mp4"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    created = next(
        item for item in client.get("/api/records").get_json()["records"]
        if item["unit_id"] == "UNIT-VIDEO"
    )
    assert created["video_ref"].startswith("org_demo_alpha/")
    assert created["evidence"]["source"]["video_ref"] == created["video_ref"]


def test_recapture_attempt_is_persisted_in_audit_history():
    client = app.test_client()
    login(client)
    initial = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-RECAPTURE",
            "order_id": "ORD-RECAPTURE",
            "operator_id": "alpha.operator",
            "attempt_type": "initial",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "",
        },
    )
    parent_id = initial.headers["Location"].split("/")[-1].split("?")[0]
    recapture_page = client.get(f"/capture?recapture_of={parent_id}")
    assert b'name="parent_record_id"' in recapture_page.data

    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-RECAPTURE",
            "order_id": "ORD-RECAPTURE",
            "operator_id": "alpha.operator",
            "attempt_type": "recapture",
            "parent_record_id": parent_id,
            "recapture_reason": "The first image was blurry.",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "",
        },
    )

    assert response.status_code == 302
    record_id = response.headers["Location"].split("/")[-1].split("?")[0]
    recaptured = get_record(record_id, "org_demo_alpha")
    original = get_record(parent_id, "org_demo_alpha")
    assert recaptured["session_id"] == original["session_id"]
    assert recaptured["parent_record_id"] == parent_id
    assert recaptured["attempt_number"] == 2
    audit_response = client.get(f"/api/records/{record_id}/audit")
    assert audit_response.status_code == 200
    events = audit_response.get_json()["events"]
    assert events[0]["event_type"] == "RECAPTURE"
    assert events[0]["reason"] == "The first image was blurry."


def test_workflow_transition_sends_uncertain_record_to_manual_review_and_audits_reason():
    client = app.test_client()
    login(client)
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-TRANSITION-REVIEW",
            "order_id": "ORD-TRANSITION-REVIEW",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "",
        },
    )
    record_id = response.headers["Location"].split("/")[-1].split("?")[0]

    transition = client.post(
        f"/records/{record_id}/transition",
        data={"workflow_state": "MANUAL_REVIEW", "reason": "Operator confirmed the contents need human review."},
    )

    assert transition.status_code == 302
    record = next(item for item in client.get("/api/records").get_json()["records"] if item["record_id"] == record_id)
    assert record["workflow_state"] == "MANUAL_REVIEW"
    events = client.get(f"/api/records/{record_id}/audit").get_json()["events"]
    assert events[0]["event_type"] == "STATE_TRANSITION"


def test_workflow_transition_rejects_seal_for_uncertain_record():
    client = app.test_client()
    login(client)
    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-TRANSITION-UNCERTAIN",
            "order_id": "ORD-TRANSITION-UNCERTAIN",
            "operator_id": "alpha.operator",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "",
        },
    )
    record_id = response.headers["Location"].split("/")[-1].split("?")[0]

    transition = client.post(
        f"/records/{record_id}/transition",
        data={"workflow_state": "SEALED", "reason": "Attempted unsafe seal."},
    )

    assert transition.status_code == 400


def test_fixture_validator_reports_missing_files_and_dataset_counts(tmp_path):
    manifest_path = tmp_path / "manifest.csv"
    fields = ["fixture_id", "split", "sku", "quantity", "image_filename", "expected_verdict", "expected_decision", "notes"]
    rows = [
        {"fixture_id": "FX-1", "split": "train", "sku": "SKU-A", "quantity": "1", "image_filename": "present.jpg", "expected_verdict": "PASS", "expected_decision": "SEAL", "notes": "reference"},
        {"fixture_id": "FX-2", "split": "held_out", "sku": "SKU-A", "quantity": "0", "image_filename": "missing.jpg", "expected_verdict": "FAIL", "expected_decision": "FIX", "notes": "missing item"},
    ]
    with manifest_path.open("w", newline="", encoding="utf-8") as manifest_file:
        writer = csv.DictWriter(manifest_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (manifest_path.parent / "present.jpg").write_bytes(b"fixture")

    result = inspect_manifest(manifest_path)

    assert result["row_count"] == 2
    assert result["present_count"] == 1
    assert result["missing_files"] == ["missing.jpg"]
    assert result["splits"] == {"held_out": 1, "train": 1}
    assert result["verdicts"] == {"FAIL": 1, "PASS": 1}


def test_vision_evaluator_reports_false_seal_rate(tmp_path):
    manifest_path = tmp_path / "manifest.csv"
    predictions_path = tmp_path / "predictions.csv"
    manifest_fields = ["fixture_id", "split", "sku", "quantity", "image_filename", "expected_verdict", "expected_decision", "notes"]
    prediction_fields = ["fixture_id", "predicted_decision"]
    with manifest_path.open("w", newline="", encoding="utf-8") as manifest_file:
        writer = csv.DictWriter(manifest_file, fieldnames=manifest_fields)
        writer.writeheader()
        for fixture_id, verdict, decision in (
            ("H-1", "PASS", "SEAL"),
            ("H-2", "FAIL", "FIX"),
            ("H-3", "UNCERTAIN", "MANUAL_REVIEW"),
        ):
            writer.writerow({"fixture_id": fixture_id, "split": "held_out", "expected_verdict": verdict, "expected_decision": decision})
    with predictions_path.open("w", newline="", encoding="utf-8") as predictions_file:
        writer = csv.DictWriter(predictions_file, fieldnames=prediction_fields)
        writer.writeheader()
        writer.writerows([
            {"fixture_id": "H-1", "predicted_decision": "SEAL"},
            {"fixture_id": "H-2", "predicted_decision": "SEAL"},
            {"fixture_id": "H-3", "predicted_decision": "MANUAL_REVIEW"},
        ])

    result = evaluate_predictions(manifest_path, predictions_path)

    assert result["accuracy"] == 2 / 3
    assert result["false_seal_count"] == 1
    assert result["false_seal_rate"] == 0.5
    assert result["missing_predictions"] == 0


def test_calibration_gate_rejects_incomplete_double_labeled_holdout(tmp_path):
    manifest_path = tmp_path / "manifest.csv"
    fields = ["fixture_id", "split", "annotator_a_decision", "annotator_b_decision", "adjudicated_decision"]
    with manifest_path.open("w", newline="", encoding="utf-8") as manifest_file:
        writer = csv.DictWriter(manifest_file, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"fixture_id": "H-1", "split": "held_out"})

    result = validate_double_labeled_holdout(manifest_path)

    assert result["ready"] is False
    assert result["calibration_status"] == "NOT_READY"
    assert result["held_out_count"] == 1
    assert any("at least 50" in issue for issue in result["issues"])


def test_auto_seal_policy_blocks_by_default(monkeypatch):
    monkeypatch.delenv("PACKGUARD_AUTO_SEAL_ENABLED", raising=False)
    result = auto_seal_policy(
        {"calibration_status": "READY", "held_out_count": 50, "false_seal_rate": 0.0},
        {"calibration_status": "VALIDATED", "candidate_decision": "SEAL"},
    )
    assert result["auto_seal_allowed"] is False
    assert result["status"] == "BLOCKED"


def test_seal_candidate_requires_explicit_operator_confirmation():
    candidate = {"verdict": "PASS", "decision": "SEAL", "action": "SEAL", "reason": "Contents match."}

    pending = require_operator_confirmation(candidate, "")
    confirmed = require_operator_confirmation(candidate, "seal")

    assert pending["decision"] == "MANUAL_REVIEW"
    assert pending["action"] == "MANUAL_REVIEW"
    assert "operator" in pending["reason"].lower()
    assert confirmed["decision"] == "SEAL"
    assert candidate["decision"] == "SEAL"


def test_capture_keeps_unconfirmed_seal_candidate_in_manual_review():
    client = app.test_client()
    login(client)

    response = client.post(
        "/capture",
        data={
            "unit_id": "UNIT-UNCONFIRMED-SEAL",
            "order_id": "ORD-UNCONFIRMED-SEAL",
            "order_lines": "SKU-CABLE-USBC:1",
            "observed_in_box": "SKU-CABLE-USBC:1",
        },
    )
    record_id = response.headers["Location"].split("/")[-1].split("?")[0]
    record = next(item for item in client.get("/api/records").get_json()["records"] if item["record_id"] == record_id)

    assert record["evidence"]["verdict"] == "PASS"
    assert record["evidence"]["decision"] == "MANUAL_REVIEW"
    assert record["evidence"]["source"]["inspection"]["status"] == "NOT_PROVIDED"
    assert record["workflow_state"] == "MANUAL_REVIEW"


def test_agent_assessment_recommends_candidate_but_requires_human_confirmation():
    assessment = assess_pack(
        verifier_result={"verdict": "UNCERTAIN", "decision": "MANUAL_REVIEW"},
        inspection={
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "candidate_decision": "SEAL",
            "suggested_observed_contents": "SKU-CABLE-USBC:1",
            "image_quality": {"status": "GOOD"},
            "uncertainties": [],
            "calibration_status": "UNVALIDATED",
        },
        operator_observation="",
    )

    assert assessment["recommendation"] == "SEAL"
    assert assessment["suggested_observed_contents"] == "SKU-CABLE-USBC:1"
    assert assessment["requires_human_confirmation"] is True
    assert "cannot authorize SEAL" in assessment["next_action"]


def test_agent_assessment_routes_poor_image_to_recapture():
    assessment = assess_pack(
        verifier_result={"verdict": "UNCERTAIN", "decision": "MANUAL_REVIEW"},
        inspection={
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "image_quality": {"status": "POOR"},
            "uncertainties": [],
        },
        operator_observation="",
    )
    assert assessment["recommendation"] == "RECAPTURE"


def test_vision_timeout_returns_safe_manual_review(tmp_path, monkeypatch):
    monkeypatch.setenv("PACKGUARD_VISION_MODEL", "gemma3:4b")
    image_path = tmp_path / "pack.png"
    Image.new("RGB", (24, 24), "white").save(image_path)
    with patch("vision.urlopen", side_effect=TimeoutError):
        inspection = inspect_image(image_path, [])

    assessment = assess_pack(
        verifier_result={"verdict": "UNCERTAIN", "decision": "MANUAL_REVIEW"},
        inspection=inspection,
        operator_observation="",
    )

    assert inspection["status"] == "MODEL_TIMEOUT"
    assert assessment["recommendation"] == "MANUAL_REVIEW"
    assert "manually" in assessment["next_action"]


def test_vision_rejects_incomplete_json_as_a_failed_inspection(tmp_path, monkeypatch):
    monkeypatch.setenv("PACKGUARD_VISION_MODEL", "gemma3:4b")
    image_path = tmp_path / "pack.png"
    Image.new("RGB", (24, 24), "white").save(image_path)
    response = {"message": {"content": json.dumps({"detected_items": []})}}
    with patch("vision.urlopen", return_value=BytesIO(json.dumps(response).encode("utf-8"))):
        result = inspect_image(image_path, [])

    assessment = assess_pack(
        verifier_result={"verdict": "UNCERTAIN", "decision": "MANUAL_REVIEW"},
        inspection=result,
        operator_observation="",
    )
    assert result["status"] == "INVALID_MODEL_RESPONSE"
    assert assessment["recommendation"] == "MANUAL_REVIEW"
    assert result["model_response_excerpt"]


def test_moondream_presence_hint_never_infers_quantity(tmp_path, monkeypatch):
    image_path = tmp_path / "pack.png"
    Image.new("RGB", (24, 24), "white").save(image_path)
    monkeypatch.setenv("PACKGUARD_VISION_MODEL", "moondream:1.8b")
    response = {"message": {"content": "!!!MATCH!!!"}}
    with patch("vision.urlopen", return_value=BytesIO(json.dumps(response).encode("utf-8"))):
        result = inspect_image(image_path, [{"sku": "SKU-CABLE-USBC", "product_name": "USB-C cable"}])

    assert result["presence_hint"] == "MATCH"
    assert result["detected_items"][0]["sku"] == "SKU-CABLE-USBC"
    assert result["detected_items"][0]["quantity"] is None
    assessment = assess_pack(
        verifier_result={"verdict": "UNCERTAIN", "decision": "MANUAL_REVIEW"},
        inspection=result,
        operator_observation="",
    )
    assert assessment["recommendation"] == "MANUAL_REVIEW"


def test_moondream_routes_multiple_expected_skus_to_manual_review(tmp_path, monkeypatch):
    image_path = tmp_path / "pack.png"
    Image.new("RGB", (24, 24), "white").save(image_path)
    monkeypatch.setenv("PACKGUARD_VISION_MODEL", "moondream:1.8b")
    result = inspect_image(image_path, [{"sku": "SKU-A"}, {"sku": "SKU-B"}])
    assert result["status"] == "MULTIPLE_CANDIDATES_UNSUPPORTED"
    assessment = assess_pack(
        verifier_result={"verdict": "UNCERTAIN", "decision": "MANUAL_REVIEW"},
        inspection=result,
        operator_observation="",
    )
    assert assessment["recommendation"] == "MANUAL_REVIEW"


def test_image_only_clear_match_emits_seal_candidate_but_not_final_seal():
    workflow = run_pack_agent_workflow(
        {
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "detected_items": [{"sku": "SKU-A", "quantity": 1, "confidence": 0.96, "variant_match": True}],
            "extra_items": [],
            "image_quality": {"status": "GOOD"},
            "occlusion": {"status": "CLEAR"},
            "uncertainties": [],
            "confidence": 0.96,
            "calibration_status": "UNVALIDATED",
        },
        expected_lines="SKU-A:1",
        observed_contents="",
    )

    assert workflow["candidate_decision"] == "SEAL"
    assert workflow["decision"] == "manual_review"
    assert workflow["action"] == "request_operator_confirmation"
    assert workflow["trace"]["decision_basis"]["automatic_seal_authorized"] is False


def test_image_only_clear_mismatch_routes_to_fix_candidate():
    workflow = run_pack_agent_workflow(
        {
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "detected_items": [{"sku": "SKU-B", "quantity": 1, "confidence": 0.96, "variant_match": True}],
            "extra_items": [],
            "image_quality": {"status": "GOOD"},
            "occlusion": {"status": "CLEAR"},
            "uncertainties": [],
            "confidence": 0.96,
            "calibration_status": "UNVALIDATED",
        },
        expected_lines="SKU-A:1",
        observed_contents="",
    )

    assert workflow["candidate_decision"] == "FIX"
    assert workflow["decision"] == "fix"
    assert workflow["action"] == "open_fix_workflow"


def test_presence_only_model_never_infers_quantity_or_seal():
    workflow = run_pack_agent_workflow(
        {
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "presence_hint": "MATCH",
            "detected_items": [{"sku": "SKU-A", "quantity": None}],
            "extra_items": [],
            "image_quality": {"status": "UNKNOWN"},
            "uncertainties": ["The model did not verify quantity."],
        },
        expected_lines="SKU-A:1",
        observed_contents="",
    )

    assert workflow["candidate_decision"] == "MANUAL_REVIEW"
    assert workflow["decision"] == "manual_review"
    assert workflow["vision_observation"] == ""


def test_extra_visual_item_blocks_seal_candidate_and_routes_to_fix():
    workflow = run_pack_agent_workflow(
        {
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "detected_items": [{"sku": "SKU-A", "quantity": 1, "confidence": 0.98, "variant_match": True}],
            "extra_items": [{"description": "Unordered red bottle", "confidence": 0.97}],
            "image_quality": {"status": "GOOD"},
            "occlusion": {"status": "CLEAR"},
            "uncertainties": [],
            "confidence": 0.98,
        },
        expected_lines="SKU-A:1",
        observed_contents="",
    )

    assert workflow["candidate_decision"] == "FIX"
    assert workflow["decision"] == "fix"
    assert workflow["action"] == "open_fix_workflow"


def test_wrong_variant_blocks_seal_candidate_and_routes_to_fix():
    workflow = run_pack_agent_workflow(
        {
            "status": "SUGGESTIONS_READY_UNCALIBRATED",
            "detected_items": [{"sku": "SKU-A", "quantity": 1, "confidence": 0.98, "variant_match": False}],
            "extra_items": [],
            "image_quality": {"status": "GOOD"},
            "occlusion": {"status": "CLEAR"},
            "uncertainties": [],
            "confidence": 0.98,
        },
        expected_lines="SKU-A:1",
        observed_contents="",
    )

    assert workflow["candidate_decision"] == "FIX"
    assert workflow["decision"] == "fix"
    assert workflow["action"] == "open_fix_workflow"


def test_predict_holdout_uses_full_catalog_for_default_gemma(monkeypatch):
    monkeypatch.delenv("PACKGUARD_VISION_MODEL", raising=False)
    from predict_holdout import CATALOG, candidates_for

    assert candidates_for({"sku": "SKU-CABLE-USBC"}) == CATALOG


def test_auto_seal_policy_allows_only_explicit_validated_configuration(monkeypatch):
    monkeypatch.setenv("PACKGUARD_AUTO_SEAL_ENABLED", "true")
    monkeypatch.setenv("PACKGUARD_MAX_FALSE_SEAL_RATE", "0.01")
    result = auto_seal_policy(
        {"calibration_status": "READY", "held_out_count": 50, "false_seal_rate": 0.0},
        {"calibration_status": "VALIDATED", "candidate_decision": "SEAL"},
    )
    assert result["auto_seal_allowed"] is True
    assert result["status"] == "ALLOWED"


def test_production_readiness_reports_missing_external_infrastructure(monkeypatch):
    monkeypatch.setenv("PACKGUARD_ENV", "development")
    monkeypatch.setenv("PACKGUARD_ALLOW_DEMO_LOGIN", "true")
    report = readiness_report()
    assert report["ready"] is False
    assert report["checks"]["production_environment"] is False
    assert report["checks"]["postgres_adapter_implemented"] is False
    assert report["checks"]["csrf_protection_implemented"] is True
    assert report["checks"]["shared_login_throttling_implemented"] is True
    assert report["automatic_seal"] == "blocked_until_calibration"


def test_readiness_distinguishes_adapters_from_live_infrastructure(monkeypatch):
    monkeypatch.setenv("PACKGUARD_ENV", "production")
    monkeypatch.setenv("PACKGUARD_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PACKGUARD_ALLOW_DEMO_LOGIN", "false")
    monkeypatch.setenv("PACKGUARD_PUBLIC_URL", "https://packguard.example.test")
    monkeypatch.setenv("PACKGUARD_DATABASE_URL", "postgresql://example/db")
    monkeypatch.setenv("PACKGUARD_OBJECT_STORAGE_URL", "s3://bucket")

    report = readiness_report()

    assert report["ready"] is False
    assert report["configured_postgres_url_present"] is True
    assert report["configured_object_storage_url_present"] is True
    assert report["checks"]["postgres_adapter_implemented"] is False
    assert report["checks"]["private_object_storage_adapter_implemented"] is True
    assert report["checks"]["external_identity_provider_implemented"] is False


def test_readiness_recognizes_https_oidc_configuration(monkeypatch):
    monkeypatch.setenv("PACKGUARD_OIDC_ISSUER_URL", "https://identity.example.test/tenant")
    monkeypatch.setenv("PACKGUARD_OIDC_CLIENT_ID", "packguard-client")
    monkeypatch.setenv("PACKGUARD_OIDC_CLIENT_SECRET", "test-secret-not-returned")
    monkeypatch.setenv("PACKGUARD_OIDC_REDIRECT_URI", "https://packguard.example.test/auth/callback")

    report = readiness_report()

    assert report["checks"]["external_identity_provider_implemented"] is True
    assert "test-secret-not-returned" not in repr(report)


def test_production_health_reports_live_database_and_private_storage_checks():
    response = app.test_client().get("/health/production")

    assert response.status_code == 503
    report = response.get_json()
    assert report["checks"]["database_reachable"] is True
    assert report["checks"]["migrations_current"] is True
    assert report["checks"]["private_object_storage_reachable"] is False
    assert report["automatic_seal"] == "blocked_until_calibration"
