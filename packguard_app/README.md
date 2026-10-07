# PackGuard

A lightweight outbound pack verification workflow for merchant-fulfilled and 3PL orders. It records what should be in the box, what was observed, the decision, and portable evidence for downstream Returns and Recovery managers.

## Run locally

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r ..\requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. The app seeds the synthetic sample CSV on first start. The database is created at `packguard.db` and is ignored by git through the repository's `*.db` rule if added locally.

## Production-oriented deployment

The Compose stack runs Waitress, PostgreSQL, and a private MinIO bucket. It binds the web app to loopback; put it behind an HTTPS reverse proxy before any network access. This is a deployment baseline, not a claim that the remaining production-readiness gates have passed.

From this directory, copy `.env.example` to `.env`, replace every placeholder with long random values, and set the real HTTPS public URL. Keep `.env` out of source control. Then run:

```powershell
docker compose up --build -d
docker compose run --rm --entrypoint flask app --app wsgi create-operator
```

The operator command prompts for a username, organization ID, and password without echoing the password; when using OIDC, provision the user's verified email as the username. Register the configured `PACKGUARD_OIDC_REDIRECT_URI` at your identity provider and enforce MFA in that provider. Demo login is disabled in production, and production startup does not seed synthetic accounts or records. Production requests require CSRF tokens, and failed logins are throttled across app workers. Set `PACKGUARD_TRUSTED_PROXY_COUNT` to the exact number of trusted reverse-proxy hops; never expose the app port directly when trusting forwarded headers. `PACKGUARD_AUTO_SEAL_ENABLED` is fixed to `false` in this stack. Configure the reverse proxy, TLS, firewall, and durable backups separately; use a managed private S3-compatible bucket instead of the bundled MinIO service for hosted production.

The app applies versioned database migrations at startup. If moving existing SQLite data, first take a backup and provision an empty PostgreSQL database and private bucket, then set `PACKGUARD_DATABASE_URL`, `PACKGUARD_OBJECT_STORAGE_URL`, `PACKGUARD_S3_ENDPOINT_URL`, and `PACKGUARD_UPLOAD_DIR` and run:

```powershell
python migrate_sqlite_to_postgres.py .\packguard.db
```

The transfer refuses a non-empty target, copies referenced local evidence before rows, and backfills legacy session IDs. Verify the target before switching traffic. The `PACKGUARD_TEST_POSTGRES_URL` environment variable enables the real-PostgreSQL migration integration test; without it that test is skipped.

For backup, set the database and object-storage environment variables and run `./ops/backup.ps1`; it writes a custom PostgreSQL dump and copies bucket objects to a timestamped directory. To rehearse recovery, set `PACKGUARD_RESTORE_TEST_DATABASE_URL` to an empty database whose name ends in `_restore_test`, set `PACKGUARD_RESTORE_TEST_OBJECT_STORAGE_URL` to a dedicated test bucket/prefix, then run `./ops/restore-check.ps1 -BackupDirectory <backup-folder>`. Check record counts and authenticated media access in the isolated restored app before relying on the backup. The scripts require PostgreSQL client tools and AWS CLI. Never use `docker compose down -v` on data you need to keep.

Vision calibration uses `fixtures/vision/heldout_50_template.csv`. Replace its blank rows with 50 real held-out pack images, independent `annotator_a_decision` and `annotator_b_decision` labels, and an `adjudicated_decision`. The calibration gate remains `NOT_READY` until all 50 rows are complete; do not use synthetic or generated labels.

### Free general assistant answers

PackGuard uses Ollama and the local `qwen3:1.7b` model. It does not connect to OpenAI, Gemini, or another hosted answer provider. General questions and recent chat context are sent only to the Ollama service running on this computer; PackGuard-specific order answers are read from the current organization's local database. No cloud API key is required. Install Ollama, then download the model once:

```powershell
ollama pull qwen3:1.7b
```

Pack inspection uses local Ollama vision models. For a single expected SKU with no typed observation, PackGuard defaults to `moondream:1.8b` for a fast image-based presence hint. Structured SKU/quantity assessment uses `gemma3:4b` unless `PACKGUARD_CONTRACT_VISION_MODEL` is set. Download the model(s) needed for your workflow:

```powershell
ollama pull gemma3:4b
```

Start Ollama before starting PackGuard. `OLLAMA_MODEL` selects the chat assistant model; set `PACKGUARD_VISION_MODEL` to change the vision model, or `OLLAMA_BASE_URL` to select another local Ollama server. Vision assessment runs from the captured image without an operator interpreting it, but remains uncalibrated; uncertain cases route to review, and an operator must still confirm `SEAL`.

## Use

1. Select a workspace to exercise tenant isolation.
2. Open **Catalog** and add or review organization-specific SKUs, Product IDs, variants, barcode IDs, supplier, source/arrival address, recipient, destination address, and reference photos. These logistics fields are catalog defaults and must be entered from trusted shipment/order data; product images do not reveal addresses. Synthetic sample SKUs are seeded from the reference CSV and are not ground-truth training data.
3. Open **New check** and search by product name, Product ID, SKU, or barcode. Selecting a product fills its SKU and shows the saved logistics route. Product-only checks use expected quantity 1 as a starting value; change it when the real order quantity is known. External order IDs remain optional; a local check ID is generated if none is supplied.
4. Use **Open camera** on a supported secure browser, or use the photo file input on a phone or computer. With observed contents blank, a photo is required for the vision agent to inspect. The agent records detected products and a candidate action; presence-only inference never invents quantities.
5. Review the deterministic decision and evidence record.
6. Consume `/api/records` for downstream integration.

