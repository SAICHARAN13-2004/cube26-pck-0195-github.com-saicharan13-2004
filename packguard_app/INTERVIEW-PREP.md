# PackGuard Competition Interview Prep

Use these answers as a guide, not a script to memorize. Replace any bracketed deployment detail with what you actually ran during the demo.

## 60-second introduction

**Q: What did you build?**

PackGuard is an evidence-first web application for checking outbound orders before a package is sealed. An operator enters the expected contents, records what they observed, and captures package images. A deterministic verifier checks SKU quantities. For CUBE contract captures, one batched vision assessment feeds a bounded workflow with a verifier and a coordinator. The workflow can recommend correction, recapture, or operator review, and saves its decision trail in the Evidence Contract 1.1 record. Vision is advisory; an operator retains the final authority to seal.

**Q: What problem does it solve?**

Packing errors can lead to returns, customer complaints, and chargebacks. PackGuard checks a package while it is still open and preserves evidence that downstream operations teams can review.

**Q: Who is the user?**

The primary user is a warehouse pack operator. A supervisor can review uncertain cases, and downstream Returns or Recovery workflows can consume the evidence API.

## Product scope and workflow

**Q: Walk me through one capture end to end.**

The operator signs in, selects the Pack capture flow, enters the order and expected SKU quantity, and records observed contents. In the CUBE flow, they take an overview and label shot, with an optional detail shot. The browser uploads images directly to private object storage. The server creates the evidence record and then runs one batched vision assessment in the background. The verifier compares expected and observed contents; the coordinator chooses a next step. The record stores the checks, decision, image metadata, hash, and agent handoff details. The operator can open the read-only record and review or override a check with a required reason.

**Q: What are the possible outcomes?**

The pack workflow uses `SEAL`, `FIX`, `RECAPTURE`, and `MANUAL_REVIEW` as product decisions. The underlying evidence verdicts are `pass`, `fail`, and `uncertain`. Exact SKU and quantity agreement can be a deterministic pass. A mismatch routes to correction. Poor image quality can request recapture. Missing, ambiguous, or conflicting evidence routes to a person. The model cannot authorize `SEAL`.

**Q: What does the application treat as authoritative?**

The deterministic comparison of expected and operator-observed SKU quantities is authoritative for the structured check. Vision results are suggestions and remain uncalibrated. Human review is required when the evidence is uncertain, contradictory, or visually suggested.

**Q: What is out of scope?**

This repository implements the Pack Manager pod. It does not implement the Receiving, Prep, and Returns manager applications, connect to a marketplace order provider, control conveyor or sealing equipment, or claim a calibrated product-identity model.

## AI and agent workflow

**Q: Is this an AI agent or an agentic system?**

The most accurate description is a human-supervised, bounded agentic workflow in a web application. A vision model inspects the images. A verifier checks structured evidence. A coordinator selects and records a constrained next action. It has agent-like handoffs and action selection, but it is not a group of independent LLMs freely planning, and it does not control the warehouse by itself.

**Q: What are the agents or roles?**

In the CUBE capture workflow, the roles are `vision_agent`, `evidence_verifier`, and `workflow_coordinator`. The vision role produces image suggestions in one model call. The verifier compares the operator observation and any structured vision candidate against expected order lines. The coordinator uses fixed safety rules to select the next action. The action and handoffs are recorded in `checks[].detail.agent_run`.

**Q: How do the agents communicate?**

They pass structured Python dictionaries. The vision result includes image quality, detected items, uncertainty, token usage, and latency. The verifier returns a tri-state verdict and comparisons. The coordinator records handoffs, its decision basis, and the selected action. This keeps the workflow inspectable and avoids relying on free-form text between components.

**Q: What makes it agentic if most steps use rules?**

The system observes a capture, routes structured findings through specialist roles, selects a next action based on the findings, and persists the result without a person directing each internal step. The model supplies visual evidence; deterministic code controls the safety-critical comparisons and action boundaries. It is bounded agentic automation, not unrestricted autonomy.

**Q: Does it use multiple LLM calls?**

No. The contract requires one model call per capture. All captured shots are sent together in one batched assessment. The verifier and coordinator are deterministic software roles; they do not make extra model calls.

**Q: What actions can it take?**

It can select and record `open_fix_workflow`, `request_recapture`, `route_manual_review`, or `request_operator_confirmation`. These route the next human workflow step in the evidence record. They do not edit an external order or activate warehouse equipment.

**Q: What happens when agents disagree?**

If structured operator observations disagree with the visual candidate, the coordinator routes the case to manual review. Uncalibrated vision cannot overrule the deterministic or human evidence to produce a seal decision.

**Q: What does it do when the model fails?**

The capture and evidence record are saved before vision completes. The model runs asynchronously; a failed or timed-out assessment leaves the record `pending` so the operator is not blocked. The selected next step is human review. The worker currently runs in the web process, so a process restart can leave a pending record requiring operational follow-up; this is a production-hardening item for a durable job queue.

## Architecture and API

**Q: What is the technology stack?**

The application is built with Python and Flask, server-rendered HTML templates, and browser JavaScript for camera capture and direct uploads. SQLite supports local development; PostgreSQL is the production database path. Images use private local storage in development or S3-compatible object storage in deployment. The vision adapter calls a local Ollama model.

**Q: What are the main components?**

