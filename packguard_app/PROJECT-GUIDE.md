# PackGuard project guide

This guide contains the local run instructions, demo login, current Alpha catalog identifiers, walkthrough, and a ready-to-read video script.

## What PackGuard does

PackGuard is a Flask web app for checking outbound packages before they are sealed. An operator enters what an order should contain, records what is observed in the open package, and saves the result with the order and package details. The app compares SKU quantities and reports a result such as PASS, FAIL, or UNCERTAIN. Records can include image or video evidence.

This local project uses synthetic demo data. A PASS means the entered expected and observed lines match; it is not a claim that the app independently recognized the products in an image. Images and videos are stored as evidence. General-purpose assistant answers require a local Ollama service and model.

## Start the app

Open PowerShell and run:

```powershell
cd "C:\Users\kanuk\OneDrive\Desktop\packguard_ai\packguard_app"
python app.py
```

Keep the PowerShell window open while using the app. Open the login page:

http://127.0.0.1:5000/login

If the project virtual environment is needed, use:

```powershell
& "C:\Users\kanuk\OneDrive\Desktop\packguard_ai\.venv\Scripts\Activate.ps1"
pip install -r "..\requirements.txt"
python app.py
```

To stop the app, return to its PowerShell window and press Ctrl+C.

## Demo login

Alpha workspace:

- Username: `alpha.operator`
- Password: `alpha-demo`

Bravo workspace:

- Username: `bravo.operator`
- Password: `bravo-demo`

Use Alpha for the walkthrough below. The workspaces are separate demo organizations.

## Product IDs, SKUs, and order IDs

A Product ID identifies a catalog entry. A SKU is the product code used in the expected and observed order lines. Enter contents as `SKU:quantity`; separate multiple products with semicolons. For example: `SKU-CABLE-USBC:1;SKU-BOTTLE-750:2`.

Current Alpha catalog values read from the local database:

| Product | Product ID | SKU |
|---|---|---|
| USB-C cable | `PRD-04B82B654689` | `SKU-CABLE-USBC` |
| 750 mL bottle | `PRD-A5274924E442` | `SKU-BOTTLE-750` |
| 500-piece puzzle | `PRD-EC5405BCB528` | `SKU-PUZZLE-500` |

Product IDs can differ if products are recreated or the local database changes. Confirm them on the Catalog page before recording. Use SKUs, not Product IDs, in the expected and observed contents fields.

New check does not create or look up an order automatically. The operator types the order ID, unit ID, and expected lines. This local project has no marketplace/order-system connector, so an order ID alone cannot retrieve authoritative items. Copy expected lines from the trusted order system before checking. Existing local sample order IDs include `ORD-50101` and `ORD-DEMO-PASS`. For a clean walkthrough, use the fresh IDs listed in the script below.

### Result behavior

The operator-entered expected and observed SKU/quantity lines drive the traditional deterministic check. Matching lines produce PASS; a wrong SKU or quantity produces FAIL; missing or malformed observed lines produce UNCERTAIN. Select the operator verdict `Seal` to confirm a matching PASS as the product decision `SEAL`. An uploaded photo can receive an uncalibrated vision suggestion for review, but that suggestion does not override a valid manual comparison or authorize `SEAL`.

The vision suggestion uses the full organization catalog, including unrelated products, so it can flag possible wrong or extra items. The default local vision model may not support multiple candidates; in that case visual assessment is unavailable and the result stays with the structured operator comparison or routes to review. Vision never verifies product identity by itself.

## Pages in the walkthrough

1. **Login** — sign in to the local Alpha demo workspace.
2. **Dashboard** — see workspace metrics, recent checks, and the New check button.
3. **Catalog** — review product names, Product IDs, and SKUs.
4. **New check** — enter the unit ID, order ID, expected and observed contents, operator, and verdict. Photo and video evidence are optional.
5. **Record detail** — review saved IDs, expected and observed lines, SKU checks, evidence, and result.
6. **Alerts** — review checks requiring attention, when present.
7. **Evaluation summary** — view result counts for the workspace's stored records. The local database may already contain earlier demo activity, so totals can be higher than the checks created in this walkthrough.
8. **Assistant** — ask questions about PackGuard and workspace data. It is available at `http://127.0.0.1:5000/assistant` after login. General answers need local Ollama.

## Ready-to-read video script

Follow the bracketed directions and read the spoken lines. The data below is synthetic demo data.

### Scene 1: Start and login

[In PowerShell, run `python app.py` from the project folder. Keep the window open. In a browser, open `http://127.0.0.1:5000/login`.]

**Say:** “PackGuard is a local application for recording outbound package checks before sealing. I’ve started the server and opened the login page. I’m using the synthetic Alpha demo workspace.”

[Sign in as `alpha.operator` with password `alpha-demo`.]

**Say:** “I’m signing in as the Alpha demo operator. The records I create in this walkthrough are synthetic examples.”

