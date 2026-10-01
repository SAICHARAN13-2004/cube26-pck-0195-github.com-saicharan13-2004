# PackGuard Pack Manager Demo Video

Target length: 6-8 minutes. Record the browser window and a small amount of narration. Use synthetic demo data and say that clearly.

## Before recording

1. Open PowerShell.
2. Paste:

```powershell
cd C:\Users\kanuk\OneDrive\Desktop\packguard_ai\packguard_app
$env:PACKGUARD_ENV="development"
$env:PACKGUARD_ALLOW_DEMO_LOGIN="true"
$env:PACKGUARD_AUTO_SEAL_ENABLED="false"
..\.venv\Scripts\python.exe app.py
```

3. Open a browser and go to:

```text
http://127.0.0.1:5000/login
```

4. Use:

```text
Username: alpha.operator
Password: alpha-demo
```

Do not show passwords, terminal secrets, database files, or personal files in the recording.

## Scene 1: Problem and login

Show the login page.

Say:

> "PackGuard is an outbound Pack Manager. It checks an open package against the order before sealing, preserves evidence, and routes uncertain cases to people instead of guessing."

Log in and wait for the Pack station dashboard.

## Scene 2: Dashboard

Show these dashboard areas:

- Pack station heading.
- Workspace name.
- New check button.
- Total checks.
- Ready to seal.
- Stop and fix.
- Needs review.
- Evidence readiness banner.
- Evaluation summary button.
- Recent pre-seal checks.
- Search box for products, orders, units, and record IDs.
- Recent photo or video evidence if present.

Say:

> "The dashboard is the operator's queue. It separates ready-to-seal, failed, and uncertain work, and every decision remains linked to a record."

Click **Catalog** in the navigation before creating the first check.

## Scene 3: Catalog

Show a seeded product or open **Add product**.

If adding a product, demonstrate these fields:

- SKU.
- Product name.
- Brand.
- External ID.
- Barcode.
- Variant attributes as JSON.
- Reference image.

Use a synthetic example such as:

```text
SKU-DEMO-CABLE
USB-C cable demo
{"color":"blue"}
```

Explain:

> "The catalog is organization-scoped. It stores identity and reference information. Reference images help operators, but they are not order evidence and do not independently determine a verdict."

Return to **New check**.

## Scene 4: Correct package

Click **New check**.

Use the **Correct package** demo preset, or enter the fields manually:

```text
Packing unit / carton ID: UNIT-PASS-DEMO
Outbound order ID: ORD-PASS-DEMO
Packing operator: alpha.operator
Packing channel: Merchant fulfilled
Attempt: Initial capture
Expected outbound order lines: SKU-CABLE-USBC:1
Observed items in open box: SKU-CABLE-USBC:1
Operator verdict: Seal
```

Explain each field:

- **Packing unit / carton ID** identifies the physical package.
- **Outbound order ID** identifies the customer order.
- **Packing operator** identifies the person running the check.
- **Packing channel** identifies merchant-fulfilled or 3PL packing.
- **Attempt** distinguishes the initial check from a recapture.
- **Expected outbound order lines** define what belongs in the box.
- **Observed items in open box** record what the operator sees.
- **Operator verdict** records the human's decision.
- **Override reason** is required when production mode disagrees with the deterministic result.

Use **Open camera** or upload an open-box photo. Explain that the photo is preserved as evidence.

Click **Run pack check**.

Show the resulting record and say:

> "The exact SKU and quantity match. PackGuard records PASS and the SEAL decision, but the uncalibrated local vision model cannot authorize sealing by itself."

## Scene 5: Record page

Point to each section:

- Order and packing-unit context.
- Verdict and workflow state.
- Recapture package button.
- Send to manual review button when available.
- Print evidence report button.
- Copy record ID button.
- Pack session and attempt history.
- Expected order lines.
- Observed open-box contents.
- Evidence source and photo reference.
- Per-SKU checks.
- Agent assessment.
- Vision status and provider.
- Portable evidence metadata.
- Audit history.

Click **Print evidence report** and show the print preview, then cancel printing.

Click **Copy record ID** and show the button confirmation.

