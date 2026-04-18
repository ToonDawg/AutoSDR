"""Typer-based CLI. Installed as the ``autosdr`` console script."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import and_, func, select

from autosdr import killswitch
from autosdr import config as config_module
from autosdr.config import default_workspace_settings, get_settings
from autosdr.connectors import get_connector as _get_connector
from autosdr.connectors.base import ConnectorError, OutgoingMessage
from autosdr.db import create_all, session_scope
from autosdr.llm import get_usage_snapshot
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    LlmCall,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)

app = typer.Typer(help="AutoSDR POC — autonomous SDR for small business owners.")
campaign_app = typer.Typer(help="Manage outreach campaigns.")
test_app = typer.Typer(help="Connectivity and health tests.")
hitl_app = typer.Typer(help="Human-in-the-loop operations.")
sim_app = typer.Typer(help="Simulate inbound messages (for testing).")
logs_app = typer.Typer(help="Inspect LLM call and thread transcripts.")

app.add_typer(campaign_app, name="campaign")
app.add_typer(test_app, name="test")
app.add_typer(hitl_app, name="hitl")
app.add_typer(sim_app, name="sim")
app.add_typer(logs_app, name="logs")

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


def get_connector():
    """Wrap :func:`autosdr.connectors.get_connector` with a friendly CLI error.

    Connector constructors raise :class:`ConnectorError` when required config
    is missing (e.g. ``TEXTBEE_API_KEY``). Instead of surfacing a traceback,
    print the problem and exit with code 2 so scripts can detect it.
    """

    try:
        return _get_connector()
    except ConnectorError as exc:
        console.print(f"[red]connector misconfigured[/red]: {exc}")
        console.print(
            "[dim]tip: check .env — set CONNECTOR + the provider-specific keys "
            "(e.g. TEXTBEE_API_KEY, TEXTBEE_DEVICE_ID, or SMSGATE_USERNAME + "
            "SMSGATE_PASSWORD). Pass --dry-run to skip real SMS entirely.[/dim]"
        )
        raise typer.Exit(code=2) from exc


def _apply_test_mode_flags(dry_run: bool, override_to: str | None) -> None:
    """Propagate ``--dry-run`` / ``--override-to`` into the settings layer.

    Both are ordinary env-backed settings, so the cleanest way to make a CLI
    flag win over a ``.env`` value is to mutate ``os.environ`` before Settings
    is first built. We also clear the singleton in case anything upstream
    already instantiated it.
    """

    if dry_run:
        os.environ["DRY_RUN"] = "true"
    if override_to:
        os.environ["SMS_OVERRIDE_TO"] = override_to.strip()
    if dry_run or override_to:
        config_module.reset_settings_for_tests()


# ---------------------------------------------------------------------------
# init / import
# ---------------------------------------------------------------------------


@app.command()
def init(
    business_name: str = typer.Option(
        "My Business", "--business-name", help="Short label for the workspace."
    ),
    business_dump: str = typer.Option(
        ..., "--business-dump", help="Free-form description of your business (what you sell, who you sell to)."
    ),
    tone: str = typer.Option(
        ..., "--tone", help="Desired tone for generated messages."
    ),
    default_region: str = typer.Option(
        "AU",
        "--region",
        help="Region hint for phone number parsing (ISO 3166-1 alpha-2).",
    ),
) -> None:
    """Create (or replace) the single workspace row."""

    _configure_logging()
    create_all()
    env = get_settings()
    ws_settings = default_workspace_settings(env)
    ws_settings["default_region"] = default_region

    with session_scope() as session:
        existing = session.query(Workspace).first()
        if existing:
            existing.business_name = business_name
            existing.business_dump = business_dump
            existing.tone_prompt = tone
            existing.settings = ws_settings
            console.print(f"[green]workspace updated[/green] id={existing.id}")
        else:
            workspace = Workspace(
                business_name=business_name,
                business_dump=business_dump,
                tone_prompt=tone,
                settings=ws_settings,
            )
            session.add(workspace)
            session.flush()
            console.print(f"[green]workspace created[/green] id={workspace.id}")

    console.print(
        f"connector=[cyan]{env.connector}[/cyan]  "
        f"next: [green]autosdr import <file>[/green] then "
        f"[green]autosdr campaign create[/green]"
    )


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
            console.print("[red]run `autosdr init` first[/red]")
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
# campaign subcommands
# ---------------------------------------------------------------------------


@campaign_app.command("create")
def campaign_create(
    name: str = typer.Option(..., "--name"),
    goal: str = typer.Option(..., "--goal"),
    per_day: int = typer.Option(20, "--per-day", min=1),
    connector_type: str = typer.Option("android_sms", "--connector-type"),
) -> None:
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            console.print("[red]run `autosdr init` first[/red]")
            raise typer.Exit(code=1)

        campaign = Campaign(
            workspace_id=workspace.id,
            name=name,
            goal=goal,
            outreach_per_day=per_day,
            connector_type=connector_type,
            status=CampaignStatus.DRAFT,
        )
        session.add(campaign)
        session.flush()
        console.print(f"[green]campaign created[/green] id={campaign.id} name={name!r}")


@campaign_app.command("list")
def campaign_list() -> None:
    with session_scope() as session:
        campaigns = session.query(Campaign).all()
        tbl = Table(title="Campaigns")
        tbl.add_column("id", overflow="fold")
        tbl.add_column("name")
        tbl.add_column("status")
        tbl.add_column("per_day")
        tbl.add_column("queued")
        tbl.add_column("contacted")
        tbl.add_column("replied")
        tbl.add_column("won")
        tbl.add_column("lost")
        for c in campaigns:
            totals = _campaign_status_breakdown(session, c.id)
            tbl.add_row(
                c.id,
                c.name,
                c.status,
                str(c.outreach_per_day),
                str(totals["queued"]),
                str(totals["contacted"]),
                str(totals["replied"]),
                str(totals["won"]),
                str(totals["lost"]),
            )
        console.print(tbl)


def _campaign_status_breakdown(session, campaign_id: str) -> dict[str, int]:
    out = {s: 0 for s in ("queued", "contacted", "replied", "won", "lost", "skipped")}
    stmt = (
        select(CampaignLead.status, func.count(CampaignLead.id))
        .where(CampaignLead.campaign_id == campaign_id)
        .group_by(CampaignLead.status)
    )
    for status_name, count in session.execute(stmt).all():
        out[status_name] = int(count)
    return out


@campaign_app.command("assign")
def campaign_assign(
    campaign_id: str,
    all_unassigned: bool = typer.Option(
        False, "--all-unassigned", help="Assign every lead not yet in this campaign."
    ),
    limit: Optional[int] = typer.Option(None, "--limit", help="Cap the number assigned."),
) -> None:
    """Assign leads to a campaign."""

    with session_scope() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            console.print(f"[red]campaign {campaign_id} not found[/red]")
            raise typer.Exit(code=1)

        if not all_unassigned:
            console.print(
                "[yellow]only --all-unassigned is supported in the POC;"
                " per-lead assignment is a v1 feature[/yellow]"
            )
            raise typer.Exit(code=1)

        # Find leads that (a) are 'new' status and (b) not already in this campaign.
        subquery = select(CampaignLead.lead_id).where(
            CampaignLead.campaign_id == campaign.id
        )
        leads_query = (
            select(Lead)
            .where(
                and_(
                    Lead.workspace_id == campaign.workspace_id,
                    Lead.status == LeadStatus.NEW,
                    ~Lead.id.in_(subquery),
                )
            )
            .order_by(Lead.import_order.asc())
        )
        if limit:
            leads_query = leads_query.limit(limit)

        leads = list(session.execute(leads_query).scalars())
        existing_count = (
            session.execute(
                select(func.count(CampaignLead.id)).where(
                    CampaignLead.campaign_id == campaign.id
                )
            ).scalar_one()
            or 0
        )

        for idx, lead in enumerate(leads):
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=int(existing_count) + idx + 1,
                    status=CampaignLeadStatus.QUEUED,
                )
            )

        console.print(f"[green]assigned[/green] {len(leads)} leads to {campaign.name!r}")


@campaign_app.command("activate")
def campaign_activate(campaign_id: str) -> None:
    _set_campaign_status(campaign_id, CampaignStatus.ACTIVE)


@campaign_app.command("pause")
def campaign_pause(campaign_id: str) -> None:
    _set_campaign_status(campaign_id, CampaignStatus.PAUSED)


@campaign_app.command("stop")
def campaign_stop(campaign_id: str) -> None:
    _set_campaign_status(campaign_id, CampaignStatus.COMPLETED)


def _set_campaign_status(campaign_id: str, status_value: str) -> None:
    with session_scope() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            console.print(f"[red]campaign {campaign_id} not found[/red]")
            raise typer.Exit(code=1)
        campaign.status = status_value
        console.print(f"[green]campaign {campaign.name!r} -> {status_value}[/green]")


# ---------------------------------------------------------------------------
# run / pause / resume / stop / status
# ---------------------------------------------------------------------------


@app.command()
def run(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Mock SMS for a full workflow rehearsal. Forces FileConnector "
            "regardless of CONNECTOR — nothing hits the wire, every outbound "
            "is appended to data/outbox.jsonl. The LLM still runs normally so "
            "you can review data/logs/llm-YYYYMMDD.jsonl afterwards."
        ),
    ),
    override_to: Optional[str] = typer.Option(
        None,
        "--override-to",
        help=(
            "E.164 phone number that every outbound SMS is redirected to "
            "(e.g. your own phone). Use with the real connector to rehearse "
            "outbound end-to-end against one device before pointing at real "
            "leads."
        ),
    ),
) -> None:
    """Start the webhook server + scheduler. Ctrl+C to stop."""

    _apply_test_mode_flags(dry_run=dry_run, override_to=override_to)
    _configure_logging()
    create_all()
    env = get_settings()

    import uvicorn

    from autosdr.webhook import create_app

    killswitch.write_pid_file()
    # Belt-and-braces PID file cleanup: `finally` blocks don't always run
    # if Python exits with a signal exit code, so we register atexit AND
    # explicit signal handlers that clean the PID file before re-raising.
    atexit.register(killswitch.clear_pid_file)

    def _cleanup_on_signal(signum: int, _frame) -> None:
        killswitch.clear_pid_file()
        # Re-raise via default handler so uvicorn sees the signal.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    # Only install for SIGTERM — SIGINT should stay with uvicorn so Ctrl+C
    # still surfaces a KeyboardInterrupt cleanly to uvicorn's own handler.
    signal.signal(signal.SIGTERM, _cleanup_on_signal)

    mode_suffix = ""
    if env.dry_run:
        mode_suffix += " [bold yellow]DRY-RUN[/bold yellow]"
    if env.sms_override_to:
        mode_suffix += f" [bold yellow]OVERRIDE->{env.sms_override_to}[/bold yellow]"
    console.print(
        f"[green]autosdr starting[/green] pid={os.getpid()} "
        f"connector={env.connector} host={env.host} port={env.port}"
        + mode_suffix
    )

    try:
        uvicorn.run(
            create_app(run_scheduler_task=True),
            host=env.host,
            port=env.port,
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

    env = get_settings()
    create_all()

    paused = killswitch.is_flag_set()
    pid = killswitch.read_pid_file()
    console.print(
        f"paused={'yes' if paused else 'no'}  pid={pid if pid else 'n/a'}  "
        f"connector={env.connector}"
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
            console.print("[yellow]no workspace — run `autosdr init`[/yellow]")
            return

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
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        for c in active:
            sent_24h = (
                session.execute(
                    select(func.count(Message.id))
                    .join(Thread, Thread.id == Message.thread_id)
                    .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
                    .where(
                        CampaignLead.campaign_id == c.id,
                        Message.role == MessageRole.AI,
                        Message.created_at >= cutoff,
                    )
                ).scalar_one()
                or 0
            )
            remaining = max(0, c.outreach_per_day - int(sent_24h))
            tbl.add_row(c.name, str(c.outreach_per_day), str(sent_24h), str(remaining))
        console.print(tbl)


# ---------------------------------------------------------------------------
# HITL
# ---------------------------------------------------------------------------


@hitl_app.command("list")
def hitl_list() -> None:
    """List threads waiting for owner attention."""

    with session_scope() as session:
        threads = (
            session.query(Thread)
            .filter(Thread.status == ThreadStatus.PAUSED_FOR_HITL)
            .all()
        )
        if not threads:
            console.print("[green]no threads waiting for HITL[/green]")
            return
        tbl = Table(title="HITL queue")
        tbl.add_column("thread_id", overflow="fold")
        tbl.add_column("reason")
        tbl.add_column("updated_at")
        for t in threads:
            tbl.add_row(t.id, t.hitl_reason or "", str(t.updated_at))
        console.print(tbl)


@hitl_app.command("show")
def hitl_show(thread_id: str) -> None:
    with session_scope() as session:
        t = session.get(Thread, thread_id)
        if t is None:
            console.print(f"[red]thread {thread_id} not found[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[bold]thread {t.id}[/bold]  status={t.status}  "
            f"hitl_reason={t.hitl_reason}"
        )
        if t.hitl_context:
            console.print(t.hitl_context)
        messages = (
            session.query(Message)
            .filter(Message.thread_id == t.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        if not messages:
            console.print("[dim](no messages yet)[/dim]")
        for m in messages:
            console.print(f"[{m.created_at.isoformat()}] [{m.role}] {m.content}")


@hitl_app.command("resume")
def hitl_resume(thread_id: str) -> None:
    """Mark a HITL thread as active again so the AI takes over."""

    with session_scope() as session:
        t = session.get(Thread, thread_id)
        if t is None:
            console.print(f"[red]thread {thread_id} not found[/red]")
            raise typer.Exit(code=1)
        t.status = ThreadStatus.ACTIVE
        t.hitl_reason = None
        t.hitl_context = None
        console.print(f"[green]thread {t.id} -> active[/green]")


# ---------------------------------------------------------------------------
# test / sim
# ---------------------------------------------------------------------------


@test_app.command("sms")
def test_sms(
    to: str = typer.Option(..., "--to", help="E.164 phone number to send a test SMS to."),
    content: str = typer.Option(
        "AutoSDR test message — ignore.", "--content"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Write to data/outbox.jsonl via FileConnector instead of real send.",
    ),
    override_to: Optional[str] = typer.Option(
        None,
        "--override-to",
        help="Redirect the test send to this number (e.g. your own phone).",
    ),
) -> None:
    """Send a test SMS via the configured connector."""

    _apply_test_mode_flags(dry_run=dry_run, override_to=override_to)
    _configure_logging()
    connector = get_connector()

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
    connector = get_connector()

    async def _run() -> None:
        incoming = connector.parse_webhook(
            {"contact_uri": contact_uri, "content": content, "from": contact_uri, "text": content}
        )
        with session_scope() as session:
            workspace = session.query(Workspace).first()
            if workspace is None:
                console.print("[red]run `autosdr init` first[/red]")
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
            # llm
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
