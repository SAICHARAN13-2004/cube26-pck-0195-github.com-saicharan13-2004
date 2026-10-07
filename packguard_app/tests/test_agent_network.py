import json
import pytest
from app import app
from a2a import execute_a2a_transfer, handle_agent_command
from database import (
    connect,
    get_agent_transfer,
    list_agent_transfers,
    list_agent_messages,
)


def login(client, username="alpha.operator", password="alpha-demo"):
    return client.post("/login", data={"username": username, "password": password})


def login_bravo(client, username="bravo.operator", password="bravo-demo"):
    return client.post("/login", data={"username": username, "password": password})


def test_schema_migration_tables_exist():
    with connect("__system__") as conn:
        tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "agent_transfers" in tables
        assert "agent_messages" in tables


def test_a2a_normal_transfer_execution():
    result = execute_a2a_transfer(
        org_id="org_demo_alpha",
        product_sku="SKU-CABLE-USBC",
        product_name="USB-C cable",
        quantity=2,
        source_point="Supplier Intake Dock (Point A)",
        destination_point="PackGuard Packing Pod 03 (Point B)",
        scenario="normal",
    )

    assert result["verdict"] == "PASS"
    assert result["status"] == "COMPLETED"
    assert result["decision"] == "SEAL"
    assert len(result["messages"]) == 5
    assert result["evidence_hash"] is not None

    # Verify message flow
    msgs = result["messages"]
    assert msgs[0]["message_type"] == "HANDSHAKE_REQUEST"
    assert msgs[1]["message_type"] == "HANDSHAKE_ACK"
    assert msgs[2]["message_type"] == "DISPATCH_MANIFEST"
    assert msgs[3]["message_type"] == "INSPECTION_RUN"
    assert msgs[4]["message_type"] == "TRANSFER_CONFIRMED"

    # Verify DB persistence
    transfer_in_db = get_agent_transfer(result["transfer_id"], "org_id_placeholder" if False else "org_demo_alpha")
    assert transfer_in_db is not None
    assert transfer_in_db["product_sku"] == "SKU-CABLE-USBC"
    assert transfer_in_db["verdict"] == "PASS"

    messages_in_db = list_agent_messages(result["transfer_id"], "org_demo_alpha")
    assert len(messages_in_db) == 5


def test_a2a_discrepancy_short_transfer():
    result = execute_a2a_transfer(
        org_id="org_demo_alpha",
        product_sku="SKU-BOTTLE-750",
        product_name="750 mL bottle",
        quantity=3,
        source_point="Supplier Intake Dock (Point A)",
        destination_point="PackGuard Packing Pod 03 (Point B)",
        scenario="short",
    )

    assert result["verdict"] == "FAIL"
    assert result["decision"] == "FIX"
    assert result["status"] == "REJECTED_DISCREPANCY"
    assert "Discrepancy" in result["summary"] or "short" in result["summary"].lower()
    assert result["messages"][4]["message_type"] == "TRANSFER_HALTED"


def test_a2a_uncertain_damaged_transfer():
    result = execute_a2a_transfer(
        org_id="org_demo_alpha",
        product_sku="SKU-LAMP-LED",
        product_name="LED lamp",
        quantity=1,
        source_point="Inbound Prep Center Pod 02 (Point A)",
        destination_point="PackGuard Packing Pod 03 (Point B)",
        scenario="uncertain",
    )

    assert result["verdict"] == "UNCERTAIN"
    assert result["decision"] == "MANUAL_REVIEW"
    assert result["status"] == "HOLD_FOR_REVIEW"


def test_handle_agent_command():
    # Command to transfer
    res = handle_agent_command("Transfer 2 units of SKU-CABLE-USBC from Point A to Point B", "org_demo_alpha")
    assert res["action_taken"] == "A2A_TRANSFER_EXECUTED"
    assert "Autonomous Agent Dispatch complete" in res["answer"]
    assert res["transfer"]["verdict"] == "PASS"

    # Command to check status
    status_res = handle_agent_command("What is your agent status?", "org_demo_alpha")
    assert status_res["action_taken"] == "STATUS_REPORT"
    assert "PackGuard Autonomous Agent Station" in status_res["answer"]


def test_agent_network_web_routes():
    with app.test_client() as client:
        # Before login, redirect to login
        resp = client.get("/agent-network")
        assert resp.status_code == 302

        # Login
        login(client)

        # GET /agent-network
        resp = client.get("/agent-network")
        assert resp.status_code == 200
        assert b"Agent workflow" in resp.data
        assert b"Prep inbox" in resp.data
        assert b"Pack assessments" in resp.data
        assert b"Returns handoff" in resp.data
        assert b"Transfer Verification Scenario" not in resp.data
        assert b"Connected Peer: Origin Inbound" not in resp.data

        # Simulated transfer endpoint is retired
        transfer_payload = {
            "product_sku": "SKU-CABLE-USBC",
            "quantity": 2,
            "source_point": "Supplier Intake Dock (Point A)",
            "destination_point": "PackGuard Packing Pod 03 (Point B)",
            "scenario": "normal",
        }
        post_resp = client.post(
            "/api/agent/transfer",
            json=transfer_payload,
        )
        assert post_resp.status_code == 410
        assert "no longer available" in post_resp.get_json()["error"]

        # Old simulation history and transcripts are retired too
        list_resp = client.get("/api/agent/transfers")
        assert list_resp.status_code == 410

        detail_resp = client.get("/api/agent/transfers/legacy-demo-id")
        assert detail_resp.status_code == 410

        # Agent status reports real workflow state without simulating a transfer
        cmd_resp = client.post("/api/agent/command", json={"command": "agent status"})
        assert cmd_resp.status_code == 200
        assert "Stage-2 prep record(s)" in cmd_resp.get_json()["answer"]
        fake_transfer = client.post(
            "/api/agent/command",
            json={"command": "transfer 2 units of SKU-CABLE-USBC"},
        )
        assert fake_transfer.status_code == 410


