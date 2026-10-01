# PackGuard Architecture

## Current vertical slice

```text
Tenant-scoped product catalog / operator form
    -> PackGuard assessment agent (local vision + operator observation)
    -> deterministic verifier (verifier.py)
    -> recommendation and next-action policy (agent.py)
    -> evidence contract (evedince.py)
    -> tenant-scoped SQLite record (database.py)
    -> tenant-scoped pack session with numbered recapture attempts
    -> dashboard / record page / JSON API
```

The catalog stores organization-scoped SKU, name, brand, external ID, barcode, variant attributes, and an optional reference image. The capture form can select catalog SKUs and use a phone camera or file upload. The default local Gemma path returns structured SKU, quantity, image-quality, and uncertainty suggestions; the agent records a recommendation and next action without requiring an operator to interpret the image. Moondream remains an optional fast presence-only model and cannot assess quantity or multiple expected SKUs. Vision estimates remain uncalibrated and never independently authorize `SEAL`; an operator must confirm a matching pack before sealing.

## Decision contract

- `PASS` + `SEAL`: every expected SKU has the exact expected quantity, with no extra SKU.
- `FAIL` + `STOP_AND_FIX`: any missing, short, extra, or wrong SKU is found.
- `UNCERTAIN` + `HOLD_FOR_REVIEW`: the order lines or observed contents are absent/invalid.

## Product decision compatibility

The product specification uses `SEAL`, `FIX`, `RECAPTURE`, and `MANUAL_REVIEW`. Evidence now carries this as a separate `decision` field while retaining the existing challenge `verdict` and `action` fields for compatibility:

- Exact verified match: `PASS` / `SEAL` plus product decision `SEAL`.
- Confirmed mismatch: `FAIL` / `STOP_AND_FIX` plus product decision `FIX`.
- No observed contents: `UNCERTAIN` / `HOLD_FOR_REVIEW` plus product decision `RECAPTURE`.
- Malformed or missing order data: `UNCERTAIN` / `HOLD_FOR_REVIEW` plus product decision `MANUAL_REVIEW`.

Catalog product names and variant attributes are attached to deterministic and candidate SKU checks. The vision candidate remains a model suggestion, not a verified identity.

The current local vision adapter provides image-quality, SKU, quantity, possible-extra, occlusion, and uncertainty suggestions. Confidence values are model estimates and are not calibrated probabilities. The assessment page explicitly identifies the suggested decision, rationale, and next action. Human confirmation and the deterministic verifier remain authoritative.

## Operating targets and capture policy

The initial commercial kill thresholds are an `UNCERTAIN` rate of 10% and a pending/model-fallback rate of 5% across pack checks. These are targets, not measured claims; the Evaluation page reports both rates and whether each target is exceeded. A pending or failed model call fails open operationally: the record is saved and the operator may continue using their own existing judgment, but PackGuard does not authorize `SEAL`.

PackGuard uses a single-shot capture policy for the current MVP. Operators are asked to spread items apart and show the full open box. If the model reports partial or uncertain occlusion, the record stays uncertain and the occlusion reason is retained. This is a known geometric failure mode, separate from recognition and quantity failures. Bounding boxes, when supplied by a future model, are evidence for explanation only; presence and count are the load-bearing rules.

The production candidate set includes the full organization catalog, including unrelated and look-alike decoys, so the model must discriminate rather than only confirm expected SKUs. The fast Moondream fallback cannot process multiple candidates and therefore routes those cases to manual review; a structured vision provider is required for automated decoy discrimination.

## Pack evidence invariant

The regular outbound capture pipeline compares expected order lines with operator-entered observed contents before assigning an overall result. A failed SKU or quantity check forces overall `FAIL`; missing or malformed structured observations become `UNCERTAIN`. An uploaded photo can receive an uncalibrated vision suggestion for human review, but it does not override a valid manual comparison or authorize `SEAL`. A matching result still requires the operator to explicitly select `Seal` before the product decision becomes `SEAL`. CUBE contract capture has a separate asynchronous vision workflow. Receiving-only carton, supplier, and Purchase Order checks are not part of this Pack Manager decision path.

Every decision stores expected contents, observed contents, individual checks, source provenance, reason, organization, operator, unit, and timestamp.

## Bounded agentic Pack workflow

Contract captures pass through three specialist roles in `orchestrator.py`: the vision agent returns one batched image assessment, the evidence verifier runs deterministic expected-versus-observed comparisons on both operator input and any structured vision candidate, and the workflow coordinator selects a bounded next action. The action executor persists that route in `outcome.decision`; its handoffs, basis, and selected tool are recorded under `checks[].detail.agent_run`, which is the contract's permitted extension point. The UI shows the handoff and selected action.

This is agentic because the system observes a capture, hands results between role-specific workers, selects and executes a constrained workflow action, and records the result without waiting for a person to direct each internal step. It remains human-supervised: model output is uncalibrated, disagreement routes to review, and no agent can seal an order. Vision still runs once per capture to preserve the contract's cost limit. Recapture and correction actions route the operator; they do not change warehouse equipment or external order systems.

## Tenancy

Every record query requires an `org_id` predicate. Record detail also requires both the record ID and organization ID, so guessing another tenant's record key returns `404`. The two sample organizations are seeded from the synthetic reference CSV.

## Production hardening still required

- Improve visual detection, variant matching, OCR/barcode reading, and measure confidence on real labeled images.
- Replace manually entered demo orders with canonical marketplace orders and a provider adapter.
- Move SQLite to Postgres with database-enforced row-level security for multi-process deployment.
- Store uploaded images in private object storage with signed, tenant-scoped URLs.
- Capture an independently labelled 50-unit held-out evaluation set and report false-SEAL rate, false positives, false negatives, recapture and manual-review rates.
