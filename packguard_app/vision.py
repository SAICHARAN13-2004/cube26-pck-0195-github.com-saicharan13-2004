"""Local Ollama vision suggestions for uploaded pack images.

Predictions are uncalibrated and must never independently authorize SEAL.
"""

import base64
from io import BytesIO
import json
import os
import re
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageOps


VISION_CONTRACT_VERSION = "packguard.vision.v1"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _prepare_model_image(image_bytes: bytes) -> bytes:
	"""Downscale an inference copy to reduce local vision latency; preserve originals elsewhere."""
	with Image.open(BytesIO(image_bytes)) as source:
		image = ImageOps.exif_transpose(source).convert("RGB")
		max_dimension = max(320, min(896, int(os.environ.get("PACKGUARD_VISION_MAX_DIMENSION", "640"))))
		image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
		output = BytesIO()
		image.save(output, format="JPEG", quality=82, optimize=True)
		return output.getvalue()


def _empty_result(status: str, provider: str, reason: str, photo_path: str | Path | None = None) -> dict[str, Any]:
	return {
		"status": status,
		"detected_items": [],
		"extra_items": [],
		"uncertainties": [reason],
		"image_quality": {"status": "UNKNOWN", "score": None, "reason": reason},
		"occlusion": {"status": "UNCERTAIN", "reason": reason},
		"confidence": None,
		"provider": provider,
		"calibration_status": "UNVALIDATED",
		"contract_version": VISION_CONTRACT_VERSION,
		"photo_path": str(photo_path) if photo_path else None,
	}


def _looks_like_supported_image(path: Path, image: bytes) -> bool:
	suffix = path.suffix.lower()
	if suffix not in ALLOWED_EXTENSIONS:
		return False
	if suffix in {".jpg", ".jpeg"}:
		return image.startswith(b"\xff\xd8\xff")
	if suffix == ".png":
		return image.startswith(b"\x89PNG\r\n\x1a\n")
	return len(image) >= 12 and image.startswith(b"RIFF") and image[8:12] == b"WEBP"


def _bounded_score(value: Any) -> float | None:
	try:
		score = float(value)
	except (TypeError, ValueError):
		return None
	return score if 0 <= score <= 1 else None


