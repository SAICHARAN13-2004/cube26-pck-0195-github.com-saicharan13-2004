# PackGuard production readiness

## Implemented

- SQLite for local development and a psycopg PostgreSQL adapter selected by `PACKGUARD_DATABASE_URL`.
- Versioned, idempotent schema migrations with a PostgreSQL advisory lock and legacy SQLite schema upgrades.
- A guarded SQLite-to-PostgreSQL transfer utility that copies referenced evidence into S3-compatible storage before copying rows.
- Local development media storage and private S3-compatible object storage; evidence is served through authenticated, organization-scoped routes.
- The Compose MinIO setup provisions a private bucket and a dedicated bucket-scoped app policy; the PackGuard container does not receive MinIO root credentials.
- Database-backed operator accounts, hidden-password `flask create-operator` provisioning, and explicit rejection of demo usernames in production.
- Provider-neutral OIDC discovery/login with PKCE, verified-email matching against pre-provisioned operators, and stable issuer/subject binding; production password login is disabled when OIDC is configured.
- Production CSRF validation on browser forms and JSON assistant requests; persistent account/IP login throttling with HMAC-hashed keys shared across workers.
- Trusted proxy handling is opt-in and uses a configured hop count; the Compose sample binds the app to loopback and assumes one trusted reverse proxy.
- Production startup does not seed demo accounts or synthetic records. Secret validation, secure cookies, security headers, and Waitress-based Compose deployment are included.
- A hard operator-confirmation gate: a `SEAL` candidate without an explicit operator `Seal` verdict becomes `MANUAL_REVIEW`. Automatic vision sealing remains disabled in the supplied deployment.
- Health checks for database and object-storage reachability. The readiness report never returns secret values.

## Still required before external production

Check `/health/production`. It returns `503` until every configured gate passes; adding DSNs alone is insufficient.

1. Configure the OIDC issuer, client ID/secret, and HTTPS callback in `.env`; register `/auth/callback` at the provider and enforce MFA there. The app validates the provider identity but cannot assert that your provider's MFA policy is enabled.
2. Deploy behind a correctly configured HTTPS reverse proxy and restrict network access. The Compose app port binds to loopback; MinIO and PostgreSQL are not published to the host.
3. Replace bundled MinIO with a managed private object-storage service for hosted production. Enforce bucket-level public-access blocking, least-privilege credentials, retention, and tested restore procedures.
4. Use managed PostgreSQL, run the optional migration integration test against the target server version, and rehearse SQLite transfer and database restore before switching traffic.
5. Configure scheduled off-host backups, monitoring, alerting, structured logs, incident response, and capacity/load tests.
6. Implement the marketplace/order adapter and verify authorization, webhook signatures, retries, and idempotency.
7. Add barcode/OCR capture and test it against real packaging labels.
8. Complete at least 50 real held-out units with two independent human labels and adjudication. Measure precision/recall, confusion matrix, false seals, and recapture/manual-review rates.

Keep operator confirmation required and automatic sealing disabled until the independent vision evaluation is complete and approved. The supplied synthetic records and fixture images are not production ground truth.
