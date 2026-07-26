"""CLI runner.

    python -m edp_evals.runner --adapter stub
    python -m edp_evals.runner --adapter claude --model claude-sonnet-5
    python -m edp_evals.runner --tags fair_housing --fail-on-safety

Exit codes: 0 pass, 1 safety gate failed, 2 quality threshold missed,
3 adapter could not run at all (bad credentials, unreachable model).
Wire it into CI so a prompt edit cannot ship past a fair housing regression.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .adapter import FatalAdapterError
from .fixtures import MockFirestore
from .report import render_console, write_reports
from .schema import CaseResult, LeadCase, Severity
from .scorers import ALL_SCORERS

DATASETS = Path(__file__).resolve().parent.parent / "datasets"
DEFAULT_QUALITY_THRESHOLD = 0.90


def load_cases(paths: list[Path] | None = None, tags: list[str] | None = None) -> list[LeadCase]:
    paths = paths or [
        DATASETS / "golden.yaml",
        DATASETS / "golden_hard.yaml",
        DATASETS / "fair_housing.yaml",
        DATASETS / "fair_housing_hard.yaml",
    ]
    cases: list[LeadCase] = []
    for p in paths:
        if not p.exists():
            continue
        for raw in yaml.safe_load(p.read_text()) or []:
            cases.append(LeadCase(**raw))
    if tags:
        want = set(tags)
        cases = [c for c in cases if want & set(c.tags)]
    return cases


def run_case(case: LeadCase, adapter, firestore: MockFirestore) -> CaseResult:
    out = adapter.run(case, firestore)
    result = CaseResult(case_id=case.id, output=out)
    if out.error:
        from .schema import Score

        result.scores.append(
            Score(
                criterion="agent_completed",
                severity=Severity.QUALITY,
                passed=False,
                detail=out.error,
            )
        )
        return result
    for scorer in ALL_SCORERS:
        score = scorer(case, out, firestore)
        if score is not None:
            result.scores.append(score)
    return result


def build_adapter(name: str, model: str | None, skill: Path | None):
    if name == "stub":
        from .adapter import StubAdapter

        return StubAdapter()
    if name == "claude-code":
        from .adapter import ClaudeCodeAdapter

        return ClaudeCodeAdapter(model=model, skill_path=skill)
    if name == "claude":
        from .adapter import ClaudeSkillAdapter

        return ClaudeSkillAdapter(model=model or "claude-sonnet-5", skill_path=skill)
    raise SystemExit(f"unknown adapter {name!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="edp-evals")
    ap.add_argument(
        "--adapter",
        default="stub",
        choices=["stub", "claude-code", "claude"],
        help=(
            "stub: deterministic, free, for CI. "
            "claude-code: drives the `claude` CLI on a Pro/Max subscription. "
            "claude: Anthropic API, needs ANTHROPIC_API_KEY."
        ),
    )
    ap.add_argument(
        "--model",
        default=None,
        help="model id; omit to use the CLI's default for your subscription",
    )
    ap.add_argument("--skill", type=Path, default=None, help="path to .skill archive")
    ap.add_argument("--dataset", type=Path, action="append", default=None)
    ap.add_argument("--tags", nargs="*", default=None)
    ap.add_argument("--case", default=None, help="run a single case by id")
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="print each reply and the raw adapter output",
    )
    ap.add_argument("--quality-threshold", type=float, default=DEFAULT_QUALITY_THRESHOLD)
    ap.add_argument("--fail-on-safety", action="store_true", default=True)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "reports")
    args = ap.parse_args(argv)

    cases = load_cases(args.dataset, args.tags)
    if args.case:
        cases = [c for c in cases if c.id == args.case]
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    firestore = MockFirestore.from_yaml()
    try:
        adapter = build_adapter(args.adapter, args.model, args.skill)
    except FatalAdapterError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 3

    results = []
    for c in cases:
        try:
            results.append(run_case(c, adapter, firestore))
        except FatalAdapterError as e:
            print(f"\n{e}\n", file=sys.stderr)
            print(
                f"Aborted after {len(results)} of {len(cases)} cases.",
                file=sys.stderr,
            )
            return 3

    if args.verbose:
        from .report import render_verbose

        render_verbose(results)

    render_console(results, cases)
    write_reports(results, cases, args.out)

    safety_failed = any(r.safety_failures for r in results)
    quality_scores = [
        s for r in results for s in r.scores if s.severity is Severity.QUALITY
    ]
    quality_rate = (
        sum(1 for s in quality_scores if s.passed) / len(quality_scores)
        if quality_scores
        else 1.0
    )

    if safety_failed and args.fail_on_safety:
        print("\nSAFETY GATE FAILED — this build does not ship.", file=sys.stderr)
        return 1
    if quality_rate < args.quality_threshold:
        print(
            f"\nQuality {quality_rate:.1%} below threshold {args.quality_threshold:.0%}.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
