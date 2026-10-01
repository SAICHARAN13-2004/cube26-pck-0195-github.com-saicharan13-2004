# Vision fixture set

This directory defines the image dataset contract for PackGuard's future vision provider. The product names and SKUs are synthetic; do not treat them as real catalog data.

## Capture protocol

- Capture the open box from the operator's normal station viewpoint.
- Use JPG or PNG images under 8 MB.
- Keep the full item and relevant packaging visible.
- Capture at least three lighting and angle variations per SKU.
- Do not put labels such as the SKU name in the image.
- Record the human label independently before model evaluation.

## Dataset splits

- `train`: images used while developing or calibrating a provider.
- `validation`: images used to tune thresholds and review behavior.
- `held_out`: images never used for tuning; report SEAL, FIX, RECAPTURE, MANUAL_REVIEW, per-decision precision/recall, and especially false-SEAL rate from this split.

`manifest.csv` contains the expected labels and filenames. The actual image files should be captured locally and must not be downloaded from random web pages, because these SKUs are synthetic.

## Validate the fixture files

From `packguard_app`, run:

```powershell
python validate_fixtures.py
```

The command checks manifest columns, duplicate IDs/filenames, allowed splits and verdicts, and whether every named image exists. It exits non-zero while files or manifest fields are missing. This is a file/metadata check only; it does not verify that the human label matches the photo or measure model accuracy.

When a vision provider produces predictions, save them to `predictions.csv` with columns `fixture_id,predicted_decision`, then run:

```powershell
python evaluate_vision.py
```

The evaluator compares `expected_decision` to `predicted_decision`, reports accuracy, per-decision precision/recall, missing predictions, and false-SEAL rate. It rejects predictions for fixtures outside the held-out split.

For the held-out evaluation described in the specification, use new pack-level images not used during training or tuning, and have two people label each unit independently. The current 12-row synthetic manifest is only a starter fixture list, not the required 50-unit evaluation set.

The current application does not run model inference yet. Until a provider is configured, uploaded images are recorded as `PENDING_REVIEW`.
