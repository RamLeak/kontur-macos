"""Persistence model for the EGE Informatics Workspace.

The models intentionally keep bounded JSON payloads and immutable snapshots in
separate rows. Domain services enforce semantic immutability and owner scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON as SAJSON
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    # SQLite DateTime columns are intentionally naive for cross-version
    # portability; make the UTC choice explicit without deprecated utcnow().
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourcePolicy(Base):
    __tablename__ = "source_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    canonical_origin: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), default="disabled")
    permission_status: Mapped[str] = mapped_column(String(32), default="review_required")
    enabled_operations: Mapped[list[str]] = mapped_column(SAJSON, default=list)
    policy_version: Mapped[str] = mapped_column(String(32), default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_policy_id: Mapped[str] = mapped_column(ForeignKey("source_policies.id"))
    source_url: Mapped[str] = mapped_column(Text)
    observed_url: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    parser_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="accepted")
    manifest_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("source_name", "external_id", name="uq_task_source_external"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_name: Mapped[str] = mapped_column(String(64), default="kompege", index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    line_number: Mapped[int] = mapped_column(Integer, index=True)
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_revisions.id", use_alter=True, name="fk_task_current_revision"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TaskRevision(Base):
    __tablename__ = "task_revisions"
    __table_args__ = (UniqueConstraint("task_id", "revision_number", name="uq_task_revision_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(String(255))
    line_number: Mapped[int] = mapped_column(Integer, index=True)
    exam_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Preparation target is an owner/catalog label, not a claim about official
    # source provenance. ``exam_year`` remains the observed source year.
    target_exam_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_year_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    statement: Mapped[str] = mapped_column(Text)
    statement_latex: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    parser_version: Mapped[str] = mapped_column(String(64), default="fixture-v1")
    rights_status: Mapped[str] = mapped_column(String(32), default="review_required", index=True)
    asset_manifest: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list)
    subtask_manifest: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list)
    table_manifest: Mapped[dict[str, Any] | None] = mapped_column(SAJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskBookmark(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("owner_id", "task_id", name="uq_bookmark_owner_task"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    task_revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_name: Mapped[str] = mapped_column(String(64), default="kompege")
    canonical_url: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    detected_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    expected_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cache_state: Mapped[str] = mapped_column(String(32), default="metadata_only", index=True)
    rights_status: Mapped[str] = mapped_column(String(32), default="review_required")
    fetch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hash_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskAsset(Base):
    __tablename__ = "task_assets"
    __table_args__ = (UniqueConstraint("revision_id", "asset_id", name="uq_revision_asset"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    runner_filename: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_sync_idempotency"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_policy_id: Mapped[str] = mapped_column(ForeignKey("source_policies.id"))
    mode: Mapped[str] = mapped_column(String(32))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    counters: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    failures: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    # Nullable for backward compatibility with v1-v3 rows. New admissions
    # always populate it and scope their lookup by owner.
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceSyncJob(Base):
    """Durable owner-scoped bounded catalog synchronization job."""

    __tablename__ = "source_sync_jobs"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_source_sync_owner_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_policy_id: Mapped[str] = mapped_column(ForeignKey("source_policies.id"))
    mode: Mapped[str] = mapped_column(String(32), default="catalog-sync")
    request_hash: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    max_items: Mapped[int] = mapped_column(Integer, default=1000)
    target_exam_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_index: Mapped[int] = mapped_column(Integer, default=0)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    counters: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    failures: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SourceSyncItem(Base):
    """One durable catalog item checkpoint within a SourceSyncJob."""

    __tablename__ = "source_sync_items"
    __table_args__ = (
        UniqueConstraint("job_id", "ordinal", name="uq_source_sync_item_ordinal"),
        UniqueConstraint("job_id", "canonical_url", name="uq_source_sync_item_url"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("source_sync_jobs.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    canonical_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CatalogEntry(Base):
    """A source revision pinned into an immutable owner-scoped catalog job."""

    __tablename__ = "catalog_entries"
    __table_args__ = (UniqueConstraint("job_id", "revision_id", name="uq_catalog_job_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("source_sync_jobs.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyReservation(Base):
    """Owner-scoped durable reservation for assistant request keys.

    The unique owner/key pair is claimed before any message/context/run work is
    performed.  This turns SQLite's write serialization into a server-side
    concurrency gate instead of relying on a browser draft lock.
    """

    __tablename__ = "idempotency_reservations"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_idempotency_owner_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_hash: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True, index=True)
    context_pack_id: Mapped[str | None] = mapped_column(ForeignKey("context_packs.id"), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SolveSession(Base):
    __tablename__ = "solve_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    solve_session_id: Mapped[str] = mapped_column(ForeignKey("solve_sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verification_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    independent: Mapped[bool] = mapped_column(Boolean, default=True)
    hint_count: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CodeDocument(Base):
    __tablename__ = "code_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    solve_session_id: Mapped[str] = mapped_column(ForeignKey("solve_sessions.id"), unique=True)
    language: Mapped[str] = mapped_column(String(32), default="python")
    current_version_id: Mapped[str | None] = mapped_column(nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CodeVersion(Base):
    __tablename__ = "code_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("code_documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CodeRun(Base):
    __tablename__ = "code_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    solve_session_id: Mapped[str] = mapped_column(ForeignKey("solve_sessions.id"), index=True)
    code_version_id: Mapped[str] = mapped_column(ForeignKey("code_versions.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    executor_status: Mapped[str] = mapped_column(String(64), default="unavailable")
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BoardDocument(Base):
    __tablename__ = "board_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    solve_session_id: Mapped[str] = mapped_column(ForeignKey("solve_sessions.id"), unique=True)
    current_version_id: Mapped[str | None] = mapped_column(nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class BoardVersion(Base):
    __tablename__ = "board_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("board_documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Annotation(Base):
    __tablename__ = "annotations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    solve_session_id: Mapped[str] = mapped_column(ForeignKey("solve_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    anchor_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    source_revision_hash: Mapped[str] = mapped_column(String(64))
    anchor_status: Mapped[str] = mapped_column(String(32), default="attached")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CodeSample(Base):
    __tablename__ = "code_samples"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(32), default="python")
    usage_note: Mapped[str] = mapped_column(Text, default="")
    task_lines: Mapped[list[int]] = mapped_column(SAJSON, default=list)
    tags: Mapped[list[str]] = mapped_column(SAJSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version_id: Mapped[str | None] = mapped_column(nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CodeSampleVersion(Base):
    __tablename__ = "code_sample_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sample_id: Mapped[str] = mapped_column(ForeignKey("code_samples.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContextPack(Base):
    __tablename__ = "context_packs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    # Exact redacted sections are retained so an idempotent preview can be
    # replayed without rebuilding against a changed workspace projection.
    rendered_sections: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list)
    rendered_hash: Mapped[str] = mapped_column(String(64))
    redaction_counters: Mapped[dict[str, int]] = mapped_column(SAJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    solve_session_id: Mapped[str | None] = mapped_column(ForeignKey("solve_sessions.id"), nullable=True)
    context_pack_id: Mapped[str | None] = mapped_column(ForeignKey("context_packs.id"), nullable=True)
    requested_model: Mapped[str] = mapped_column(String(64))
    requested_effort: Mapped[str] = mapped_column(String(32))
    effective_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    route_reason: Mapped[str] = mapped_column(Text, default="")
    tutor_mode: Mapped[str] = mapped_column(String(64), default="hint")
    policy_version: Mapped[str] = mapped_column(String(32), default="1")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    content: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("agent_run_id", "sequence", name="uq_agent_event_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TutorFeedback(Base):
    __tablename__ = "tutor_feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    value: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("taxonomy_version", "code", name="uq_skill_taxonomy_code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(128), index=True)
    line_number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255))
    taxonomy_version: Mapped[str] = mapped_column(String(32), default="v1")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SkillPrerequisite(Base):
    __tablename__ = "skill_prerequisites"
    __table_args__ = (UniqueConstraint("skill_id", "prerequisite_id", name="uq_skill_prereq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    prerequisite_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)


class TaskSkill(Base):
    __tablename__ = "task_skills"
    __table_args__ = (UniqueConstraint("revision_id", "skill_id", "taxonomy_version", name="uq_task_skill"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    taxonomy_version: Mapped[str] = mapped_column(String(32), default="v1")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    mapping_status: Mapped[str] = mapped_column(String(32), default="proposed")
    mapping_source: Mapped[str] = mapped_column(String(64), default="manual")


class MasteryEvidence(Base):
    __tablename__ = "mastery_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(32), default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MasteryState(Base):
    __tablename__ = "mastery_states"
    __table_args__ = (UniqueConstraint("owner_id", "skill_id", name="uq_mastery_owner_skill"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="insufficient_evidence")
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    independent_successes: Mapped[int] = mapped_column(Integer, default=0)
    hint_dependency: Mapped[float] = mapped_column(Float, default=0.0)
    algorithm_version: Mapped[str] = mapped_column(String(32), default="mastery-v1")
    generation: Mapped[str] = mapped_column(String(36), default=new_id)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ErrorEvent(Base):
    __tablename__ = "error_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"), nullable=True)
    task_revision_id: Mapped[str | None] = mapped_column(ForeignKey("task_revisions.id"), nullable=True, index=True)
    exam_item_id: Mapped[str | None] = mapped_column(ForeignKey("exam_items.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    error_type: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    prevention_note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="user")
    task_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32), default="error-capture-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ErrorPattern(Base):
    __tablename__ = "error_patterns"
    __table_args__ = (UniqueConstraint("owner_id", "normalized_type", name="uq_error_pattern_owner_type"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    normalized_type: Mapped[str] = mapped_column(String(128))
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"), nullable=True)
    error_pattern_id: Mapped[str | None] = mapped_column(ForeignKey("error_patterns.id"), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    interval_days: Mapped[int] = mapped_column(Integer, default=1)
    reason_codes: Mapped[list[str]] = mapped_column(SAJSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list[str]] = mapped_column(SAJSON, default=list)
    task_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32), default="mastery-v1")
    status: Mapped[str] = mapped_column(String(32), default="due")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DailyPlan(Base):
    __tablename__ = "daily_plans"
    __table_args__ = (UniqueConstraint("owner_id", "plan_date", name="uq_daily_plan_owner_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    plan_date: Mapped[str] = mapped_column(String(10), index=True)
    algorithm_version: Mapped[str] = mapped_column(String(32), default="daily-plan-v1")
    items: Mapped[list[dict[str, Any]]] = mapped_column(SAJSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExamBlueprint(Base):
    __tablename__ = "exam_blueprints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    exam_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_lines: Mapped[dict[str, int]] = mapped_column(SAJSON, default=dict)
    constraints: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExamSession(Base):
    __tablename__ = "exam_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    blueprint_id: Mapped[str] = mapped_column(ForeignKey("exam_blueprints.id"), index=True)
    seed: Mapped[int] = mapped_column(Integer)
    rng_algorithm: Mapped[str] = mapped_column(String(64), default="python-random")
    rng_version: Mapped[str] = mapped_column(String(32), default="v1")
    scoring_version: Mapped[str] = mapped_column(String(32), default="score-v1")
    pool_manifest_hash: Mapped[str] = mapped_column(String(64))
    ordered_revision_ids: Mapped[list[str]] = mapped_column(SAJSON, default=list)
    lifecycle: Mapped[str] = mapped_column(String(32), default="active", index=True)
    sealed: Mapped[bool] = mapped_column(Boolean, default=True)
    # ``legacy`` preserves pre-mode exam rows and requests. Explicit new
    # sessions use ``strict`` or ``study`` and keep that choice immutable.
    mode: Mapped[str] = mapped_column(String(16), default="legacy", server_default="legacy")
    # P4 preparation metadata is immutable once a ready session is created.
    # started_at remains non-null for legacy SQLite compatibility; ready
    # responses mask it and /start replaces it with the actual start instant.
    prepare_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    prepare_options: Mapped[dict[str, Any] | None] = mapped_column(SAJSON, nullable=True)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paused_seconds: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExamItem(Base):
    __tablename__ = "exam_items"
    __table_args__ = (UniqueConstraint("exam_session_id", "position", name="uq_exam_item_position"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exam_session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    task_revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)


class ExamAnswer(Base):
    __tablename__ = "exam_answers"
    __table_args__ = (UniqueConstraint("exam_item_id", name="uq_exam_answer_item"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exam_item_id: Mapped[str] = mapped_column(ForeignKey("exam_items.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ExamWorkspace(Base):
    """Owner-scoped code/board draft for one immutable exam item."""

    __tablename__ = "exam_workspaces"
    __table_args__ = (UniqueConstraint("exam_session_id", "exam_item_id", name="uq_exam_workspace_item"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    exam_session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    exam_item_id: Mapped[str] = mapped_column(ForeignKey("exam_items.id"), index=True)
    code_content: Mapped[str] = mapped_column(Text, default="", server_default="")
    code_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    board_snapshot: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict, server_default="{}")
    board_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ExamCodeRun(Base):
    """Immutable server-run output for one frozen exam workspace version."""

    __tablename__ = "exam_code_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_exam_code_run_owner_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    exam_session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    exam_item_id: Mapped[str] = mapped_column(ForeignKey("exam_items.id"), index=True)
    task_revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("exam_workspaces.id"), index=True)
    workspace_version: Mapped[int] = mapped_column(Integer)
    code_content: Mapped[str] = mapped_column(Text)
    code_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    executor_status: Mapped[str] = mapped_column(String(64), default="unavailable")
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExamStudyAiContextSnapshot(Base):
    """Immutable server-owned context captured for one exam Study AI request."""

    __tablename__ = "exam_study_ai_context_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    exam_session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    exam_item_id: Mapped[str] = mapped_column(ForeignKey("exam_items.id"), index=True)
    task_revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    workspace_version: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExamStudyAiRun(Base):
    """Owner/item-bound Study AI request backed by an attested AgentRun."""

    __tablename__ = "exam_study_ai_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_exam_study_ai_owner_key"),
        UniqueConstraint("agent_run_id", name="uq_exam_study_ai_agent_run"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    exam_session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    exam_item_id: Mapped[str] = mapped_column(ForeignKey("exam_items.id"), index=True)
    task_revision_id: Mapped[str] = mapped_column(ForeignKey("task_revisions.id"), index=True)
    workspace_version: Mapped[int] = mapped_column(Integer)
    context_snapshot_id: Mapped[str] = mapped_column(ForeignKey("exam_study_ai_context_snapshots.id"), index=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    message_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExamReport(Base):
    __tablename__ = "exam_reports"
    __table_args__ = (UniqueConstraint("exam_session_id", name="uq_exam_report_session"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exam_session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    scoring_version: Mapped[str] = mapped_column(String(32))
    report_json: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    report_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LearningEvent(Base):
    __tablename__ = "learning_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_learning_event_id"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_learning_event_idempotency"),
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    schema_version: Mapped[str] = mapped_column(String(32), default="v1")
    policy_version: Mapped[str] = mapped_column(String(32), default="1")
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))


class ProjectionCheckpoint(Base):
    __tablename__ = "projection_checkpoints"
    __table_args__ = (
        UniqueConstraint("owner_id", "projector_name", "projector_version", name="uq_projection_checkpoint"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    projector_name: Mapped[str] = mapped_column(String(128))
    projector_version: Mapped[str] = mapped_column(String(32))
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[str] = mapped_column(String(36), default=new_id)
    projection_hash: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    algorithm_version: Mapped[str] = mapped_column(String(32))
    metrics: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_job_idempotency"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(SAJSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OwnerSyncRecord(Base):
    """Mutable drafts/preferences; never grading or learning evidence."""

    __tablename__ = "owner_sync_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    context: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    value: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OwnerSyncWrite(Base):
    """Durable successful response for an uncertain network retry."""

    __tablename__ = "owner_sync_writes"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_owner_sync_write"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(SAJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class OwnerSyncChange(Base):
    """Transactional invalidation metadata; contains no user content."""

    __tablename__ = "owner_sync_changes"
    __table_args__ = {"sqlite_autoincrement": True}
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    topic: Mapped[str] = mapped_column(String(64))
