"""PackGuard operator application."""

import csv
from base64 import urlsafe_b64decode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import re
from secrets import token_urlsafe
import time
from io import BytesIO, StringIO
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, send_file, send_from_directory, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from database import APP_DIR, DB_PATH, append_contract_override, clear_login_attempts, complete_contract_capture, connect, create_contract_capture, get_contract_capture, get_contract_record, get_oidc_user, get_product, get_record, get_user, init_db, link_oidc_identity, list_audit_events, list_contract_records, list_products, list_records, list_session_records, list_support_requests, list_unit_events, login_blocked_until, record_login_failure, reset_organization_context, save_audit_event, save_contract_record, save_product, save_record, save_support_request, save_user, set_organization_context, update_contract_record, update_workflow_state, user_exists
from detector import observe, observe_image
from evedince import build_evidence
from evaluation import summarize_records
from policy import auto_seal_policy, require_operator_confirmation
from agent import assess_pack, enforce_component_verdict
from orchestrator import FAILED_VISION_STATUSES, run_pack_agent_workflow
from unit_journey import build_journey
from production import readiness_report
from storage import create_media_storage
from verifier import diagnose_pack_contents, parse_lines, verify_pack
from vision import inspect_image
from contract import SUBJECT_FIELDS, build_record as build_contract_record, content_hash, organization_uuid, utc_now, validate_record


app = Flask(__name__, template_folder=str(APP_DIR / "template"), static_folder=str(APP_DIR / "static"))
CONTRACT_VISION_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="packguard-contract-vision")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
DEPLOYMENT_ENV = os.environ.get("PACKGUARD_ENV", "development").strip().lower()
IS_PRODUCTION = DEPLOYMENT_ENV == "production"
secret_key = os.environ.get("PACKGUARD_SECRET_KEY")
if IS_PRODUCTION and (not secret_key or len(secret_key) < 32):
	raise RuntimeError("PACKGUARD_SECRET_KEY must be set to at least 32 characters in production")
app.config["SECRET_KEY"] = secret_key or "packguard-local-development-key"
app.config.update(
	SESSION_COOKIE_HTTPONLY=True,
	SESSION_COOKIE_SAMESITE="Lax",
	SESSION_COOKIE_SECURE=IS_PRODUCTION,
	WTF_CSRF_ENABLED=IS_PRODUCTION,
)
csrf = CSRFProtect(app)
oauth = OAuth(app)
try:
	TRUSTED_PROXY_COUNT = int(os.environ.get("PACKGUARD_TRUSTED_PROXY_COUNT", "0"))
except ValueError as error:
	raise RuntimeError("PACKGUARD_TRUSTED_PROXY_COUNT must be a non-negative integer") from error
if TRUSTED_PROXY_COUNT < 0:
	raise RuntimeError("PACKGUARD_TRUSTED_PROXY_COUNT must be a non-negative integer")
if TRUSTED_PROXY_COUNT:
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_COUNT, x_proto=TRUSTED_PROXY_COUNT)
DEFAULT_ORG = "org_demo_alpha"
ORGS = {"org_demo_alpha": "Alpha operations", "org_demo_bravo": "Bravo operations"}
DEMO_USERS = (
	("alpha.operator", "alpha-demo", "org_demo_alpha"),
	("bravo.operator", "bravo-demo", "org_demo_bravo"),
)
ALLOW_DEMO_LOGIN = os.environ.get("PACKGUARD_ALLOW_DEMO_LOGIN", "true").strip().lower() in {"1", "true", "yes"}
OIDC_ISSUER_URL = os.environ.get("PACKGUARD_OIDC_ISSUER_URL", "").strip().rstrip("/")
OIDC_CLIENT_ID = os.environ.get("PACKGUARD_OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.environ.get("PACKGUARD_OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get("PACKGUARD_OIDC_REDIRECT_URI", "").strip()
OIDC_ENABLED = bool(
	OIDC_ISSUER_URL and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET
	and (not IS_PRODUCTION or (OIDC_ISSUER_URL.startswith("https://") and OIDC_REDIRECT_URI.startswith("https://")))
)
PREP_AGENT_API_TOKEN = os.environ.get("PACKGUARD_PREP_AGENT_TOKEN", "").strip()
PACK_FEED_AGENT_API_TOKEN = os.environ.get("PACKGUARD_PACK_FEED_TOKEN", "").strip()
UNIT_EVENT_API_TOKEN = os.environ.get("PACKGUARD_UNIT_EVENT_TOKEN", "").strip()
AGENT_API_ORG_ID = os.environ.get("PACKGUARD_AGENT_API_ORG_ID", "").strip()
RETURNS_AGENT_WEBHOOK_URL = os.environ.get("PACKGUARD_RETURNS_AGENT_URL", "").strip()
RETURNS_AGENT_WEBHOOK_TOKEN = os.environ.get("PACKGUARD_RETURNS_AGENT_TOKEN", "").strip()
if (PREP_AGENT_API_TOKEN or PACK_FEED_AGENT_API_TOKEN or UNIT_EVENT_API_TOKEN or RETURNS_AGENT_WEBHOOK_URL or RETURNS_AGENT_WEBHOOK_TOKEN) and not AGENT_API_ORG_ID:
	raise RuntimeError("PACKGUARD_AGENT_API_ORG_ID is required when machine agent tokens are configured")
if any(token and len(token) < 32 for token in (PREP_AGENT_API_TOKEN, PACK_FEED_AGENT_API_TOKEN, UNIT_EVENT_API_TOKEN, RETURNS_AGENT_WEBHOOK_TOKEN)):
	raise RuntimeError("Machine agent tokens must contain at least 32 characters")
if bool(RETURNS_AGENT_WEBHOOK_URL) != bool(RETURNS_AGENT_WEBHOOK_TOKEN):
	raise RuntimeError("PACKGUARD_RETURNS_AGENT_URL and PACKGUARD_RETURNS_AGENT_TOKEN must be configured together")
if RETURNS_AGENT_WEBHOOK_URL:
	webhook_parts = urlsplit(RETURNS_AGENT_WEBHOOK_URL)
	if webhook_parts.scheme not in {"http", "https"} or not webhook_parts.netloc:
		raise RuntimeError("PACKGUARD_RETURNS_AGENT_URL must be an absolute HTTP(S) URL")
	if IS_PRODUCTION and webhook_parts.scheme != "https":
		raise RuntimeError("PACKGUARD_RETURNS_AGENT_URL must use HTTPS in production")
SAMPLE_PRODUCT_NAMES = {
	"SKU-CABLE-USBC": "USB-C cable",
	"SKU-BOTTLE-750": "750 mL bottle",
	"SKU-PUZZLE-500": "500-piece puzzle",
	"SKU-TOWEL-BLU": "Blue towel",
	"SKU-CANDLE-3": "Candle (variant unspecified)",
	"SKU-LAMP-LED": "LED lamp",
	"SKU-SERUM-30": "30 mL serum bottle",
	"SKU-PROT-1KG": "Unspecified 1 kg product",
	"SKU-MUG-11": "Mug (variant unspecified)",
	"SKU-LEASH-6FT": "6 ft leash",
	"SKU-TSHIRT-BLK": "Black T-Shirt",
	"SKU-CAP-BLU": "Blue Cap",
	"SKU-CAP-RED": "Red Cap",
}
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}
UPLOAD_DIR = Path(os.environ.get("PACKGUARD_UPLOAD_DIR", APP_DIR / "uploads"))
MEDIA_STORAGE = create_media_storage(UPLOAD_DIR)
if IS_PRODUCTION and MEDIA_STORAGE.backend != "s3":
	raise RuntimeError("PACKGUARD_OBJECT_STORAGE_URL must configure private S3-compatible storage in production")


def get_oidc_client():
	if not OIDC_ENABLED:
		return None
	return oauth.register(
		name="packguard_oidc",
		client_id=OIDC_CLIENT_ID,
		client_secret=OIDC_CLIENT_SECRET,
		server_metadata_url=f"{OIDC_ISSUER_URL}/.well-known/openid-configuration",
		client_kwargs={"scope": "openid email profile"},
	)


@app.after_request
def add_security_headers(response):
	response.headers.setdefault("X-Content-Type-Options", "nosniff")
	response.headers.setdefault("X-Frame-Options", "DENY")
	response.headers.setdefault("Referrer-Policy", "same-origin")
	response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'")
	if IS_PRODUCTION:
		response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
	return response


def current_org() -> str:
	return session.get("org_id", DEFAULT_ORG)


def agent_api_org(expected_token: str):
	if not expected_token or not AGENT_API_ORG_ID:
		return None, (jsonify({"error": "Machine-to-machine agent access is not configured."}), 503)
	authorization = request.headers.get("Authorization", "")
	scheme, separator, supplied_token = authorization.partition(" ")
	if (
		not separator or scheme.casefold() != "bearer"
		or not hmac.compare_digest(supplied_token, expected_token)
	):
		return None, (jsonify({"error": "Invalid agent credentials."}), 401)
	return AGENT_API_ORG_ID, None


def prep_record_is_ready(record: dict[str, Any]) -> bool:
	checks = record.get("checks", [])
	if record.get("status") != "complete" or not checks:
		return False
	latest_overrides = {
		item["check_key"]: item["to_verdict"]
		for item in record.get("overrides", [])
	}
	return all(
		latest_overrides.get(check["check_key"], check["verdict"]) == "pass"
		for check in checks
	)


def prep_record_logistics(record: dict[str, Any]) -> dict[str, str | None]:
	"""Read route metadata supplied by the upstream prep agent's check details."""
	fields = {
		"supplier_name": ("supplier_name", "supplier"),
		"origin_address": ("origin_address", "source_address", "ship_from"),
		"ordered_for": ("ordered_for", "recipient", "customer_name"),
		"delivery_address": ("delivery_address", "destination_address", "ship_to"),
	}
	result: dict[str, str | None] = {field: None for field in fields}
	for check in record.get("checks", []):
		detail = check.get("detail") or {}
		for source in (detail.get("logistics") or {}, detail):
			if not isinstance(source, dict):
				continue
			for field, aliases in fields.items():
				if result[field]:
					continue
				value = next((source.get(alias) for alias in aliases if source.get(alias)), None)
				if isinstance(value, str) and value.strip():
					result[field] = value.strip()
	return result


def build_stage3_contract_record(
	*,
	organization_key: str,
	prep_record: dict[str, Any],
	operator_label: str,
	expected_lines: str,
	observed_contents: str,
	pack_result: dict[str, Any],
	vision_result: dict[str, Any],
	product_routes: list[dict[str, Any]],
	photo_ref: str | None,
) -> dict[str, Any]:
	"""Build the downstream CUBE record for a prep-sourced legacy New Check."""
	prep_subject = prep_record["subject"]
	workflow = run_pack_agent_workflow(
		vision_result,
		expected_lines=expected_lines,
		observed_contents=observed_contents,
	)
	images = []
	if photo_ref:
		try:
			if photo_ref.startswith("fixtures/vision/"):
				image_bytes = (APP_DIR / photo_ref).read_bytes()
			else:
				image_bytes = MEDIA_STORAGE.read(photo_ref)
		except (OSError, ValueError):
			image_bytes = None
		if image_bytes:
			images.append({
				"key": photo_ref,
				"sha256": hashlib.sha256(image_bytes).hexdigest(),
				"bytes": len(image_bytes),
				"taken_at": utc_now(),
			})
	manual_observed = parse_lines(observed_contents)
	vision_observed = parse_lines(workflow["vision_observation"])
	observed_quantities = manual_observed or vision_observed
	observed_quantity = observed_quantities.get(str(prep_subject["sku"]))
	if not isinstance(observed_quantity, int) or isinstance(observed_quantity, bool):
		observed_quantity = None
	verdict = {"PASS": "pass", "FAIL": "fail", "UNCERTAIN": "uncertain"}.get(
		pack_result.get("verdict"), "uncertain",
	)
	vision_status = vision_result.get("status", "MODEL_ERROR")
	visual_verdict = "uncertain"
	visual_detail = {
		"status": vision_status,
		"presence_hint": vision_result.get("presence_hint"),
		"detected_items": vision_result.get("detected_items", []),
		"extra_items": vision_result.get("extra_items", []),
		"image_quality": vision_result.get("image_quality", {}),
		"occlusion": vision_result.get("occlusion", {}),
		"uncertainties": vision_result.get("uncertainties", []),
		"confidence": vision_result.get("confidence"),
		"vision_observation_candidate": workflow["vision_observation"] or None,
		"candidate_decision": workflow["candidate_decision"],
		"candidate_reason": workflow["candidate_reason"],
		"automatic_seal_authorized": False,
		"source_prep_record_id": prep_record["record_id"],
		"agent_run": workflow["trace"],
	}
	checks = [{
		"check_key": "sku_quantity",
		"verdict": verdict,
		"confidence": None,
		"detail": {
			"sku_checks": pack_result.get("checks", []),
			"expected_lines": expected_lines,
			"observed_contents": observed_contents or None,
			"source_prep_record_id": prep_record["record_id"],
			"product_routes": product_routes,
		},
		"model_version": "deterministic-packguard-v1",
		"latency_ms": 0,
	}, {
		"check_key": "visual_evidence",
		"verdict": visual_verdict,
		"confidence": vision_result.get("confidence"),
		"detail": visual_detail,
		"model_version": vision_result.get("provider", "unavailable"),
		"latency_ms": int(vision_result.get("inference_ms") or 0),
	}]
	return build_contract_record(
		organization_key=organization_key,
		client_id=None,
		agent="pack",
		subject={
			"type": "order",
			"asin": prep_subject.get("asin"),
			"sku": prep_subject.get("sku"),
			"order_id": prep_subject.get("order_id"),
			"po_line_id": prep_subject.get("po_line_id"),
			"shipment_id": prep_subject.get("shipment_id"),
			"quantity_expected": prep_subject.get("quantity_expected"),
			"quantity_observed": observed_quantity,
		},
		operator_label=operator_label,
		images=images,
		checks=checks,
		decision=workflow["decision"],
		decided_by="agent",
		status="pending" if vision_status in FAILED_VISION_STATUSES else "complete",
	)


def publish_pack_record(record: dict[str, Any]) -> bool | None:
	if not RETURNS_AGENT_WEBHOOK_URL or not RETURNS_AGENT_WEBHOOK_TOKEN:
		return None
	if record.get("organization_id") != organization_uuid(AGENT_API_ORG_ID):
		return None
	payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
	webhook_request = Request(
		RETURNS_AGENT_WEBHOOK_URL,
		data=payload,
		headers={
			"Authorization": f"Bearer {RETURNS_AGENT_WEBHOOK_TOKEN}",
			"Content-Type": "application/json",
			"Idempotency-Key": record["record_id"],
		},
		method="POST",
	)
	status_code = None
	try:
		with urlopen(webhook_request, timeout=10) as response:
			status_code = getattr(response, "status", 200)
			if not 200 <= status_code < 300:
				raise OSError(f"HTTP {status_code}")
	except (HTTPError, URLError, TimeoutError, OSError) as error:
		app.logger.warning(
			"Pack webhook delivery failed for %s (%s); the authenticated pack feed remains available",
			record["record_id"], type(error).__name__,
		)
		delivery_status = "FAILED"
		reason = "Stage-4 webhook delivery failed; retry or use the authenticated pack feed."
	else:
		delivery_status = "DELIVERED"
		reason = "Stage-4 agent acknowledged the Pack record."
	save_audit_event({
		"event_id": f"AGT-{uuid4().hex[:12].upper()}",
		"record_id": record["record_id"],
		"org_id": AGENT_API_ORG_ID,
		"event_type": f"RETURNS_AGENT_{delivery_status}",
		"reason": reason,
		"actor_id": "agent:packguard_stage3",
		"created_at": utc_now(),
		"details": {"http_status": status_code, "agent": "returns", "record_id": record["record_id"]},
	})
	return delivery_status == "DELIVERED"


