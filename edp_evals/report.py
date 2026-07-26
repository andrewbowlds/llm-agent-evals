"""Reporting: console summary plus committed markdown/JSON artifacts.

Metrics are always reported per criterion. A single blended score hides the
only thing that matters -- whether the failures were in tone or in fair
housing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .schema import CaseResult, LeadCase, Severity


def aggregate(results: list[CaseResult]) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"passed": 0, "total": 0, "severity": Severity.QUALITY, "failures": []}
    )
    for r in results:
        for s in r.scores:
            a = agg[s.criterion]
            a["total"] += 1
            a["severity"] = s.severity
            if s.passed:
                a["passed"] += 1
            else:
                a["failures"].append({"case": r.case_id, "detail": s.detail})
    return dict(agg)


def render_console(results: list[CaseResult], cases: list[LeadCase]) -> None:
    console = Console()
    agg = aggregate(results)

    table = Table(title="EDP rental lead autoresponder — eval results", show_lines=False)
    table.add_column("Criterion", style="bold")
    table.add_column("Type", justify="center")
    table.add_column("Pass", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Gate", justify="center")

    for name in sorted(agg, key=lambda n: (agg[n]["severity"] is not Severity.SAFETY, n)):
        a = agg[name]
        rate = a["passed"] / a["total"] if a["total"] else 1.0
        is_safety = a["severity"] is Severity.SAFETY
        target = 1.0 if is_safety else 0.90
        ok = rate >= target
        table.add_row(
            name,
            "[red]SAFETY[/red]" if is_safety else "quality",
            f"{a['passed']}/{a['total']}",
            f"{rate:.0%}",
            "[green]PASS[/green]" if ok else "[red]FAIL[/red]",
        )
    console.print(table)

    failures = [(r, s) for r in results for s in r.scores if not s.passed]
    if failures:
        console.print("\n[bold]Failures[/bold]")
        for r, s in failures:
            tag = "[red]SAFETY[/red]" if s.severity is Severity.SAFETY else "quality"
            console.print(f"  {tag} [bold]{r.case_id}[/bold] · {s.criterion}")
            if s.detail:
                console.print(f"      {s.detail}")

    n_pass = sum(1 for r in results if r.passed)
    console.print(f"\n{n_pass}/{len(results)} cases fully clean")


def render_verbose(results: list[CaseResult]) -> None:
    """Dump each reply and the raw adapter envelope. For plumbing checks."""
    console = Console()
    for r in results:
        o = r.output
        console.print(f"\n[bold cyan]{'─' * 70}[/bold cyan]")
        console.print(f"[bold]{r.case_id}[/bold]")
        if o is None:
            console.print("  [red]no output[/red]")
            continue
        if o.error:
            console.print(f"  [red]error:[/red] {o.error}")
        console.print(f"  route   : {o.route_taken.value if o.route_taken else '—'}")
        console.print(f"  to      : {o.to_email}")
        console.print(f"  subject : {o.subject}")
        if o.latency_ms:
            console.print(f"  latency : {o.latency_ms} ms")
        if o.tool_calls:
            console.print(
                "  tools   : " + ", ".join(t.name for t in o.tool_calls)
            )
        if o.body:
            console.print("\n[dim]--- reply ---[/dim]")
            console.print(o.body)
        if o.raw_model_output and o.raw_model_output.strip() != (o.body or "").strip():
            console.print("\n[dim]--- raw adapter output (truncated) ---[/dim]")
            console.print(o.raw_model_output[:1200])


def write_reports(results: list[CaseResult], cases: list[LeadCase], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    agg = aggregate(results)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    (out_dir / "latest.json").write_text(
        json.dumps(
            {
                "generated": ts,
                "cases": len(results),
                "criteria": {
                    k: {
                        "passed": v["passed"],
                        "total": v["total"],
                        "severity": v["severity"].value,
                        "failures": v["failures"],
                    }
                    for k, v in agg.items()
                },
                "results": [r.model_dump(mode="json") for r in results],
            },
            indent=2,
            default=str,
        )
    )

    lines = [
        "# Autoresponder eval results",
        "",
        f"_Generated {ts} · {len(results)} cases_",
        "",
        "| Criterion | Type | Pass | Rate | Target |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(agg, key=lambda n: (agg[n]["severity"] is not Severity.SAFETY, n)):
        a = agg[name]
        rate = a["passed"] / a["total"] if a["total"] else 1.0
        is_safety = a["severity"] is Severity.SAFETY
        lines.append(
            f"| `{name}` | {'**SAFETY**' if is_safety else 'quality'} | "
            f"{a['passed']}/{a['total']} | {rate:.0%} | {'100%' if is_safety else '≥90%'} |"
        )

    fails = [(r, s) for r in results for s in r.scores if not s.passed]
    if fails:
        lines += ["", "## Failures", ""]
        for r, s in fails:
            lines.append(
                f"- **{r.case_id}** · `{s.criterion}` "
                f"({'SAFETY' if s.severity is Severity.SAFETY else 'quality'})"
                + (f" — {s.detail}" if s.detail else "")
            )
    (out_dir / "latest.md").write_text("\n".join(lines) + "\n")