## Scene 6: Failed package

Return to **New check**.

Click **Mismatch**, or enter:

```text
Packing unit / carton ID: UNIT-FAIL-DEMO
Outbound order ID: ORD-FAIL-DEMO
Packing operator: alpha.operator
Expected outbound order lines: SKU-CABLE-USBC:1
Observed items in open box: SKU-BOTTLE-750:1
Operator verdict: Stop and fix
```

Click **Run pack check**.

Show:

- Failed identity/quantity check.
- FAIL verdict.
- FIX decision.
- STOP_AND_FIX workflow state.
- Reason explaining the mismatch.

Say:

> "A wrong item cannot be hidden by a matching quantity. The package is stopped and must be corrected before sealing."

## Scene 7: Uncertain package

Return to **New check**.

Click **Needs review**, or enter:

```text
Packing unit / carton ID: UNIT-REVIEW-DEMO
Outbound order ID: ORD-REVIEW-DEMO
Packing operator: alpha.operator
Expected outbound order lines: SKU-CABLE-USBC:1
Observed items in open box: leave empty
Operator verdict: Stop and fix
```

Leave the photo empty or use a deliberately unclear image.

Click **Run pack check**.

Show:

- UNCERTAIN verdict.
- RECAPTURE or MANUAL_REVIEW action.
- Missing observation or visual evidence reason.
- No automatic seal.

Say:

> "When the evidence is missing or unclear, PackGuard does not guess. The operator gets a recapture or manual-review action, while the record preserves the uncertainty reason."

## Scene 8: Recapture

From the uncertain record, click **Recapture package**.

Show that these values are preserved:

- Packing unit / carton ID.
- Outbound order ID.
- Packing operator.
- Expected order lines.
- Original session link.

Choose:

```text
Attempt: Recapture
Recapture reason: Poor lighting or blocked item
```

Upload a clearer image and submit.

Show the session history with the original attempt and the recapture attempt.

Say:

> "A recapture does not erase the first decision. Both attempts remain linked in the audit history."

## Scene 9: Alerts

Open **Alerts** from the navigation.

Show:

- Active alert count.
- FAIL alerts.
- UNCERTAIN alerts.
- Operator or system disagreement.
- Open inspection evidence button.
- Attached photo or video evidence.

Say:

> "Alerts bring failed and uncertain packages back to the operator instead of letting them disappear in a log."

## Scene 10: Evaluation

Open **Evaluation** or **View summary**.

Show:

- Total records.
- PASS count.
- FAIL count.
- UNCERTAIN count.
- SEAL, FIX, RECAPTURE, and MANUAL_REVIEW decisions.
- Human agreement.
- Uncertain-rate target.
- Pending-rate target.
- All-items-present metric.
- Quantities-correct metric.
- Vision readiness status.

Say:

> "Presence and quantity are reported separately because recognizing an item is easier than counting repeated or partially hidden items. The current local CPU vision baseline safely falls back to manual review and is not claimed as autonomous production verification."

## Scene 11: Assistant

Open **Assistant**.

Ask:

```text
What does PackGuard do?
```

Then ask:

```text
How do I handle an uncertain package?
```

Show that the assistant explains the local workflow and does not invent order data.

## Scene 12: Support

Open **Support**.

Fill:

```text
Name: Demo operator
Email: demo@example.com
Subject: Review uncertain package
Message: Please review ORD-REVIEW-DEMO and confirm the open-box evidence.
```

Submit the support request.

Show the success message and explain:

> "Support requests are stored in the organization-scoped support queue. This local demo does not claim to send external email."

## Closing statement

Say:

> "PackGuard is an evidence-first outbound Pack Manager. It compares expected and observed contents, preserves photos and decisions, catches wrong or missing items when evidence supports the check, and fails open when vision is unavailable. Uncertain cases remain visible and never become silent seals. Autonomous visual sealing remains disabled until a faster provider passes an independent held-out evaluation."

## End screen

Finish on the record page showing:

- Verdict.
- Expected versus observed contents.
- Individual checks.
- Agent assessment.
- Evidence.
- Audit history.