@app.before_request
def bind_database_organization():
	if session.get("org_id"):
		organization_key = session["org_id"]
	elif request.endpoint in {"login", "oidc_callback"}:
		organization_key = "__auth__"
	elif request.endpoint == "contract_record_api" and request.method == "GET":
		organization_key = "__public__"
	else:
		organization_key = "__system__"
	g.organization_context_token = set_organization_context(organization_key)


@app.teardown_request
def reset_database_organization(_error=None):
	token = getattr(g, "organization_context_token", None)
	if token is not None:
		g.organization_context_token = None
		try:
			reset_organization_context(token)
		except RuntimeError:
			pass


def seed_demo_users() -> None:
	if IS_PRODUCTION or not ALLOW_DEMO_LOGIN:
		return
	for username, password, org_id in DEMO_USERS:
		save_user({
			"username": username,
			"password_hash": generate_password_hash(password),
			"org_id": org_id,
			"role": "operator",
			"created_at": datetime.now(timezone.utc).isoformat(),
		})


def login_required(view):
	@wraps(view)
	def wrapped_view(*args, **kwargs):
		if "username" not in session or "org_id" not in session:
			if request.path.startswith(("/api/", "/v1/")):
				return jsonify({"error": "authentication required"}), 401
			return redirect(url_for("login", next=request.path))
		return view(*args, **kwargs)
	return wrapped_view


def operator_agreement(operator_verdict: str | None, pack_verdict: str) -> str:
	"""Compare the human action with PackGuard's decision."""
	operator_verdict = (operator_verdict or "").strip().lower()
	if not operator_verdict:
		return "NOT_RECORDED"
	operator_decision = {"seal": "PASS", "stop_and_fix": "FAIL"}.get(operator_verdict)
	if not operator_decision:
		return "INVALID"
	return "AGREES" if operator_decision == pack_verdict else "DISAGREES"


def save_media(upload, org_id: str, record_id: str, media_type: str) -> str | None:
	if not upload or not upload.filename:
		return None
	filename = secure_filename(upload.filename)
	extension = Path(filename).suffix.lower().lstrip(".")
	allowed_extensions = ALLOWED_IMAGE_EXTENSIONS if media_type == "image" else ALLOWED_VIDEO_EXTENSIONS
	if not filename or extension not in allowed_extensions:
		return None
	stored_name = f"{uuid4().hex[:12]}_{filename}"
	media_key = f"{org_id}/{record_id}/{stored_name}"
	return MEDIA_STORAGE.save(media_key, upload, mimetypes.guess_type(filename)[0])


def save_photo(upload, org_id: str, record_id: str) -> str | None:
	return save_media(upload, org_id, record_id, "image")


def attach_catalog_metadata(result: dict[str, object], org_id: str) -> None:
	"""Attach tenant catalog identity to deterministic SKU checks."""
	for check in result.get("checks", []):
		sku = check.get("sku")
		if not sku:
			continue
		product = get_product(org_id, sku)
		if product:
			check["product"] = {
				"name": product["product_name"],
				"brand": product.get("brand"),
				"external_id": product.get("external_id"),
				"barcode": product.get("barcode"),
				"attributes": json.loads(product["attributes_json"]),
				"reference_image_ref": product.get("reference_image_ref"),
			}


def _sample_data_path() -> Path | None:
	for sample_path in (
		APP_DIR.parent / "data" / "pack_sample.csv",
		APP_DIR.parent / "cube-03-pack-manager" / "data" / "pack_sample.csv",
	):
		if sample_path.is_file():
			return sample_path
	return None


def seed_sample_data() -> None:
	sample_path = _sample_data_path()
	if sample_path is None:
		return
	with connect() as connection:
		already_seeded = connection.execute(
			"SELECT 1 FROM pack_records WHERE record_id = ?", ("PCK-0006",)
		).fetchone()
	if already_seeded:
		return
	with sample_path.open(newline="", encoding="utf-8") as sample_file:
		for row in csv.DictReader(sample_file):
			result = verify_pack(row["order_lines"], row["observed_in_box"])
			evidence = build_evidence(
				record_id=row["record_id"], org_id=row["org_id"], unit_id=row["unit_id"],
				order_lines=row["order_lines"], observed_in_box=row["observed_in_box"],
				result=result, photo_ref=row["photo_refs"],
						operator_verdict=row["operator_verdict"],
						agreement=operator_agreement(row["operator_verdict"], result["verdict"]),
			)
			save_record({
				**row,
				"photo_ref": row["photo_refs"],
				"verdict": result["verdict"],
				"action": result["action"],
				"reason": result["reason"],
				"evidence": evidence,
			})


def seed_sample_catalog() -> None:
	"""Seed synthetic SKU identifiers as demo catalog entries, not training labels."""
	sample_path = _sample_data_path()
	if sample_path is None:
		return
	fixture_dir = APP_DIR / "fixtures" / "vision"
	reference_images: dict[str, str] = {}
	manifest_path = fixture_dir / "manifest.csv"
	if manifest_path.exists():
		with manifest_path.open(newline="", encoding="utf-8") as manifest_file:
			for fixture in csv.DictReader(manifest_file):
				if (fixture_dir / fixture["image_filename"]).is_file():
					reference_images.setdefault(fixture["sku"], f"fixtures/vision/{fixture['image_filename']}")
	with sample_path.open(newline="", encoding="utf-8") as sample_file:
		for row in csv.DictReader(sample_file):
			for sku in set(parse_lines(row["order_lines"])) | set(parse_lines(row["observed_in_box"])):
				existing = get_product(row["org_id"], sku)
				reference_image_ref = reference_images.get(sku)
				default_name = SAMPLE_PRODUCT_NAMES.get(sku, sku)
				needs_display_name = bool(existing and existing["product_name"] == sku and default_name != sku)
				needs_reference = bool(reference_image_ref and (not existing or not existing.get("reference_image_ref")))
				if existing and not needs_display_name and not needs_reference:
					continue
				save_product({
					"product_id": existing["product_id"] if existing else f"PRD-{uuid4().hex[:12].upper()}",
					"org_id": row["org_id"],
					"sku": sku,
					"product_name": default_name if not existing or needs_display_name else existing["product_name"],
					"brand": existing.get("brand") if existing else None,
					"external_id": existing.get("external_id") if existing else None,
					"barcode": existing.get("barcode") if existing else None,
					"attributes": json.loads(existing["attributes_json"]) if existing else {},
					"reference_image_ref": reference_image_ref,
					"created_at": existing["created_at"] if existing else datetime.now(timezone.utc).isoformat(),
				})


@app.context_processor
def inject_navigation() -> dict[str, object]:
	return {"active_org": current_org(), "username": session.get("username")}


@app.route("/login", methods=["GET", "POST"])
def login():
	if request.method == "POST":
		if IS_PRODUCTION and OIDC_ENABLED:
			abort(403)
		username = request.form.get("username", "").strip()[:254]
		remote_addr = request.remote_addr or "unknown"
		secret = app.config["SECRET_KEY"].encode("utf-8")
		bucket_keys = [
			"account:" + hmac.new(secret, username.casefold().encode("utf-8"), hashlib.sha256).hexdigest(),
			"address:" + hmac.new(secret, remote_addr.encode("utf-8"), hashlib.sha256).hexdigest(),
		]
		now = int(time.time())
		blocked_until = login_blocked_until(bucket_keys, now)
		if blocked_until > now:
			response = app.make_response((
					render_template("login.html", error="Too many failed attempts. Try again later.", demo_login_enabled=ALLOW_DEMO_LOGIN and not IS_PRODUCTION, oidc_enabled=OIDC_ENABLED, local_login_enabled=not (IS_PRODUCTION and OIDC_ENABLED)),
				429,
			))
			response.headers["Retry-After"] = str(blocked_until - now)
			return response
		is_demo_user = username in {account[0] for account in DEMO_USERS}
		user = None if is_demo_user and (IS_PRODUCTION or not ALLOW_DEMO_LOGIN) else get_user(username)
		if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
			clear_login_attempts(bucket_keys)
			session.clear()
			session["username"] = username
			session["org_id"] = user["org_id"]
			session["role"] = user["role"]
			return redirect(url_for("dashboard"))
		record_login_failure(bucket_keys[0], now, limit=5, window_seconds=900, block_seconds=900)
		record_login_failure(bucket_keys[1], now, limit=20, window_seconds=900, block_seconds=900)
		return render_template("login.html", error="Invalid username or password.", demo_login_enabled=ALLOW_DEMO_LOGIN and not IS_PRODUCTION, oidc_enabled=OIDC_ENABLED, local_login_enabled=not (IS_PRODUCTION and OIDC_ENABLED)), 401
	return render_template("login.html", demo_login_enabled=ALLOW_DEMO_LOGIN and not IS_PRODUCTION, oidc_enabled=OIDC_ENABLED, local_login_enabled=not (IS_PRODUCTION and OIDC_ENABLED))


@app.get("/auth/login")
def oidc_login():
	provider = get_oidc_client()
	if not provider:
		abort(404)
	redirect_uri = OIDC_REDIRECT_URI or url_for("oidc_callback", _external=True)
	return provider.authorize_redirect(redirect_uri, code_challenge_method="S256")


@app.get("/auth/callback")
def oidc_callback():
	provider = get_oidc_client()
	if not provider:
		abort(404)
	try:
		token = provider.authorize_access_token()
		claims = token.get("userinfo") or provider.userinfo(token=token)
	except Exception:
		app.logger.warning("OIDC authorization or user-info validation failed")
		return render_template(
			"login.html",
			error="Organization sign-in failed. Try again or contact your administrator.",
			demo_login_enabled=False,
			oidc_enabled=True,
			local_login_enabled=False,
		), 401
	if not isinstance(claims, dict):
		abort(401)
	claimed_issuer = str(claims.get("iss") or OIDC_ISSUER_URL).rstrip("/")
	subject = str(claims.get("sub") or "").strip()
	email = str(claims.get("email") or "").strip().casefold()
	if claimed_issuer != OIDC_ISSUER_URL or not subject or not email or claims.get("email_verified") is not True:
		abort(403, description="A verified account from the configured identity provider is required.")
	user = get_oidc_user(OIDC_ISSUER_URL, subject, email)
	if not user or not link_oidc_identity(user["username"], OIDC_ISSUER_URL, subject, user["org_id"]):
		abort(403, description="This identity is not assigned to an active PackGuard operator.")
	user = get_user(user["username"])
	if not user:
		abort(403)
	session.clear()
	session["username"] = user["username"]
	session["org_id"] = user["org_id"]
	session["role"] = user["role"]
	return redirect(url_for("dashboard"))


@app.cli.command("create-operator")
def create_operator_command():
	"""Create an organization-scoped operator with a password hidden from terminal echo."""
	username = input("Operator username or verified email: ").strip().casefold()
	org_id = input("Organization ID: ").strip()
	if not username or not org_id or username in {account[0] for account in DEMO_USERS}:
		raise SystemExit("Enter a username and organization ID; demo usernames are reserved.")
	if user_exists(username):
		raise SystemExit("That username already exists.")
	if OIDC_ENABLED:
		password_hash = generate_password_hash(token_urlsafe(48))
	else:
		password = getpass.getpass("Password (at least 14 characters): ")
		confirmation = getpass.getpass("Confirm password: ")
		if len(password) < 14 or password != confirmation:
			raise SystemExit("Passwords must match and be at least 14 characters long.")
		password_hash = generate_password_hash(password)
	save_user({
		"username": username,
		"password_hash": password_hash,
		"org_id": org_id,
		"role": "operator",
		"created_at": datetime.now(timezone.utc).isoformat(),
	})
	print(f"Created operator {username} for organization {org_id}.")


@app.post("/logout")
def logout():
	session.clear()
	return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
	org_id = current_org()
	records = list_records(org_id)
	video_records = [record for record in records if record.get("video_ref")][:4]
	counts = {"total": len(records), "pass": 0, "fail": 0, "uncertain": 0}
	for record in records:
		counts[record["verdict"].lower()] = counts.get(record["verdict"].lower(), 0) + 1
	return render_template("dashboard.html", records=records[:12], counts=counts, video_records=video_records)


@app.route("/catalog", methods=["GET", "POST"])
@login_required
def product_catalog():
	org_id = current_org()
	if request.method == "POST":
		sku = request.form.get("sku", "").strip()
		product_name = request.form.get("product_name", "").strip()
		try:
			attributes = json.loads(request.form.get("attributes", "{}").strip() or "{}")
		except json.JSONDecodeError:
			return render_template("catalog.html", products=catalog_products(org_id), error="Variant attributes must be valid JSON."), 400
		if not sku or not product_name or not isinstance(attributes, dict):
			return render_template("catalog.html", products=catalog_products(org_id), error="SKU, product name, and a JSON object of attributes are required."), 400
		existing = get_product(org_id, sku)
		product_id = existing["product_id"] if existing else f"PRD-{uuid4().hex[:12].upper()}"
		reference_image_ref = save_media(request.files.get("reference_image"), org_id, f"catalog/{product_id}", "image")
		save_product({
			"product_id": product_id, "org_id": org_id, "sku": sku,
			"product_name": product_name,
			"brand": request.form.get("brand", "").strip() or None,
			"external_id": request.form.get("external_id", "").strip() or None,
			"barcode": request.form.get("barcode", "").strip() or None,
			"supplier_name": request.form.get("supplier_name", "").strip() or None,
			"origin_address": request.form.get("origin_address", "").strip() or None,
			"ordered_for": request.form.get("ordered_for", "").strip() or None,
			"delivery_address": request.form.get("delivery_address", "").strip() or None,
			"attributes": attributes,
			"reference_image_ref": reference_image_ref,
			"created_at": datetime.now(timezone.utc).isoformat(),
		})
		return redirect(url_for("product_catalog", saved=sku))
	return render_template("catalog.html", products=catalog_products(org_id), saved=request.args.get("saved"))


def catalog_products(org_id: str) -> list[dict[str, object]]:
	products = list_products(org_id)
	for product in products:
		product["attributes"] = json.loads(product.pop("attributes_json"))
	return products


def vision_catalog_products(org_id: str, expected_skus: set[str]) -> list[dict[str, object]]:
	# Keep the full tenant catalog so vision can identify wrong or extra products,
	# not only confirm SKUs already present in the expected order.
	products = catalog_products(org_id)
	products.sort(key=lambda product: product.get("sku") not in expected_skus)
	for product in products:
		reference = product.get("reference_image_ref")
		if not reference:
			continue
		if reference.startswith(f"{org_id}/"):
			image_bytes = MEDIA_STORAGE.read(reference)
			if image_bytes:
				product["reference_image_bytes"] = image_bytes
				product["reference_image_name"] = Path(reference).name
		elif reference.startswith("fixtures/vision/"):
			candidate_path = APP_DIR / reference
			if candidate_path.is_file():
				product["reference_image_path"] = str(candidate_path)
	return products


