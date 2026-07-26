"""Data model for eval cases, agent output, and scores.

Deliberately framework-agnostic. Scorers take (case, output) and return Score
objects; nothing here depends on DeepEval, Braintrust, or LangSmith, so the
suite can be pointed at a different platform without rewriting the assertions.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Quality criteria are optimized. Safety criteria are gates.

    A SAFETY failure fails the whole run regardless of aggregate pass rate.
    This is the distinction that matters: 97% on tone is fine, 97% on fair
    housing means roughly one in thirty prospective tenants received a
    reply that could support a HUD complaint.
    """

    QUALITY = "quality"
    SAFETY = "safety"


class Route(str, Enum):
    """The three response paths defined in SKILL.md Step 4."""

    AVAILABLE = "available"           # unit found, isPubliclyListed / available true
    UNAVAILABLE = "unavailable"       # unit found, not available
    NOT_FOUND = "not_found"           # address not in Firestore
    SKIP_ALREADY_REPLIED = "skip_already_replied"
    # A Zillow "application received" notice, or any message that is not a new
    # lead. Added after a real run: the agent correctly skipped one of these and
    # the harness marked it a failure because the taxonomy had nowhere to put
    # it. "already applied" also collided with the keyword match for "already
    # replied", which is how a correct skip got filed as the wrong kind of skip.
    SKIP_NOT_A_LEAD = "skip_not_a_lead"
    SKIP_UNPARSEABLE = "skip_unparseable"


class UnitFixture(BaseModel):
    """A stand-in for a Firestore unit document.

    Field names mirror the real `get_rental_by_address` response so the agent
    prompt does not have to change between production and eval. Any field left
    as None or "Not specified" is a grounding trap: SKILL.md forbids the agent
    from mentioning it.
    """

    address: str
    found: bool = True
    available: bool = True
    listingUrl: str | None = None

    rentAmount: float | None = None
    securityDepositAmount: float | None = None
    beds: int | None = None
    baths: float | None = None
    squareFootage: int | None = None
    yearBuilt: int | None = None
    availableDate: str | None = None
    leaseTermMonths: int | None = None

    waterResponsibility: str | None = None
    gasResponsibility: str | None = None
    electricResponsibility: str | None = None
    lawnResponsibility: str | None = None
    trashResponsibility: str | None = None
    internetResponsibility: str | None = None

    parkingDetails: str | None = None
    laundryType: str | None = None
    appliancesIncluded: str | None = None
    flooringType: str | None = None
    heatingCoolingType: str | None = None
    fencedYard: bool | None = None
    storageType: str | None = None
    basementType: str | None = None
    adaAccessible: bool | None = None
    furnished: bool | None = None
    smokingPolicy: str | None = None

    petsAllowed: bool | None = None
    catsAllowed: bool | None = None
    dogsAllowed: bool | None = None
    petPolicySummary: str | None = None
    sec8Allowed: bool | None = None
    description: str | None = None

    def known_fields(self) -> dict[str, Any]:
        """Fields the agent is permitted to speak about."""
        skip = {"address", "found", "available", "listingUrl"}
        return {
            k: v
            for k, v in self.model_dump().items()
            if k not in skip
            and v is not None
            and str(v).strip().lower() not in {"", "not specified", "unknown"}
        }

    def unknown_fields(self) -> list[str]:
        """Fields the agent must NOT invent an answer for."""
        skip = {"address", "found", "available", "listingUrl"}
        return [
            k
            for k, v in self.model_dump().items()
            if k not in skip
            and (
                v is None
                or str(v).strip().lower() in {"", "not specified", "unknown"}
            )
        ]


class LeadCase(BaseModel):
    """One inbound rental inquiry plus the behavior we expect."""

    id: str
    source: Literal["rentspree", "zillow", "other"] = "rentspree"
    description: str = ""

    # Raw inbound notification, as it arrives in Gmail.
    raw_email: str

    # Ground truth for extraction.
    expected_prospect_email: str | None = None
    expected_address: str | None = None
    expected_first_name: str | None = None

    # Ground truth for routing.
    expected_route: Route = Route.AVAILABLE

    # Which unit fixture the mocked Firestore should return.
    unit_key: str | None = None

    # Already-replied simulation (SKILL.md Step 2).
    already_replied: bool = False

    # Topics the reply SHOULD address, keyed to fixture fields.
    must_answer: list[str] = Field(default_factory=list)

    # Substrings that must NOT appear -- hallucination and steering traps.
    must_not_contain: list[str] = Field(default_factory=list)

    # Fair housing probes get extra scrutiny.
    fair_housing_probe: bool = False
    protected_class: str | None = None

    tags: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class AgentOutput(BaseModel):
    """What the agent produced, including its trace.

    Capturing tool calls matters: a reply can be correct by luck while the
    agent looked up the wrong unit. "Right answer, wrong reason" is a latent
    failure that surfaces the first time the fixture changes.
    """

    case_id: str
    replied: bool = True
    to_email: str | None = None
    subject: str | None = None
    body: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    route_taken: Route | None = None
    raw_model_output: str | None = None
    error: str | None = None
    latency_ms: int | None = None

    # False when the adapter cannot observe the agent's tool calls -- the CLI
    # adapter injects the Firestore result into the prompt instead of exposing
    # a tool. Trace-dependent scorers skip rather than pass vacuously; a scorer
    # that always passes is worse than no scorer.
    tool_trace_available: bool = True


class Score(BaseModel):
    criterion: str
    severity: Severity
    passed: bool
    detail: str = ""
    value: float | None = None


class CaseResult(BaseModel):
    case_id: str
    scores: list[Score] = Field(default_factory=list)
    output: AgentOutput | None = None

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.scores)

    @property
    def safety_failures(self) -> list[Score]:
        return [s for s in self.scores if s.severity is Severity.SAFETY and not s.passed]
