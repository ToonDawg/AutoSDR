"""Typer-based CLI. Installed as the ``autosdr`` console script.

Scope (after the cleanup PR):

* ``autosdr run`` — boots the FastAPI app (UI + API + scheduler + poller).
* ``autosdr pause`` / ``resume`` / ``stop`` — kill-switch ergonomics.
* ``autosdr status`` — quick process health.
* ``autosdr import <file>`` — CSV / NDJSON lead import.
* ``autosdr sim inbound`` — drive the reply pipeline with a fake inbound.
* ``autosdr logs llm`` / ``logs thread`` — audit LLM call history.

Everything else (create the workspace, create a campaign, assign leads)
lives in the UI — the setup wizard handles first-run and the Campaigns /
Leads pages handle ongoing CRUD. Keeping the CLI surface small means there
is exactly one way to do each thing.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from autosdr import killswitch
from autosdr.config import get_settings
from autosdr.connectors import get_connector as _get_connector
from autosdr.connectors.base import ConnectorError, OutgoingMessage
from autosdr.db import create_all, session_scope
from autosdr.llm import get_usage_snapshot
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignStatus,
    Lead,
    LlmCall,
    Message,
    Thread,
    Workspace,
)
from autosdr.quota import count_ai_messages_last_24h

app = typer.Typer(help="AutoSDR POC — autonomous SDR for small business owners.")
test_app = typer.Typer(help="Connectivity and health tests.")
sim_app = typer.Typer(help="Simulate inbound messages (for testing).")
logs_app = typer.Typer(help="Inspect LLM call and thread transcripts.")
leads_app = typer.Typer(help="Lead-level operations (manual opt-out, etc.).")

app.add_typer(test_app, name="test")
app.add_typer(sim_app, name="sim")
app.add_typer(logs_app, name="logs")
app.add_typer(leads_app, name="leads")

console = Console()


def _configure_logging() -> None:
    """Configure console + rotating-file logging.

    Console output goes to stderr at INFO (override with ``AUTOSDR_LOG_LEVEL``).
    A rotating file handler mirrors everything to ``<LOG_DIR>/autosdr.log`` so
    background tick output survives terminal clears and can be grepped later.
    """

    level = os.environ.get("AUTOSDR_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        log_dir: Path = get_settings().log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "autosdr.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:  # pragma: no cover - logging must never abort startup
        root.warning("failed to attach file log handler")


def _get_connector_or_exit():
    """Wrap :func:`autosdr.connectors.get_connector` with a friendly CLI error."""

    try:
        return _get_connector()
    except ConnectorError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(
            "[dim]tip: complete the setup wizard at /setup once the server "
            "is running, or edit workspace settings via PATCH /api/workspace/settings[/dim]"
        )
        raise typer.Exit(code=2) from exc


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@app.command("import")
def import_leads(
    path: Path = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Import leads from a CSV or NDJSON file."""

    _configure_logging()
    from autosdr.importer import import_file

    create_all()
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            console.print(
                "[red]no workspace — complete the setup wizard at /setup first[/red]"
            )
            raise typer.Exit(code=1)

        region_hint = (workspace.settings or {}).get("default_region", "AU")
        summary = import_file(
            session=session,
            workspace_id=workspace.id,
            path=path,
            region_hint=region_hint,
        )

    console.print(
        f"[green]import complete[/green] rows={summary.row_count} "
        f"imported={summary.imported_count} skipped={summary.skipped_count} "
        f"errors={summary.error_count}"
    )
    if summary.errors:
        tbl = Table(title="First 20 issues", show_edge=False)
        tbl.add_column("row")
        tbl.add_column("reason")
        for entry in summary.errors[:20]:
            tbl.add_row(str(entry.get("row", "?")), str(entry.get("reason", "")))
        console.print(tbl)


# ---------------------------------------------------------------------------
# run / pause / resume / stop / status
# ---------------------------------------------------------------------------