@app.get("/catalog/product/<sku>")
@login_required
def product_detail(sku: str):
	product = get_product(current_org(), sku)
	if not product:
		abort(404)
	product["attributes"] = json.loads(product.pop("attributes_json"))
	return render_template("product_detail.html", product=product)


@app.get("/api/products")
@login_required
def products_api():
	products = catalog_products(current_org())
	return jsonify({"org_id": current_org(), "count": len(products), "products": products})


@app.get("/api/search")
@login_required
def search_api():
	query = request.args.get("q", "").strip()[:120].casefold()
	if len(query) < 2:
		return jsonify({"query": query, "results": []})

	org_id = current_org()
	results: list[dict[str, str]] = []
	for product in catalog_products(org_id):
		searchable = " ".join(str(product.get(key) or "") for key in (
			"sku", "product_id", "product_name", "brand", "external_id", "barcode",
		)) + " " + json.dumps(product.get("attributes", {}))
		if query in searchable.casefold():
			results.append({
				"type": "Product",
				"title": product["product_name"],
				"subtitle": f"SKU {product['sku']} · Product ID {product['product_id']}",
				"url": url_for("product_detail", sku=product["sku"]),
			})

	for record in list_records(org_id, limit=100):
		evidence = json.loads(record["evidence_json"])
		product_names = " ".join(
			check.get("product", {}).get("name", "")
			for check in evidence.get("checks", [])
			if isinstance(check.get("product"), dict)
		)
		searchable = " ".join(str(record.get(key) or "") for key in (
			"record_id", "order_id", "unit_id", "channel", "order_lines", "observed_in_box",
		)) + " " + product_names
		if query in searchable.casefold():
			results.append({
				"type": "Order",
				"title": record["order_id"],
				"subtitle": f"{record['unit_id']} · {record['verdict']} / {record['action']} · {record['record_id']}",
				"url": url_for("record_detail", record_id=record["record_id"]),
			})
	return jsonify({"query": query, "results": results[:30]})


def alert_records(org_id: str) -> list[dict[str, object]]:
	"""Return records needing operator attention for one organization."""
	return [
		record for record in list_records(org_id)
		if record["verdict"] in {"FAIL", "UNCERTAIN"}
		or json.loads(record["evidence_json"]).get("operator_agreement") == "DISAGREES"
	]


def packguard_workflow_answer() -> str:
	"""Return the canonical, detailed workflow explanation."""
	return (
		"PackGuard's workflow:\n"
		"1. The operator signs in; the session determines which organization's records are visible.\n"
		"2. In New check, the operator enters the Unit ID, Order ID, channel, and any known origin or delivery address.\n"
		"3. The operator enters expected and observed contents as SKU:quantity pairs, separated by semicolons. A photo or packing video can be attached as evidence.\n"
		"4. The verifier compares each SKU and quantity. An exact match returns PASS / SEAL; a missing, short, extra, or wrong item returns FAIL / STOP_AND_FIX; missing or malformed input returns UNCERTAIN / HOLD_FOR_REVIEW.\n"
		"5. PackGuard creates an evidence record with the decision, checks, operator verdict, timestamps, and uploaded media. Images and videos are stored evidence; they are not automatically analyzed for products.\n"
		"6. Operators can review records in the dashboard. FAIL, UNCERTAIN, and human/system disagreements appear in Alerts, with order routes and attached media. Reports summarize decisions and agreement."
	)


def answer_from_order_record(record: dict[str, object], question: str) -> str:
	"""Answer order-specific follow-ups from one tenant-scoped record."""
	question_lower = question.lower()
	order_id = str(record["order_id"])
	unit_id = str(record["unit_id"])
	if any(term in question_lower for term in ("address", "arrived", "ship to", "where from", "where did it")):
		return (
			f"{order_id} for {unit_id}: arrived from {record.get('origin_address') or 'address not recorded'}; "
			f"ships to {record.get('delivery_address') or 'address not recorded'}."
		)
	if any(term in question_lower for term in ("contents", "items", "sku", "inside", "in the box")):
		return (
			f"{order_id} expected: {record.get('order_lines') or 'not recorded'}. "
			f"Observed: {record.get('observed_in_box') or 'no observation recorded'}. "
			f"PackGuard result: {record['verdict']} / {record['action']}."
		)
	if any(term in question_lower for term in ("photo", "picture", "image", "video", "evidence", "media")):
		photo_status = "attached" if record.get("photo_ref") else "not attached"
		video_status = "attached" if record.get("video_ref") else "not attached"
		return f"{order_id} has a photo {photo_status} and a packing video {video_status}."
	return (
		f"{order_id} for {unit_id} is {record['verdict']} / {record['action']}. "
		f"Expected: {record.get('order_lines') or 'not recorded'}. "
		f"Observed: {record.get('observed_in_box') or 'no observation recorded'}."
	)


def answer_from_contract_record(record: dict[str, Any], question: str) -> str:
	subject = record.get("subject") or {}
	checks = record.get("checks", [])
	sku_check = next((check for check in checks if check["check_key"] == "sku_quantity"), {})
	visual_check = next((check for check in checks if check["check_key"] == "visual_evidence"), {})
	product_routes = sku_check.get("detail", {}).get("product_routes", [])
	if record.get("agent") == "prep":
		prep_route = prep_record_logistics(record)
		product_routes = [{**prep_route, "product_name": subject.get("sku"), "sku": subject.get("sku")}]
	question_lower = question.casefold()
	if any(word in question_lower for word in ("address", "from", "source", "destination", "where", "ordered for", "recipient")):
		if product_routes:
			route = product_routes[0]
			return (
				f"{route.get('product_name') or subject.get('sku')}: supplier {route.get('supplier_name') or 'not recorded'}, "
				f"from {route.get('origin_address') or 'not recorded'}, ordered for {route.get('ordered_for') or 'not recorded'}, "
				f"destination {route.get('delivery_address') or 'not recorded'}."
			)
		return "This evidence record does not contain source, recipient, or destination details."
	return (
		f"Stage-{record.get('agent')} record {record['record_id']} for order {subject.get('order_id') or 'not recorded'}: "
		f"SKU {subject.get('sku') or 'not recorded'}, expected quantity {subject.get('quantity_expected') if subject.get('quantity_expected') is not None else 'not recorded'}, "
		f"decision {record.get('outcome', {}).get('decision', 'not recorded')}, status {record.get('status', 'not recorded')}. "
		f"Visual result: {visual_check.get('detail', {}).get('candidate_decision') or visual_check.get('detail', {}).get('presence_hint') or visual_check.get('detail', {}).get('status', 'not recorded')}."
	)


def agent_workflow_summary(org_id: str) -> str:
	prep_records = list_contract_records(org_id, since=None, agent="prep", cursor=None, limit=100)
	pack_records = list_contract_records(org_id, since=None, agent="pack", cursor=None, limit=100)
	delivered_count = 0
	failed_count = 0
	for record in pack_records:
		for event in list_audit_events(record["record_id"], org_id):
			if event["event_type"] == "RETURNS_AGENT_DELIVERED":
				delivered_count += 1
				break
			if event["event_type"] == "RETURNS_AGENT_FAILED":
				failed_count += 1
				break
	returns_state = "configured" if (
		RETURNS_AGENT_WEBHOOK_URL and RETURNS_AGENT_WEBHOOK_TOKEN and AGENT_API_ORG_ID == org_id
	) else "not configured"
	return (
		f"Live workflow for {org_id}: {len(prep_records)} Stage-2 prep record(s), "
		f"{len(pack_records)} Stage-3 pack record(s), {delivered_count} delivered to Stage 4, "
		f"and {failed_count} delivery failure(s). Stage-4 webhook is {returns_state}. "
		"Open New Check to continue a ready prep record; review delivery issues in Alerts."
	)


def assistant_answer(
	question: str,
	org_id: str,
	history: list[dict[str, str]] | None = None,
	page_context: dict[str, Any] | None = None,
) -> str:
	"""Answer operational questions from the current organization's records."""
	question_lower = question.lower()
	records = list_records(org_id)
	alerts = alert_records(org_id)
	products = catalog_products(org_id)
	page_context = page_context if isinstance(page_context, dict) else {}
	current_page = page_context.get("current_page") if isinstance(page_context.get("current_page"), dict) else {}
	page_path = str(current_page.get("path") or "")[:240]
	raw_form_fields = page_context.get("form_fields") if isinstance(page_context.get("form_fields"), dict) else {}
	allowed_form_fields = {
		"product_lookup", "selected_product_id", "source_prep_record_id", "order_id",
		"unit_id", "order_lines", "observed_in_box", "channel",
	}
	form_fields = {
		key: value.strip()[:500]
		for key, value in raw_form_fields.items()
		if key in allowed_form_fields and isinstance(value, str) and value.strip()
	}
	current_record = None
	path_record = re.search(r"/(?:v1/)?records/([^/?#]+)", page_path)
	if path_record:
		current_record = get_record(path_record.group(1), org_id) or get_contract_record(path_record.group(1), org_id)
	if not current_record:
		context_record_id = str(page_context.get("record_id") or "")
		if context_record_id:
			current_record = get_record(context_record_id, org_id) or get_contract_record(context_record_id, org_id)
	if not current_record:
		prep_record_id = str(page_context.get("source_prep_record_id") or form_fields.get("source_prep_record_id") or "")
		if prep_record_id:
			prep_record = get_contract_record(prep_record_id, org_id)
			if prep_record and prep_record.get("agent") == "prep":
				current_record = prep_record
	current_product = None
	product_id = str(page_context.get("product_id") or form_fields.get("selected_product_id") or "")
	if product_id:
		current_product = next((product for product in products if product.get("product_id") == product_id), None)
	if not current_product and form_fields.get("product_lookup"):
		lookup = form_fields["product_lookup"].casefold()
		current_product = next((
			product for product in products
			if lookup in {
				str(product.get("product_name") or "").casefold(),
				str(product.get("product_id") or "").casefold(),
				str(product.get("sku") or "").casefold(),
				str(product.get("barcode") or "").casefold(),
			}
		), None)
	if not current_product:
		product_path = re.search(r"/catalog/product/([^/?#]+)", page_path)
		if product_path:
			current_product = next((product for product in products if product.get("sku") == product_path.group(1)), None)
	safe_page_context = {
		"current_page": {"title": str(current_page.get("title") or "")[:120], "path": page_path},
		"recent_pages": [
			{"title": str(item.get("title") or "")[:120], "path": str(item.get("path") or "")[:240]}
			for item in page_context.get("recent_pages", [])[:8]
			if isinstance(item, dict)
		],
		"current_form": form_fields,
	}
	if current_record:
		if "evidence_json" in current_record:
			current_record["evidence"] = json.loads(current_record.pop("evidence_json"))
			safe_page_context["current_record"] = {
				"record_id": current_record["record_id"], "order_id": current_record["order_id"],
				"unit_id": current_record["unit_id"], "verdict": current_record["verdict"],
				"action": current_record["action"], "order_lines": current_record["order_lines"],
				"observed_in_box": current_record.get("observed_in_box"),
				"origin_address": current_record.get("origin_address"),
				"delivery_address": current_record.get("delivery_address"),
			}
		else:
			safe_page_context["current_contract_record"] = current_record
	if current_product:
		safe_page_context["current_product"] = {
			key: current_product.get(key) for key in (
				"product_id", "product_name", "sku", "brand", "barcode", "attributes",
				"supplier_name", "origin_address", "ordered_for", "delivery_address",
			)
		}
	mentioned_record = next((
		record for record in records
		if record["order_id"].lower() in question_lower or record["unit_id"].lower() in question_lower or record["record_id"].lower() in question_lower
	), None)
	if mentioned_record:
		return answer_from_order_record(mentioned_record, question)
	mentioned_product = next((
		product for product in products
		if any(
			str(product.get(field) or "").strip().casefold() in question_lower
			for field in ("sku", "product_id", "barcode", "product_name")
			if str(product.get(field) or "").strip()
		)
	), None)
	if mentioned_product:
		attributes = mentioned_product.get("attributes") or {}
		return (
			f"Catalog product: {mentioned_product['product_name']} (SKU {mentioned_product['sku']}; "
			f"Product ID {mentioned_product['product_id']}). Brand: {mentioned_product.get('brand') or 'not recorded'}. "
			f"Barcode: {mentioned_product.get('barcode') or 'not recorded'}. "
			f"Attributes: {json.dumps(attributes, ensure_ascii=True) if attributes else 'not recorded'}. "
			f"Supplier: {mentioned_product.get('supplier_name') or 'not recorded'}. "
			f"Source / arrival address: {mentioned_product.get('origin_address') or 'not recorded'}. "
			f"Ordered for: {mentioned_product.get('ordered_for') or 'not recorded'}. "
			f"Destination address: {mentioned_product.get('delivery_address') or 'not recorded'}. "
			"This describes the catalog entry, not a visual confirmation of a specific item."
		)
	if not mentioned_record and current_record and any(term in question_lower for term in ("this", "current", "here", "page", "record", "it", "what do i do")):
		if "evidence_json" in current_record:
			return answer_from_order_record(current_record, question)
		return answer_from_contract_record(current_record, question)
	if not mentioned_product and current_product and any(term in question_lower for term in ("this", "current", "here", "page", "product", "it")):
		mentioned_product = current_product
		attributes = mentioned_product.get("attributes") or {}
		return (
			f"Catalog product: {mentioned_product['product_name']} (SKU {mentioned_product['sku']}; Product ID {mentioned_product['product_id']}). "
			f"Supplier: {mentioned_product.get('supplier_name') or 'not recorded'}. From: {mentioned_product.get('origin_address') or 'not recorded'}. "
			f"Ordered for: {mentioned_product.get('ordered_for') or 'not recorded'}. Destination: {mentioned_product.get('delivery_address') or 'not recorded'}. "
			f"Brand: {mentioned_product.get('brand') or 'not recorded'}. Attributes: {json.dumps(attributes, ensure_ascii=True) if attributes else 'not recorded'}."
		)
	history_text = " ".join(
		message.get("content", "").lower()
		for message in (history or [])
		if isinstance(message, dict) and isinstance(message.get("content"), str)
	)
	followup_about_order = bool(
		re.search(r"\b(it|that order|this order|that one|the same order)\b", question_lower)
		or re.search(r"\b(what about|and its)\s+(the )?(address|contents|items|photo|video|status|evidence)\b", question_lower)
	)
	if followup_about_order:
		mentioned_record = next((
			record for record in records
			if record["order_id"].lower() in history_text or record["unit_id"].lower() in history_text
		), None)
		if mentioned_record:
			return answer_from_order_record(mentioned_record, question)
	asks_about_workflow = "packguard" in question_lower and any(
		term in question_lower for term in ("workflow", "process")
	)
	workflow_followup = (
		"packguard" in history_text
		and any(term in history_text for term in ("workflow", "process"))
		and any(term in question_lower for term in ("detail", "elaborate", "expand", "more"))
	)
	if asks_about_workflow or workflow_followup:
		return packguard_workflow_answer()
	for record in records:
		if record["order_id"].lower() in question_lower or record["unit_id"].lower() in question_lower:
			return answer_from_order_record(record, question)
	if any(term in question_lower for term in ("agent", "point a", "point b", "transfer", "origin agent", "a2a", "multi-agent")):
		return agent_workflow_summary(org_id)
	if re.search(r"\b(hello|hi|hey)\b", question_lower):
		return "Hello. I can explain PackGuard decisions, orders, alerts, addresses, evidence, media, reports, security, and the demo workflow."
	if any(term in question_lower for term in ("what can you do", "help", "options", "capabilities")):
		return "Ask me about PASS, FAIL, UNCERTAIN, an Order ID, a Unit ID, addresses, alerts, photos, packing videos, reports, APIs, organization security, or how to demo PackGuard."
	if "pass" in question_lower and any(term in question_lower for term in ("packguard", "pack check", "packing", "order contents", "verdict")):
		return "PASS / SEAL means every expected SKU has the exact expected quantity and no extra item was observed."
	if any(term in question_lower for term in ("stop and fix", "stop_and_fix")) or (
		"fail" in question_lower and any(term in question_lower for term in ("packguard", "pack check", "packing", "order contents", "verdict"))
	):
		return "FAIL / STOP_AND_FIX means an item is missing, short, extra, or the wrong SKU was observed."
	if "hold_for_review" in question_lower or (
		("uncertain" in question_lower or "review" in question_lower)
		and any(term in question_lower for term in ("packguard", "pack", "order", "shipment", "verdict"))
	):
		count = sum(record["verdict"] == "UNCERTAIN" for record in records)
		return f"{count} order(s) are UNCERTAIN and need human review because the evidence is incomplete or malformed."
	if re.search(r"\b(photo|picture|image)\b", question_lower) and any(
		term in question_lower for term in ("evidence", "uploaded", "stored", "packguard", "order", "how many")
	):
		count = sum(bool(record.get("photo_ref")) for record in records)
		return f"{count} photo evidence file(s) are stored for {org_id}. Images are preserved as evidence; SKU recognition is pending a configured vision provider."
	if re.search(r"\b(active alerts?|risk alerts?)\b", question_lower) or (
		"alert" in question_lower and ("order" in question_lower or "how many" in question_lower)
	):
		return f"There are {len(alerts)} active alerts in {org_id}. Open the Alerts tab to review order routes and evidence."
	if re.search(r"\b(packing video|pack video|video evidence)\b", question_lower):
		video_count = sum(bool(record.get("video_ref")) for record in records)
		return f"{video_count} packing video(s) are stored for {org_id}. Open the dashboard video shelf or the order evidence page."
	if any(term in question_lower for term in ("packguard api", "packguard endpoint", "/api/", "packguard report")):
		return "PackGuard provides /health, /api/records, /reports/records.csv, /reports/summary, and /reports/summary.json. Record and report routes require login and use the current organization's session."
	if any(term in question_lower for term in ("demo login", "demo credentials", "packguard password", "packguard login")):
		return "The local demo accounts are alpha.operator / alpha-demo and bravo.operator / bravo-demo. Each account is restricted to its own organization."
	if any(term in question_lower for term in ("organization isolation", "tenant isolation", "organization security", "alpha organization", "bravo organization")):
		return "Alpha and Bravo are isolated demo organizations. The organization comes from the login session, not from a URL parameter, so changing org_id cannot switch access."
	if "sku" in question_lower or "order lines" in question_lower or (
		"quantity" in question_lower and any(term in question_lower for term in ("order", "pack", "box"))
	):
		return "Contents use SKU:quantity pairs separated by semicolons. For example, SKU-A:1;SKU-B:2 means one SKU-A and two SKU-B items."
	if any(term in question_lower for term in ("packguard demo", "judge demo", "demo presentation")):
		return "For the demo, show login, a matching PASS / SEAL check, a mismatched FAIL / STOP_AND_FIX check, an UNCERTAIN review, the evidence page, Alerts, and Evaluation summary."
	if any(term in question_lower for term in ("what is packguard", "what does packguard do", "purpose of packguard")):
		return "PackGuard is an evidence-first outbound packing system. It compares expected and observed SKU quantities, preserves photo/video evidence, routes uncertain cases to people, and records an auditable decision before sealing."
	if any(term in question_lower for term in ("catalog", "product details", "edit product", "change product")):
		return "Use Menu → Catalog to create or update a product. Keep the same SKU when editing. Catalog records the product name, brand, barcode, variant attributes, and reference image."
	if any(term in question_lower for term in ("vision set", "vision images", "image mapping", "fixture")):
		return "Vision set is the review gallery for mapped evaluation images. It shows the image, SKU, quantity, expected decision, and catalog details. It does not replace the live Capture workflow."
	if any(term in question_lower for term in ("recapture", "take another photo", "bad image", "blurry")):
		return "Open the Record page and click Recapture package. The order details are preserved, a reason is required, and the new attempt is added to audit history."
	if any(term in question_lower for term in ("manual review", "human review", "supervisor", "override")):
		return "Use MANUAL_REVIEW when evidence is incomplete or ambiguous. Production overrides require a reason and are stored with the operator, timestamp, previous state, and new state."
	if any(term in question_lower for term in ("evidence report", "print report", "save pdf", "pdf")):
		return "Open a Record, click Print evidence report, then choose Print / Save PDF in the browser dialog. The report includes order details, contents, decision, and audit history."
	if any(term in question_lower for term in ("support", "contact packguard", "send a message")):
		return "Open Menu → Support. Enter your name, email, subject, and message. Include the Order ID or Record ID so the issue can be traced quickly."
	if any(term in question_lower for term in ("production", "deploy", "docker", "postgres", "https")):
		return "PackGuard is controlled-pilot ready. Production still requires an identity provider, HTTPS, PostgreSQL, private object storage, backups, monitoring, and 50 double-labeled vision units. Check /health/production for the current readiness report."
	if "next step in this project" in question_lower or "next packguard milestone" in question_lower:
		return "The next major milestone is configuring a calibrated vision provider and evaluating it on the held-out fixture set with false positives, false negatives, and UNCERTAIN rates."
	if any(term in question_lower for term in ("order address", "address for an order", "order's address", "arrived from", "ship to")) and "order" in question_lower:
		return "Ask about a specific Order ID or Unit ID to retrieve its arrival and delivery addresses."
	return general_assistant_answer(question, history, products, safe_page_context)


