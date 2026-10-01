"""Generate PackGuard decisions for a held-out manifest using the configured local vision model."""

import argparse
import csv
import os
from pathlib import Path

from verifier import verify_pack
from vision import inspect_image

APP_DIR = Path(__file__).resolve().parent
DECISIONS = {"SEAL", "FIX", "RECAPTURE", "MANUAL_REVIEW"}
CATALOG = [
    {"sku": "SKU-CABLE-USBC", "product_name": "USB-C cable", "attributes": {}},
    {"sku": "SKU-BOTTLE-750", "product_name": "750 mL bottle", "attributes": {}},
    {"sku": "SKU-PUZZLE-500", "product_name": "500-piece puzzle", "attributes": {}},
    {"sku": "SKU-TOWEL-BLU", "product_name": "Blue towel", "attributes": {"color": "blue"}},
    {"sku": "SKU-CANDLE-3", "product_name": "Candle", "attributes": {}},
    {"sku": "SKU-LAMP-LED", "product_name": "LED lamp", "attributes": {}},
    {"sku": "SKU-SERUM-30", "product_name": "30 mL serum bottle", "attributes": {}},
    {"sku": "SKU-PROT-1KG", "product_name": "1 kg product package", "attributes": {}},
    {"sku": "SKU-MUG-11", "product_name": "Mug", "attributes": {}},
    {"sku": "SKU-LEASH-6FT", "product_name": "6 ft leash", "attributes": {}},
]


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as manifest_file:
        return list(csv.DictReader(manifest_file))


def decision_for(row: dict[str, str], inspection: dict[str, object]) -> str:
    status = str(inspection.get("status", "UNKNOWN"))
    if status != "SUGGESTIONS_READY_UNCALIBRATED":
        return "MANUAL_REVIEW"
    quality = str((inspection.get("image_quality") or {}).get("status", "UNKNOWN")).upper()
    if quality == "POOR":
        return "RECAPTURE"
    if inspection.get("uncertainties"):
        return "MANUAL_REVIEW"

    expected = f"{row.get('sku', '').strip()}:{row.get('quantity', '').strip()}"
    observed_lines = []
    for item in inspection.get("detected_items", []):
        if not isinstance(item, dict):
            continue
        sku = item.get("sku")
        quantity = item.get("quantity")
        if isinstance(sku, str) and isinstance(quantity, int) and quantity >= 0:
            observed_lines.append(f"{sku}:{quantity}")
    if inspection.get("extra_items") or not observed_lines:
        return "FIX" if inspection.get("extra_items") else "MANUAL_REVIEW"
    result = verify_pack(expected, ";".join(observed_lines))
    return "SEAL" if result.get("verdict") == "PASS" else "FIX"


def candidates_for(row: dict[str, str]) -> list[dict[str, object]]:
    """Use one expected candidate only for Moondream's presence-only baseline."""
    if os.environ.get("PACKGUARD_VISION_MODEL", "gemma3:4b").lower().startswith("moondream"):
        return [product for product in CATALOG if product["sku"] == row.get("sku", "").strip()]
    return CATALOG


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=APP_DIR / "fixtures" / "vision" / "heldout_real" / "manifest.csv")
    parser.add_argument("--images", type=Path, default=APP_DIR / "fixtures" / "vision" / "heldout_real")
    parser.add_argument("--output", type=Path, default=APP_DIR / "fixtures" / "vision" / "heldout_real" / "predictions.csv")
    args = parser.parse_args()

    rows = read_manifest(args.manifest)
    with args.output.open("w", newline="", encoding="utf-8") as predictions_file:
        writer = csv.DictWriter(predictions_file, fieldnames=["fixture_id", "predicted_decision", "vision_status"])
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            image_path = args.images / row["image_filename"]
            inspection = inspect_image(image_path, catalog_products=candidates_for(row))
            decision = decision_for(row, inspection)
            if decision not in DECISIONS:
                decision = "MANUAL_REVIEW"
            writer.writerow({
                "fixture_id": row["fixture_id"],
                "predicted_decision": decision,
                "vision_status": inspection.get("status", "UNKNOWN"),
            })
            print(f"[{index}/{len(rows)}] {row['fixture_id']}: {decision} ({inspection.get('status', 'UNKNOWN')})")
    print(f"Predictions saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
