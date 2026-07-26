"""Adapters that run the agent under test.

The production autoresponder is a Cowork skill: Claude reads `SKILL.md`, calls
the edp-firestore and Gmail MCP tools, and drafts a reply. To evaluate it
programmatically the harness reconstructs that loop -- same prompt, same tool
surface, mocked backends.

Critically, `ClaudeSkillAdapter` loads the prompt straight out of the deployed
`rental-lead-autoresponder.skill` archive rather than a vendored copy. Editing
the real skill changes what the suite tests, which is the only arrangement
where a green run means anything.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Protocol

from .fixtures import MockFirestore
from .schema import AgentOutput, LeadCase, Route

PKG_ROOT = Path(__file__).resolve().parent.parent

# Where to look for the agent prompt, in order of preference:
#   1. --skill, if given
#   2. the deployed .skill archive in a sibling monorepo checkout, so a run
#      inside the private repo always tests the live prompt
#   3. the vendored copy in agent/, so this repo stands alone
#
# Preferring the archive matters: if the suite quietly falls back to a stale
# vendored copy, a green run stops being evidence about production.
CANDIDATE_SKILLS = [
    PKG_ROOT.parent / "rental-lead-autoresponder.skill",
    PKG_ROOT / "rental-lead-autoresponder.skill",
    PKG_ROOT / "agent" / "SKILL.md",
]


def load_skill_prompt(skill_path: Path | None = None) -> str:
    """Load the agent prompt under test.

    Accepts a .skill archive, an unpacked skill directory, or a bare SKILL.md.
    """
    candidates = [skill_path] if skill_path else CANDIDATE_SKILLS
    for cand in candidates:
        if cand is None or not cand.exists():
            continue
        if cand.is_dir():
            return (cand / "SKILL.md").read_text()
        if cand.suffix == ".md":
            return cand.read_text()
        with zipfile.ZipFile(cand) as z:
            name = next(n for n in z.namelist() if n.endswith("SKILL.md"))
            return z.read(name).decode("utf-8")

    tried = "\n  ".join(str(c) for c in candidates if c)
    raise FileNotFoundError(
        f"No agent prompt found. Looked in:\n  {tried}\n"
        "Pass --skill to point at a .skill archive, a skill directory, or a "
        "SKILL.md."
    )


FIRESTORE_TOOL = {
    "name": "get_rental_by_address",
    "description": (
        "Look up a rental unit by street address. Returns found=false if the "
        "address is not under management."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Street address of the unit"}
        },
        "required": ["address"],
    },
}

CREATE_DRAFT_TOOL = {
    "name": "create_draft",
    "description": "Create the reply draft to the prospect.",
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
    },
}

SKIP_TOOL = {
    "name": "skip_lead",
    "description": (
        "Skip this lead without replying. Use when the prospect was already "
        "replied to, or the notification cannot be parsed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
}


class FatalAdapterError(RuntimeError):
    """Unrecoverable, and identical for every remaining case.

    Bad credentials, revoked key, exhausted quota. Retrying 20 more cases
    produces 20 more copies of the same message and 20 more billable
    round-trips, so the runner aborts on this instead of scoring it as a
    per-case failure.
    """


class Adapter(Protocol):
    def run(self, case: LeadCase, firestore: MockFirestore) -> AgentOutput: ...


class ClaudeSkillAdapter:
    """Runs the real SKILL.md prompt through an agent loop with mocked tools."""

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        skill_path: Path | None = None,
        max_turns: int = 6,
    ):
        try:
            from anthropic import Anthropic
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install anthropic") from e

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise FatalAdapterError(
                "ANTHROPIC_API_KEY is not set.\n"
                "  Get a key at https://console.anthropic.com/settings/keys\n"
                "  then: export ANTHROPIC_API_KEY=sk-ant-<your-key>"
            )
        # Catch the pasted-the-placeholder case before spending a round-trip.
        if "..." in key or len(key) < 40:
            raise FatalAdapterError(
                f"ANTHROPIC_API_KEY looks like a placeholder, not a key "
                f"(got {key[:12]!r}, length {len(key)}).\n"
                "  Real keys start with 'sk-ant-' and run ~100 characters.\n"
                "  Get one at https://console.anthropic.com/settings/keys"
            )
        self._client = Anthropic()
        self._model = model
        self._prompt = load_skill_prompt(skill_path)
        self._max_turns = max_turns

    def run(self, case: LeadCase, firestore: MockFirestore) -> AgentOutput:
        from .schema import ToolCall

        firestore.reset()
        started = time.time()
        out = AgentOutput(case_id=case.id)

        system = (
            self._prompt
            + "\n\n---\n\n## Eval harness context\n\n"
            "You are processing exactly one lead notification, shown below. "
            "Use get_rental_by_address to look up the unit. Then either call "
            "create_draft with your reply, or skip_lead if SKILL.md says to "
            "skip. Do not ask clarifying questions."
        )
        if case.already_replied:
            system += (
                "\n\nThe sent-mail check in Step 2 found an existing reply to "
                "this prospect."
            )
        else:
            system += "\n\nThe sent-mail check in Step 2 found no prior reply."

        messages: list[dict] = [
            {"role": "user", "content": f"Lead notification:\n\n{case.raw_email}"}
        ]
        tools = [FIRESTORE_TOOL, CREATE_DRAFT_TOOL, SKIP_TOOL]

        try:
            for _ in range(self._max_turns):
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=2000,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": resp.content})
                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if not tool_uses:
                    out.raw_model_output = "".join(
                        b.text for b in resp.content if b.type == "text"
                    )
                    break

                results = []
                for tu in tool_uses:
                    if tu.name == "get_rental_by_address":
                        r = firestore.get_rental_by_address(tu.input.get("address", ""))
                        out.tool_calls.append(
                            ToolCall(name=tu.name, arguments=dict(tu.input), result=r)
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": json.dumps(r, default=str),
                            }
                        )
                    elif tu.name == "create_draft":
                        out.replied = True
                        out.to_email = tu.input.get("to")
                        out.subject = tu.input.get("subject")
                        out.body = tu.input.get("body", "")
                        out.tool_calls.append(
                            ToolCall(name=tu.name, arguments=dict(tu.input))
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": "draft created",
                            }
                        )
                    elif tu.name == "skip_lead":
                        out.replied = False
                        out.tool_calls.append(
                            ToolCall(name=tu.name, arguments=dict(tu.input))
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": "skipped",
                            }
                        )
                if any(t.name in ("create_draft", "skip_lead") for t in out.tool_calls):
                    break
                messages.append({"role": "user", "content": results})
        except Exception as e:  # noqa: BLE001 - surface, don't crash the suite
            name = type(e).__name__
            if name in {
                "AuthenticationError",
                "PermissionDeniedError",
                "NotFoundError",
            } or getattr(e, "status_code", None) in {401, 403, 404}:
                raise FatalAdapterError(
                    f"{name}: {e}\n\n"
                    "  Every remaining case would fail identically, so the run "
                    "was aborted.\n"
                    "  Check ANTHROPIC_API_KEY, and that --model names a model "
                    "your key can reach."
                ) from e
            out.error = f"{name}: {e}"

        out.latency_ms = int((time.time() - started) * 1000)
        out.route_taken = infer_route(out)
        return out


UNIT_LINK_RE = re.compile(
    r"(?:rentals\.|www\.)?edprealty\.com/rentals/[A-Za-z0-9][A-Za-z0-9_-]{3,}",
    re.IGNORECASE,
)


def classify_skip(reason: str) -> Route:
    """Sort a skip into the right kind of skip.

    Order matters. "already applied" and "already replied" both contain
    "already", and the first version checked that word first -- so a correct
    skip of a Zillow application notice was filed as a duplicate-reply skip and
    scored as a routing failure. Test the specific signal before the generic one.
    """
    r = (reason or "").lower()

    not_a_lead = (
        "application",
        "applied",
        "applicant",
        "screening report",
        "further in the funnel",
        "not a lead",
        "not a new lead",
        "submitted documents",
    )
    already_replied = (
        "already replied",
        "already responded",
        "prior reply",
        "previous reply",
        "we replied",
        "sent mail",
        "duplicate notification",
    )

    if any(k in r for k in not_a_lead):
        return Route.SKIP_NOT_A_LEAD
    if any(k in r for k in already_replied):
        return Route.SKIP_ALREADY_REPLIED
    # Bare "already"/"replied" is ambiguous but most often means a duplicate.
    if "repl" in r or "already" in r:
        return Route.SKIP_ALREADY_REPLIED
    return Route.SKIP_UNPARSEABLE


def infer_route(out: AgentOutput) -> Route:
    """Classify which of SKILL.md's three response paths the agent took.

    Decided from the reply body, not from the tool trace, so the same logic
    works whether the adapter exposed a callable tool or injected the record
    into the prompt. The three paths are distinguishable by their artifacts:
    a unit-specific prescreen link (available), an explicit unavailability
    statement (unavailable), or the generic rentals index (not found).
    """
    if not out.replied:
        skip = next((t for t in out.tool_calls if t.name == "skip_lead"), None)
        return classify_skip(skip.arguments.get("reason", "") if skip else "")

    body = out.body or ""
    low = body.lower()

    # "No longer available" wins outright -- it is only ever said on that path,
    # and a stray unit link alongside it is itself the bug we want flagged.
    if "no longer available" in low or "is not available" in low:
        return Route.UNAVAILABLE
    if UNIT_LINK_RE.search(body):
        return Route.AVAILABLE
    if "edprealty.com/rentals" in low:
        return Route.NOT_FOUND

    # No link at all: fall back to the tool trace if the adapter has one.
    lookup = next(
        (t for t in out.tool_calls if t.name == "get_rental_by_address"), None
    )
    if lookup and isinstance(lookup.result, dict):
        if not lookup.result.get("found"):
            return Route.NOT_FOUND
        if not lookup.result.get("available"):
            return Route.UNAVAILABLE
    return Route.NOT_FOUND


CLI_TASK = """\
Below is a single inbound rental lead notification, plus the exact Firestore \
record returned by get_rental_by_address for that property. Apply the skill \
instructions above and decide what reply to send.