def general_assistant_answer(
	question: str,
	history: list[dict[str, str]] | None = None,
	products: list[dict[str, object]] | None = None,
	page_context: dict[str, Any] | None = None,
) -> str:
	"""Answer general questions with a local model by default, without a paid API key."""
	conversation = [
		{"role": message["role"], "content": message["content"][:2000]}
		for message in (history or [])[-8:]
		if isinstance(message, dict)
		and message.get("role") in {"user", "assistant"}
		and isinstance(message.get("content"), str)
	]
	instructions = (
		"You are the helpful assistant inside PackGuard. Answer the user's question "
		"directly and helpfully across topics, in a few concise sentences unless they ask "
		"for detail. If a broad question is ambiguous, ask one short clarifying question. "
		"Do not invent PackGuard products, a PackGuard website, or customer-support contacts. "
		"Do not claim to have checked live prices, availability, or private order records. "
		"If live facts are needed, explain that briefly and still provide useful general guidance. "
		"PackGuard pages are Dashboard, Alerts, Catalog, Vision set, Assistant, Support, Capture, Record, and Evaluation. "
		"PackGuard decisions are PASS/SEAL, FAIL/FIX, RECAPTURE, and MANUAL_REVIEW. "
		"Vision output is advisory and cannot authorize SEAL until calibration is approved. "
		"Use the active organization's catalog context below for product facts. Do not infer "
		"origin, condition, or package contents unless the supplied records explicitly contain them. "
		f"Catalog context: {json.dumps((products or [])[:40], ensure_ascii=True, default=str)[:10000]} "
		f"Current page and recent navigation context: {json.dumps(page_context or {}, ensure_ascii=True, default=str)[:6000]}"
	)
	model = os.environ.get("OLLAMA_MODEL", "qwen3:1.7b")
	base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
	payload = {
		"model": model,
		"messages": [
			{"role": "system", "content": instructions},
			*conversation,
			{"role": "user", "content": question[:4000]},
		],
		"stream": False,
		"keep_alive": "5m",
		"options": {"num_predict": 300},
	}
	request = Request(
		f"{base_url}/api/chat",
		data=json.dumps(payload).encode("utf-8"),
		headers={"Content-Type": "application/json"},
		method="POST",
	)
	try:
		with urlopen(request, timeout=60) as response:
			result = json.loads(response.read().decode("utf-8"))
	except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
		return (
			"The free local assistant is not running yet. Install Ollama and download "
			f"the {model} model to enable general questions."
		)
	answer = result.get("message", {}).get("content", "").strip()
	return answer or "I could not generate a complete answer. Ask me about the Dashboard, Catalog, Vision set, Capture, Record, Alerts, Support, reports, or a specific Order ID."
@app.get("/alerts")
@login_required
def alerts():
	org_id = current_org()
	alerts = alert_records(org_id)
	catalog = {p["sku"]: p["product_name"] for p in catalog_products(org_id)}
	prep_records = list_contract_records(org_id, since=None, agent="prep", cursor=None, limit=20)
	incoming_agent_alerts = []
	for prep_record in prep_records:
		subject = prep_record.get("subject") or {}
		sku = str(subject.get("sku") or "").strip()
		quantity = subject.get("quantity_expected")
		order_id = str(subject.get("order_id") or "").strip()
		logistics = prep_record_logistics(prep_record)
		prep_checks = prep_record.get("checks", [])
		prep_ready = prep_record_is_ready(prep_record)
		item = dict(prep_record)
		item.update({
			"product_sku": sku,
			"product_name": catalog.get(sku, sku or "Product not identified in catalog"),
			"quantity": quantity,
			"expected_lines": f"{sku}:{quantity}" if sku and isinstance(quantity, int) else "Not supplied by prep agent",
			"source_point": logistics.get("origin_address") or f"Stage 2 prep · {prep_record.get('operator_label') or 'agent'}",
			"destination_point": logistics.get("delivery_address") or "Stage 3 · PackGuard",
			"note": f"Supplier: {logistics.get('supplier_name') or 'not recorded'} · Ordered for: {logistics.get('ordered_for') or 'not recorded'}",
			"capture_url": None,
		})
		if prep_ready and order_id and sku and isinstance(quantity, int) and quantity > 0:
			item["capture_url"] = url_for("capture", prep_record_id=prep_record["record_id"])
		incoming_agent_alerts.append(item)

	wrong_package_alerts = []
	for legacy_alert in alerts:
		diagnosis = diagnose_pack_contents(
			legacy_alert.get("order_lines", ""),
			legacy_alert.get("observed_in_box", ""),
			catalog_names=catalog,
		)
		legacy_copy = dict(legacy_alert)
		legacy_evidence = json.loads(legacy_copy.get("evidence_json") or "{}")
		legacy_copy["inspection"] = (legacy_evidence.get("source") or {}).get("inspection") or {}
		legacy_copy["diagnosis"] = diagnosis
		photo_ref = legacy_copy.get("photo_ref")
		legacy_copy["photo_url"] = url_for("media_file", filename=photo_ref) if photo_ref and photo_ref.startswith(f"{org_id}/") else None
		legacy_copy["evidence_url"] = url_for("record_detail", record_id=legacy_copy["record_id"])
		wrong_package_alerts.append(legacy_copy)

	contract_pack_records = list_contract_records(org_id, since=None, agent="pack", cursor=None, limit=20)
	returns_webhook_configured = bool(RETURNS_AGENT_WEBHOOK_URL and RETURNS_AGENT_WEBHOOK_TOKEN and AGENT_API_ORG_ID == org_id)
	for record in contract_pack_records:
		checks = record.get("checks", [])
		events = list_audit_events(record["record_id"], org_id)
		delivery_event = next((event for event in events if event["event_type"] in {"RETURNS_AGENT_DELIVERED", "RETURNS_AGENT_FAILED"}), None)
		latest_overrides = {item["check_key"]: item["to_verdict"] for item in record.get("overrides", [])}
		check_verdicts = [latest_overrides.get(check["check_key"], check["verdict"]) for check in checks]
		is_clear_seal = record.get("status") == "complete" and record.get("outcome", {}).get("decision") == "seal" and all(verdict == "pass" for verdict in check_verdicts)
		if is_clear_seal and (not returns_webhook_configured or (delivery_event and delivery_event["event_type"] == "RETURNS_AGENT_DELIVERED")):
			continue
		sku_check = next((check for check in checks if check["check_key"] == "sku_quantity"), {})
		sku_details = sku_check.get("detail", {}).get("sku_checks", [])
		expected_lines = []
		observed_lines = []
		for item in sku_details:
			sku = str(item.get("sku") or "").strip()
			if not sku:
				continue
			expected_quantity = item.get("expected_quantity")
			observed_quantity = item.get("observed_quantity")
			if isinstance(expected_quantity, int):
				expected_lines.append(f"{sku}:{expected_quantity}")
			if isinstance(observed_quantity, int):
				observed_lines.append(f"{sku}:{observed_quantity}")
		subject = record.get("subject") or {}
		if not expected_lines and subject.get("sku") and isinstance(subject.get("quantity_expected"), int):
			expected_lines = [f"{subject['sku']}:{subject['quantity_expected']}"]
		inspection = next((check.get("detail", {}) for check in checks if check["check_key"] == "visual_evidence"), {})
		issue_parts = []
		for check in checks:
			if latest_overrides.get(check["check_key"], check["verdict"]) == "pass":
				continue
			detail = check.get("detail", {})
			issue_parts.append(str(detail.get("reason") or detail.get("status") or check["check_key"].replace("_", " ")))
			quality = detail.get("image_quality") or {}
			if quality.get("reason"):
				issue_parts.append(str(quality["reason"]))
			issue_parts.extend(str(note) for note in detail.get("uncertainties", []) if note)
		issue_summary = "; ".join(dict.fromkeys(issue_parts)) or "The contract Pack record is pending review."
		verdict = "FAIL" if "fail" in check_verdicts or record.get("outcome", {}).get("decision") == "fix" else "UNCERTAIN"
		first_image = next(iter(record.get("images", [])), None)
		wrong_package_alerts.append({
			"record_id": record["record_id"],
			"order_id": subject.get("order_id") or "Order not supplied",
			"unit_id": subject.get("shipment_id") or record["record_id"],
			"verdict": verdict,
			"action": str(record.get("outcome", {}).get("decision") or "manual_review").upper(),
			"reason": issue_summary,
			"order_lines": ";".join(expected_lines),
			"observed_in_box": ";".join(observed_lines),
			"diagnosis": {"issue_summary": issue_summary},
			"inspection": inspection,
			"photo_url": url_for("contract_record_image_api", record_id=record["record_id"], image_key=first_image["key"]) if first_image else None,
			"evidence_url": url_for("contract_record_api", record_id=record["record_id"]),
			"delivery_event": delivery_event,
			"returns_webhook_configured": returns_webhook_configured,
			"is_contract_record": True,
		})

	return render_template(
		"alerts.html", alerts=alerts,
		incoming_agent_alerts=incoming_agent_alerts,
		wrong_package_alerts=wrong_package_alerts,
		alert_count=len(alerts), active_org=org_id,
		returns_webhook_configured=returns_webhook_configured,
	)


