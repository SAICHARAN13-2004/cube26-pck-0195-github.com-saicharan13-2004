# PackGuard judge demo

## Start the app

From PowerShell:

```powershell
& "C:\Users\kanuk\OneDrive\Desktop\packguard_ai\.venv\Scripts\python.exe" "C:\Users\kanuk\OneDrive\Desktop\packguard_ai\packguard_app\app.py"
```

Open `http://127.0.0.1:5000/login`.

## Demo accounts

- Alpha: `alpha.operator` / `alpha-demo`
- Bravo: `bravo.operator` / `bravo-demo`

The two accounts demonstrate organization isolation. A user cannot read another organization's records by changing a URL parameter.

## PASS scenario

Before the demo, visit **Catalog** to review the synthetic starter SKUs or add a product with a name, variant attributes, and a reference image. The reference photos help identify catalog entries for humans; the current app does not use them for automatic image matching.

Create a new check with:

- Unit ID: `UNIT-PASS-DEMO`
- Order ID: `ORD-PASS-DEMO`
- Operator: `alpha.operator`
- Channel: `Amazon MFN`
- Expected: `SKU-A:1;SKU-B:2`
- Observed: `SKU-A:1;SKU-B:2`
- Operator verdict: `Seal`

Expected result: `PASS` and `SEAL`.

## FAIL scenario

Create another check with:

- Unit ID: `UNIT-FAIL-DEMO`
- Order ID: `ORD-FAIL-DEMO`
- Expected: `SKU-A:1;SKU-B:2`
- Observed: `SKU-A:1;SKU-B:1`
- Operator verdict: `Stop and fix`

Expected result: `FAIL` and `STOP_AND_FIX`.

## UNCERTAIN scenario

Create another check with:

- Unit ID: `UNIT-REVIEW-DEMO`
- Order ID: `ORD-REVIEW-DEMO`
- Expected: `SKU-A:1`
- Observed: leave empty
- Operator verdict: `Stop and fix`

Expected result: `UNCERTAIN` and `HOLD_FOR_REVIEW`.

## Image evidence

Expected SKUs may be selected from **Catalog** or entered manually. The catalog is tenant-scoped and includes SKU, name, variants, identifiers, and reference-image metadata. Its seeded demo products come from synthetic data.

On a supported secure browser, use **Open camera** for a smartphone-style capture; otherwise use the camera-enabled file chooser or upload a JPG/JPEG/PNG/WEBP under 8 MB. The image is stored under the logged-in organization and shown on the record page. The current MVP records the image as evidence but does not claim to recognize SKUs or assess blur/occlusion from pixels.

The record preserves both decision vocabularies: the challenge verdict (`PASS`/`FAIL`/`UNCERTAIN`) and the specification decision (`SEAL`/`FIX`/`RECAPTURE`/`MANUAL_REVIEW`). The Evaluation Summary reports product-decision counts. Any local vision output is an uncalibrated suggestion; it cannot override a valid structured comparison or authorize `SEAL`. False-SEAL rate remains unmeasured until a held-out evaluation is completed.

The record shows:

- Evidence ID
- Expected and observed contents
- SKU-level checks
- Operator agreement
- Image assessment status from the configured local vision path, when a photo is provided
- Vision suggestion, quality, and uncertainty details when available; unavailable or uncalibrated output routes to human review and never overrides the entered comparison

## Reports and APIs

- `/health` checks service health.
- `/api/records` returns the logged-in organization's records as JSON.
- `/reports/records.csv` downloads the logged-in organization's records.
- `/reports/summary` displays the judge-facing evaluation summary.
- `/reports/summary.json` returns the evaluation summary as JSON.

All record and report routes are login-protected and organization-scoped.

## Packing video evidence

The capture form accepts MP4, WEBM, and MOV packing videos. Upload a short packing clip in **Packing video**. The record page shows a tenant-scoped video player, and the video reference is included in the evidence and CSV export. Video is evidence only; it does not trigger SKU recognition until a vision provider is configured.
