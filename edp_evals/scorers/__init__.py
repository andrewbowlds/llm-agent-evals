"""Scorers grade one (case, output) pair and return Score objects.

Four families, in descending order of trustworthiness:

- deterministic  -- string and structural assertions drawn from SKILL.md.
                    Cheap, exact, no model in the loop. Prefer these wherever
                    the criterion can be expressed without judgment.
- fair_housing   -- safety gates for the protected classes. Mostly contextual
                    pattern matching.
- compliance     -- obligations that come from the property or from the limits
                    of the agent's authority, not from the conversation:
                    lead-based paint disclosure, pricing, screening criteria.
- judged         -- LLM-as-judge for genuinely subjective criteria (tone,
                    responsiveness). Last resort, quality only, spot-checked.
"""

from .compliance import COMPLIANCE_SCORERS
from .deterministic import DETERMINISTIC_SCORERS
from .fair_housing import FAIR_HOUSING_SCORERS
from .judged import JUDGED_SCORERS

ALL_SCORERS = (
    DETERMINISTIC_SCORERS + FAIR_HOUSING_SCORERS + COMPLIANCE_SCORERS + JUDGED_SCORERS
)

__all__ = [
    "ALL_SCORERS",
    "COMPLIANCE_SCORERS",
    "DETERMINISTIC_SCORERS",
    "FAIR_HOUSING_SCORERS",
    "JUDGED_SCORERS",
]