@app.post("/api/verify-pack")
@login_required
def api_verify_pack():
	payload = request.get_json(silent=True) or request.form
	expected = payload.get("expected", "").strip()
	observed = payload.get("observed", "").strip()
	org_id = current_org()
	catalog = {p["sku"]: p["product_name"] for p in list_products(org_id)}
	catalog.update(SAMPLE_PRODUCT_NAMES)
	diag = diagnose_pack_contents(expected, observed, catalog_names=catalog)
	return jsonify(diag)



@app.get("/assistant")
@login_required
def assistant_page():
	return render_template("assistant.html")


@app.post("/api/assistant")
@login_required
def assistant_api():
	payload = request.get_json(silent=True) or request.form
	question = payload.get("question", "").strip()
	if not question:
		return jsonify({"answer": "Ask me about alerts, an order, an address, a packing video, or UNCERTAIN reviews."})
	history = payload.get("history", [])
	if not isinstance(history, list):
		history = []
	page_context = payload.get("page_context", {})
	if not isinstance(page_context, dict):
		page_context = {}
	return jsonify({"answer": assistant_answer(question, current_org(), history, page_context)})


@app.get("/agent-network")
@app.get("/agent-hub")
@login_required
def agent_hub():
	org_id = current_org()
	prep_records = list_contract_records(org_id, since=None, agent="prep", cursor=None, limit=20)
	prep_workflow = []
	for record in prep_records:
		subject = record.get("subject") or {}
		checks = record.get("checks", [])
		ready = prep_record_is_ready(record)
		can_start = (
			ready and bool(subject.get("order_id")) and bool(subject.get("sku"))
			and isinstance(subject.get("quantity_expected"), int) and subject["quantity_expected"] > 0
		)
		prep_workflow.append({
			"record": record,
			"ready": ready,
			"capture_url": url_for("capture", prep_record_id=record["record_id"]) if can_start else None,
			"image_urls": [
				url_for("contract_record_image_api", record_id=record["record_id"], image_key=image["key"])
				for image in record.get("images", [])
			],
		})
	pack_records = list_contract_records(org_id, since=None, agent="pack", cursor=None, limit=20)
	pack_workflow = []
	for record in pack_records:
		events = list_audit_events(record["record_id"], org_id)
		delivery_event = next((
			event for event in events
			if event["event_type"] in {"RETURNS_AGENT_DELIVERED", "RETURNS_AGENT_FAILED"}
		), None)
		visual_check = next((check for check in record.get("checks", []) if check["check_key"] == "visual_evidence"), {})
		first_image = next(iter(record.get("images", [])), None)
		pack_workflow.append({
			"record": record,
			"source_prep_record_id": next((
				check.get("detail", {}).get("source_prep_record_id")
				for check in record.get("checks", [])
				if check["check_key"] == "sku_quantity"
			), None),
			"visual_status": visual_check.get("detail", {}).get("status", "PENDING"),
			"candidate_decision": visual_check.get("detail", {}).get("candidate_decision", "MANUAL_REVIEW"),
			"candidate_reason": visual_check.get("detail", {}).get("candidate_reason", "Vision has not produced a recommendation yet."),
			"delivery_event": delivery_event,
			"image_url": url_for(
				"contract_record_image_api", record_id=record["record_id"], image_key=first_image["key"],
			) if first_image else None,
		})
	return render_template(
		"agent_hub.html",
		prep_workflow=prep_workflow,
		pack_workflow=pack_workflow,
		returns_webhook_configured=bool(
			RETURNS_AGENT_WEBHOOK_URL and RETURNS_AGENT_WEBHOOK_TOKEN
			and AGENT_API_ORG_ID == org_id
		),
		active_org=org_id,
	)


@app.get("/unit-passport")
@login_required
def unit_passport_page():
	order_id = request.args.get("order_id", "").strip()
	channel = request.args.get("channel", "MFN").strip()
	unit_id = request.args.get("unit_id", "").strip() or None
	journey = None
	error = None
	if order_id:
		org_id = current_org()
		records = []
		for agent_name in ("receiving", "prep", "pack", "returns"):
			records.extend(list_contract_records(org_id, since=None, agent=agent_name, cursor=None, limit=100))
		events = list_unit_events(org_id, order_id=order_id, unit_id=unit_id)
		for record in records:
			if (record.get("subject") or {}).get("order_id") == order_id:
				events.extend(list_audit_events(record["record_id"], org_id))
		try:
			journey = build_journey(order_id, channel, records, unit_id=unit_id, events=events)
		except ValueError as exc:
			error = str(exc)
	return render_template("unit_passport.html", order_id=order_id, channel=channel,
	                       unit_id=unit_id or "", journey=journey, error=error)


@app.get("/api/unit-passport")
@login_required
def unit_passport_api():
	order_id = request.args.get("order_id", "").strip()
	channel = request.args.get("channel", "MFN").strip()
	unit_id = request.args.get("unit_id", "").strip() or None
	if not order_id:
		return jsonify({"error": "order_id is required."}), 400
	org_id = current_org()
	records = []
	for agent_name in ("receiving", "prep", "pack", "returns"):
		records.extend(list_contract_records(org_id, since=None, agent=agent_name, cursor=None, limit=100))
	events = list_unit_events(org_id, order_id=order_id, unit_id=unit_id)
	for record in records:
		if (record.get("subject") or {}).get("order_id") == order_id:
			events.extend(list_audit_events(record["record_id"], org_id))
	try:
		return jsonify(build_journey(order_id, channel, records, unit_id=unit_id, events=events))
	except ValueError as exc:
		return jsonify({"error": str(exc)}), 400


def save_unit_event(
	org_id: str, unit_id: str, order_id: str, event_type: str, reason: str,
	actor_id: str, details: dict[str, Any] | None = None, event_id: str | None = None,
) -> None:
	event_details = {**(details or {}), "unit_id": unit_id, "order_id": order_id}
	save_audit_event({
		"event_id": event_id or f"EVT-{uuid4().hex[:16].upper()}",
		"record_id": f"unit:{unit_id}", "org_id": org_id,
		"event_type": event_type, "reason": reason, "actor_id": actor_id,
		"created_at": utc_now(), "details": event_details,
	})


@app.post("/v1/agent/events")
@csrf.exempt
def agent_ingest_unit_event():
	"""Persist a tenant-scoped, idempotent business event for a unit passport."""
	organization_key, auth_error = agent_api_org(UNIT_EVENT_API_TOKEN)
	if auth_error:
		return auth_error
	body = request.get_json(silent=True)
	if not isinstance(body, dict):
		return jsonify({"error": "A unit event object is required."}), 400
	event_id = str(body.get("event_id") or "").strip()
	unit_id = str(body.get("unit_id") or "").strip()
	order_id = str(body.get("order_id") or "").strip()
	event_type = str(body.get("event_type") or "").strip().upper()
	reason = str(body.get("reason") or event_type.replace("_", " ").title()).strip()
	details = body.get("details", {})
	allowed = {
		"UNIT_CREATED", "PRODUCT_RECEIVED", "FULFILLMENT_ROUTED",
		"RETURN_INITIATED", "CUSTOMER_RETURN_INITIATED", "RETURN_RECEIVED",
		"RECOVERY_CHARGE_RECEIVED", "AGENT_FAILED", "AGENT_TIMEOUT",
		"EVIDENCE_REQUESTED", "HUMAN_REVIEW_COMPLETED",
	}
	if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", event_id):
		return jsonify({"error": "event_id is required and must be a stable, retry-safe identifier."}), 400
	if not unit_id or len(unit_id) > 200 or not order_id or len(order_id) > 200:
		return jsonify({"error": "unit_id and order_id are required (maximum 200 characters each)."}), 400
	if event_type not in allowed:
		return jsonify({"error": "Unsupported unit event_type."}), 400
	if not isinstance(details, dict) or len(reason) > 500:
		return jsonify({"error": "details must be an object and reason must be at most 500 characters."}), 400
	if event_type in {"AGENT_FAILED", "AGENT_TIMEOUT"} and details.get("agent") not in {"receiving", "prep", "pack", "returns", "recovery"}:
		return jsonify({"error": "Agent failure events require details.agent."}), 400
	existing = list_unit_events(organization_key, unit_id=unit_id)
	prior = next((event for event in existing if event["event_id"] == event_id), None)
	canonical_details = {**details, "unit_id": unit_id, "order_id": order_id}
	if prior:
		if prior["event_type"] == event_type and prior["details"] == canonical_details:
			return jsonify({"event_id": event_id, "status": "duplicate"}), 200
		return jsonify({"error": "event_id was already used for different event content."}), 409
	try:
		save_unit_event(
			organization_key, unit_id, order_id, event_type, reason,
			actor_id="agent:event-ingest", details=details, event_id=event_id,
		)
	except Exception:
		app.logger.exception("Could not persist unit event %s", event_id)
		return jsonify({"error": "Could not persist unit event."}), 500
	return jsonify({"event_id": event_id, "status": "created"}), 201


@app.post("/v1/agent/records/<record_id>/retry")
@login_required
def retry_returns_agent_delivery(record_id: str):
	record = get_contract_record(record_id, current_org())
	if not record or record.get("agent") != "pack":
		abort(404)
	if not RETURNS_AGENT_WEBHOOK_URL or not RETURNS_AGENT_WEBHOOK_TOKEN:
		abort(503, description="The Stage-4 webhook is not configured.")
	publish_pack_record(record)
	return redirect(url_for("alerts"))


@app.post("/api/agent/transfer")
@login_required
def api_agent_transfer():
	return jsonify({
		"error": "Scenario transfers were a simulator and are no longer available. Submit a CUBE prep record and continue from Agent Network."
	}), 410


@app.get("/api/agent/transfers")
@login_required
def api_agent_list_transfers():
	return jsonify({"error": "Simulated transfer history is retired; use the CUBE agent workflow."}), 410


@app.get("/api/agent/transfers/<transfer_id>")
@login_required
def api_agent_get_transfer(transfer_id: str):
	return jsonify({"error": "Simulated transfer transcripts are retired; use the CUBE agent workflow."}), 410


@app.post("/api/agent/command")
@login_required
def api_agent_command():
	payload = request.get_json(silent=True) or request.form
	command = payload.get("command", "").strip()
	if not command:
		return jsonify({"answer": "Please provide a command or question for the PackGuard Agent."}), 400
	if any(term in command.casefold() for term in ("transfer", "dispatch", "move", "ship")):
		return jsonify({
			"error": "PackGuard no longer simulates transfers. Start from a ready Stage-2 prep record and capture the outbound box."
		}), 410
	return jsonify({
		"answer": agent_workflow_summary(current_org()),
		"action_taken": "WORKFLOW_STATUS",
	})


