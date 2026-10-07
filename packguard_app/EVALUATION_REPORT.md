# PackGuard Pack Manager Evaluation Report

Date: 2026-10-01
Track: CUBE Buildathon - Pack Manager

## Scope

PackGuard evaluates an outbound order before the carton is sealed. It compares expected order lines with observed open-box contents, stores photo/video evidence, and produces `SEAL`, `FIX`, `RECAPTURE`, or `MANUAL_REVIEW`.

Receiving-only checks such as supplier, Purchase Order intake, carton count, and units per carton are not part of the Pack Manager decision path.

## Current measured results

| Area | Result | Method |
| --- | --- | --- |
| Deterministic SKU and quantity comparison | Passing automated tests | `pytest tests/test_packguard.py` |
| Extra or missing SKU handling | Passing automated tests | Expected and observed line-map comparisons |
| Missing observation handling | `UNCERTAIN` / recapture | Automated route test |
| Missing photo handling | `UNCERTAIN` / recapture | Automated route test |
| Overall PASS masking a failed or uncertain check | Prevented | Automated invariant test |
| Vision SKU/count accuracy | Not measurable | All 50 saved held-out predictions timed out; no usable model decisions |
| False positives | Not measurable | No usable held-out model predictions |
| False negatives | Not measurable | No usable held-out model predictions |
| False-SEAL rate | Not measured; auto-seal blocked | Calibration gate requires 50 independently double-labeled units |
| `all_items_present` | Not measured on independent vision predictions | Report separately from quantity correctness |
| `quantities_correct` | Not measured on independent vision predictions | Count errors must not be hidden by SKU recognition |
| `UNCERTAIN` rate target | 10% | Product kill threshold; current live result is reported in the Evaluation page |
| Pending/model-fallback rate target | 5% | Product kill threshold; current live result is reported in the Evaluation page |

## Vision evaluation status

The repository contains 50 held-out images and populated annotator A, annotator B, and adjudicated decision manifests under `fixtures/vision/heldout_real/`. Their independent, blinded collection process must be confirmed by the dataset owner; the files alone cannot prove reviewer independence. The saved prediction file contains 50 `MODEL_TIMEOUT` results, so there are no valid model predictions to compare and no vision accuracy or calibration claim is made.

The currently verified local behavior is:

- Moondream can provide a fast single-SKU presence hint.
- Moondream does not verify quantity.
- Multiple expected SKUs route to human review.
- Gemma 3 4B timed out at 90 seconds on a 320px validation image; Qwen2.5-VL 3B timed out at 120 seconds on the same small validation image. Ollama reported no GPU allocation for the loaded model.
- Moondream 1.8B returned a single-SKU `MATCH` presence hint in about 1.1 seconds on a validation image, but returned no quantity and cannot validate a full pack.
- The 50-image prediction run produced `MODEL_TIMEOUT` for all 50 images. This is an unavailable evaluation, not a 0% accuracy result.
- Vision output cannot authorize `SEAL`.
- Production candidates include the full tenant catalog, including look-alike decoys. The Moondream fallback routes multi-candidate cases to manual review.
- The MVP uses one open-box image and reports occlusion separately; it does not claim that hidden items are visible.

## Failure modes

1. A blurry, blocked, or missing photo results in `RECAPTURE` or `MANUAL_REVIEW`.
2. A missing or extra SKU results in `FAIL` / `FIX` when the operator observation identifies it.
3. A malformed order or observed line results in `UNCERTAIN`.
4. A vision timeout or invalid model response is saved as manual review rather than blocking record creation.
5. Uncalibrated vision suggestions never become authoritative evidence.
6. Occlusion can hide an item even when recognition is correct; it is reported as a separate uncertainty category.

## Required next measurement

Confirm the 50-image labels were independently produced without exposure to model outputs. Run one non-held-out validation image to completion on the target inference hardware first; then freeze the model and configuration before generating predictions for the 50 held-out images. Report per-check precision, recall, false positives, false negatives, `UNCERTAIN` rate, inference timeouts, and false-SEAL rate. Do not enable automatic sealing until the policy gate passes.

## Automated test status

The full local suite passed 80 tests; one optional PostgreSQL migration integration test was skipped because `PACKGUARD_TEST_POSTGRES_URL` was not configured.
