"""Evaluate vision predictions against held-out PackGuard fixture labels."""

import argparse
import csv
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = APP_DIR / "fixtures" / "vision" / "manifest.csv"
DEFAULT_PREDICTIONS = APP_DIR / "fixtures" / "vision" / "predictions.csv"
DECISIONS = ("SEAL", "FIX", "RECAPTURE", "MANUAL_REVIEW")
DOUBLE_LABEL_COLUMNS = ("annotator_a_decision", "annotator_b_decision", "adjudicated_decision")


def _read_csv(path: Path) -> list[dict[str, str]]:
	with path.open(newline="", encoding="utf-8-sig") as csv_file:
		return list(csv.DictReader(csv_file))


def validate_double_labeled_holdout(manifest_path: str | Path) -> dict[str, Any]:
	"""Report whether a manifest is eligible for production calibration."""
	rows = _read_csv(Path(manifest_path))
	held_out = [row for row in rows if row.get("split", "").strip() == "held_out"]
	issues: list[str] = []
	if len(held_out) < 50:
		issues.append(f"Requires at least 50 held_out rows; found {len(held_out)}")
	for column in DOUBLE_LABEL_COLUMNS:
		missing = sum(not row.get(column, "").strip().upper() for row in held_out)
		if missing:
			issues.append(f"{missing} held_out rows are missing {column}")
	for row in held_out:
		a = row.get("annotator_a_decision", "").strip().upper()
		b = row.get("annotator_b_decision", "").strip().upper()
		adjudicated = row.get("adjudicated_decision", "").strip().upper()
		for label, value in (("annotator_a_decision", a), ("annotator_b_decision", b), ("adjudicated_decision", adjudicated)):
			if value and value not in DECISIONS:
				issues.append(f"{row.get('fixture_id', '<unknown>')} has invalid {label}: {value}")
	return {
		"ready": not issues,
		"held_out_count": len(held_out),
		"issues": issues,
		"calibration_status": "READY" if not issues else "NOT_READY",
	}


def evaluate_predictions(
	manifest_path: str | Path = DEFAULT_MANIFEST,
	predictions_path: str | Path = DEFAULT_PREDICTIONS,
) -> dict[str, Any]:
	"""Compare held-out fixture labels with predictions keyed by fixture_id."""
	manifest_file = Path(manifest_path)
	predictions_file = Path(predictions_path)
	if not manifest_file.is_file():
		raise FileNotFoundError(f"Manifest not found: {manifest_file}")
	if not predictions_file.is_file():
		raise FileNotFoundError(
			f"Predictions not found: {predictions_file}. Create it with fixture_id,predicted_decision columns."
		)

	manifest_rows = _read_csv(manifest_file)
	prediction_rows = _read_csv(predictions_file)
	held_out = {
		row["fixture_id"]: (row.get("adjudicated_decision") or row["expected_decision"]).strip().upper()
		for row in manifest_rows
		if row.get("split", "").strip() == "held_out"
	}
	if not held_out:
		raise ValueError("The manifest contains no held_out fixtures.")

	predictions: dict[str, str] = {}
	for row in prediction_rows:
		fixture_id = row.get("fixture_id", "").strip()
		prediction = row.get("predicted_decision", "").strip().upper()
		if fixture_id in predictions:
			raise ValueError(f"Duplicate prediction for fixture_id {fixture_id}.")
		if prediction not in DECISIONS:
			raise ValueError(f"Invalid predicted_decision {prediction!r} for {fixture_id}.")
		predictions[fixture_id] = prediction

	unknown_predictions = sorted(set(predictions) - set(held_out))
	if unknown_predictions:
		raise ValueError(f"Predictions contain non-held-out fixture IDs: {', '.join(unknown_predictions)}")

	confusion = {actual: {predicted: 0 for predicted in DECISIONS} for actual in DECISIONS}
	correct = 0
	for fixture_id, actual in held_out.items():
		predicted = predictions.get(fixture_id)
		if predicted is None:
			continue
		confusion[actual][predicted] += 1
		correct += actual == predicted

	compared = len(predictions)
	unsafe_actuals = sum(actual != "SEAL" for actual in held_out.values())
	false_seals = sum(
		actual != "SEAL" and predictions.get(fixture_id) == "SEAL"
		for fixture_id, actual in held_out.items()
	)
	class_metrics = {}
	for label in DECISIONS:
		true_positive = confusion[label][label]
		predicted_count = sum(confusion[actual][label] for actual in DECISIONS)
		actual_count = sum(confusion[label].values())
		class_metrics[label] = {
			"precision": true_positive / predicted_count if predicted_count else None,
			"recall": true_positive / actual_count if actual_count else None,
		}

	return {
		"split": "held_out",
		"held_out_count": len(held_out),
		"predictions_received": compared,
		"missing_predictions": len(held_out) - compared,
		"accuracy": correct / len(held_out),
		"false_seal_count": false_seals,
		"false_seal_rate": false_seals / unsafe_actuals if unsafe_actuals else None,
		"confusion_matrix": confusion,
		"class_metrics": class_metrics,
	}


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
	parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
	args = parser.parse_args()
	try:
		result = evaluate_predictions(args.manifest, args.predictions)
	except (FileNotFoundError, ValueError, KeyError) as error:
		print(f"Evaluation unavailable: {error}")
		return 1

	print(f"Held-out fixtures: {result['held_out_count']}")
	print(f"Predictions received: {result['predictions_received']}")
	print(f"Missing predictions: {result['missing_predictions']}")
	print(f"Accuracy: {result['accuracy']:.1%}")
	if result["false_seal_rate"] is None:
		print("False-SEAL rate: not measurable (no held-out negative cases)")
	else:
		print(f"False-SEAL rate: {result['false_seal_rate']:.1%} ({result['false_seal_count']} false seals)")
	print(f"Confusion matrix: {result['confusion_matrix']}")
	print(f"Per-class precision/recall: {result['class_metrics']}")
	if result["missing_predictions"]:
		print("WARNING: incomplete predictions; do not use this as a final evaluation report.")
	return 0 if not result["missing_predictions"] else 2


if __name__ == "__main__":
	raise SystemExit(main())