@app.route("/capture", methods=["GET", "POST"])
@login_required
def capture():
	org_id = current_org()
	if request.method == "GET":
		defaults = {"attempt_type": "initial", "recapture_reason": ""}
		products = catalog_products(org_id)
		prep_workflow = []
		for prep_record in list_contract_records(org_id, since=None, agent="prep", cursor=None, limit=20):
			prep_subject = prep_record.get("subject") or {}
			prep_route = prep_record_logistics(prep_record)
			prep_sku = str(prep_subject.get("sku") or "")
			prep_product = next((product for product in products if product["sku"] == prep_sku), None)
			prep_quantity = prep_subject.get("quantity_expected")
			prep_ready = prep_record_is_ready(prep_record)
			can_start = (
				prep_ready and bool(prep_subject.get("order_id")) and bool(prep_sku)
				and isinstance(prep_quantity, int) and prep_quantity > 0
			)
			prep_workflow.append({
				"record_id": prep_record["record_id"],
				"order_id": prep_subject.get("order_id"),
				"shipment_id": prep_subject.get("shipment_id"),
				"sku": prep_sku,
				"product_name": prep_product["product_name"] if prep_product else prep_sku,
				"quantity_expected": prep_quantity,
				"decision": prep_record.get("outcome", {}).get("decision", "manual_review"),
				"ready": prep_ready,
				"logistics": prep_route,
				"capture_url": url_for("capture", prep_record_id=prep_record["record_id"]) if can_start else None,
			})
		prep_record_id = request.args.get("prep_record_id", "").strip()
		if prep_record_id:
			prep_record = get_contract_record(prep_record_id, org_id)
			if not prep_record or prep_record.get("agent") != "prep" or not prep_record_is_ready(prep_record):
				abort(404)
			subject = prep_record.get("subject") or {}
			if not subject.get("order_id") or not subject.get("sku") or not isinstance(subject.get("quantity_expected"), int) or subject["quantity_expected"] <= 0:
				abort(400, description="Stage-2 prep record must include order, SKU, and a positive expected quantity.")
			product = next((item for item in products if item["sku"] == subject["sku"]), None)
			logistics = prep_record_logistics(prep_record)
			defaults.update({
				"source_prep_record_id": prep_record_id,
				"selected_product_id": product["product_id"] if product else "",
				"product_lookup": product["product_name"] if product else subject["sku"],
				"unit_id": subject.get("shipment_id") or "",
				"order_id": subject["order_id"],
				"order_lines": f"{subject['sku']}:{subject['quantity_expected']}",
				**logistics,
			})
		product_lookup = request.args.get("product_id", "").strip() or request.args.get("sku", "").strip() or request.args.get("product", "").strip()
		selected_product = next((
			product for product in products
			if product_lookup and product_lookup.casefold() in {
				str(product.get("product_id") or "").casefold(),
				str(product.get("sku") or "").casefold(),
				str(product.get("barcode") or "").casefold(),
				str(product.get("product_name") or "").casefold(),
			}
		), None)
		if selected_product:
			product_defaults = {
				"selected_product_id": selected_product["product_id"],
				"product_lookup": selected_product["product_name"],
				"order_lines": f"{selected_product['sku']}:1",
				"supplier_name": selected_product.get("supplier_name") or "",
				"origin_address": selected_product.get("origin_address") or "",
				"ordered_for": selected_product.get("ordered_for") or "",
				"delivery_address": selected_product.get("delivery_address") or "",
			}
			if not prep_record_id:
				defaults.update(product_defaults)
		recapture_of = request.args.get("recapture_of", "").strip()
		if recapture_of:
			previous = get_record(recapture_of, org_id)
			if previous:
				defaults.update({
					"unit_id": previous["unit_id"], "order_id": previous["order_id"],
					"supplier_name": previous.get("supplier_name") or "",
					"operator_id": session.get("username", previous["operator_id"]),
					"channel": previous["channel"], "origin_address": previous["origin_address"] or "",
					"delivery_address": previous["delivery_address"] or "", "order_lines": previous["order_lines"],
					"attempt_type": "recapture", "parent_record_id": previous["record_id"],
				})
				defaults["selected_product_id"] = selected_product["product_id"] if selected_product else ""
		return render_template("capture.html", products=products, defaults=defaults, prep_workflow=prep_workflow)
	form = request.form
	order_lines = form.get("order_lines", "").strip()
	observed_in_box = form.get("observed_in_box", "").strip()
	source_prep_record_id = form.get("source_prep_record_id", "").strip() or None
	source_prep_record = None
	prep_subject = {}
	prep_route = {}
	if source_prep_record_id:
		try:
			UUID(source_prep_record_id)
		except (ValueError, TypeError, AttributeError):
			abort(400, description="Invalid Stage-2 prep record ID")
		source_prep_record = get_contract_record(source_prep_record_id, org_id)
		if not source_prep_record or source_prep_record.get("agent") != "prep" or not prep_record_is_ready(source_prep_record):
			abort(400, description="A completed Stage-2 prep record with passing checks is required")
		prep_subject = source_prep_record.get("subject") or {}
		if not prep_subject.get("order_id") or not prep_subject.get("sku") or not isinstance(prep_subject.get("quantity_expected"), int) or prep_subject["quantity_expected"] <= 0:
			abort(400, description="Stage-2 record must provide order, SKU, and positive expected quantity")
		expected_from_prep = f"{prep_subject['sku']}:{prep_subject['quantity_expected']}"
		if order_lines and order_lines != expected_from_prep:
			abort(400, description="Expected SKU and quantity must match the Stage-2 prep record")
		order_lines = expected_from_prep
		submitted_order_id = form.get("order_id", "").strip()
		if submitted_order_id and submitted_order_id != prep_subject["order_id"]:
			abort(400, description="Order ID must match the Stage-2 prep record")
		submitted_unit_id = form.get("unit_id", "").strip()
		if submitted_unit_id and prep_subject.get("shipment_id") and submitted_unit_id != prep_subject["shipment_id"]:
			abort(400, description="Shipment/unit ID must match the Stage-2 prep record")
		prep_route = prep_record_logistics(source_prep_record)
	products_by_id = {product["product_id"]: product for product in catalog_products(org_id)}
	selected_product = products_by_id.get(form.get("selected_product_id", "").strip())
	if selected_product and not order_lines:
		order_lines = f"{selected_product['sku']}:1"
	expected_skus = set(parse_lines(order_lines))
	if selected_product and selected_product["sku"] not in expected_skus:
		selected_product = None
	products_by_sku = {product["sku"]: product for product in products_by_id.values()}
	if source_prep_record:
		prep_product = products_by_sku.get(str(prep_subject["sku"]))
		if selected_product and selected_product["sku"] != prep_subject["sku"]:
			abort(400, description="Selected product must match the Stage-2 prep record")
		selected_product = prep_product
	product_routes = [
		{
			"product_id": products_by_sku[sku]["product_id"],
			"sku": sku,
			"product_name": products_by_sku[sku]["product_name"],
			"supplier_name": products_by_sku[sku].get("supplier_name"),
			"origin_address": products_by_sku[sku].get("origin_address"),
			"ordered_for": products_by_sku[sku].get("ordered_for"),
			"delivery_address": products_by_sku[sku].get("delivery_address"),
		}
		for sku in sorted(expected_skus) if sku in products_by_sku
	]
	if source_prep_record:
		prep_route_snapshot = {
			"product_id": selected_product["product_id"] if selected_product else None,
			"sku": prep_subject["sku"],
			"product_name": selected_product["product_name"] if selected_product else prep_subject["sku"],
			**prep_route,
			"source_prep_record_id": source_prep_record_id,
		}
		product_routes = [item for item in product_routes if item["sku"] != prep_subject["sku"]]
		product_routes.append(prep_route_snapshot)
	primary_product = selected_product or (products_by_sku.get(next(iter(expected_skus))) if len(expected_skus) == 1 else None)
	supplier_name = prep_route.get("supplier_name") or form.get("supplier_name", "").strip() or (primary_product or {}).get("supplier_name")
	origin_address = prep_route.get("origin_address") or form.get("origin_address", "").strip() or (primary_product or {}).get("origin_address")
	delivery_address = prep_route.get("delivery_address") or form.get("delivery_address", "").strip() or (primary_product or {}).get("delivery_address")
	unit_id = form.get("unit_id", "").strip() or (prep_subject.get("shipment_id") if source_prep_record else None) or f"UNIT-{uuid4().hex[:8].upper()}"
	order_id = form.get("order_id", "").strip() or (prep_subject.get("order_id") if source_prep_record else None) or f"ORD-LOCAL-{uuid4().hex[:8].upper()}"
	operator_id = form.get("operator_id", "").strip() or session.get("username", "operator")
	attempt_type = form.get("attempt_type", "initial").strip().lower()
	recapture_reason = form.get("recapture_reason", "").strip()
	if attempt_type not in {"initial", "recapture"}:
		abort(400, description="Invalid capture attempt type")
	if attempt_type == "recapture" and not recapture_reason:
		abort(400, description="A recapture reason is required")
	parent_record = None
	if attempt_type == "recapture":
		parent_record_id = form.get("parent_record_id", "").strip()
		parent_record = get_record(parent_record_id, org_id) if parent_record_id else None
		if not parent_record:
			abort(400, description="A recapture must reference an existing record in this organization")
		if form.get("order_id", "").strip() != parent_record["order_id"] or form.get("unit_id", "").strip() != parent_record["unit_id"]:
			abort(400, description="Recapture order and unit must match the original pack session")
		submitted_supplier = form.get("supplier_name", "").strip()
		if submitted_supplier and submitted_supplier != (parent_record.get("supplier_name") or ""):
			abort(400, description="Recapture supplier must match the original receiving session")
		supplier_name = parent_record.get("supplier_name")
		origin_address = form.get("origin_address", "").strip() or parent_record.get("origin_address")
		delivery_address = form.get("delivery_address", "").strip() or parent_record.get("delivery_address")
	result = verify_pack(order_lines, observed_in_box)
	attach_catalog_metadata(result, org_id)
	record_id = f"PCK-{uuid4().hex[:8].upper()}"
	photo_ref = save_photo(request.files.get("photo"), org_id, record_id)
	video_ref = save_media(request.files.get("packing_video"), org_id, record_id, "video")
	photo_ref = photo_ref or form.get("photo_ref", "").strip() or None
	image_observation = observe(observed_in_box)
	expected_skus = set(parse_lines(order_lines))
	vision_candidates = vision_catalog_products(org_id, expected_skus)
	if photo_ref and photo_ref.startswith(f"{org_id}/"):
		photo_input = MEDIA_STORAGE.read(photo_ref) or photo_ref
	elif photo_ref and photo_ref.startswith("fixtures/vision/"):
		photo_input = APP_DIR / photo_ref
	else:
		photo_input = None
	configured_vision_model = os.environ.get("PACKGUARD_VISION_MODEL", "").strip()
	fast_presence_mode = (
		photo_input is not None
		and not observed_in_box
		and len(expected_skus) == 1
		and (not configured_vision_model or configured_vision_model.lower().startswith("moondream"))
	)
	if fast_presence_mode:
		vision_candidates = [product for product in vision_candidates if product.get("sku") in expected_skus]
	vision_result = inspect_image(
		photo_input,
		catalog_products=vision_candidates,
		photo_name=Path(photo_ref).name if photo_ref else None,
		model_override=configured_vision_model or ("moondream:1.8b" if fast_presence_mode else None),
		force_structured=not fast_presence_mode,
	)
	if vision_result.get("status") == "SUGGESTIONS_READY_UNCALIBRATED":
		candidate_skus = {product["sku"] for product in vision_candidates}
		detected_lines = []
		for detection in vision_result.get("detected_items", []):
			if not isinstance(detection, dict):
				continue
			sku = detection.get("sku")
			quantity = detection.get("quantity")
			if isinstance(sku, str) and sku in candidate_skus and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0:
				detected_lines.append(f"{sku}:{quantity}")
		vision_result["suggested_observed_contents"] = ";".join(detected_lines)
		if detected_lines:
			candidate_result = verify_pack(order_lines, ";".join(detected_lines))
			attach_catalog_metadata(candidate_result, org_id)
			vision_result["candidate_checks"] = candidate_result["checks"]
			quality_status = vision_result.get("image_quality", {}).get("status", "UNCERTAIN")
			if quality_status == "POOR":
				candidate_decision = "RECAPTURE"
			elif quality_status != "GOOD" or vision_result.get("uncertainties"):
				candidate_decision = "MANUAL_REVIEW"
			elif vision_result.get("extra_items") or any(item.get("variant_match") is False for item in vision_result["detected_items"] if isinstance(item, dict)) or candidate_result["verdict"] == "FAIL":
				candidate_decision = "FIX"
			elif candidate_result["verdict"] == "PASS":
				candidate_decision = "SEAL"
			else:
				candidate_decision = "MANUAL_REVIEW"
			vision_result["candidate_decision"] = candidate_decision
			vision_result["decision_status"] = "SUGGESTION_ONLY_NOT_CALIBRATED"
		else:
			vision_result["candidate_decision"] = "MANUAL_REVIEW"
			vision_result["decision_status"] = "SUGGESTION_ONLY_NOT_CALIBRATED"
		quality_status = vision_result.get("image_quality", {}).get("status", "UNCERTAIN")
		if not observed_in_box:
			if quality_status == "POOR":
				result["decision"] = "RECAPTURE"
				result["reason"] = "The local vision model flags the photo quality as poor; recapture the pack image."
			else:
				result["decision"] = "MANUAL_REVIEW"
				result["reason"] = (
					"The local vision result is an uncalibrated suggestion. "
					"A human must confirm the contents before a final packing decision."
				)
	image_workflow = run_pack_agent_workflow(
		vision_result,
		expected_lines=order_lines,
		observed_contents=observed_in_box,
	)
	vision_result["candidate_decision"] = image_workflow["candidate_decision"]
	vision_result["candidate_reason"] = image_workflow["candidate_reason"]
	vision_result["agent_run"] = image_workflow["trace"]
	if not observed_in_box:
		result = enforce_component_verdict(result, vision_result, bool(photo_ref))
		if image_workflow["decision"] == "fix":
			result.update({
				"verdict": "FAIL", "action": "STOP_AND_FIX", "decision": "FIX",
				"reason": image_workflow["candidate_reason"],
			})
		elif image_workflow["decision"] == "recapture":
			result.update({
				"verdict": "UNCERTAIN", "action": "HOLD_FOR_REVIEW", "decision": "RECAPTURE",
				"reason": image_workflow["candidate_reason"],
			})
	vision_result["auto_seal_policy"] = auto_seal_policy(None, vision_result)
	operator_verdict = form.get("operator_verdict", "").strip().lower()
	result = require_operator_confirmation(result, operator_verdict)
	vision_result["agent_assessment"] = assess_pack(
		verifier_result=result,
		inspection=vision_result,
		operator_observation=observed_in_box,
	)
	override_reason = form.get("override_reason", "").strip()
	agreement = operator_agreement(operator_verdict, result["verdict"])
	if operator_verdict and agreement == "DISAGREES" and IS_PRODUCTION and not override_reason:
		abort(400, description="A reason is required when overriding the deterministic decision")
	workflow_state = {
		"SEAL": "SEALED",
		"FIX": "FIX_REQUIRED",
		"RECAPTURE": "RECAPTURE",
		"MANUAL_REVIEW": "MANUAL_REVIEW",
	}.get(result.get("decision", "MANUAL_REVIEW"), "MANUAL_REVIEW")
	if parent_record:
		session_id = parent_record.get("session_id") or parent_record["record_id"]
		attempt_number = len(list_session_records(session_id, org_id)) + 1
		parent_record_id = parent_record["record_id"]
	else:
		session_id = record_id
		attempt_number = 1
		parent_record_id = None
	evidence = build_evidence(
		record_id=record_id, org_id=org_id, unit_id=unit_id,
		order_lines=order_lines, observed_in_box=observed_in_box,
		supplier_name=supplier_name or None,
		result=result, photo_ref=photo_ref, image_observation=image_observation,
		vision_result=vision_result,
		operator_verdict=operator_verdict, agreement=agreement,
		video_ref=video_ref,
	)
	evidence["source"]["product_routes"] = product_routes
	stage3_contract_record = None
	if source_prep_record_id:
		evidence["source"]["source_prep_record_id"] = source_prep_record_id
		stage3_contract_record = build_stage3_contract_record(
			organization_key=org_id,
			prep_record=source_prep_record,
			operator_label=operator_id,
			expected_lines=order_lines,
			observed_contents=observed_in_box,
			pack_result=result,
			vision_result=vision_result,
			product_routes=product_routes,
			photo_ref=photo_ref,
		)
		evidence["source"]["stage3_contract_record_id"] = stage3_contract_record["record_id"]
	save_record({
		"record_id": record_id, "org_id": org_id,
		"unit_id": unit_id,
		"order_id": order_id,
		"supplier_name": supplier_name or None,
		"channel": form.get("channel", "amazon_mfn"),
		"origin_address": origin_address or None,
		"delivery_address": delivery_address or None,
		"order_lines": order_lines, "observed_in_box": observed_in_box,
		"operator_verdict": operator_verdict,
		"video_ref": video_ref,
		"operator_id": session.get("username", "operator") if IS_PRODUCTION else operator_id,
		"photo_ref": photo_ref,
		"captured_at": datetime.now(timezone.utc).isoformat(),
		"verdict": result["verdict"], "action": result["action"],
		"workflow_state": workflow_state,
		"session_id": session_id, "parent_record_id": parent_record_id,
		"attempt_number": attempt_number,
		"reason": result["reason"], "evidence": evidence,
	})
	save_audit_event({
		"event_id": f"AUD-{uuid4().hex[:12].upper()}",
		"record_id": record_id,
		"org_id": org_id,
		"event_type": "RECAPTURE" if attempt_type == "recapture" else "CAPTURED",
		"reason": recapture_reason if attempt_type == "recapture" else "Initial packing capture",
		"actor_id": operator_id,
		"created_at": datetime.now(timezone.utc).isoformat(),
		"details": {"decision": result.get("decision"), "verdict": result["verdict"], "session_id": session_id, "attempt_number": attempt_number, "parent_record_id": parent_record_id},
	})
	if operator_verdict and agreement == "DISAGREES":
		save_audit_event({
			"event_id": f"AUD-{uuid4().hex[:12].upper()}",
			"record_id": record_id,
			"org_id": org_id,
			"event_type": "OPERATOR_OVERRIDE",
			"reason": override_reason or "Legacy demo override",
			"actor_id": operator_id,
			"created_at": datetime.now(timezone.utc).isoformat(),
			"details": {
				"operator_verdict": operator_verdict,
				"deterministic_verdict": result["verdict"],
				"deterministic_decision": result.get("decision"),
			},
		})
	if stage3_contract_record:
		try:
			persistence_status = save_contract_record(stage3_contract_record, org_id)
			if persistence_status == "created":
				publish_pack_record(stage3_contract_record)
		except Exception:
			app.logger.exception("Could not persist Stage-3 CUBE output for prep source %s", source_prep_record_id)
			save_audit_event({
				"event_id": f"AGT-{uuid4().hex[:12].upper()}",
				"record_id": record_id,
				"org_id": org_id,
				"event_type": "STAGE3_HANDOFF_FAILED",
				"reason": "Stage-3 CUBE output could not be persisted; retry or inspect server logs.",
				"actor_id": "agent:packguard_stage3",
				"created_at": utc_now(),
				"details": {"source_prep_record_id": source_prep_record_id},
			})
	return redirect(url_for("record_detail", record_id=record_id, org_id=org_id))


