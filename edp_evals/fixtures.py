"""Mocked Firestore layer.

The production agent calls `get_rental_by_address` over MCP. Evaluating against
live Firestore would be slow, non-deterministic, and would mutate real records,
so the harness serves the same shape from a fixture file instead.

Keeping the response shape identical to the MCP tool is the point: the agent
prompt is unchanged between production and eval, so what passes here is
evidence about what runs at 7am.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import UnitFixture

DATASETS = Path(__file__).resolve().parent.parent / "datasets"


class MockFirestore:
    """Serves unit lookups from a YAML fixture, and records what was asked."""

    def __init__(self, units: dict[str, UnitFixture]):
        self._units = units
        # Only units that exist under management are addressable. A fixture with
        # found:false models an address we do not manage, so it must miss the
        # lookup entirely rather than resolve to an unavailable unit.
        self._by_address = {
            self._normalize(u.address): u
            for u in units.values()
            if u.address and u.found
        }
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "MockFirestore":
        path = path or DATASETS / "units.yaml"
        raw = yaml.safe_load(path.read_text()) or {}
        units = {k: UnitFixture(**v) for k, v in raw.items()}
        return cls(units)

    @staticmethod
    def _normalize(address: str) -> str:
        """Loose match so 'W Oregon St' and 'West Oregon Street' collide.

        Real inbound addresses are inconsistent -- Zillow, RentSpree, and the
        prospect all format them differently. The agent should still resolve.
        """
        a = address.lower().strip().rstrip(".")
        for long, short in (
            (" street", " st"), (" avenue", " ave"), (" drive", " dr"),
            (" road", " rd"), (" lane", " ln"), (" court", " ct"),
            (" west ", " w "), (" east ", " e "),
            (" north ", " n "), (" south ", " s "),
        ):
            a = a.replace(long, short)
        a = a.replace(",", " ")
        # Drop trailing city/state/zip noise.
        for noise in ("evansville", " in ", " indiana"):
            a = a.replace(noise, " ")
        return " ".join(a.split())

    def get(self, key: str) -> UnitFixture | None:
        return self._units.get(key)

    def get_rental_by_address(self, address: str) -> dict[str, Any]:
        """Mirror of the edp-firestore MCP tool."""
        self.calls.append({"tool": "get_rental_by_address", "address": address})
        unit = self._by_address.get(self._normalize(address or ""))
        if unit is None:
            return {"found": False, "address": address}
        payload = unit.model_dump()
        payload["found"] = True
        return payload

    def reset(self) -> None:
        self.calls.clear()


def load_units(path: Path | None = None) -> dict[str, UnitFixture]:
    path = path or DATASETS / "units.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    return {k: UnitFixture(**v) for k, v in raw.items()}