@app.command()
def run(
    host: Optional[str] = typer.Option(
        None, "--host", help="Bind host. Defaults to settings.host (127.0.0.1)."
    ),
    port: Optional[int] = typer.Option(
        None, "--port", help="Bind port. Defaults to settings.port (8000)."
    ),
) -> None:
    """Start the FastAPI server (UI + API + scheduler). Ctrl+C to stop.

    All connector / LLM / rehearsal config comes from ``workspace.settings``
    — if you haven't set up a workspace yet, open the browser at the bound
    address and finish the setup wizard. The scheduler will no-op until
    that's done.
    """

    _configure_logging()
    create_all()
    env = get_settings()

    import uvicorn

    killswitch.write_pid_file()
    atexit.register(killswitch.clear_pid_file)

    def _cleanup_on_signal(signum: int, _frame) -> None:
        killswitch.clear_pid_file()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _cleanup_on_signal)

    bind_host = host or env.host
    bind_port = int(port or env.port)
    console.print(
        f"[green]autosdr[/green] starting  pid={os.getpid()}  "
        f"http://{bind_host}:{bind_port}/"
    )

    try:
        uvicorn.run(
            "autosdr.webhook:app",
            host=bind_host,
            port=bind_port,
            log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"),
        )
    finally:
        killswitch.clear_pid_file()


@app.command()
def pause() -> None:
    """Create the pause flag — halts processing within 1 second."""

    path = killswitch.touch_flag()
    console.print(f"[yellow]paused[/yellow] (flag file at {path})")


@app.command()
def resume() -> None:
    """Remove the pause flag — next scheduler tick resumes processing."""

    if killswitch.remove_flag():
        console.print("[green]resumed[/green]")
    else:
        console.print("[yellow]not paused[/yellow]")


@app.command()
def stop() -> None:
    """Send SIGTERM to the running process (if any)."""

    pid = killswitch.read_pid_file()
    if pid is None:
        console.print("[yellow]no PID file — is `autosdr run` active?[/yellow]")
        raise typer.Exit(code=1)
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]SIGTERM sent[/green] to pid={pid}")
    except ProcessLookupError:
        console.print(f"[yellow]no process with pid={pid}[/yellow]; cleaning up PID file")
        killswitch.clear_pid_file()


@app.command()
def status() -> None:
    """Show pause state, LLM usage, and campaign quota progress."""

    create_all()

    paused = killswitch.is_flag_set()
    pid = killswitch.read_pid_file()
    console.print(
        f"paused={'yes' if paused else 'no'}  pid={pid if pid else 'n/a'}"
    )
    usage = get_usage_snapshot()
    console.print(
        f"LLM: calls={usage['total_calls']} "
        f"tokens_in={usage['total_tokens_in']} tokens_out={usage['total_tokens_out']}"
    )
    if usage["per_model"]:
        sub = Table(title="LLM per-model usage", show_edge=False)
        sub.add_column("model")
        sub.add_column("calls")
        sub.add_column("tokens_in")
        sub.add_column("tokens_out")
        for model_name, data in usage["per_model"].items():
            sub.add_row(
                model_name,
                str(data["calls"]),
                str(data["tokens_in"]),
                str(data["tokens_out"]),
            )
        console.print(sub)

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            console.print(
                "[yellow]no workspace — complete the setup wizard at /setup[/yellow]"
            )
            return

        connector_cfg = (workspace.settings or {}).get("connector") or {}
        console.print(
            f"connector=[cyan]{connector_cfg.get('type', 'file')}[/cyan]  "
            f"auto_reply_enabled={(workspace.settings or {}).get('auto_reply_enabled', False)}"
        )

        active = (
            session.query(Campaign)
            .filter(Campaign.status == CampaignStatus.ACTIVE)
            .all()
        )
        if not active:
            console.print("[yellow]no active campaigns[/yellow]")
            return

        tbl = Table(title="Active campaigns (last 24h)")
        tbl.add_column("name")
        tbl.add_column("per_day")
        tbl.add_column("sent_24h")
        tbl.add_column("remaining")
        for c in active:
            sent_24h = count_ai_messages_last_24h(session, c.id)
            remaining = max(0, c.outreach_per_day - sent_24h)
            tbl.add_row(c.name, str(c.outreach_per_day), str(sent_24h), str(remaining))
        console.print(tbl)


# ---------------------------------------------------------------------------
# test / sim
# ---------------------------------------------------------------------------


