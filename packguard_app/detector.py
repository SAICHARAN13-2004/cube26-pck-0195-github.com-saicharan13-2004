"""Observation adapter boundary.

This MVP uses the operator's structured observation. A camera/model adapter
can replace ``observe`` while preserving the verifier's contract.
"""

from typing import Any


def observe(observed_in_box: str | None) -> dict[str, Any]:
	"""Return the captured observation and its provenance."""
	value = (observed_in_box or "").strip()
	return {
		"observed_in_box": value,
		"source": "operator_structured_observation" if value else "missing",
		"confidence": None,
	}


def observe_image(photo_ref: str | None) -> dict[str, Any]:
	"""Describe an uploaded image until a vision model supplies observations."""
	return {
		"photo_ref": photo_ref,
		"source": "image_pending_vision" if photo_ref else "missing",
		"confidence": None,
		"observed_in_box": "",
	}
