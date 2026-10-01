# PackGuard Pack Manager Demo Script

Target length: 3-5 minutes

## 1. Explain the moment

Say: "PackGuard checks an outbound order while the box is still open. It prevents wrong, missing, and extra items from reaching the buyer and leaves evidence for Returns and Recovery."

## 2. Show a correct pack

1. Open `New check`.
2. Load `Correct package`.
3. Enter or select the expected SKU and quantity.
4. Enter the observed open-box contents.
5. Add an open-box photo.
6. Submit the check.
7. Show the expected versus observed trace, individual checks, evidence, and the human-confirmation boundary.

## 3. Show a wrong pack

1. Start another check.
2. Load `Mismatch`.
3. Submit it.
4. Show the failed SKU/quantity check and `FIX` / `STOP_AND_FIX` decision.

## 4. Show uncertainty

1. Start another check.
2. Load `Needs review` or omit the photo/observed contents.
3. Submit it.
4. Show `UNCERTAIN`, the recapture or manual-review action, and the reason.

## 5. Show the CUBE multi-agent capture

Use a deployment with private S3-compatible storage configured; the contract capture page disables submission without direct-upload storage.

1. Open **CUBE contract capture** from the New check page (or visit `/capture/contract`).
2. Enter an order and SKU; leave observed contents blank to demonstrate an uncertain observation, or enter a known observation.
3. Capture the required **Overview** and **Labels** shots with the rear camera. Add the optional **Detail** shot if useful.
4. Submit. Show that images upload directly to private storage and the record is saved before vision finishes.
5. Refresh the assessment and show the `vision_agent → evidence_verifier → workflow_coordinator` handoff, the selected next action, and its saved decision basis.
6. Explain the action: a mismatch routes to correction, poor image quality requests recapture, and uncertain or conflicting evidence routes to operator review. A matching result still requires operator confirmation; no agent can seal the order.

## 6. Show traceability

Open the record page and show:

- expected order lines;
- observed contents;
- evidence source and media;
- per-check status;
- agent recommendation and next action;
- audit history and recapture support;
- printable evidence report.

## 7. State the limitation honestly

Say: "The deterministic order comparison is authoritative. Local vision is advisory until the held-out evaluation is completed, so PackGuard never lets an uncalibrated model authorize sealing."

## Recording checklist

- Record the browser and terminal setup only if useful; do not show secrets.
- Use synthetic catalog data or your own captured fixtures.
- Include one correct case, one wrong-item case, and one uncertain case.
- Upload the final video link in the submission form; the repository cannot create that external link automatically.
