from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Fallback used if pricing.yaml is missing or malformed, so cost tracking
# degrades to an estimate rather than crashing the request path.
_FALLBACK = {
    "currency": "USD",
    "default": {"input_per_1m": 1.00, "output_per_1m": 3.00},
    "providers": {},
}


class CostService:
    """Centralized cost calculation. The ONLY place provider/model prices live.

    Reads a data-driven pricing table (app/config/pricing.yaml). New providers
    or models are added by editing that file — no call sites change. Unknown
    (provider, model) pairs fall back to the table's `default` rate.
    """

    def __init__(self, pricing_file: Path) -> None:
        self._pricing_file = pricing_file
        self._table: dict[str, Any] = self._load(pricing_file)

    @staticmethod
    def _load(pricing_file: Path) -> dict[str, Any]:
        try:
            with pricing_file.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            if not isinstance(data, dict) or "default" not in data:
                raise ValueError("pricing.yaml missing required 'default' section")
            return data
        except Exception as exc:  # noqa: BLE001 - never let pricing break a request
            logger.warning("Falling back to built-in pricing (%s): %s", pricing_file, exc)
            return dict(_FALLBACK)

    @property
    def currency(self) -> str:
        return str(self._table.get("currency", "USD"))

    def _rate(self, provider: str, model: str) -> tuple[float, float]:
        providers = self._table.get("providers", {}) or {}
        entry = (providers.get(provider, {}) or {}).get(model)
        if not isinstance(entry, dict):
            entry = self._table.get("default", {}) or {}
        return (
            float(entry.get("input_per_1m", 0.0) or 0.0),
            float(entry.get("output_per_1m", 0.0) or 0.0),
        )

    def estimate(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        in_rate, out_rate = self._rate(provider, model)
        cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
        return round(cost, 6)

    def price_table(self) -> dict[str, Any]:
        return self._table

    def estimate_bulk(
        self,
        *,
        count: int,
        provider: str,
        model: str,
        avg_input_per_email: float,
        avg_output_per_email: float,
    ) -> dict[str, Any]:
        """Estimate the workload + cost of a bulk run of `count` emails.

        Averages are supplied by the caller (historical, with sane defaults),
        keeping all price math in this one service.
        """
        est_input = int(round(count * avg_input_per_email))
        est_output = int(round(count * avg_output_per_email))
        est_cost = self.estimate(provider, model, est_input, est_output)
        return {
            "emails_selected": count,
            "est_input_tokens": est_input,
            "est_output_tokens": est_output,
            "est_cost": est_cost,
            "currency": self.currency,
        }
