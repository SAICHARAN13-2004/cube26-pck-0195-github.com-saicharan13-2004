from werkzeug.security import generate_password_hash

import app as packguard_app
from database import get_user, save_user


class FakeOIDCProvider:
	def __init__(self, claims):
		self.claims = claims
		self.redirect_uri = None
		self.code_challenge_method = None

	def authorize_redirect(self, redirect_uri, code_challenge_method):
		self.redirect_uri = redirect_uri
		self.code_challenge_method = code_challenge_method
		return packguard_app.redirect("https://identity.example.test/authorize")

	def authorize_access_token(self):
		return {"userinfo": self.claims}


def _save_operator(username):
	save_user({
		"username": username,
		"password_hash": generate_password_hash("a-long-test-password-2026"),
		"org_id": "org_oidc_test",
		"role": "operator",
		"created_at": "2026-01-01T00:00:00+00:00",
	})


def test_oidc_login_uses_pkce_and_callback_links_preprovisioned_verified_user(monkeypatch):
	issuer = "https://identity.example.test"
	claims = {"iss": issuer, "sub": "stable-subject-1", "email": "Operator@Example.Test", "email_verified": True}
	provider = FakeOIDCProvider(claims)
	monkeypatch.setattr(packguard_app, "OIDC_ENABLED", True)
	monkeypatch.setattr(packguard_app, "OIDC_ISSUER_URL", issuer)
	monkeypatch.setattr(packguard_app, "get_oidc_client", lambda: provider)
	_save_operator("operator@example.test")
	client = packguard_app.app.test_client()

	start = client.get("/auth/login")
	assert start.status_code == 302
	assert provider.code_challenge_method == "S256"
	assert provider.redirect_uri.endswith("/auth/callback")

	callback = client.get("/auth/callback")
	user = get_user("operator@example.test")
	with client.session_transaction() as browser_session:
		assert browser_session["username"] == "operator@example.test"
		assert browser_session["org_id"] == "org_oidc_test"

	assert callback.status_code == 302
	assert user["oidc_issuer"] == issuer
	assert user["oidc_subject"] == "stable-subject-1"


def test_oidc_rejects_unverified_email_and_does_not_link_account(monkeypatch):
	issuer = "https://identity.example.test"
	provider = FakeOIDCProvider({
		"iss": issuer,
		"sub": "unverified-subject",
		"email": "unverified@example.test",
		"email_verified": False,
	})
	monkeypatch.setattr(packguard_app, "OIDC_ENABLED", True)
	monkeypatch.setattr(packguard_app, "OIDC_ISSUER_URL", issuer)
	monkeypatch.setattr(packguard_app, "get_oidc_client", lambda: provider)
	_save_operator("unverified@example.test")

	response = packguard_app.app.test_client().get("/auth/callback")

	assert response.status_code == 403
	assert get_user("unverified@example.test")["oidc_subject"] is None


def test_oidc_identity_cannot_be_rebound_to_a_different_subject(monkeypatch):
	issuer = "https://identity.example.test"
	provider = FakeOIDCProvider({
		"iss": issuer,
		"sub": "stable-subject-first",
		"email": "locked@example.test",
		"email_verified": True,
	})
	monkeypatch.setattr(packguard_app, "OIDC_ENABLED", True)
	monkeypatch.setattr(packguard_app, "OIDC_ISSUER_URL", issuer)
	monkeypatch.setattr(packguard_app, "get_oidc_client", lambda: provider)
	_save_operator("locked@example.test")
	client = packguard_app.app.test_client()
	assert client.get("/auth/callback").status_code == 302

	provider.claims["sub"] = "stable-subject-second"
	response = client.get("/auth/callback")

	assert response.status_code == 403
	assert get_user("locked@example.test")["oidc_subject"] == "stable-subject-first"


def test_production_oidc_hides_local_password_form_and_blocks_password_post(monkeypatch):
	monkeypatch.setattr(packguard_app, "IS_PRODUCTION", True)
	monkeypatch.setattr(packguard_app, "OIDC_ENABLED", True)
	client = packguard_app.app.test_client()

	page = client.get("/login")
	post = client.post("/login", data={"username": "alpha.operator", "password": "alpha-demo"})

	assert b"Continue with organization sign-in" in page.data
	assert b'class="login-form"' not in page.data
	assert post.status_code == 403


def test_oidc_operator_provisioning_does_not_request_unused_password(monkeypatch):
	from unittest.mock import patch

	monkeypatch.setattr(packguard_app, "OIDC_ENABLED", True)
	answers = iter(("CLI.Operator@Example.Test", "org_cli_oidc"))
	monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
	with patch("app.getpass.getpass", side_effect=AssertionError("OIDC provisioning must not request a local password")):
		result = packguard_app.app.test_cli_runner().invoke(args=["create-operator"])

	user = get_user("cli.operator@example.test")
	assert result.exit_code == 0
	assert user["org_id"] == "org_cli_oidc"
