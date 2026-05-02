"""AST lint: ``with session_scope()``/``with db_session()`` must not contain ``await``.

Background — see ``docs/tickets/0008-reply-pipeline-tx-across-await.md``.

Holding a SQLite write transaction across an LLM API ``await`` (5–15s
end-to-end) wedges the asyncio event loop and starves every other request,
because the LLM client's ``_log_call`` audit-row writer has to wait the full
``PRAGMA busy_timeout`` for the parent transaction to commit. Ticket 0008
restructured ``process_incoming_message`` into a phased "read snapshot →
await LLM → write outcome" shape; this test pins the no-await-in-session
invariant so future changes can't regress.

The rule applies to **every** session opened in async code, not just the
one in ``reply.py``. ``with db_session() as session:`` (the FastAPI shim
in :mod:`autosdr.api.deps`) is the same context manager as
``with session_scope() as session:`` — both names are walked.

There are three pre-existing violations the ticket explicitly defers as
out-of-scope follow-ups (``scheduler.py``, ``api/threads.py``,
``pipeline/followup.py``). They are listed in :data:`_KNOWN_VIOLATIONS`
with the rationale; with Part B (``_log_call`` on the loop executor) and
Part C (``busy_timeout`` tightened to 5s), they no longer cause the
2-minute deadlock the ticket was written for — at worst they impose a 5s
pause on the audit row write. A follow-up ticket ports them to the
phased pattern.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_AUTOSDR_ROOT = _REPO_ROOT / "autosdr"

# Both names refer to the same context manager — see :mod:`autosdr.api.deps`.
_SESSION_NAMES: frozenset[str] = frozenset({"session_scope", "db_session"})


# Files that hold a session across ``await`` deliberately, with a short
# rationale so a future reader can see why the rule was relaxed for them.
# Each entry is the path *relative to ``autosdr/``*. The map is the
# allowlist's documentation: any new file in this map should also have a
# follow-up ticket linked to it.
#
# WARNING: do NOT add to this map without filing a follow-up ticket. The
# whole point of the lint is to prevent new violations from creeping in.
_KNOWN_VIOLATIONS: dict[str, str] = {
    "scheduler.py": (
        "_run_campaign_tick / _poll_inbound_once hold a session across the "
        "outreach pipeline's LLM calls. Same root cause as ticket 0008's "
        "reply.py path — port to the phased pattern in a follow-up."
    ),
    "api/threads.py": (
        "send_draft holds a session across the connector send. The "
        "atomic-message-with-state-flip semantics are load-bearing for "
        "the HITL approve flow; restructuring deserves its own ticket."
    ),
    "pipeline/followup.py": (
        "_run_followup holds a session across connector.send so the "
        "Message row + status flip commit together. Follow-up beat "
        "fires on a single thread at a time with delays, so contention "
        "is rare; restructuring deserves its own ticket."
    ),
    "api/campaigns.py": (
        "kickoff_campaign holds a session across run_campaign_outreach_batch. "
        "Operator-initiated path (button click), bounded by the requested "
        "count. Inherits the scheduler.py phased-pattern follow-up."
    ),
    "api/leads.py": (
        "post_enrich_batch holds a session across enrich_lead() per "
        "candidate. Operator-initiated warm-up path; the scan worker "
        "(autosdr/pipeline/scans.py) already does this correctly with "
        "the phased pattern and is the production data path. Restructuring "
        "this ad-hoc REST handler is a follow-up."
    ),
    "api/scans.py": (
        "run_scans single-lead branch holds a session across "
        "scan_one_lead(). Manual operator re-scan (one lead at a time, "
        "low frequency); the batch scan worker uses the phased pattern."
    ),
}


def _iter_python_sources() -> list[Path]:
    """Every ``.py`` file under ``autosdr/`` (sorted, deterministic order)."""

    return sorted(p for p in _AUTOSDR_ROOT.rglob("*.py") if p.is_file())


def _is_session_call(node: ast.expr) -> bool:
    """True if ``node`` is a Call to ``session_scope()`` / ``db_session()``."""

    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _SESSION_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in _SESSION_NAMES
    return False


def _has_await_in_subtree(body: list[ast.stmt]) -> bool:
    """True if any ``Await`` node exists anywhere in ``body``.

    Walks **across** nested function definitions too — a
    closure-defined coroutine that's awaited inside the session
    counts. Skips ``async with`` / ``async for`` because those
    introduce their own awaits via the protocol; we only flag the
    explicit ``Await`` form which is what the deadlock pattern uses.
    """

    for stmt in body:
        for child in ast.walk(stmt):
            if isinstance(child, ast.Await):
                return True
    return False


def _violations_in_file(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, snippet)`` for every offending ``with`` block.

    The snippet is the first line of the offending ``with`` statement,
    so failure messages point at the exact source location.
    """

    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    lines = text.splitlines()
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        opens_session = any(
            _is_session_call(item.context_expr) for item in node.items
        )
        if not opens_session:
            continue
        if not _has_await_in_subtree(node.body):
            continue
        snippet = lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else ""
        found.append((node.lineno, snippet))
    return found


def test_known_violations_still_violate() -> None:
    """The allowlist must not drift out of sync with reality.

    If a file on :data:`_KNOWN_VIOLATIONS` has been refactored to be
    compliant, drop it from the map — leaving stale entries silently
    weakens the lint over time.
    """

    for rel_path in _KNOWN_VIOLATIONS:
        full = _AUTOSDR_ROOT / rel_path
        assert full.exists(), f"allowlisted file no longer exists: {rel_path}"
        violations = _violations_in_file(full)
        assert violations, (
            f"{rel_path} is on the no-await-in-session allowlist but the file "
            "no longer holds a session across an await — please drop it from "
            "_KNOWN_VIOLATIONS."
        )


@pytest.mark.parametrize(
    "source_path",
    [pytest.param(p, id=str(p.relative_to(_REPO_ROOT))) for p in _iter_python_sources()],
)
def test_no_await_inside_session_scope(source_path: Path) -> None:
    """No ``with session_scope()`` / ``with db_session()`` may contain ``await``.

    Walks every ``.py`` file under ``autosdr/`` and fails on any ``with``
    statement that opens a DB session and contains an ``Await`` node in
    its subtree. Allowlisted files (see :data:`_KNOWN_VIOLATIONS`) are
    skipped — but ``test_known_violations_still_violate`` ensures the
    allowlist itself stays honest.
    """

    rel = str(source_path.relative_to(_AUTOSDR_ROOT))
    if rel in _KNOWN_VIOLATIONS:
        pytest.skip(f"allowlisted: {_KNOWN_VIOLATIONS[rel]}")

    violations = _violations_in_file(source_path)
    if not violations:
        return

    # Build a precise multi-line failure message so the operator sees
    # the file:line of every offending block, not just a count.
    lines = "\n".join(
        f"  {source_path.relative_to(_REPO_ROOT)}:{lineno}: {snippet}"
        for lineno, snippet in violations
    )
    pytest.fail(
        "found `await` inside `with session_scope()` / `with db_session()`:\n"
        + lines
        + "\n\nThe rule: a session is a SQLite write transaction; holding it "
        "across an `await` to a remote service stalls the audit-log writer "
        "for `busy_timeout` seconds and starves the asyncio event loop. "
        "Restructure into the phased read-snapshot → await → write-outcome "
        "pattern (see autosdr/pipeline/reply.py::process_incoming_message)."
    )