`app.py` handles Flask routes and the capture lifecycle. `contract.py` validates and serializes Evidence Contract 1.1 records. `verifier.py` compares SKU quantities. `orchestrator.py` runs the bounded role handoffs and selects an action. `vision.py` calls the vision model. `database.py` and `schema.py` store tenant-scoped data and apply migrations. `storage.py` handles private media storage and presigned uploads.

**Q: Which API endpoints implement the evidence contract?**

- `POST /v1/captures` creates a capture and returns presigned upload URLs.
- `POST /v1/captures/{id}/complete` verifies uploaded images and creates the record.
- `GET /v1/records/{id}` returns one read-only record.
- `GET /v1/records?since=&agent=&cursor=` lists organization-scoped records with pagination.

There are also record-scoped image and override routes. The list endpoint applies the `since` filter to UTC capture times.

**Q: How do you avoid routing image bytes through Flask?**

The server returns short-lived presigned PUT URLs. The browser uploads directly to private S3-compatible storage. The completion request sends image keys and capture times; the server reads the stored bytes to calculate SHA-256 and byte length.

**Q: How does the record match Evidence Contract 1.1?**

`contract.py` validates the fixed top-level, subject, image, check, outcome, and override fields. The version remains `1.1`. Agent workflow data is placed under `checks[].detail`, which is the contract's allowed extension point. Pack checks use the stable keys `sku_quantity` and `visual_evidence`.

**Q: What does `content_hash` mean?**

It is SHA-256 over the concatenated image hashes and serialized checks as defined by the application. It detects content changes relative to that hash, but it is not tamper-evident, immutable, or externally anchored. I would not describe it as a digital signature or chain of custody by itself.

## Security and reliability

**Q: How is organization isolation enforced?**

Application queries use the authenticated organization context and include organization filters. PostgreSQL tenant tables have row-level security enabled and forced. Record image access checks that the image key belongs to the requested record before storage is read. A different organization cannot list or fetch the record or its images.

**Q: Can an unauthenticated customer view a record?**

The individual record and image links are read-only and can be shared without login, as the contract requires. They are addressed by unguessable UUID record IDs, and an image is served only when its key is listed on that record. Listing records, creating captures, and adding overrides require an authenticated organization context.

**Q: Does local SQLite provide forced RLS?**

No. SQLite is for local development and does not support PostgreSQL row-level security. The RLS requirement must be demonstrated on the PostgreSQL deployment path.

**Q: What prevents a bad observation from becoming a pass?**

The verifier returns `UNCERTAIN` for missing or malformed observations. The component-verdict policy prevents an uncertain or failed check from being masked by an overall pass. Vision suggestions are uncalibrated and cannot authorize sealing.

**Q: What are the capture requirements?**

The CUBE page requires guided Overview and Labels images and allows an optional Detail image. It requests the rear camera, which requires HTTPS or localhost. Direct upload requires private S3-compatible storage. Uploads retry on failure; the operator can retry without retaking successfully uploaded shots.

## Evaluation and cost

**Q: What have you measured?**

The deterministic SKU and quantity comparison, missing and extra item handling, uncertain observation behavior, and the contract/API workflow have automated test coverage. The focused contract/API run was reported as 8 passing tests. Independent vision accuracy, precision, recall, false positive rate, false negative rate, and false-SEAL rate have not been measured.

**Q: Why haven’t you claimed model accuracy?**

There is no completed independent held-out evaluation set. The project calls for at least 50 unseen pack images with two independent labels and adjudication. Until that evaluation is complete, model scores are suggestions rather than calibrated probabilities.

**Q: What are the cost controls?**

The CUBE capture path batches all images and checks into one model call per capture. It records `latency_ms` and stores returned token usage under the visual check's detail. A dollar cost per decision has not yet been measured, so I would report the token and latency instrumentation without claiming a final unit cost.

**Q: What do you test?**

Tests cover record schema validation, capture and completion behavior, batched model invocation, pending status on model failure, pagination, organization isolation, image access, and append-only overrides. The focused contract/API test run reported 8 passed. The full suite should be run in the developer's local environment before final submission because the managed sandbox could not create the temporary upload directory during collection.

## Limitations and roadmap

**Q: What is the biggest technical limitation?**

Vision is not calibrated on an independent held-out dataset. It can miss occluded items or misidentify similar products, and count estimation is not yet validated. Deterministic structured observations and operator review remain necessary.

**Q: What would you improve next?**

First, finish and independently label the 50-image evaluation set and report per-check precision, recall, and false-SEAL rate. Next, move asynchronous vision jobs to a durable worker queue so restarts do not strand pending records. Then improve barcode/OCR and variant matching against the complete tenant catalog, and integrate canonical order data only after its source and permissions are defined.

**Q: Why not turn on automatic sealing now?**

There is no measured independent false-SEAL rate, and the current vision output is uncalibrated. Automatic sealing stays disabled until an appropriately labeled holdout evaluation and explicit safety gate justify it. A mistaken seal can ship the wrong order, so this needs evidence rather than a demo result.

**Q: What would you say if a judge calls it a web app rather than an agent?**

It is both: the product interface is a web app, and the CUBE capture flow contains a bounded agentic workflow. The agent roles evaluate evidence and choose a recorded next action. I describe it as human-supervised agentic AI, not a fully autonomous warehouse system.

## Closing answer

**Q: Why is this useful?**

PackGuard turns an informal last-second packing check into a repeatable workflow with structured comparisons, captured evidence, explicit uncertainty, organization isolation, and a traceable next action. It is designed to help an operator make a better-supported decision without asking an unvalidated model to make a high-impact shipping decision alone.
