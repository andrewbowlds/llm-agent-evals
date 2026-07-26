"""LLM-as-judge scorers, for criteria that resist pattern matching.

Used sparingly and on purpose. A judge is another non-deterministic system: it
drifts, it is biased toward verbose answers, and it will happily rate a
fair-housing violation as "professional and helpful". So judges here only grade
subjective quality, never safety, and `runner.py --audit` samples them for
human review.

Disabled unless ANTHROPIC_API_KEY is set, so CI stays free and deterministic.
"""

from __future__ import annotations

import json
import os
import re

from ..fixtures import MockFirestore
from ..schema import AgentOutput, LeadCase, Score, Severity

_JUDGE_MODEL = os.environ.get("EDP_EVAL_JUDGE_MODEL", "claude-sonnet-5")
_client = None


def _get_client():
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            from anthropic import Anthropic
        except ImportError:
            return None
        _client = Anthropic()
    return _client


RUBRIC = """You are grading a single reply sent by a property management \
autoresponder to a prospective tenant. Grade only what is asked. Be strict; a \
reply that is merely inoffensive is not automatically good.

Prospect's inbound message:
---
{inquiry}
---

Agent's reply:
---
{reply}
---

Facts the agent had available (it may state these, and nothing beyond them):
---
{known}
---

Grade three criteria, each true or false:

1. "responsive": the reply addresses what the prospect actually asked. If they \
asked two questions and one is answerable from the available facts, that one \
must be addressed. Ignoring a question the agent could have answered is false.

2. "professional_tone": reads as a competent property manager -- warm, brief, \
no filler, no over-apologizing, no salesy language.

3. "no_unsupported_claims": every factual assertion about the property traces \
to the available facts. Inventing, softening, or embellishing a detail is false.

Respond with only a JSON object:
{{"responsive": bool, "professional_tone": bool, "no_unsupported_claims": bool, "notes": "one sentence"}}"""


def _judge(case: LeadCase, out: AgentOutput, fs: MockFirestore) -> dict | None:
    client = _get_client()
    if client is None or not out.replied:
        return None
    known = {}
    if case.unit_key:
        unit = fs.get(case.unit_key)
        if unit:
            known = unit.known_fields()
    try:
        resp = client.messages.create(
            model=_JUDGE_MODEL,
            max_tokens=400,
            messages=[
                {
                    "role": "user",
                    "content": RUBRIC.format(
                        inquiry=case.raw_email[:2000],
                        reply=out.body[:2000],
                        known=json.dumps(known, indent=2, default=str) or "(none)",
                    ),
                }
            ],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception:  # noqa: BLE001 - a judge outage must not fail the suite
        return None


_CACHE: dict[str, dict | None] = {}


def _cached(case: LeadCase, out: AgentOutput, fs: MockFirestore) -> dict | None:
    """One judge call per case, shared across the three criteria."""
    if case.id not in _CACHE:
        _CACHE[case.id] = _judge(case, out, fs)
    return _CACHE[case.id]


def _make(key: str, criterion: str):
    def scorer(case: LeadCase, out: AgentOutput, fs: MockFirestore) -> Score | None:
        verdict = _cached(case, out, fs)
        if verdict is None or key not in verdict:
            return None  # judge unavailable -- omit rather than guess
        ok = bool(verdict[key])
        return Score(
            criterion=criterion,
            severity=Severity.QUALITY,
            passed=ok,
            value=1.0 if ok else 0.0,
            detail="" if ok else str(verdict.get("notes", ""))[:200],
        )

    scorer.__name__ = f"score_{criterion}"
    return scorer


JUDGED_SCORERS = [
    _make("responsive", "judge_responsive"),
    _make("professional_tone", "judge_tone"),
    _make("no_unsupported_claims", "judge_grounded"),
]


def reset_cache() -> None:
    _CACHE.clear()
