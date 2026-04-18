"""SQLAlchemy ORM models for AutoSDR.

Mirrors the schemas in Doc 2 (data-architecture). UUIDs are stored as ``TEXT``
for SQLite portability; JSONB becomes JSON (TEXT under the hood) on SQLite. The
application code does not depend on JSON path operators, so Postgres is a
lossless drop-in for v1.

Status enum values are validated at the application layer for simplicity — a
CHECK constraint would fight ORM-level defaults during inserts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict: JSON,
        list: JSON,
    }


# ---------------------------------------------------------------------------
# Status vocabularies (centralised so callers can reference constants).
# ---------------------------------------------------------------------------

class LeadStatus:
    NEW = "new"
    CONTACTED = "contacted"
    REPLIED = "replied"
    WON = "won"
    LOST = "lost"
    SKIPPED = "skipped"


class ContactType:
    MOBILE = "mobile"
    LANDLINE = "landline"
    TOLL_FREE = "toll_free"
    UNKNOWN = "unknown"
    EMAIL = "email"


class CampaignStatus:
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class CampaignLeadStatus:
    QUEUED = "queued"
    CONTACTED = "contacted"
    REPLIED = "replied"
    WON = "won"
    LOST = "lost"
    SKIPPED = "skipped"


class ThreadStatus:
    ACTIVE = "active"
    PAUSED = "paused"
    PAUSED_FOR_HITL = "paused_for_hitl"
    WON = "won"
    LOST = "lost"
    SKIPPED = "skipped"

    CLOSED = {WON, LOST, SKIPPED}


class MessageRole:
    AI = "ai"
    HUMAN = "human"
    LEAD = "lead"


class ImportJobStatus:
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class LlmCallPurpose:
    """Which pipeline stage issued the LLM call."""

    ANALYSIS = "analysis"
    GENERATION = "generation"
    EVALUATION = "evaluation"
    CLASSIFICATION = "classification"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_name: Mapped[str] = mapped_column(Text, nullable=False)
    business_dump: Mapped[str] = mapped_column(Text, nullable=False)
    business_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tone_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Lead(Base):
    __tablename__ = "lead"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "contact_uri", name="lead_contact_uri_workspace_unique"
        ),
        Index("idx_lead_workspace_status", "workspace_id", "status"),
        Index("idx_lead_import_order", "workspace_id", "import_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspace.id"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    import_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LeadStatus.NEW
    )
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ImportJob(Base):
    __tablename__ = "import_job"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspace.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # csv | json
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ImportJobStatus.PENDING
    )
    mapping_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Campaign(Base):
    __tablename__ = "campaign"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspace.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    outreach_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    connector_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="android_sms"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CampaignStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class CampaignLead(Base):
    __tablename__ = "campaign_lead"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", name="campaign_lead_unique"),
        Index(
            "idx_campaign_lead_status",
            "campaign_id",
            "status",
            "queue_position",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign.id"), nullable=False
    )
    lead_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lead.id"), nullable=False
    )
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CampaignLeadStatus.QUEUED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Thread(Base):
    __tablename__ = "thread"
    __table_args__ = (
        Index("idx_thread_status", "status"),
        Index("idx_thread_campaign_lead", "campaign_lead_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_lead_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_lead.id"), nullable=False
    )
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ThreadStatus.ACTIVE
    )
    auto_reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    hitl_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    hitl_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        Index("idx_message_thread", "thread_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("thread.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class LlmCall(Base):
    """Persistent log of every LLM invocation for POC review and refinement.

    Every call (successful or failed, including self-heal retries) is written
    here so the owner can audit prompt behaviour after the fact — ``autosdr
    logs llm`` surfaces these rows, and ``autosdr logs thread <id>`` stitches
    them into the per-thread transcript alongside inbound/outbound messages.
    """

    __tablename__ = "llm_call"
    __table_args__ = (
        Index("idx_llm_call_created_at", "created_at"),
        Index("idx_llm_call_thread", "thread_id", "created_at"),
        Index("idx_llm_call_purpose", "purpose", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    temperature: Mapped[float | None] = mapped_column(nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    response_format: Mapped[str] = mapped_column(String(16), nullable=False, default="text")

    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_parsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UnmatchedWebhook(Base):
    __tablename__ = "unmatched_webhook"
    __table_args__ = (
        Index(
            "idx_unmatched_webhook_workspace",
            "workspace_id",
            "received_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspace.id"), nullable=False
    )
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sender_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def next_import_order(session: Any, workspace_id: str) -> int:
    """Next global ``lead.import_order`` for a workspace."""

    from sqlalchemy import func, select

    result = session.execute(
        select(func.coalesce(func.max(Lead.import_order), 0)).where(
            Lead.workspace_id == workspace_id
        )
    ).scalar_one()
    return int(result) + 1