def inspect_image(
	photo_path: str | Path | bytes | None,
	catalog_products: list[dict[str, Any]] | None = None,
	photo_name: str | None = None,
	additional_images: list[tuple[bytes, str]] | None = None,
	model_override: str | None = None,
	force_structured: bool = False,
	capture_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Ask the local vision model for image quality and candidate SKU counts."""
	if not photo_path:
		return _empty_result("NOT_PROVIDED", "none", "No pack photo was supplied.")
	if isinstance(photo_path, bytes):
		image_path = None
		image_bytes = photo_path
		image_name = photo_name or "evidence.jpg"
		photo_descriptor = image_name
	else:
		image_path = Path(photo_path)
		if not image_path.is_file():
			return _empty_result("IMAGE_NOT_FOUND", "none", "The photo file was not found.", image_path)
		if image_path.stat().st_size > MAX_IMAGE_BYTES:
			return _empty_result("IMAGE_TOO_LARGE", "none", "The photo is larger than 8 MB.", image_path)
		image_bytes = image_path.read_bytes()
		image_name = image_path.name
		photo_descriptor = str(image_path)
	if len(image_bytes) > MAX_IMAGE_BYTES:
		return _empty_result("IMAGE_TOO_LARGE", "none", "The photo is larger than 8 MB.", photo_descriptor)
	if not _looks_like_supported_image(Path(image_name), image_bytes):
		return _empty_result("INVALID_IMAGE", "none", "The file is not a valid supported JPG, PNG, or WEBP image.", photo_descriptor)

	model = model_override or os.environ.get("PACKGUARD_VISION_MODEL", "gemma3:4b")
	base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
	moondream_mode = model.lower().startswith("moondream") and not force_structured
	if moondream_mode and len(catalog_products or []) != 1:
		return _empty_result(
			"MULTIPLE_CANDIDATES_UNSUPPORTED", f"ollama:{model}",
			"The fast local presence model currently supports one expected catalog SKU at a time; send this pack to human review.", image_path,
		)
	max_references = 0 if moondream_mode else max(0, int(os.environ.get("PACKGUARD_MAX_VISION_REFERENCES", "1")))
	products = catalog_products or []
	image_inputs = [base64.b64encode(_prepare_model_image(image_bytes)).decode("ascii")]
	image_labels = ["Image 1 is the open-box pack photo. It is the only image showing the current order."]
	for additional_bytes, additional_name in additional_images or []:
		if len(additional_bytes) > MAX_IMAGE_BYTES or not _looks_like_supported_image(Path(additional_name), additional_bytes):
			return _empty_result("INVALID_IMAGE", "none", "An additional capture image is invalid or too large.", additional_name)
		image_inputs.append(base64.b64encode(_prepare_model_image(additional_bytes)).decode("ascii"))
		image_labels.append(f"Image {len(image_inputs)} is an additional guided shot of the same open-box order.")
	candidates = []
	reference_count = 0
	for product in products:
		candidate = {
			"sku": product.get("sku"),
			"name": product.get("product_name"),
			"brand": product.get("brand"),
			"barcode": product.get("barcode"),
			"attributes": product.get("attributes", {}),
		}
		reference_path = product.get("reference_image_path")
		reference_bytes = product.get("reference_image_bytes")
		reference_name = product.get("reference_image_name")
		if reference_count < max_references:
			if isinstance(reference_bytes, bytes):
				reference_file = Path(reference_name or "reference.jpg")
			elif reference_path:
				reference_file = Path(reference_path)
				reference_bytes = reference_file.read_bytes() if reference_file.is_file() else None
			else:
				reference_file = None
			if (
				reference_file
				and reference_bytes
				and len(reference_bytes) <= MAX_IMAGE_BYTES
				and reference_file != image_path
				and _looks_like_supported_image(reference_file, reference_bytes)
			):
				image_inputs.append(base64.b64encode(_prepare_model_image(reference_bytes)).decode("ascii"))
				reference_count += 1
				candidate["reference_image_number"] = len(image_inputs)
				image_labels.append(
					f"Image {len(image_inputs)} is a catalog reference for {candidate['sku']} ({candidate['name']}), not a pack-order photo."
				)
		candidates.append(candidate)

	if moondream_mode:
		candidate = products[0]
		prompt = (
			"You inspect a photo of an open packing box. The expected catalog item is "
			f"{candidate.get('product_name') or candidate.get('sku')} (SKU {candidate.get('sku')}). "
			"Is this exact item visibly present in image 1? Reply with exactly one token: MATCH, NOT_PRESENT, or UNCERTAIN. "
			"Do not infer quantity. If identity is unclear, reply UNCERTAIN."
		)
	else:
		prompt = (
		"You are the visual inspection step of a packing-verification agent. Inspect image 1, the open box. "
		"Other images are catalog references only and must never be counted as packed items. "
		"For each catalog candidate, decide whether it is visibly present and count only clearly visible units. "
		"Use the exact candidate SKU only when the image supports the match; otherwise use null and explain uncertainty. "
		"Assess blur, lighting, framing, and occlusion. A single photo cannot verify items hidden under another item; mark occlusion PARTIAL or UNCERTAIN instead of guessing. Never guess. "
		"Return JSON only in this schema: "
		'{"image_quality":{"status":"GOOD|POOR|UNCERTAIN","score":0.0,"reason":""},'
		'"occlusion":{"status":"CLEAR|PARTIAL|UNCERTAIN","reason":""},'
		'"detected_items":[{"sku":"known catalog SKU or null","quantity":1,"confidence":0.0,"variant_match":true,"reason":""}],'
		'"extra_items":[{"description":"","sku":null,"confidence":0.0}],'
		'"uncertainties":["reason"],"overall_confidence":0.0}. '
		"All scores are model estimates, not calibrated probabilities.\n"
		f"Catalog candidates: {json.dumps(candidates, ensure_ascii=True)}\n"
		f"Capture checks and expected quantities: {json.dumps(capture_context or {}, ensure_ascii=True)}\n"
		+ "\n".join(image_labels)
		)
	payload = {
		"model": model,
		"messages": [{"role": "user", "content": prompt, "images": image_inputs}],
		"stream": False,
		"keep_alive": "5m",
		"options": {"temperature": 0, "num_predict": 24 if moondream_mode else 240, "num_ctx": 2048},
	}
	if not moondream_mode:
		payload["format"] = "json"
	request = Request(
		f"{base_url}/api/chat",
		data=json.dumps(payload).encode("utf-8"),
		headers={"Content-Type": "application/json"},
		method="POST",
	)
	inference_started = monotonic()
	try:
		with urlopen(request, timeout=float(os.environ.get("PACKGUARD_VISION_TIMEOUT", "90"))) as response:
			model_response = json.loads(response.read().decode("utf-8"))
	except HTTPError as error:
		status = "MODEL_NOT_INSTALLED" if error.code == 404 else "MODEL_ERROR"
		return _empty_result(status, f"ollama:{model}", f"Local vision request failed (HTTP {error.code}).", image_path)
	except TimeoutError:
		result = _empty_result("MODEL_TIMEOUT", f"ollama:{model}", "Local vision exceeded the configured time limit; send this package to human review.", image_path)
		result["inference_ms"] = round((monotonic() - inference_started) * 1000)
		return result
	except (URLError, OSError, json.JSONDecodeError):
		return _empty_result("MODEL_UNAVAILABLE", f"ollama:{model}", "The local vision model could not be reached.", image_path)

	content = model_response.get("message", {}).get("content", "")
	if moondream_mode:
		normalized = re.sub(r"[^A-Z_]", " ", str(content).upper()).split()
		answer = next((token for token in normalized if token in {"MATCH", "NOT_PRESENT", "UNCERTAIN"}), "UNCERTAIN")
		candidate_sku = products[0].get("sku")
		matched = answer == "MATCH"
		reason = (
			"The local model suggests the expected product is visible, but it did not verify quantity."
			if matched else "The local model could not confirm the expected product; absence is not proven."
		)
		return {
			"status": "SUGGESTIONS_READY_UNCALIBRATED",
			"detected_items": ([{"sku": candidate_sku, "quantity": None, "confidence": None, "variant_match": None, "reason": reason}] if matched else []),
			"extra_items": [],
			"uncertainties": [reason],
			"image_quality": {"status": "UNKNOWN", "score": None, "reason": "This fast presence check does not assess image quality."},
			"confidence": None,
			"provider": f"ollama:{model}",
			"calibration_status": "UNVALIDATED",
			"contract_version": VISION_CONTRACT_VERSION,
			"photo_path": photo_descriptor,
			"presence_hint": answer,
			"inference_ms": round((monotonic() - inference_started) * 1000),
			"token_usage": {
				"prompt": model_response.get("prompt_eval_count"),
				"completion": model_response.get("eval_count"),
			},
		}
	try:
		inspection = json.loads(content)
	except (TypeError, json.JSONDecodeError):
		return _empty_result("INVALID_MODEL_RESPONSE", f"ollama:{model}", "The vision model did not return structured JSON.", image_path)
	if (
		not isinstance(inspection, dict)
		or not isinstance(inspection.get("image_quality"), dict)
		or not isinstance(inspection.get("occlusion", {}), dict)
		or not isinstance(inspection.get("detected_items"), list)
		or not isinstance(inspection.get("extra_items"), list)
		or not isinstance(inspection.get("uncertainties"), list)
	):
		invalid_result = _empty_result("INVALID_MODEL_RESPONSE", f"ollama:{model}", "The vision model response was incomplete or did not match the required JSON schema.", image_path)
		invalid_result["model_response_excerpt"] = content[:500] if isinstance(content, str) else ""
		return invalid_result

	quality = inspection.get("image_quality")
	occlusion = inspection.get("occlusion", {})
	occlusion_status = str(occlusion.get("status", "UNCERTAIN")).upper()
	uncertainties = list(inspection.get("uncertainties", []))
	if occlusion_status in {"PARTIAL", "UNCERTAIN"}:
		uncertainties.append(str(occlusion.get("reason") or "Some items may be hidden by occlusion in the single capture."))
	return {
		"status": "SUGGESTIONS_READY_UNCALIBRATED",
		"detected_items": inspection.get("detected_items", []),
		"extra_items": inspection.get("extra_items", []),
		"uncertainties": uncertainties,
		"image_quality": {
			"status": str(quality.get("status", "UNCERTAIN")).upper(),
			"score": _bounded_score(quality.get("score")),
			"reason": str(quality.get("reason", "")),
		},
		"occlusion": {"status": occlusion_status, "reason": str(occlusion.get("reason", ""))},
		"confidence": _bounded_score(inspection.get("overall_confidence")),
		"provider": f"ollama:{model}",
		"calibration_status": "UNVALIDATED",
		"contract_version": VISION_CONTRACT_VERSION,
		"photo_path": photo_descriptor,
		"inference_ms": round((monotonic() - inference_started) * 1000),
		"token_usage": {
			"prompt": model_response.get("prompt_eval_count"),
			"completion": model_response.get("eval_count"),
		},
	}