def test_tenant_isolation_for_transfers():
    # Create transfer in alpha
    alpha_transfer = execute_a2a_transfer(
        org_id="org_demo_alpha",
        product_sku="SKU-CABLE-USBC",
        product_name="USB-C cable",
        quantity=1,
        source_point="Supplier Intake Dock (Point A)",
        destination_point="PackGuard Packing Pod 03 (Point B)",
    )

    with app.test_client() as client:
        # Login as bravo
        login_bravo(client)

        # The deprecated simulation API cannot expose legacy transfers
        list_resp = client.get("/api/agent/transfers")
        assert list_resp.status_code == 410

        # Direct legacy transcript requests are retired
        direct_resp = client.get(f"/api/agent/transfers/{alpha_transfer['transfer_id']}")
        assert direct_resp.status_code == 410


def test_diagnose_pack_contents_problem_statement_example():
    from verifier import diagnose_pack_contents

    catalog = {
        "SKU-TSHIRT-BLK": "Black T-Shirt",
        "SKU-CAP-BLU": "Blue Cap",
        "SKU-CAP-RED": "Red Cap",
    }

    # Problem Statement Scenario:
    # Expected: 2 x Black T-Shirt, 1 x Blue Cap
    # Detected: 2 x Black T-Shirt, 1 x Red Cap
    result = diagnose_pack_contents(
        "SKU-TSHIRT-BLK:2;SKU-CAP-BLU:1",
        "SKU-TSHIRT-BLK:2;SKU-CAP-RED:1",
        catalog_names=catalog,
    )

    assert result["status"] == "STOP & FIX"
    assert result["decision"] == "STOP & FIX"
    assert result["is_wrong_package"] is True
    assert len(result["wrong_items"]) == 1
    assert result["wrong_items"][0]["expected_name"] == "Blue Cap"
    assert result["wrong_items"][0]["detected_name"] == "Red Cap"
    assert "Expected Blue Cap, Detected Red Cap" in result["issues"]
    assert len(result["items_present"]) == 1
    assert result["items_present"][0]["name"] == "Black T-Shirt"


def test_diagnose_pack_contents_missing_and_extra():
    from verifier import diagnose_pack_contents

    catalog = {
        "SKU-TSHIRT-BLK": "Black T-Shirt",
        "SKU-CAP-BLU": "Blue Cap",
    }

    # Missing item
    missing_res = diagnose_pack_contents(
        "SKU-TSHIRT-BLK:2;SKU-CAP-BLU:1",
        "SKU-TSHIRT-BLK:2",
        catalog_names=catalog,
    )
    assert missing_res["status"] == "STOP & FIX"
    assert missing_res["is_wrong_package"] is True
    assert len(missing_res["missing_items"]) == 1
    assert missing_res["missing_items"][0]["name"] == "Blue Cap"

    # Exact match -> SEAL
    match_res = diagnose_pack_contents(
        "SKU-TSHIRT-BLK:2;SKU-CAP-BLU:1",
        "SKU-TSHIRT-BLK:2;SKU-CAP-BLU:1",
        catalog_names=catalog,
    )
    assert match_res["status"] == "SEAL"
    assert match_res["decision"] == "SEAL"
    assert match_res["is_wrong_package"] is False
    assert len(match_res["items_present"]) == 2


def test_two_box_alerts_page_and_verify_api():
    with app.test_client() as client:
        login(client)

        # GET /alerts
        resp = client.get("/alerts")
        assert resp.status_code == 200
        assert b"Stage 2 input" in resp.data
        assert b"Stage 3 output" in resp.data
        assert b'aria-label="Open alerts"' in resp.data
        assert b'aria-label="Log out"' in resp.data
        assert resp.data.count(b'aria-label="Open alerts"') == 1
        assert resp.data.count(b'aria-label="Log out"') == 1
        assert b"Autonomous Verification Agent: Online" not in resp.data
        assert b"Active Peer:" not in resp.data
        assert b">Alerts</a>" not in resp.data
        assert b"PCK-ALERT-WRONG-01" not in resp.data
        assert b"INB-TRF-0912" not in resp.data
        assert b"INB-TRF-0844" not in resp.data

        # POST /api/verify-pack
        post_resp = client.post(
            "/api/verify-pack",
            json={
                "expected": "SKU-TSHIRT-BLK:2;SKU-CAP-BLU:1",
                "observed": "SKU-TSHIRT-BLK:2;SKU-CAP-RED:1",
            },
        )
        assert post_resp.status_code == 200
        data = post_resp.get_json()
        assert data["decision"] == "STOP & FIX"
        assert data["is_wrong_package"] is True
        assert any("Expected Blue Cap, Detected Red Cap" in issue for issue in data["issues"])

