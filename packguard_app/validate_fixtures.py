"""Validate PackGuard's local vision fixture manifest and image files."""

import csv
from collections import Counter
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = APP_DIR / "fixtures" / "vision" / "manifest.csv"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
REQUIRED_COLUMNS = {
	"fixture_id", "split", "sku", "quantity", "image_filename",
	"expected_verdict", "expected_decision", "notes",
}
VALID_SPLITS = {"train", "validation", "held_out"}
VALID_VERDICTS = {"PASS", "FAIL", "UNCERTAIN"}
VALID_DECISIONS = {"SEAL", "FIX", "RECAPTURE", "MANUAL_REVIEW"}


def inspect_manifest(manifest_path: str | Path = MANIFEST_PATH) -> dict[str, Any]:
	path = Path(manifest_path)
	result: dict[str, Any] = {
		"manifest": str(path),
		"row_count": 0,
		"present_count": 0,
		"missing_files": [],
		"errors": [],
		"splits": {},
		"verdicts": {},
	}
	if not path.is_file():
		result["errors"].append(f"Manifest not found: {path}")
		return result

	with path.open(newline="", encoding="utf-8-sig") as manifest_file:
		reader = csv.DictReader(manifest_file)
		columns = set(reader.fieldnames or [])
		missing_columns = sorted(REQUIRED_COLUMNS - columns)
		if missing_columns:
			result["errors"].append(f"Missing columns: {', '.join(missing_columns)}")
			return result

		fixture_ids: set[str] = set()
		image_names: set[str] = set()
		splits: Counter[str] = Counter()
		verdicts: Counter[str] = Counter()
		decisions: Counter[str] = Counter()
		for row_number, row in enumerate(reader, start=2):
			result["row_count"] += 1
			fixture_id = (row.get("fixture_id") or "").strip()
			filename = (row.get("image_filename") or "").strip()
			split = (row.get("split") or "").strip()
			verdict = (row.get("expected_verdict") or "").strip().upper()
			decision = (row.get("expected_decision") or "").strip().upper()

			if not fixture_id:
				result["errors"].append(f"Row {row_number}: fixture_id is empty")
			elif fixture_id in fixture_ids:
				result["errors"].append(f"Row {row_number}: duplicate fixture_id {fixture_id}")
			fixture_ids.add(fixture_id)

			if not filename or Path(filename).name != filename:
				result["errors"].append(f"Row {row_number}: image_filename must be a plain filename")
				continue
			if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
				result["errors"].append(f"Row {row_number}: unsupported image extension for {filename}")
			if filename in image_names:
				result["errors"].append(f"Row {row_number}: duplicate image_filename {filename}")
			image_names.add(filename)

			if split not in VALID_SPLITS:
				result["errors"].append(f"Row {row_number}: invalid split {split!r}")
			splits[split] += 1
			if verdict not in VALID_VERDICTS:
				result["errors"].append(f"Row {row_number}: invalid expected_verdict {verdict!r}")
			verdicts[verdict] += 1
			if decision not in VALID_DECISIONS:
				result["errors"].append(f"Row {row_number}: invalid expected_decision {decision!r}")
			decisions[decision] += 1

			if (path.parent / filename).is_file():
				result["present_count"] += 1
			else:
				result["missing_files"].append(filename)

	result["splits"] = dict(sorted(splits.items()))
	result["verdicts"] = dict(sorted(verdicts.items()))
	result["decisions"] = dict(sorted(decisions.items()))
	return result


def main() -> int:
	result = inspect_manifest()
	print(f"Manifest rows: {result['row_count']}")
	print(f"Images present: {result['present_count']}")
	print(f"Images missing: {len(result['missing_files'])}")
	print(f"Split counts: {result['splits']}")
	print(f"Verdict counts: {result['verdicts']}")
	print(f"Decision counts: {result['decisions']}")
	for error in result["errors"]:
		print(f"ERROR: {error}")
	for filename in result["missing_files"]:
		print(f"MISSING: {filename}")
	if result["errors"] or result["missing_files"] or result["row_count"] == 0:
		return 1
	print("Fixture manifest validation passed. Human label accuracy is not evaluated by this command.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
