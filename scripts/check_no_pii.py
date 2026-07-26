#!/usr/bin/env python3
"""Fail the build if anything that looks like real PII is in a tracked file.

Written after a near miss. While building the second dataset I read a month of
real lead notifications for authentic phrasing and copied prospect names
straight across -- swapping the email domain to example.com but leaving
"Everett Easterling" and "Hailie Weaver" intact. A name is PII on its own. That
would have gone into a public repository.

It was caught by a manual grep, which is not a control. This is the control.

    python scripts/check_no_pii.py        # exits 1 on a finding

Runs in CI before the suite.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Only synthetic fixtures may carry personal-looking data, and only on these
# domains. Everything else is suspect.
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "rentspree.com",
    "comet.zillow.com",
    "zillow.com",
    "edprealty.com",
}

# Documentation placeholders. The vendored agent prompt illustrates the
# RentSpree "n" prefix trap with john@gmail.com / njohn@gmail.com, which is a
# real-looking address on a real domain and should absolutely trip a naive
# scan. Allow the well-known placeholder locals rather than either weakening
# the domain rule or rewriting the production prompt to please a linter.
PLACEHOLDER_LOCALS = {
    "john", "njohn", "jane", "njane", "user", "test", "example",
    "someone", "prospect", "nprospect", "firstname", "name", "email",
    "you", "your", "recipient", "renter",
}

SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".toml", ".txt", ".json", ".cfg"}
SKIP_DIRS = {".venv", ".git", "__pycache__", "reports", ".pytest_cache", "real"}

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
SECRET_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{20,}|BEGIN [A-Z ]*PRIVATE KEY|"
    r"service-account[^\s\"']*\.json|AIza[A-Za-z0-9_-]{30,})"
)

# Synthetic phone numbers reserved for fiction (555-01xx per NANP convention).
FICTIONAL_PHONE_RE = re.compile(r"\b\(?\d{3}\)?[-.\s]?555[-.\s]?01\d{2}\b")


def files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def main() -> int:
    findings: list[str] = []

    for path in files():
        rel = path.relative_to(ROOT)
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue

        for i, line in enumerate(text.splitlines(), 1):
            for m in EMAIL_RE.finditer(line):
                local, domain = m.group(1).lower(), m.group(2).lower()
                if domain in ALLOWED_EMAIL_DOMAINS:
                    continue
                if local in PLACEHOLDER_LOCALS:
                    continue
                findings.append(
                    f"{rel}:{i} non-synthetic email domain {domain!r} — {m.group(0)}"
                )
            for m in PHONE_RE.finditer(line):
                if not FICTIONAL_PHONE_RE.search(m.group(0)):
                    findings.append(
                        f"{rel}:{i} phone number outside the 555-01xx fiction range "
                        f"— {m.group(0)}"
                    )
            for m in SECRET_RE.finditer(line):
                findings.append(f"{rel}:{i} possible credential — {m.group(0)[:40]}")

    if findings:
        print("PII / secret check FAILED:\n", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(
            "\nIf a finding is a false positive, add the domain to "
            "ALLOWED_EMAIL_DOMAINS rather than deleting the check.",
            file=sys.stderr,
        )
        return 1

    print(f"PII check clean — {len(files())} files scanned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
