# PackGuard 50-Image Holdout Collection

This is the path to real uncertain, pending, presence, quantity, and false-SEAL measurements. Do not generate labels, reuse tuning images, or let the model see the labels before evaluation.

## Required set

Capture 50 new outbound open-box images from the normal packing station. Use a balanced, realistic mix:

- 15 clear correct packs with every item visible;
- 10 wrong-SKU or wrong-variant packs;
- 8 missing-item packs;
- 7 extra-item packs;
- 5 quantity/count cases, including repeated units;
- 3 partially occluded packs;
- 2 blurry or poorly lit packs.

Use real pack photos or controlled physical mock orders. Keep the order manifest separate from the image files. Do not use catalog reference photos as pack evidence.

## Capture fields

For each image, add one row to `fixtures/vision/heldout_50_template.csv`:

- `fixture_id`: `HOLD-001` through `HOLD-050`;
- `sku` and `quantity`: the expected order line;
- `image_filename`: the captured image filename;
- `notes`: the known scenario category, without exposing the expected answer to annotators.

Keep the model and tuning team away from the final 50 images until labels are frozen.

## Independent labeling

Give the same image and expected order to annotator A and annotator B separately. They must record:

- `all_items_present`: `PASS`, `FAIL`, or `UNCERTAIN`;
- `quantities_correct`: `PASS`, `FAIL`, or `UNCERTAIN`;
- final operator decision: `SEAL`, `FIX`, `RECAPTURE`, or `MANUAL_REVIEW`.

They must not see each other's labels or any model output. A third reviewer adjudicates disagreements and records the final labels. For an occluded item, use `UNCERTAIN` unless the visible evidence proves the count.

The current CSV has the final-decision fields required by the existing calibration gate. Keep the presence/count labels in a separate blinded label sheet or extend the CSV with the same three-way fields before running the metric script.

## Run the checks

From `packguard_app`:

```powershell
python validate_fixtures.py
python evaluate_vision.py --manifest fixtures/vision/heldout_50_template.csv --predictions fixtures/vision/predictions.csv
```

The prediction file must contain one row per image:

```text
fixture_id,predicted_decision
HOLD-001,SEAL
```

Record these results in `EVALUATION_REPORT.md`:

- decision accuracy;
- per-decision precision and recall;
- false positives and false negatives;
- false-SEAL count and rate;
- `all_items_present` accuracy;
- `quantities_correct` accuracy;
- UNCERTAIN rate;
- pending/model-fallback rate;
- occlusion-caused uncertainty separately from recognition errors.

Do not enable automatic sealing unless the 50-row double-label gate is complete and the measured false-SEAL rate passes the policy threshold.

## What can be done now

The code, metrics, CSV contract, validator, and evaluator are ready. The 50 physical images and two independent human labelers cannot be created truthfully by software; they must come from the packing setup or controlled mock orders.