Respond with ONLY a JSON object, no prose, no code fence:

{{"action": "draft", "to": "<prospect email>", "subject": "<subject>", "body": "<full reply body>"}}

or, if the skill says to skip this lead:

{{"action": "skip", "reason": "<why>"}}

--- LEAD NOTIFICATION ---
{email}

--- get_rental_by_address RESULT ---
{firestore}

--- SENT-MAIL CHECK (Step 2) ---
{replied}
"""


class ClaudeCodeAdapter:
    """Runs the agent through the Claude Code CLI, on a Pro/Max subscription.

    Uses `claude -p` rather than the Anthropic API, so it bills against an
    existing Claude subscription instead of API credits. ANTHROPIC_API_KEY is
    explicitly stripped from the subprocess environment -- if it is set it
    takes precedence over the keychain credentials and defeats the purpose.

    Tradeoff, stated plainly: this adapter injects the Firestore result into
    the prompt instead of exposing a callable tool, so there is no tool trace.
    `unit_resolution` is therefore not scored under this adapter -- it returns
    None rather than passing vacuously. Every routing, grounding, and fair
    housing scorer still applies, which is the part that matters.

    Requires `claude` on PATH and a completed `claude login`.
    """

    def __init__(
        self,
        model: str | None = None,
        skill_path: Path | None = None,
        timeout: int = 120,
    ):
        import shutil

        if shutil.which("claude") is None:
            raise FatalAdapterError(
                "The `claude` CLI is not on PATH.\n"
                "  Install: https://code.claude.com/docs\n"
                "  Then run `claude login` to authenticate with your subscription."
            )
        self._model = model
        self._prompt = load_skill_prompt(skill_path)
        self._timeout = timeout
        self._timeouts = 0

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # An API key beats keychain credentials; drop it so the subscription
        # is what actually gets used.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def run(self, case: LeadCase, firestore: MockFirestore) -> AgentOutput:
        import subprocess
        import tempfile

        firestore.reset()
        started = time.time()
        out = AgentOutput(case_id=case.id, tool_trace_available=False)

        record = firestore.get_rental_by_address(case.expected_address or "")
        task = CLI_TASK.format(
            email=case.raw_email,
            firestore=json.dumps(record, indent=2, default=str),
            replied=(
                "An existing reply to this prospect was found."
                if case.already_replied
                else "No prior reply to this prospect was found."
            ),
        )

        cmd = ["claude", "-p", task, "--output-format", "json", "--max-turns", "1"]
        if self._model:
            cmd += ["--model", self._model]
        cmd += ["--append-system-prompt", self._prompt]

        try:
            # Run from an empty cwd so the repo's own .claude config, skills,
            # and MCP servers don't load into the eval.
            with tempfile.TemporaryDirectory() as cwd:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=self._env(),
                    cwd=cwd,
                    # Closed stdin: an unauthenticated or prompting CLI must
                    # fail immediately rather than block the whole suite
                    # waiting for input that will never arrive.
                    stdin=subprocess.DEVNULL,
                )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                low = err.lower()
                if any(
                    k in low
                    for k in ("not logged in", "authenticate", "unauthorized", "login")
                ):
                    raise FatalAdapterError(
                        f"Claude Code is not authenticated: {err[:300]}\n"
                        "  Run `claude login` and retry."
                    )
                out.error = f"claude exited {proc.returncode}: {err[:300]}"
                out.latency_ms = int((time.time() - started) * 1000)
                return out

            text = _extract_cli_text(proc.stdout)
            out.raw_model_output = text
            _apply_decision(out, text, case)
        except FatalAdapterError:
            raise
        except subprocess.TimeoutExpired:
            self._timeouts += 1
            if self._timeouts >= 2:
                raise FatalAdapterError(
                    f"`claude` timed out after {self._timeout}s on two cases.\n"
                    "  Aborting rather than stalling the whole suite.\n"
                    "  Check `claude login`, then try: claude -p 'say hi'"
                ) from None
            out.error = f"claude timed out after {self._timeout}s"
        except Exception as e:  # noqa: BLE001
            out.error = f"{type(e).__name__}: {e}"

        out.latency_ms = int((time.time() - started) * 1000)
        if out.route_taken is None:
            out.route_taken = infer_route(out)
        return out


def _extract_cli_text(stdout: str) -> str:
    """Pull the assistant's final text out of `claude -p --output-format json`.

    The envelope has changed across releases, so try the known shapes in order
    and fall back to the raw stdout rather than throwing. `--output-format json`
    is a convenience, not a contract.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # stream-json: newline-delimited events. Take the last text we see.
        texts = []
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            texts.extend(_walk_for_text(ev))
        return texts[-1] if texts else stdout

    if isinstance(data, str):
        return data
    # Current shape: {"type":"result","result":"<text>",...}
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        return data["result"]
    found = _walk_for_text(data)
    return found[-1] if found else stdout