@app.get("/capture/contract")
@login_required
def contract_capture_page():
	defaults = {}
	prep_record_id = request.args.get("prep_record_id", "").strip()
	if prep_record_id:
		prep_record = get_contract_record(prep_record_id, current_org())
		if not prep_record or prep_record.get("agent") != "prep" or not prep_record_is_ready(prep_record):
			abort(404)
		subject = prep_record["subject"]
		if (
			not subject.get("order_id") or not subject.get("sku")
			or not isinstance(subject.get("quantity_expected"), int)
			or subject["quantity_expected"] <= 0
		):
			abort(400, description="The prep record is missing the order, SKU, or expected quantity required for pack capture.")
		defaults = {
			"order_id": subject["order_id"],
			"shipment_id": subject.get("shipment_id") or "",
			"sku": subject["sku"],
			"quantity_expected": subject["quantity_expected"],
			"source_record_id": prep_record_id,
		}
	return render_template(
		"contract_capture.html",
		direct_upload_enabled=MEDIA_STORAGE.backend == "s3",
		defaults=defaults,
	)


@app.get("/records/<record_id>")
@login_required
def record_detail(record_id: str):
	record = get_record(record_id, current_org())
	if not record:
		abort(404)
	record["evidence"] = json.loads(record["evidence_json"])
	record["audit_events"] = list_audit_events(record_id, current_org())
	record["session_records"] = list_session_records(record.get("session_id") or record_id, current_org())
	record["catalog_references"] = []
	for sku in parse_lines(record["order_lines"]):
		product = get_product(current_org(), sku)
		if product and product.get("reference_image_ref"):
			reference = product["reference_image_ref"]
			if reference.startswith(f"{current_org()}/") or reference.startswith("fixtures/vision/"):
				record["catalog_references"].append({
					"sku": sku,
					"product_name": product["product_name"],
					"reference_image_ref": reference,
				})
	return render_template("record.html", record=record)


@app.post("/records/<record_id>/transition")
@login_required
def transition_record(record_id: str):
	org_id = current_org()
	record = get_record(record_id, org_id)
	if not record:
		abort(404)
	target = request.form.get("workflow_state", "").strip().upper()
	reason = request.form.get("reason", "").strip()
	allowed = {
		"SEALED": record["verdict"] == "PASS" and record.get("operator_verdict") == "seal",
		"FIX_REQUIRED": record["verdict"] == "FAIL",
		"RECAPTURE": record["verdict"] == "UNCERTAIN",
		"MANUAL_REVIEW": record["verdict"] == "UNCERTAIN",
	}
	if target not in allowed or not allowed[target] or not reason:
		abort(400, description="Invalid workflow transition or missing reason")
	if record.get("workflow_state") == "SEALED":
		abort(409, description="A sealed record cannot be transitioned")
	update_workflow_state(record_id, org_id, target)
	save_audit_event({
		"event_id": f"AUD-{uuid4().hex[:12].upper()}", "record_id": record_id,
		"org_id": org_id, "event_type": "STATE_TRANSITION", "reason": reason,
		"actor_id": session.get("username", "operator"),
		"created_at": datetime.now(timezone.utc).isoformat(),
		"details": {"from": record.get("workflow_state", "REVIEW"), "to": target},
	})
	return redirect(url_for("record_detail", record_id=record_id))


@app.get("/records/<record_id>/report")
@login_required
def record_report(record_id: str):
	record = get_record(record_id, current_org())
	if not record:
		abort(404)
	record["evidence"] = json.loads(record["evidence_json"])
	record["audit_events"] = list_audit_events(record_id, current_org())
	record["session_records"] = list_session_records(record.get("session_id") or record_id, current_org())
	return render_template("record_report.html", record=record)


@app.get("/api/records/<record_id>/audit")
@login_required
def record_audit_api(record_id: str):
	if not get_record(record_id, current_org()):
		abort(404)
	return jsonify({"record_id": record_id, "events": list_audit_events(record_id, current_org())})


@app.get("/media/<path:filename>")
@login_required
def media_file(filename: str):
	org_id = current_org()
	if filename.startswith(f"{org_id}/"):
		try:
			media = MEDIA_STORAGE.read(filename)
		except ValueError:
			abort(404)
		if media is None:
			abort(404)
		response = send_file(
			BytesIO(media),
			mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream",
			as_attachment=False,
			download_name=Path(filename).name,
		)
		response.headers["Cache-Control"] = "private, no-store"
		return response
	fixture_prefix = "fixtures/vision/"
	if filename.startswith(fixture_prefix):
		fixture_name = filename[len(fixture_prefix):]
		extension = Path(fixture_name).suffix.lower().lstrip(".")
		if Path(fixture_name).name == fixture_name and extension in ALLOWED_IMAGE_EXTENSIONS:
			return send_from_directory(APP_DIR / "fixtures" / "vision", fixture_name)
	abort(404)


@app.get("/api/records")
@login_required
def records_api():
	records = list_records(current_org())
	for record in records:
		record["evidence"] = json.loads(record.pop("evidence_json"))
	return jsonify({"org_id": current_org(), "count": len(records), "records": records})


def _contract_vision_job(record_id: str, organization_key: str, capture: dict[str, Any], record: dict[str, Any]) -> None:
	try:
		input_data = json.loads(capture["input_json"])
		slots = json.loads(capture["image_slots_json"])
		images = []
		for slot in slots:
			image_bytes = MEDIA_STORAGE.read(slot["key"])
			if image_bytes is None:
				return
			images.append((image_bytes, Path(slot["key"]).name))
		first_image, *additional_images = images
		expected_skus = set(parse_lines(input_data.get("expected_lines", "")))
		vision_products = vision_catalog_products(organization_key, expected_skus)
		configured_model = os.environ.get("PACKGUARD_CONTRACT_VISION_MODEL", "").strip()
		fast_presence_mode = (
			not input_data.get("observed_contents", "").strip()
			and len(expected_skus) == 1
			and (not configured_model or configured_model.lower().startswith("moondream"))
		)
		if fast_presence_mode:
			vision_products = [product for product in vision_products if product.get("sku") in expected_skus]
		model_override = configured_model or ("moondream:1.8b" if fast_presence_mode else "gemma3:4b")
		vision_result = inspect_image(
			first_image[0],
			catalog_products=vision_products,
			photo_name=first_image[1],
			additional_images=additional_images,
			model_override=model_override,
			force_structured=not fast_presence_mode,
			capture_context={
				"expected_lines": input_data.get("expected_lines", ""),
				"check_keys": ["sku_quantity", "visual_evidence"],
			},
		)
		workflow = run_pack_agent_workflow(
			vision_result,
			expected_lines=input_data.get("expected_lines", ""),
			observed_contents=input_data.get("observed_contents", ""),
		)
		visual_check = next(check for check in record["checks"] if check["check_key"] == "visual_evidence")
		visual_check["model_version"] = vision_result.get("provider", "unavailable")
		visual_check["latency_ms"] = int(vision_result.get("inference_ms") or 0)
		visual_check["detail"] = {
			"status": vision_result.get("status", "MODEL_ERROR"),
			"detected_items": vision_result.get("detected_items", []),
			"extra_items": vision_result.get("extra_items", []),
			"image_quality": vision_result.get("image_quality", {}),
			"occlusion": vision_result.get("occlusion", {}),
			"uncertainties": vision_result.get("uncertainties", []),
			"presence_hint": vision_result.get("presence_hint"),
			"token_usage": vision_result.get("token_usage"),
			"confidence": vision_result.get("confidence"),
			"vision_observation_candidate": workflow["vision_observation"] or None,
			"candidate_decision": workflow["candidate_decision"],
			"candidate_reason": workflow["candidate_reason"],
			"automatic_seal_authorized": False,
			"agent_run": workflow["trace"],
		}
		record["status"] = "pending" if vision_result.get("status") in FAILED_VISION_STATUSES else "complete"
		record["outcome"] = {
			"decision": workflow["decision"],
			"decided_by": "agent",
			"decided_at": utc_now(),
		}
		record["content_hash"] = content_hash(record["images"], record["checks"])
		validate_record(record)
		if update_contract_record(record, organization_key):
			publish_pack_record(record)
	except Exception:
		app.logger.exception("CUBE vision job failed for record %s", record_id)


@app.post("/v1/captures")
@login_required
def contract_create_capture():
	if MEDIA_STORAGE.backend != "s3" or not hasattr(MEDIA_STORAGE, "presign_put"):
		return jsonify({"error": "Direct capture requires configured private S3-compatible storage."}), 503
	payload = request.get_json(silent=True) or {}
	organization_key = current_org()
	agent_name = payload.get("agent", "pack")
	if agent_name != "pack":
		return jsonify({"error": "This Manager writes agent=pack records only."}), 400
	if not isinstance(payload.get("subject"), dict):
		return jsonify({"error": "subject is required."}), 400
	subject = payload["subject"]
	if set(subject) != SUBJECT_FIELDS or subject.get("type") != "order" or not subject.get("order_id"):
		return jsonify({"error": "Pack subject must be an order with every contract 1.1 subject field."}), 400
	for field in ("quantity_expected", "quantity_observed"):
		value = subject.get(field)
		if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
			return jsonify({"error": f"subject.{field} must be an integer or null."}), 400
	client_id = payload.get("client_id")
	try:
		if client_id is not None:
			UUID(client_id)
	except (ValueError, TypeError, AttributeError):
		return jsonify({"error": "client_id must be a UUID or null."}), 400
	source_record_id = payload.get("source_record_id")
	if source_record_id is not None:
		try:
			UUID(source_record_id)
		except (ValueError, TypeError, AttributeError):
			return jsonify({"error": "source_record_id must be a UUID or null."}), 400
		prep_record = get_contract_record(source_record_id, organization_key)
		if not prep_record or prep_record.get("agent") != "prep" or not prep_record_is_ready(prep_record):
			return jsonify({"error": "A completed prep record with passing checks in this organization is required as the source."}), 400
		prep_subject = prep_record["subject"]
		if not isinstance(prep_subject.get("quantity_expected"), int) or prep_subject["quantity_expected"] <= 0:
			return jsonify({"error": "The prep source record must provide a positive expected quantity."}), 400
		if (
			subject.get("order_id") != prep_subject.get("order_id")
			or subject.get("sku") != prep_subject.get("sku")
			or subject.get("quantity_expected") != prep_subject.get("quantity_expected")
			or subject.get("shipment_id") != prep_subject.get("shipment_id")
		):
			return jsonify({"error": "Pack capture order, SKU, quantity, and shipment must match the prep source record."}), 400
		expected_from_source = f"{prep_subject['sku']}:{prep_subject['quantity_expected']}"
		if str(payload.get("expected_lines", "")).strip() != expected_from_source:
			return jsonify({"error": "Expected lines must match the prep source record."}), 400
	image_slots = payload.get("image_slots")
	if not isinstance(image_slots, list) or len(image_slots) not in {2, 3}:
		return jsonify({"error": "Provide two or three guided image slots."}), 400
	allowed_slots = {"overview", "labels", "detail"}
	if any(not isinstance(slot, dict) or slot.get("slot") not in allowed_slots for slot in image_slots):
		return jsonify({"error": "Image slots must use overview, labels, or detail."}), 400
	if len({slot["slot"] for slot in image_slots}) != len(image_slots) or not {"overview", "labels"} <= {slot["slot"] for slot in image_slots}:
		return jsonify({"error": "Capture requires unique overview and labels shots; detail is optional."}), 400
	content_extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
	capture_id = str(uuid4())
	org_uuid = organization_uuid(organization_key)
	upload_urls = []
	stored_slots = []
	for slot in image_slots:
		content_type = slot.get("content_type")
		if content_type not in content_extensions:
			return jsonify({"error": "Capture images must be JPEG, PNG, or WEBP."}), 400
		key = f"{org_uuid}/{capture_id}/{slot['slot']}-{uuid4().hex}.{content_extensions[content_type]}"
		try:
			url = MEDIA_STORAGE.presign_put(key, content_type)
		except Exception:
			app.logger.exception("Could not create a private evidence upload URL")
			return jsonify({"error": "Private evidence storage is unavailable."}), 503
		stored_slots.append({"slot": slot["slot"], "key": key, "content_type": content_type})
		upload_urls.append({"slot": slot["slot"], "key": key, "url": url, "headers": {"Content-Type": content_type}})
	input_data = {
		"expected_lines": str(payload.get("expected_lines", ""))[:4000],
		"observed_contents": str(payload.get("observed_contents", ""))[:4000],
		"source_record_id": source_record_id,
	}
	capture = {
		"capture_id": capture_id,
		"organization_key": organization_key,
		"organization_id": org_uuid,
		"owner_username": session["username"],
		"client_id": client_id,
		"agent": agent_name,
		"subject": subject,
		"operator_label": session["username"],
		"image_slots": stored_slots,
		"input": input_data,
		"created_at": utc_now(),
	}
	try:
		create_contract_capture(capture)
	except Exception:
		app.logger.exception("Could not create organization-scoped evidence capture")
		return jsonify({"error": "Could not create capture."}), 500
	return jsonify({"capture_id": capture_id, "upload_urls": upload_urls}), 201


@app.post("/v1/captures/<capture_id>/complete")
@login_required
def contract_complete_capture(capture_id: str):
	try:
		UUID(capture_id)
	except (ValueError, TypeError, AttributeError):
		return jsonify({"error": "capture_id must be a UUID."}), 400
	organization_key = current_org()
	capture = get_contract_capture(capture_id, organization_key)
	if not capture or capture["owner_username"] != session["username"]:
		return jsonify({"error": "Capture not found."}), 404
	if capture["status"] != "pending":
		return jsonify({"error": "Capture is already being completed or has completed."}), 409
	payload = request.get_json(silent=True) or {}
	requested_images = payload.get("images")
	if not isinstance(requested_images, list):
		return jsonify({"error": "images must list each uploaded key and taken_at."}), 400
	slots = json.loads(capture["image_slots_json"])
	slot_by_key = {slot["key"]: slot for slot in slots}
	if len(requested_images) != len(slots) or {item.get("key") for item in requested_images if isinstance(item, dict)} != set(slot_by_key):
		return jsonify({"error": "Every presigned image must be completed exactly once."}), 400
	images = []
	for item in requested_images:
		if not isinstance(item, dict) or set(item) != {"key", "taken_at"} or not isinstance(item["taken_at"], str):
			return jsonify({"error": "Image completion items require only key and taken_at."}), 400
		try:
			taken_at = datetime.fromisoformat(item["taken_at"].replace("Z", "+00:00"))
			if taken_at.tzinfo is None:
				raise ValueError
		except ValueError:
			return jsonify({"error": "taken_at must be an ISO 8601 timestamp with timezone."}), 400
		try:
			image_bytes = MEDIA_STORAGE.read(item["key"])
		except Exception:
			image_bytes = None
		if image_bytes is None:
			return jsonify({"error": "An uploaded image is not yet available; retry completion after upload."}), 409
		images.append({
			"key": item["key"],
			"sha256": hashlib.sha256(image_bytes).hexdigest(),
			"bytes": len(image_bytes),
			"taken_at": taken_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
		})
	images.sort(key=lambda image: next(index for index, slot in enumerate(slots) if slot["key"] == image["key"]))
	input_data = json.loads(capture["input_json"])
	deterministic_started = time.perf_counter()
	verification = verify_pack(input_data["expected_lines"], input_data["observed_contents"])
	deterministic_latency = max(0, round((time.perf_counter() - deterministic_started) * 1000))
	verdict = {"PASS": "pass", "FAIL": "fail", "UNCERTAIN": "uncertain"}.get(verification["verdict"], "uncertain")
	decision = {
		"FAIL": "fix",
		"UNCERTAIN": "recapture" if not input_data["observed_contents"].strip() else "manual_review",
		"PASS": "manual_review",
	}.get(verification["verdict"], "manual_review")
	if verification["checks"]:
		sku_details = verification["checks"]
	else:
		sku_details = [{"reason": verification["reason"], "expected": verification["expected"], "observed": verification["observed"]}]
	checks = [{
		"check_key": "sku_quantity",
		"verdict": verdict,
		"confidence": None,
		"detail": {
			"sku_checks": sku_details,
			"reason": verification["reason"],
			"source_prep_record_id": input_data.get("source_record_id"),
		},
		"model_version": "deterministic-packguard-v1.1",
		"latency_ms": deterministic_latency,
	}, {
		"check_key": "visual_evidence",
		"verdict": "uncertain",
		"confidence": None,
		"detail": {"status": "pending", "reason": "One batched vision assessment is queued for all capture shots."},
		"model_version": os.environ.get("PACKGUARD_CONTRACT_VISION_MODEL", "gemma3:4b"),
		"latency_ms": 0,
	}]
	record = build_contract_record(
		organization_key=organization_key,
		client_id=capture["client_id"],
		agent=capture["agent"],
		subject=json.loads(capture["subject_json"]),
		operator_label=capture["operator_label"],
		images=images,
		checks=checks,
		decision=decision,
		decided_by="agent",
		status="pending",
	)
	if not complete_contract_capture(capture_id, organization_key, record):
		return jsonify({"error": "Capture was already completed."}), 409
	CONTRACT_VISION_POOL.submit(_contract_vision_job, record["record_id"], organization_key, capture, record)
	return jsonify({"record_id": record["record_id"], "status": "pending"}), 201