### CUBE Evidence Contract 1.1

The **CUBE contract capture** page and `/v1` API implement the fixed 1.1 record shape for this Pack Manager pod. The operator capture API writes `agent=pack` records; the machine integration accepts `agent=prep` records and exposes the resulting `agent=pack` records to downstream agents. The Pack check keys are published here and must remain stable:

| `check_key` | Meaning |
| --- | --- |
| `sku_quantity` | Deterministic comparison of expected and observed SKU quantities. |
| `visual_evidence` | One batched vision assessment over all captured shots. |

Capture requires guided `overview` and `labels` shots, with an optional `detail` shot. Images upload directly to private S3-compatible storage using short-lived presigned URLs. The capture page and override action require an authenticated operator; a UUID record link and its image links are read-only and can be shared. `/v1/records` is organization-scoped and paginated, with `since` filtered against UTC `captured_at` values.

When a single expected SKU is supplied and observed contents are blank, the default fast path uses Moondream for product-presence recognition. It cannot count units; the result stays `MANUAL_REVIEW` unless a quantity-capable structured model is explicitly configured and returns a clear candidate. Clear structured mismatches can route to `FIX`. A `SEAL` candidate always remains under operator confirmation while vision is uncalibrated; automatic sealing stays disabled. Supplier and route fields come from catalog metadata and show as not recorded when absent; they are not inferred from product images.

The record starts as `pending` and remains available if vision times out or fails. The model is called once with every shot; latency and returned token usage are stored under `checks[].detail` for the visual check. A failed call leaves the record pending for recovery and does not block the operator. PostgreSQL tenant tables, including contract captures, records, and overrides, use enabled and forced row-level security; images are private and reached through record-scoped routes.

After the vision response, a bounded agent workflow hands evidence to deterministic verification and then to a coordinator that selects and records a next action such as recapture, correction, or operator review. The capture UI displays the agent handoff and selected action. This workflow cannot seal an order or control external warehouse systems; operators keep final authority. See [ARCHITECTURE.md](ARCHITECTURE.md#bounded-agentic-pack-workflow) for its decision boundaries.

### Prep and returns agent integration

Configure `PACKGUARD_AGENT_API_ORG_ID`, `PACKGUARD_PREP_AGENT_TOKEN`, and `PACKGUARD_PACK_FEED_TOKEN` in the PackGuard process environment. The organization ID must match the target workspace. Use separate random bearer tokens of at least 32 characters; give the prep agent only the prep token and the downstream returns agent only the pack-feed token. Do not put tokens in source control or URLs.

- `POST /v1/agent/records` accepts a complete CUBE 1.1 `agent=prep` record as JSON with `Authorization: Bearer <prep-token>`. The record must belong to the configured organization, have passing checks, and reference evidence images available in PackGuard's configured private storage with matching byte counts and SHA-256 digests. Identical retries are safe; reusing a record ID with different content is rejected.
- Include route fields in a prep check's `detail.logistics`: `supplier_name`, `origin_address`, `ordered_for`, and `delivery_address`. New Check opens with `/capture?prep_record_id=<prep-record-uuid>`, loads the order/SKU/quantity and logistics from that record, rejects modified expected quantities or shipment IDs, and saves the prep record ID on the Pack evidence.
- A New Check started from a prep record also writes a CUBE `agent=pack` record and publishes it to the returns webhook/feed. It does not require typed observed contents, but an open-box photo is required for image-based inspection.
- `GET /v1/agent/records?agent=pack&limit=50` returns paginated CUBE `agent=pack` records with `Authorization: Bearer <pack-feed-token>`. Use the returned `next_cursor` for subsequent pages or `since=<UTC timestamp>` for incremental polling. The output's `sku_quantity.detail.source_prep_record_id` links it to its prep input.
- To push results immediately, also configure `PACKGUARD_RETURNS_AGENT_URL` and `PACKGUARD_RETURNS_AGENT_TOKEN` together. After assessment is persisted, PackGuard POSTs the complete CUBE pack record as JSON with `Authorization: Bearer <returns-token>` and `Idempotency-Key: <record_id>`. The receiving agent should deduplicate by record ID; if delivery fails, PackGuard logs a warning and the authenticated pack feed remains available. Production webhook URLs must use HTTPS.
- Pack capture still needs stage-3 overview and label images of the actual open outbound box. Prep images are retained as upstream evidence and are not treated as proof of the outbound box contents.

## Honest scope

The regular New check flow uses the deterministic verifier on operator-entered SKU/quantity observations. The CUBE contract flow can run from images without typed observed contents, but the fast presence model cannot verify quantity. All image results are uncalibrated; `SEAL` is a candidate only and requires operator confirmation. Photos and videos remain attached to the record. The supplied CSV is demo data, not production ground truth.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the boundaries and production hardening plan.

See [INTERVIEW-PREP.md](INTERVIEW-PREP.md) for an end-to-end competition interview guide with project-specific answers and limitations.

See [EVALUATION_REPORT.md](EVALUATION_REPORT.md), [HOLDOUT-COLLECTION-GUIDE.md](HOLDOUT-COLLECTION-GUIDE.md), [DEMO-SCRIPT.md](DEMO-SCRIPT.md), and [SUBMISSION-CHECKLIST.md](SUBMISSION-CHECKLIST.md) for the current measured scope, evaluation protocol, demo flow, and remaining external submission steps.
