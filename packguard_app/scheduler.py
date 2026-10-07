"""Background scheduler for automatic Stage‑3 processing.

The scheduler runs in a daemon thread started when the Flask app is imported.
It periodically scans the database for Stage‑2 (prep) records that have not yet been
processed by the Stage‑3 agent and invokes the local `/api/agent/stage3` endpoint
for each pending record.

Configuration:
- `SCAN_INTERVAL_SECONDS` – how often to run the scan (default 30 s).
- `ORG_ID` – organization to operate on (uses the `AGENT_API_ORG_ID` env var if set).

The scheduler is lightweight and does not depend on external job libraries, keeping
the project footprint small.
"""

import os
import threading
import time
import json
from urllib.parse import urljoin

import requests

# Local imports – the path works because this file lives in the same package as app.py
from packguard_app import database
from packguard_app import app  # noqa: F401 – ensures Flask app is initialized

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SECONDS = int(os.getenv("STAGE3_SCHEDULER_INTERVAL", "30"))
# Use the same org ID that the rest of the system defaults to for the agent API.
ORG_ID = os.getenv("AGENT_API_ORG_ID", "default-org")

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _is_stage2_record(record: dict) -> bool:
    """Return True if the record looks like a Stage‑2 (prep) record.

    The convention used elsewhere is that Stage‑2 records have IDs prefixed with
    ``PREP-``.  If the naming scheme ever changes, adjust this function.
    """
    return str(record.get("record_id", "")).startswith("PREP-")


def _stage3_id_for(record: dict) -> str:
    """Derive the expected Stage‑3 record ID for a given Stage‑2 record.

    Example: ``PREP-12345`` -> ``STG3-12345``.
    """
    return "STG3-" + record["record_id"][5:]


def _has_stage3_record(stage3_id: str) -> bool:
    """Check whether a Stage‑3 record already exists.
    """
    return database.get_record(stage3_id, ORG_ID) is not None


def _invoke_stage3_api(product_id: str) -> None:
    """Call the local Stage‑3 endpoint.

    The endpoint is part of the same Flask app, so we use a plain HTTP request to the
    running server (assumed to be on ``localhost:5000``).  Adjust the URL if the
    app runs on a different host/port.
    """
    url = urljoin("http://localhost:5000", "/api/agent/stage3")
    payload = {"product_id": product_id, "image_base64": None}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        # Logging is optional; we just print a concise message.
        print(f"[Stage3 Scheduler] Triggered Stage‑3 for {product_id}: {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        print(f"[Stage3 Scheduler] Failed to trigger Stage‑3 for {product_id}: {exc}")


def _process_pending_records() -> None:
    """Main loop that scans for pending Stage‑2 records and triggers Stage‑3.
    """
    # Retrieve a batch of recent records – the limit keeps the query cheap.
    records = database.list_records(ORG_ID, limit=200)
    for rec in records:
        if not _is_stage2_record(rec):
            continue
        stage3_id = _stage3_id_for(rec)
        if _has_stage3_record(stage3_id):
            continue
        # Extract the product identifier – we assume the suffix after ``PREP-``
        product_id = rec["record_id"][5:]
        _invoke_stage3_api(product_id)


def _scheduler_loop() -> None:
    """Continuously run the pending‑record processor at the configured interval."""
    while True:
        try:
            _process_pending_records()
        except Exception as exc:  # noqa: BLE001
            print(f"[Stage3 Scheduler] Unexpected error: {exc}")
        time.sleep(SCAN_INTERVAL_SECONDS)


def start_stage3_scheduler() -> None:
    """Start the background thread (daemon) that runs the scheduler.

    This function is safe to call multiple times – it will start a new thread only
    if one is not already alive.
    """
    if getattr(start_stage3_scheduler, "_thread", None) and start_stage3_scheduler._thread.is_alive():
        return
    thread = threading.Thread(target=_scheduler_loop, name="Stage3Scheduler", daemon=True)
    thread.start()
    start_stage3_scheduler._thread = thread
    print("[Stage3 Scheduler] Started background scheduler thread.")

# Automatically start the scheduler when this module is imported (i.e., when the app
# starts).  Importing ``start_stage3_scheduler`` from ``app.py`` ensures the side‑
# effect occurs exactly once.
start_stage3_scheduler()