def _walk_for_text(node) -> list[str]:
    """Collect assistant text blocks from an arbitrarily-shaped envelope."""
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            out.append(node["text"])
        if isinstance(node.get("result"), str):
            out.append(node["result"])
        for v in node.values():
            out.extend(_walk_for_text(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk_for_text(v))
    return out


def _apply_decision(out: AgentOutput, text: str, case: LeadCase) -> None:
    """Parse the model's JSON decision into the AgentOutput."""
    from .schema import ToolCall

    blob = text.strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```[a-zA-Z]*\n?", "", blob)
        blob = re.sub(r"\n?```\s*$", "", blob)
    m = re.search(r"\{.*\}", blob, re.DOTALL)
    if not m:
        out.error = f"no JSON decision in output: {text[:200]!r}"
        return
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        out.error = f"unparseable decision JSON: {e}"
        return

    if str(d.get("action", "")).lower() == "skip":
        out.replied = False
        reason = str(d.get("reason", ""))
        out.tool_calls.append(ToolCall(name="skip_lead", arguments={"reason": reason}))
        out.route_taken = classify_skip(reason)
        return

    out.replied = True
    out.to_email = d.get("to")
    out.subject = d.get("subject")
    out.body = d.get("body", "") or ""
    out.tool_calls.append(
        ToolCall(
            name="create_draft",
            arguments={"to": out.to_email, "subject": out.subject, "body": out.body},
        )
    )


class StubAdapter:
    """Deterministic fake agent.

    Exists so the harness itself can be tested -- and CI can run -- without an
    API key or token spend. It implements the happy path only; several scorers
    are expected to fail against it. That is the intended behavior: a scorer
    that cannot fail is not a scorer.
    """

    def __init__(self, behavior: str = "reasonable"):
        self.behavior = behavior

    def run(self, case: LeadCase, firestore: MockFirestore) -> AgentOutput:
        from .schema import ToolCall

        firestore.reset()
        out = AgentOutput(case_id=case.id)

        if case.already_replied:
            out.replied = False
            out.tool_calls.append(
                ToolCall(name="skip_lead", arguments={"reason": "already replied"})
            )
            out.route_taken = Route.SKIP_ALREADY_REPLIED
            return out

        addr = case.expected_address or ""
        res = firestore.get_rental_by_address(addr)
        out.tool_calls.append(
            ToolCall(name="get_rental_by_address", arguments={"address": addr}, result=res)
        )

        name = f", {case.expected_first_name}" if case.expected_first_name else ""
        if not res.get("found"):
            body = (
                f"Hello{name},\n\nThank you for your interest in {addr}! You can view our "
                "available rentals and start a pre-screen here: "
                "https://edprealty.com/rentals\n\nAndrew"
            )
        elif not res.get("available"):
            body = (
                f"Hello{name},\n\nThank you for your interest in {addr}. Unfortunately this "
                "unit is no longer available. You can view our other available rentals at: "
                "https://edprealty.com/rentals\n\nAndrew"
            )
        else:
            lines = []
            for topic in case.must_answer:
                val = res.get(topic)
                if val is not None and str(val).strip().lower() not in {
                    "", "not specified", "unknown"
                }:
                    lines.append(f"- {topic}: {val}")
            qa = ("To answer your questions:\n" + "\n".join(lines) + "\n\n") if lines else ""
            body = (
                f"Hello{name},\n\nThank you for your interest in {addr}!\n\n{qa}"
                f"To get started, complete our quick pre-screen here: {res.get('listingUrl')}\n\n"
                "If you qualify, you'll be able to self-schedule a tour right away. After your "
                "tour, if you'd like to move forward, the next step is a rental application "
                "-- $40 per adult applicant.\n\nAndrew"
            )

        out.to_email = case.expected_prospect_email
        out.subject = f"Re: [NEW LEAD] I'm interested in {addr}."
        out.body = body
        out.route_taken = infer_route(out)
        return out
