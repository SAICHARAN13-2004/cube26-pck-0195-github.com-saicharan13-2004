"""Deployment readiness checks that never expose secret values."""

import os
from importlib.util import find_spec
from typing import Any
from urllib.parse import urlparse

from database import DATABASE_BACKEND


def readiness_report() -> dict[str, Any]:
    issues: list[str] = []
    environment = os.environ.get("PACKGUARD_ENV", "development").strip().lower()
    secret = os.environ.get("PACKGUARD_SECRET_KEY", "")
    public_url = os.environ.get("PACKGUARD_PUBLIC_URL", "")
    database_url = os.environ.get("PACKGUARD_DATABASE_URL", "")
    migration_database_url = os.environ.get("PACKGUARD_DATABASE_MIGRATION_URL", "")
    runtime_database_user = urlparse(database_url).username if database_url else None
    migration_database_user = urlparse(migration_database_url).username if migration_database_url else None
    object_storage = os.environ.get("PACKGUARD_OBJECT_STORAGE_URL", "")
    oidc_issuer = os.environ.get("PACKGUARD_OIDC_ISSUER_URL", "").strip()
    oidc_client_id = os.environ.get("PACKGUARD_OIDC_CLIENT_ID", "").strip()
    oidc_client_secret = os.environ.get("PACKGUARD_OIDC_CLIENT_SECRET", "")
    oidc_redirect_uri = os.environ.get("PACKGUARD_OIDC_REDIRECT_URI", "").strip()
    demo_login = os.environ.get("PACKGUARD_ALLOW_DEMO_LOGIN", "true").strip().lower() in {"1", "true", "yes"}

    checks = {
        "production_environment": environment == "production",
        "strong_secret": len(secret) >= 32,
        "demo_login_disabled": not demo_login,
        "https_public_url": public_url.startswith("https://"),
        "postgres_adapter_implemented": DATABASE_BACKEND == "postgresql" and find_spec("psycopg") is not None,
        "database_roles_separated": bool(runtime_database_user and migration_database_user and runtime_database_user != migration_database_user),
        "private_object_storage_adapter_implemented": object_storage.startswith("s3://") and find_spec("boto3") is not None,
        "production_operator_accounts_supported": True,
        "csrf_protection_implemented": find_spec("flask_wtf") is not None,
        "shared_login_throttling_implemented": True,
        "external_identity_provider_implemented": bool(
            oidc_issuer.startswith("https://") and oidc_client_id and oidc_client_secret
            and oidc_redirect_uri.startswith("https://") and find_spec("authlib") is not None
        ),
        "marketplace_order_adapter_implemented": False,
        "barcode_ocr_implemented": False,
        "vision_calibration_ready": False,
    }
    messages = {
        "production_environment": "PACKGUARD_ENV must be production.",
        "strong_secret": "PACKGUARD_SECRET_KEY must be at least 32 characters.",
        "demo_login_disabled": "PACKGUARD_ALLOW_DEMO_LOGIN must be false.",
        "https_public_url": "PACKGUARD_PUBLIC_URL must use HTTPS.",
        "postgres_adapter_implemented": "Configure PACKGUARD_DATABASE_URL for PostgreSQL and install the psycopg adapter.",
        "database_roles_separated": "Use separate non-superuser runtime and schema-migration PostgreSQL roles.",
        "private_object_storage_adapter_implemented": "Configure an s3:// bucket and install the boto3 adapter for private object storage.",
        "production_operator_accounts_supported": "",
        "csrf_protection_implemented": "Install and enable Flask-WTF CSRF protection for production requests.",
        "shared_login_throttling_implemented": "Database-backed login throttling is not available.",
        "external_identity_provider_implemented": "Configure an HTTPS OIDC issuer, client ID/secret, HTTPS redirect URI, and Authlib; enforce MFA at the identity provider.",
        "marketplace_order_adapter_implemented": "Marketplace/order API integration is not implemented.",
        "barcode_ocr_implemented": "Barcode and OCR integration is not implemented.",
        "vision_calibration_ready": "Vision evaluation/calibration has not been approved for production automation.",
    }
    for name, passed in checks.items():
        if not passed and messages.get(name):
            issues.append(messages[name])
    return {
        "ready": not issues,
        "environment": environment,
        "checks": checks,
        "issues": issues,
        "automatic_seal": "blocked_until_calibration",
        "configured_postgres_url_present": database_url.startswith(("postgres://", "postgresql://")),
        "configured_object_storage_url_present": bool(object_storage),
    }
