"""Deterministic fulfillment-channel routing for the PackGuard prototype."""


def decide_fulfillment_route(channel: str) -> dict[str, str]:
	normalized = channel.strip().casefold().replace("-", "_").replace(" ", "_")
	aliases = {
		"fba": "fba",
		"amazon_fba": "fba",
		"mfn": "mfn",
		"amazon_mfn": "mfn",
		"merchant_fulfilled": "mfn",
		"3pl": "3pl",
		"third_party_logistics": "3pl",
	}
	fulfillment = aliases.get(normalized)
	if fulfillment is None:
		raise ValueError("Fulfillment channel must be FBA, MFN, or 3PL.")

	if fulfillment == "fba":
		next_agent, skipped_agent = "prep", "pack"
	else:
		next_agent, skipped_agent = "pack", "prep"

	return {
		"fulfillment_channel": fulfillment.upper(),
		"next_agent": next_agent,
		"skipped_agent": skipped_agent,
	}
