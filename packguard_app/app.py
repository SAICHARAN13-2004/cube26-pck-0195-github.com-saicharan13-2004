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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, send_file, send_from_directory, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from database import APP_DIR, DB_PATH, append_contract_override, clear_login_attempts, complete_contract_capture, connect, create_contract_capture, get_contract_capture, get_contract_record, get_oidc_user, get_product, get_record, get_user, init_db, link_oidc_identity, list_audit_events, list_contract_records, list_products, list_records, list_session_records, list_support_requests, login_blocked_until, record_login_failure, reset_organization_context, save_audit_event, save_product, save_record, save_support_request, save_user, set_organization_context, update_contract_record, update_workflow_state, user_exists
from detector import observe, observe_image
from evedince import build_evidence
from evaluation import summarize_records
from policy import auto_seal_policy, require_operator_confirmation
from agent import assess_pack, enforce_component_verdict
from orchestrator import FAILED_VISION_STATUSES, run_pack_agent_workflow
from production import readiness_report
from storage import create_media_storage
from verifier import parse_lines, verify_pack
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
CSRFProtect(app)
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
		reset_organization_context(token)


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


def assistant_answer(question: str, org_id: str, history: list[dict[str, str]] | None = None) -> str:
	"""Answer operational questions from the current organization's records."""
	question_lower = question.lower()
	records = list_records(org_id)
	alerts = alert_records(org_id)
	mentioned_record = next((
		record for record in records
		if record["order_id"].lower() in question_lower or record["unit_id"].lower() in question_lower or record["record_id"].lower() in question_lower
	), None)
	if mentioned_record:
		return answer_from_order_record(mentioned_record, question)
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
	return general_assistant_answer(question, history)


def general_assistant_answer(question: str, history: list[dict[str, str]] | None = None) -> str:
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
		"Vision output is advisory and cannot authorize SEAL until calibration is approved."
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
	alerts = alert_records(current_org())
	return render_template("alerts.html", alerts=alerts, alert_count=len(alerts))


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
	return jsonify({"answer": assistant_answer(question, current_org(), history)})


@app.route("/capture", methods=["GET", "POST"])
@login_required
def capture():
	org_id = current_org()
	if request.method == "GET":
		defaults = {"attempt_type": "initial", "recapture_reason": ""}
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
		return render_template("capture.html", products=catalog_products(org_id), defaults=defaults)
	form = request.form
	order_lines = form.get("order_lines", "").strip()
	observed_in_box = form.get("observed_in_box", "").strip()
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
		if form.get("supplier_name", "").strip() != (parent_record.get("supplier_name") or ""):
			abort(400, description="Recapture supplier must match the original receiving session")
	result = verify_pack(order_lines, observed_in_box)
	attach_catalog_metadata(result, org_id)
	record_id = f"PCK-{uuid4().hex[:8].upper()}"
	photo_ref = save_photo(request.files.get("photo"), org_id, record_id)
	video_ref = save_media(request.files.get("packing_video"), org_id, record_id, "video")
	photo_ref = photo_ref or form.get("photo_ref", "").strip() or None
	image_observation = observe(observed_in_box)
	vision_candidates = vision_catalog_products(org_id, set(parse_lines(order_lines)))
	if photo_ref and photo_ref.startswith(f"{org_id}/"):
		photo_input = MEDIA_STORAGE.read(photo_ref) or photo_ref
	elif photo_ref and photo_ref.startswith("fixtures/vision/"):
		photo_input = APP_DIR / photo_ref
	else:
		photo_input = None
	vision_result = inspect_image(
		photo_input,
		catalog_products=vision_candidates,
		photo_name=Path(photo_ref).name if photo_ref else None,
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
	if not observed_in_box:
		result = enforce_component_verdict(result, vision_result, bool(photo_ref))
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
		record_id=record_id, org_id=org_id, unit_id=form.get("unit_id", "").strip() or "UNIT-NEW",
		order_lines=order_lines, observed_in_box=observed_in_box,
		supplier_name=form.get("supplier_name", "").strip() or None,
		result=result, photo_ref=photo_ref, image_observation=image_observation,
		vision_result=vision_result,
		operator_verdict=operator_verdict, agreement=agreement,
		video_ref=video_ref,
	)
	save_record({
		"record_id": record_id, "org_id": org_id,
		"unit_id": form.get("unit_id", "").strip() or "UNIT-NEW",
		"order_id": form.get("order_id", "").strip() or "ORD-NEW",
		"supplier_name": form.get("supplier_name", "").strip() or None,
		"channel": form.get("channel", "amazon_mfn"),
		"origin_address": form.get("origin_address", "").strip() or None,
		"delivery_address": form.get("delivery_address", "").strip() or None,
		"order_lines": order_lines, "observed_in_box": observed_in_box,
		"operator_verdict": operator_verdict,
		"video_ref": video_ref,
		"operator_id": session.get("username", "operator") if IS_PRODUCTION else form.get("operator_id", "operator"),
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
		"actor_id": form.get("operator_id", "operator"),
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
			"actor_id": form.get("operator_id", "operator"),
			"created_at": datetime.now(timezone.utc).isoformat(),
			"details": {
				"operator_verdict": operator_verdict,
				"deterministic_verdict": result["verdict"],
				"deterministic_decision": result.get("decision"),
			},
		})
	return redirect(url_for("record_detail", record_id=record_id, org_id=org_id))


@app.get("/capture/contract")
@login_required
def contract_capture_page():
	return render_template("contract_capture.html", direct_upload_enabled=MEDIA_STORAGE.backend == "s3")


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
		slots = json.loads(capture["image_slots_json"])
		images = []
		for slot in slots:
			image_bytes = MEDIA_STORAGE.read(slot["key"])
			if image_bytes is None:
				return
			images.append((image_bytes, Path(slot["key"]).name))
		first_image, *additional_images = images
		expected_skus = set(parse_lines(json.loads(capture["input_json"]).get("expected_lines", "")))
		vision_products = vision_catalog_products(organization_key, expected_skus)
		vision_result = inspect_image(
			first_image[0],
			catalog_products=vision_products,
			photo_name=first_image[1],
			additional_images=additional_images,
			model_override=os.environ.get("PACKGUARD_CONTRACT_VISION_MODEL", "gemma3:4b"),
			force_structured=True,
			capture_context={
				"expected_lines": json.loads(capture["input_json"]).get("expected_lines", ""),
				"check_keys": ["sku_quantity", "visual_evidence"],
			},
		)
		input_data = json.loads(capture["input_json"])
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
			"token_usage": vision_result.get("token_usage"),
			"confidence": vision_result.get("confidence"),
			"vision_observation_candidate": workflow["vision_observation"] or None,
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
		update_contract_record(record, organization_key)
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
		"detail": {"sku_checks": sku_details, "reason": verification["reason"]},
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
	return render_template("vision_fixtures.html", fixtures=fixtures)


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