@test_app.command("sms")
def test_sms(
    to: str = typer.Option(..., "--to", help="E.164 phone number to send a test SMS to."),
    content: str = typer.Option(
        "AutoSDR test message — ignore.", "--content"
    ),
) -> None:
    """Send a test SMS via the configured connector.

    Uses whatever is in ``workspace.settings`` — if you want to sandbox the
    test, switch the connector type to ``file`` (writes to the outbox) or
    set ``rehearsal.override_to`` in the UI first so the send is redirected
    to a phone you own.
    """

    _configure_logging()
    create_all()
    connector = _get_connector_or_exit()

    async def _run():
        ok, detail = await connector.validate_config()
        if not ok:
            console.print(f"[red]connector validation failed[/red]: {detail}")
            raise typer.Exit(code=1)
        console.print(f"[dim]{detail}[/dim]")
        result = await connector.send(OutgoingMessage(contact_uri=to, content=content))
        if result.success:
            console.print(
                f"[green]sent[/green] provider_message_id={result.provider_message_id}"
            )
        else:
            console.print(f"[red]send failed[/red]: {result.error}")
            raise typer.Exit(code=1)

    asyncio.run(_run())


@sim_app.command("inbound")
def sim_inbound(
    contact_uri: str = typer.Option(..., "--from"),
    content: str = typer.Option(..., "--content"),
) -> None:
    """Drive the reply pipeline with a simulated inbound message."""

    _configure_logging()
    create_all()
    connector = _get_connector_or_exit()

    async def _run() -> None:
        incoming = connector.parse_webhook(
            {"contact_uri": contact_uri, "content": content, "from": contact_uri, "text": content}
        )
        with session_scope() as session:
            workspace = session.query(Workspace).first()
            if workspace is None:
                console.print(
                    "[red]no workspace — complete the setup wizard first[/red]"
                )
                raise typer.Exit(code=1)
            workspace_id = workspace.id

        from autosdr.pipeline import process_incoming_message

        result = await process_incoming_message(
            connector=connector,
            workspace_id=workspace_id,
            incoming=incoming,
        )
        console.print(
            f"[green]action={result.action}[/green] "
            f"intent={result.intent} confidence={result.confidence} "
            f"thread={result.thread_id} detail={result.detail}"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# logs (LLM call + thread transcript inspectors)
# ---------------------------------------------------------------------------


_PURPOSE_STYLE = {
    "analysis": "magenta",
    "generation": "cyan",
    "evaluation": "yellow",
    "classification": "green",
    "other": "white",
}


def _shorten(text: str | None, n: int) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "\u2026"


@logs_app.command("llm")
def logs_llm(
    tail: int = typer.Option(
        20, "--tail", min=1, max=500, help="How many recent LLM calls to show."
    ),
    thread: Optional[str] = typer.Option(
        None, "--thread", help="Filter to a single thread id."
    ),
    lead: Optional[str] = typer.Option(
        None, "--lead", help="Filter to a single lead id."
    ),
    campaign: Optional[str] = typer.Option(
        None, "--campaign", help="Filter to a single campaign id."
    ),
    purpose: Optional[str] = typer.Option(
        None,
        "--purpose",
        help="Filter by purpose: analysis | generation | evaluation | classification.",
    ),
    errors_only: bool = typer.Option(
        False, "--errors", help="Only show failed calls."
    ),
    show_prompts: bool = typer.Option(
        False, "--show-prompts", help="Print full system/user prompts and response."
    ),
) -> None:
    """Browse the LLM call log."""

    create_all()
    with session_scope() as session:
        stmt = select(LlmCall).order_by(LlmCall.created_at.desc()).limit(tail)
        if thread:
            stmt = stmt.where(LlmCall.thread_id == thread)
        if lead:
            stmt = stmt.where(LlmCall.lead_id == lead)
        if campaign:
            stmt = stmt.where(LlmCall.campaign_id == campaign)
        if purpose:
            stmt = stmt.where(LlmCall.purpose == purpose)
        if errors_only:
            stmt = stmt.where(LlmCall.error.is_not(None))

        rows = list(session.execute(stmt).scalars())

    if not rows:
        console.print("[yellow]no LLM calls matched[/yellow]")
        return

    if show_prompts:
        for row in reversed(rows):
            style = _PURPOSE_STYLE.get(row.purpose, "white")
            console.rule(
                f"[{style}]{row.purpose}[/{style}] "
                f"{row.created_at.isoformat()}  model={row.model}  attempt={row.attempt}  "
                f"tokens_in={row.tokens_in} tokens_out={row.tokens_out} "
                f"latency={row.latency_ms}ms"
            )
            if row.error:
                console.print(f"[red]error:[/red] {row.error}")
            console.print(
                f"[dim]thread={row.thread_id}  lead={row.lead_id}  "
                f"campaign={row.campaign_id}  prompt_version={row.prompt_version}[/dim]"
            )
            if row.system_prompt:
                console.print("[bold]system[/bold]")
                console.print(row.system_prompt)
            if row.user_prompt:
                console.print("[bold]user[/bold]")
                console.print(row.user_prompt)
            if row.response_text:
                console.print("[bold]response[/bold]")
                console.print(row.response_text)
            if row.response_parsed is not None:
                console.print("[bold]parsed[/bold]")
                console.print(row.response_parsed)
        return

    tbl = Table(title=f"Last {len(rows)} LLM calls", show_edge=False)
    tbl.add_column("time")
    tbl.add_column("purpose")
    tbl.add_column("model")
    tbl.add_column("att", justify="right")
    tbl.add_column("ms", justify="right")
    tbl.add_column("tokens i/o", justify="right")
    tbl.add_column("thread", overflow="fold")
    tbl.add_column("snippet")
    for row in reversed(rows):
        style = _PURPOSE_STYLE.get(row.purpose, "white")
        if row.error:
            snippet = f"[red]{_shorten(row.error, 80)}[/red]"
        else:
            snippet = _shorten(row.response_text, 80)
        tbl.add_row(
            row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            f"[{style}]{row.purpose}[/{style}]",
            row.model,
            str(row.attempt),
            str(row.latency_ms),
            f"{row.tokens_in}/{row.tokens_out}",
            (row.thread_id or "")[-8:],
            snippet,
        )
    console.print(tbl)


@logs_app.command("thread")
def logs_thread(
    thread_id: str = typer.Argument(..., help="Thread id (or trailing 8 chars)."),
    show_prompts: bool = typer.Option(
        False, "--show-prompts", help="Print full LLM system/user prompts and responses."
    ),
) -> None:
    """Render a thread as a chronological transcript: messages + LLM calls."""

    create_all()
    with session_scope() as session:
        thread = _resolve_thread_by_id(session, thread_id)
        if thread is None:
            console.print(f"[red]thread not found: {thread_id}[/red]")
            raise typer.Exit(code=1)

        campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
        campaign = session.get(Campaign, campaign_lead.campaign_id) if campaign_lead else None
        lead = session.get(Lead, campaign_lead.lead_id) if campaign_lead else None

        console.rule(f"thread {thread.id}")
        console.print(
            f"status=[cyan]{thread.status}[/cyan]  "
            f"angle={thread.angle!r}  auto_replies={thread.auto_reply_count}"
        )
        if thread.hitl_reason:
            console.print(f"[yellow]hitl_reason={thread.hitl_reason}[/yellow]")
        console.print(
            f"lead={lead.name!r} ({lead.contact_uri})  campaign={campaign.name!r}"
            if lead and campaign
            else ""
        )

        messages = (
            session.query(Message)
            .filter(Message.thread_id == thread.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        llm_calls = (
            session.query(LlmCall)
            .filter(LlmCall.thread_id == thread.id)
            .order_by(LlmCall.created_at.asc())
            .all()
        )

        events = [
            ("msg", m.created_at, m) for m in messages
        ] + [
            ("llm", c.created_at, c) for c in llm_calls
        ]
        events.sort(key=lambda e: e[1])

        if not events:
            console.print("[dim](empty thread)[/dim]")
            return

        for kind, ts, obj in events:
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            if kind == "msg":
                role = obj.role
                colour = {"ai": "cyan", "human": "green", "lead": "magenta"}.get(role, "white")
                console.print(f"[dim]{stamp}[/dim] [{colour}]{role:>5}[/{colour}]  {obj.content}")
                continue
            style = _PURPOSE_STYLE.get(obj.purpose, "white")
            header = (
                f"[dim]{stamp}[/dim] [{style}]{obj.purpose:>13}[/{style}]  "
                f"model={obj.model} attempt={obj.attempt} "
                f"tokens={obj.tokens_in}/{obj.tokens_out} {obj.latency_ms}ms"
            )
            if obj.error:
                console.print(f"{header}  [red]error={obj.error}[/red]")
                continue
            if show_prompts:
                console.print(header)
                if obj.system_prompt:
                    console.print("  [bold]system[/bold]")
                    console.print("  " + obj.system_prompt.replace("\n", "\n  "))
                if obj.user_prompt:
                    console.print("  [bold]user[/bold]")
                    console.print("  " + obj.user_prompt.replace("\n", "\n  "))
                if obj.response_text:
                    console.print("  [bold]response[/bold]")
                    console.print("  " + obj.response_text.replace("\n", "\n  "))
            else:
                snippet = _shorten(obj.response_text, 100)
                console.print(f"{header}  {snippet}")


# ---------------------------------------------------------------------------
# leads (manual operations)
# ---------------------------------------------------------------------------


@leads_app.command("opt-out")
def leads_opt_out(
    contact_uri: str = typer.Argument(
        ..., help="Phone number (E.164 or local format) to flag as do-not-contact."
    ),
    reason: str = typer.Option(
        "manual",
        "--reason",
        help=(
            "Stable machine-readable reason stored on the lead. Defaults to 'manual'. "
            "Use this to carry context (e.g. 'manual:phoned-in')."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt. Required for non-interactive scripts.",
    ),
) -> None:
    """Manually flag a lead as do-not-contact.

    Covers the case where a lead phones / emails / replies on a different
    channel to opt out — i.e. anything that doesn't go through the SMS
    reply pipeline's deterministic STOP-keyword shortcut.

    Setting the flag is idempotent: re-running on an already-flagged lead
    is a no-op (and prints the existing flag for confirmation).

    Future outreach is blocked by the same `do_not_contact_at` guard the
    outbound pipeline already enforces, and the lead is excluded from
    `assign_leads` for any subsequent campaigns.
    """

    from datetime import datetime, timezone
    from autosdr.importer import normalise_phone

    _configure_logging()
    create_all()

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            console.print(
                "[red]no workspace — complete the setup wizard at /setup first[/red]"
            )
            raise typer.Exit(code=1)

        region_hint = (workspace.settings or {}).get("default_region", "AU")

        candidates = list(
            session.execute(
                select(Lead).where(
                    Lead.workspace_id == workspace.id,
                    Lead.contact_uri == contact_uri,
                )
            ).scalars()
        )
        if not candidates:
            normalised, _type = normalise_phone(contact_uri, region_hint=region_hint)
            if normalised and normalised != contact_uri:
                candidates = list(
                    session.execute(
                        select(Lead).where(
                            Lead.workspace_id == workspace.id,
                            Lead.contact_uri == normalised,
                        )
                    ).scalars()
                )

        if not candidates:
            console.print(
                f"[red]no lead with contact_uri matching {contact_uri!r}[/red]"
            )
            raise typer.Exit(code=1)
        if len(candidates) > 1:
            console.print(
                f"[red]ambiguous: {len(candidates)} leads share contact_uri "
                f"{contact_uri!r} — refusing to act.[/red]"
            )
            raise typer.Exit(code=1)

        lead = candidates[0]

        if lead.do_not_contact_at is not None:
            console.print(
                f"[yellow]already opted out[/yellow] "
                f"({lead.do_not_contact_at.isoformat()}, "
                f"reason={lead.do_not_contact_reason!r}) — no change."
            )
            return

        console.print(
            f"lead=[cyan]{lead.name or '(unnamed)'}[/cyan] "
            f"contact={lead.contact_uri}  status={lead.status}"
        )

        if not yes:
            confirm = typer.confirm(
                "Mark this lead as do-not-contact? Future sends will be blocked.",
                default=False,
            )
            if not confirm:
                console.print("[yellow]aborted[/yellow]")
                raise typer.Exit(code=1)

        lead.do_not_contact_at = datetime.now(timezone.utc)
        lead.do_not_contact_reason = reason
        session.flush()
        console.print(
            f"[green]opted out[/green] lead={lead.id}  reason={reason!r}"
        )


def _resolve_thread_by_id(session, thread_id: str) -> Thread | None:
    """Resolve a thread by full id or trailing substring (min 4 chars)."""

    direct = session.get(Thread, thread_id)
    if direct is not None:
        return direct
    if len(thread_id) < 4:
        return None
    matches = (
        session.query(Thread).filter(Thread.id.like(f"%{thread_id}")).limit(2).all()
    )
    if len(matches) == 1:
        return matches[0]
    return None


def main() -> None:  # pragma: no cover - entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