### Scene 2: Dashboard

[Show the dashboard. Point to the New check button, summary metrics, and recent checks.]

**Say:** “This is the workspace dashboard. It shows a summary of recorded checks and gives access to recent records. I’ll start by reviewing the product catalog.”

### Scene 3: Catalog

[Open Catalog. Find the USB-C cable and 750 mL bottle. Point out their Product IDs and SKUs.]

**Say:** “The catalog lists products available in this workspace. The USB-C cable has Product ID PRD-04B82B654689 and SKU SKU-CABLE-USBC. The 750 milliliter bottle has Product ID PRD-A5274924E442 and SKU SKU-BOTTLE-750. Product IDs identify catalog entries; the SKU is the code I enter for package contents.”

### Scene 4: Matching package

[Return to the dashboard and select New check. Enter the values below.]

- Packing unit / carton ID: `UNIT-VIDEO-PASS-01`
- Outbound order ID: `ORD-VIDEO-PASS-01`
- Packing operator: `alpha.operator`
- Packing channel: Merchant fulfilled
- Expected outbound order lines: `SKU-CABLE-USBC:1`
- Observed items in open box: `SKU-CABLE-USBC:1`
- Operator verdict: Seal
- Photo and video: leave blank for this text-only example

[Select Run pack check.]

**Say:** “I’m creating a check for package UNIT-VIDEO-PASS-01 and order ORD-VIDEO-PASS-01. The order expects one USB-C cable, and the observed contents show one USB-C cable. I’ll run the check.”

[On the record detail page, point to order and unit IDs, expected and observed lines, the SKU check, and result.]

**Say:** “The saved record keeps the order and package identifiers with the check details. The expected and observed SKU and quantity match, so the deterministic check passes. I also selected Seal as the operator verdict. The vision model is advisory; its unavailable or uncalibrated assessment does not override the manual result.”

### Scene 5: Mismatched package

[Return to New check and enter the following.]

- Packing unit / carton ID: `UNIT-VIDEO-FAIL-01`
- Outbound order ID: `ORD-VIDEO-FAIL-01`
- Packing operator: `alpha.operator`
- Packing channel: Merchant fulfilled
- Expected outbound order lines: `SKU-CABLE-USBC:1`
- Observed items in open box: `SKU-BOTTLE-750:1`
- Operator verdict: Stop and fix

[Select Run pack check.]

**Say:** “For this order, the expected contents are one USB-C cable, but the observation lists one 750 milliliter bottle. The entered SKUs differ, so PackGuard reports a FAIL and indicates that the package needs attention.”

### Scene 6: Missing observation

[Return to New check and enter the following. Leave the observed contents field empty.]

- Packing unit / carton ID: `UNIT-VIDEO-REVIEW-01`
- Outbound order ID: `ORD-VIDEO-REVIEW-01`
- Packing operator: `alpha.operator`
- Packing channel: Merchant fulfilled
- Expected outbound order lines: `SKU-CABLE-USBC:1`
- Observed items in open box: leave blank
- Operator verdict: Stop and fix

[Select Run pack check.]

**Say:** “For this check, the expected contents are recorded, but the observed contents are blank. PackGuard reports UNCERTAIN because there isn’t enough observation to confirm a match. This should be reviewed.”

### Scene 7: Dashboard and summary

[Return to the dashboard. Show the three recent checks. Select View summary or Evaluation summary in the footer.]

**Say:** “The dashboard now includes the checks I just created. The evaluation summary shows counts across records stored in this workspace. It may include earlier demo activity as well as today’s examples. The three examples demonstrate a match, a mismatch, and missing observation.”

### Scene 8: Assistant (optional)

[Open Assistant in the navigation. Ask: `What is the status of ORD-VIDEO-PASS-01?`]

**Say:** “The assistant is available on its own page and can answer questions about PackGuard and the current workspace. General-purpose answers require the local Ollama service and model.”

### Scene 9: Close

**Say:** “That completes the PackGuard walkthrough. I reviewed catalog identifiers, created three package checks, opened the saved records, and reviewed the evaluation summary. All the example data is synthetic and local.”

[Return to PowerShell and press Ctrl+C to stop the server.]

## Optional image or video evidence

On New check, an operator can attach an open-box photo or packing video. Camera use depends on browser camera permission; choosing an image file is an alternative. The app stores media as evidence, and its local vision model may show an uncalibrated suggestion when available. The operator-entered SKU and quantity comparison remains authoritative; image suggestions are not proof of product identity.

## Other useful local pages

- Assistant: `http://127.0.0.1:5000/assistant`
- Evaluation summary: `http://127.0.0.1:5000/reports/summary`
- Health check: `http://127.0.0.1:5000/health`
- Records JSON: `http://127.0.0.1:5000/api/records` (requires login)
- Records CSV: `http://127.0.0.1:5000/reports/records.csv` (requires login)
