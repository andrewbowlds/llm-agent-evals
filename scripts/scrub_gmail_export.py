#!/usr/bin/env python3
"""Turn real Gmail lead notifications into an anonymized eval dataset.

The golden set is only as good as its realism, and synthetic inquiries do not
capture how people actually write. But real inquiries carry names, emails,
phone numbers, and sometimes disability disclosures -- none of which belong in
a git repository, least of all a public one.

This script is the bridge: it consumes a raw export, replaces every identifier
with a stable synthetic one, and emits YAML cases with expectations
pre-filled for a human to review.

    python scripts/scrub_gmail_export.py raw.json -o datasets/real/golden.real.yaml

Output goes under datasets/real/, which .gitignore excludes. Nothing here
writes to a tracked path.

Input format: a JSON list of {"id", "subject", "from", "body"}.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

FIRST_NAMES = [
    "Marcus", "Dana", "Priya", "Tanisha", "Kevin", "Sam", "Ellen", "Robert",
    "Alicia", "Gina", "Julia", "Doug", "Renee", "Aaron", "Michelle", "Terrence",
    "Hana", "Brianna", "Chris", "Nadia", "Omar", "Beth", "Luis", "Karen",
]
LAST_NAMES = [
    "Webb", "Holloway", "Raman", "Brooks", "Ortiz", "Whitfield", "Kwan",
    "Feldman", "Grant", "Petrov", "Sorenson", "Ramsey", "Castillo", "Delgado",
    "Barr", "Ford", "Yusuf", "Cole", "Almeida", "Nkemdi", "Haddad", "Lindqvist",
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
NAME_FIELD_RE = re.compile(r"^\s*(Name|Full Name|Renter)\s*:\s*(.+)$", re.MULTILINE)

PRESERVE_DOMAINS = {"rentspree.com", "comet.zillow.com", "zillow.com", "edprealty.com"}


def _stable(value: str, pool: list[str]) -> str:
    h = int(hashlib.sha256(value.lower().encode()).hexdigest()[:8], 16)
    return pool[h % len(pool)]


class Anonymizer:
    """Stable pseudonyms: the same real person maps to the same fake person."""

    def __init__(self) -> None:
        self.emails: dict[str, str] = {}
        self.names: dict[str, str] = {}

    def email(self, real: str) -> str:
        real_l = real.lower()
        domain = real_l.split("@")[-1]
        if domain in PRESERVE_DOMAINS:
            return real_l
        if real_l not in self.emails:
            first = _stable(real_l, FIRST_NAMES)
            last = _stable(real_l[::-1], LAST_NAMES)
            self.emails[real_l] = f"{first.lower()}{last.lower()}@example.com"
        return self.emails[real_l]

    def name(self, real: str) -> str:
        key = real.strip().lower()
        if key not in self.names:
            first = _stable(key, FIRST_NAMES)
            last = _stable(key[::-1], LAST_NAMES)
            self.names[key] = f"{first} {last}"
        return self.names[key]

    def scrub(self, text: str) -> str:
        # Names first, so the email replacement doesn't disturb the mapping.
        for m in NAME_FIELD_RE.finditer(text):
            text = text.replace(m.group(2).strip(), self.name(m.group(2)))
        text = EMAIL_RE.sub(lambda m: self.email(m.group(0)), text)
        text = PHONE_RE.sub("(812) 555-0100", text)
        return text


def infer_source(sender: str) -> str:
    s = (sender or "").lower()
    if "rentspree" in s:
        return "rentspree"
    if "zillow" in s:
        return "zillow"
    return "other"


def extract_address(text: str) -> str | None:
    for pat in (
        r"Property\s*:\s*(.+)",
        r"interested in\s+([^.\n]+?)\s*[.\n]",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="raw Gmail export (JSON list)")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    if "datasets/real" not in str(args.output).replace("\\", "/") and not str(
        args.output
    ).endswith(".real.yaml"):
        print(
            "Refusing to write outside datasets/real/ or a *.real.yaml path.\n"
            "Anonymized real data is still real data and must stay untracked.",
            file=sys.stderr,
        )
        return 2

    raw = json.loads(args.input.read_text())
    anon = Anonymizer()
    cases = []

    for i, msg in enumerate(raw[: args.limit]):
        body = anon.scrub(msg.get("body", ""))
        subject = anon.scrub(msg.get("subject", ""))
        sender = msg.get("from", "")
        emails = [
            e
            for e in EMAIL_RE.findall(body)
            if e.split("@")[-1].lower() not in PRESERVE_DOMAINS
        ]
        # RentSpree prints the prefixed copy first; the real one is the shorter.
        prospect = min(emails, key=len) if emails else None
        address = extract_address(body)

        cases.append(
            {
                "id": f"real_{i:03d}_{msg.get('id', '')[:8]}",
                "source": infer_source(sender),
                "raw_email": f"Subject: {subject}\nFrom: {sender}\n\n{body}",
                "expected_prospect_email": prospect,
                "expected_address": address,
                "expected_route": "available",   # REVIEW: verify against Firestore
                "unit_key": None,                # REVIEW: map to a fixture
                "must_answer": [],               # REVIEW: what did they ask?
                "tags": ["real", "needs_review"],
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "# AUTO-GENERATED from a real Gmail export, then anonymized.\n"
        "# Every case is marked needs_review: expected_route, unit_key and\n"
        "# must_answer are guesses and must be confirmed by a human before use.\n"
        "# This file is gitignored. Do not move it into a tracked path.\n\n"
        + yaml.safe_dump(cases, sort_keys=False, allow_unicode=True, width=100)
    )
    print(f"wrote {len(cases)} cases → {args.output}")
    print(f"unique prospects pseudonymized: {len(anon.emails)}")
    print("\nNext: review each case, set unit_key and expected_route, drop needs_review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