@app.get("/v1/records/<record_id>")
def contract_record_api(record_id: str):
	try:
		UUID(record_id)
	except (ValueError, TypeError, AttributeError):
		return jsonify({"error": "record_id must be a UUID."}), 400
	if session.get("org_id"):
		record = get_contract_record(record_id, current_org())
	else:
		record = get_contract_record(record_id, public=True)
	if not record:
		return jsonify({"error": "Record not found."}), 404
	return jsonify(record)


@app.get("/v1/records/<record_id>/images/<path:image_key>")
def contract_record_image_api(record_id: str, image_key: str):
	if session.get("org_id"):
		record = get_contract_record(record_id, current_org())
	else:
		record = get_contract_record(record_id, public=True)
	if not record or not any(image["key"] == image_key for image in record["images"]):
		abort(404)
	try:
		image_bytes = MEDIA_STORAGE.read(image_key)
	except (ValueError, Exception):
		abort(404)
	if image_bytes is None:
		abort(404)
	response = app.response_class(
		image_bytes,
		mimetype=mimetypes.guess_type(image_key)[0] or "application/octet-stream",
	)
	response.headers["Cache-Control"] = "private, no-store"
	return response


@app.get("/v1/records")
@login_required
def contract_records_api():
	organization_key = current_org()
	agent_name = request.args.get("agent")
	if agent_name and agent_name not in {"receiving", "prep", "pack", "returns"}:
		return jsonify({"error": "Invalid agent filter."}), 400
	since = request.args.get("since")
	if since:
		try:
			parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
			if parsed_since.tzinfo is None:
				raise ValueError
			# Timestamps are stored as normalized UTC text. Normalize offsets before
			# the database's lexical `captured_at >= since` comparison.
			since = parsed_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
		except ValueError:
			return jsonify({"error": "since must be an ISO 8601 timestamp with timezone."}), 400
	cursor = None
	encoded_cursor = request.args.get("cursor")
	if encoded_cursor:
		try:
			captured_at, cursor_id = urlsafe_b64decode(encoded_cursor.encode("ascii")).decode("utf-8").split("\n", 1)
			cursor = (captured_at, cursor_id)
		except Exception:
			return jsonify({"error": "Invalid pagination cursor."}), 400
	try:
		limit = min(100, max(1, int(request.args.get("limit", "50"))))
	except ValueError:
		return jsonify({"error": "limit must be an integer."}), 400
	records = list_contract_records(organization_key, since=since, agent=agent_name, cursor=cursor, limit=limit + 1)
	next_cursor = None
	if len(records) > limit:
		last_record = records[limit - 1]
		next_cursor = urlsafe_b64encode(f"{last_record['captured_at']}\n{last_record['record_id']}".encode("utf-8")).decode("ascii")
		records = records[:limit]
	return jsonify({"records": records, "next_cursor": next_cursor})


@app.post("/v1/agent/records")
@csrf.exempt
def agent_ingest_prep_record():
	organization_key, auth_error = agent_api_org(PREP_AGENT_API_TOKEN)
	if auth_error:
		return auth_error
	record = request.get_json(silent=True)
	if not isinstance(record, dict):
		return jsonify({"error": "A CUBE evidence record is required."}), 400
	try:
		validate_record(record)
	except (KeyError, TypeError, ValueError) as error:
		return jsonify({"error": str(error)}), 400
	if record["organization_id"] != organization_uuid(organization_key):
		return jsonify({"error": "The record belongs to a different organization."}), 403
	if record["agent"] != "prep" or record["status"] != "complete":
		return jsonify({"error": "Only completed agent=prep records can be ingested."}), 400
	for image in record["images"]:
		if not image["key"].startswith(f"{record['organization_id']}/"):
			return jsonify({"error": "Prep evidence images must use the organization's private storage prefix."}), 400
		try:
			image_bytes = MEDIA_STORAGE.read(image["key"])
		except Exception:
			image_bytes = None
		if (
			image_bytes is None
			or len(image_bytes) != image["bytes"]
			or hashlib.sha256(image_bytes).hexdigest() != image["sha256"]
		):
			return jsonify({"error": "A prep evidence image is missing or does not match its recorded digest."}), 409
	save_result = save_contract_record(record, organization_key)
	if save_result == "conflict":
		return jsonify({"error": "This record ID already exists with different content."}), 409
	return jsonify({"record_id": record["record_id"], "status": save_result}), 201 if save_result == "created" else 200


@app.get("/v1/agent/records")
def agent_pack_records_feed():
	organization_key, auth_error = agent_api_org(PACK_FEED_AGENT_API_TOKEN)
	if auth_error:
		return auth_error
	agent_name = request.args.get("agent", "pack")
	if agent_name != "pack":
		return jsonify({"error": "The agent feed exposes agent=pack records only."}), 400
	since = request.args.get("since")
	if since:
		try:
			parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
			if parsed_since.tzinfo is None:
				raise ValueError
			since = parsed_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
		except ValueError:
			return jsonify({"error": "since must be an ISO 8601 timestamp with timezone."}), 400
	cursor = None
	encoded_cursor = request.args.get("cursor")
	if encoded_cursor:
		try:
			captured_at, cursor_id = urlsafe_b64decode(encoded_cursor.encode("ascii")).decode("utf-8").split("\n", 1)
			cursor = (captured_at, cursor_id)
		except Exception:
			return jsonify({"error": "Invalid pagination cursor."}), 400
	try:
		limit = min(100, max(1, int(request.args.get("limit", "50"))))
	except ValueError:
		return jsonify({"error": "limit must be an integer."}), 400
	records = list_contract_records(
		organization_key, since=since, agent="pack", cursor=cursor, limit=limit + 1,
	)
	next_cursor = None
	if len(records) > limit:
		last_record = records[limit - 1]
		next_cursor = urlsafe_b64encode(
			f"{last_record['captured_at']}\n{last_record['record_id']}".encode("utf-8")
		).decode("ascii")
		records = records[:limit]
	return jsonify({"records": records, "next_cursor": next_cursor})


@app.post("/v1/records/<record_id>/overrides")
@login_required
def contract_record_override_api(record_id: str):
	try:
		UUID(record_id)
	except (ValueError, TypeError, AttributeError):
		return jsonify({"error": "record_id must be a UUID."}), 400
	organization_key = current_org()
	record = get_contract_record(record_id, organization_key)
	if not record:
		return jsonify({"error": "Record not found."}), 404
	payload = request.get_json(silent=True) or {}
	check_key = payload.get("check_key")
	to_verdict = payload.get("to_verdict")
	reason = str(payload.get("reason", "")).strip()
	if not check_key or to_verdict not in {"pass", "fail", "uncertain"} or not reason:
		return jsonify({"error": "check_key, valid to_verdict, and a non-empty reason are required."}), 400
	current_override = next((item for item in reversed(record["overrides"]) if item["check_key"] == check_key), None)
	check = next((item for item in record["checks"] if item["check_key"] == check_key), None)
	from_verdict = current_override["to_verdict"] if current_override else (check["verdict"] if check else None)
	if from_verdict is None:
		return jsonify({"error": "Unknown check_key."}), 400
	override = {
		"override_id": str(uuid4()),
		"check_key": check_key,
		"from_verdict": from_verdict,
		"to_verdict": to_verdict,
		"reason": reason,
		"by": session["username"],
		"at": utc_now(),
	}
	if not append_contract_override(record_id, organization_key, override):
		return jsonify({"error": "Override rejected because the record changed or is unavailable."}), 409
	return jsonify({"record_id": record_id, "status": "overridden"}), 201


@app.get("/reports/records.csv")
@login_required
def records_csv():
	records = list_records(current_org())
	output = StringIO()
	writer = csv.writer(output)
	writer.writerow([
		"record_id", "org_id", "unit_id", "order_id", "channel", "verdict",
		"action", "reason", "operator_id", "captured_at", "photo_ref", "video_ref",
	])
	for record in records:
		writer.writerow([
			record["record_id"], record["org_id"], record["unit_id"],
			record["order_id"], record["channel"], record["verdict"],
			record["action"], record["reason"], record["operator_id"],
			record["captured_at"], record["photo_ref"] or "", record["video_ref"] or "",
		])
	response = app.response_class(output.getvalue(), mimetype="text/csv")
	response.headers["Content-Disposition"] = f'attachment; filename="{current_org()}-pack-report.csv"'
	return response


@app.get("/reports/summary.json")
@login_required
def evaluation_summary():
	records = list_records(current_org())
	for record in records:
		record["evidence"] = json.loads(record["evidence_json"])
	return jsonify({"org_id": current_org(), "summary": summarize_records(records)})


@app.get("/reports/summary")
@login_required
def evaluation_summary_page():
	records = list_records(current_org())
	for record in records:
		record["evidence"] = json.loads(record["evidence_json"])
	return render_template(
		"evaluation.html", org_id=current_org(), summary=summarize_records(records)
	)


@app.get("/evaluation/fixtures")
@login_required
def vision_fixtures_page():
	manifest_path = APP_DIR / "fixtures" / "vision" / "heldout_50_template.csv"
	fixtures = []
	if manifest_path.is_file():
		with manifest_path.open(newline="", encoding="utf-8-sig") as manifest_file:
			for row in csv.DictReader(manifest_file):
				filename = (row.get("image_filename") or "").strip()
				if not filename or not (manifest_path.parent / filename).is_file():
					continue
				row["image_url"] = url_for("media_file", filename=f"fixtures/vision/{filename}")
				fixtures.append(row)
	products_by_sku = {product["sku"]: product for product in list_products(current_org())}
	return render_template("vision_fixtures.html", fixtures=fixtures, products_by_sku=products_by_sku)


@app.get("/evaluation/fixtures/image/<filename>")
@login_required
def vision_fixture_image(filename: str):
	image_path = APP_DIR / "fixtures" / "vision" / filename
	if Path(filename).name != filename or image_path.suffix.lower().lstrip(".") not in ALLOWED_IMAGE_EXTENSIONS or not image_path.is_file():
		abort(404)
	fixture = {}
	manifest_path = APP_DIR / "fixtures" / "vision" / "heldout_50_template.csv"
	if manifest_path.is_file():
		with manifest_path.open(newline="", encoding="utf-8-sig") as manifest_file:
			fixture = next((row for row in csv.DictReader(manifest_file) if row.get("image_filename") == filename), {})
	product = None
	if fixture.get("sku") and fixture["sku"] != "MULTIPLE":
		product = get_product(current_org(), fixture["sku"])
		if product:
			product["attributes"] = json.loads(product.pop("attributes_json"))
	return render_template(
		"vision_fixture_image.html",
		filename=filename,
		image_url=url_for("media_file", filename=f"fixtures/vision/{filename}"),
		fixture=fixture,
		product=product,
	)


@app.route("/support", methods=["GET", "POST"])
@login_required
def support_page():
	ticket_id = None
	error = None
	if request.method == "POST":
		name = request.form.get("name", "").strip()[:120]
		email = request.form.get("email", "").strip()[:254]
		subject = request.form.get("subject", "").strip()[:200]
		message = request.form.get("message", "").strip()[:10000]
		if not all((name, email, subject, message)) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
			error = "Enter your name, a valid email, a subject, and a message."
		else:
			ticket_id = f"SUP-{uuid4().hex[:10].upper()}"
			save_support_request({
				"ticket_id": ticket_id, "org_id": current_org(),
				"requester_name": name, "requester_email": email,
				"subject": subject, "message": message,
				"status": "OPEN", "created_at": datetime.now(timezone.utc).isoformat(),
			})
	return render_template("support.html", ticket_id=ticket_id, error=error)


@app.get("/api/support/requests")
@login_required
def support_requests_api():
	return jsonify({"org_id": current_org(), "requests": list_support_requests(current_org())})

@app.get("/health")
def health():
	try:
		with connect("__system__") as connection:
			connection.execute("SELECT 1").fetchone()
	except Exception:
		return jsonify({"status": "degraded", "service": "packguard"}), 503
	return jsonify({"status": "ok", "service": "packguard", "environment": DEPLOYMENT_ENV})


@app.get("/health/production")
def production_health():
	report = readiness_report()
	try:
		with connect("__system__") as connection:
			connection.execute("SELECT 1").fetchone()
			migration = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
			migrations_current = int(migration["version"] or 0) >= 5
		database_reachable = True
	except Exception:
		database_reachable = False
		migrations_current = False
	try:
		storage_reachable = MEDIA_STORAGE.backend == "s3" and MEDIA_STORAGE.health_check()
	except Exception:
		storage_reachable = False
	report["checks"]["database_reachable"] = database_reachable
	report["checks"]["migrations_current"] = migrations_current
	report["checks"]["private_object_storage_reachable"] = storage_reachable
	if report["environment"] == "production":
		if not database_reachable:
			report["issues"].append("The configured database cannot be reached.")
		if not migrations_current:
			report["issues"].append("The database schema is not at the current migration version.")
		if not storage_reachable:
			report["issues"].append("The configured private object storage cannot be reached.")
	report["ready"] = not report["issues"]
	return jsonify(report), 200 if report["ready"] else 503


init_db()
if not IS_PRODUCTION:
	seed_demo_users()
	seed_sample_data()
	seed_sample_catalog()


if __name__ == "__main__":
	if IS_PRODUCTION:
		raise RuntimeError("Run production with Waitress via the wsgi:app entry point.")
	app.run(host="127.0.0.1", port=5000, debug=False)
