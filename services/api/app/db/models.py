from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, new_id, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="user")
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    mode_preference: Mapped[str] = mapped_column(String(32), default="demo")
    preferred_image_provider: Mapped[str] = mapped_column(String(64), default="mock")
    preferred_vision_provider: Mapped[str] = mapped_column(String(64), default="auto")
    reduced_motion_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    retention_policy: Mapped[str] = mapped_column(String(32), default="pilot_90_days")
    theme: Mapped[str] = mapped_column(String(16), default="system")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    mode: Mapped[str] = mapped_column(String(32), default="individual")
    processing_mode: Mapped[str] = mapped_column(String(32), default="demo")
    provider: Mapped[str] = mapped_column(String(64), default="mock")
    model_id: Mapped[str] = mapped_column(String(120), default="mock-demo")
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation_policy: Mapped[str] = mapped_column(String(32), default="automatic")
    quality_preset: Mapped[str] = mapped_column(String(32), default="balanced")
    vision_provider: Mapped[str] = mapped_column(String(64), default="unconfigured")
    status: Mapped[str] = mapped_column(String(32), default="awaiting_photo")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    wordcloud_revision: Mapped[int] = mapped_column(Integer, default=0)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    batch_window_seconds: Mapped[int] = mapped_column(Integer, default=8)
    batch_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    require_publish_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionMember(Base):
    __tablename__ = "session_members"
    __table_args__ = (UniqueConstraint("session_id", "user_id", name="uq_session_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="active")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionInvite(Base):
    __tablename__ = "session_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer, default=20)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SceneAsset(Base):
    __tablename__ = "scene_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    object_key: Mapped[str] = mapped_column(String(400))
    mime: Mapped[str] = mapped_column(String(80), default="image/jpeg")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32), default="original")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    demo_labeled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key", name="uq_feedback_idem"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(12), default="en")
    status: Mapped[str] = mapped_column(String(32), default="accepted")
    idempotency_key: Mapped[str] = mapped_column(String(80))
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Concept(Base):
    __tablename__ = "concepts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    canonical_slug: Mapped[str] = mapped_column(String(160), unique=True)
    display_label: Mapped[str] = mapped_column(String(160))
    language: Mapped[str] = mapped_column(String(12), default="en")
    category: Mapped[str] = mapped_column(String(64), default="general")
    synonyms_json: Mapped[list] = mapped_column(JSON, default=list)
    taxonomy_version: Mapped[str] = mapped_column(String(32), default="street-v1")


class FeedbackConcept(Base):
    __tablename__ = "feedback_concepts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    feedback_id: Mapped[str] = mapped_column(ForeignKey("feedback.id"), index=True)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_phrase: Mapped[str] = mapped_column(String(200))
    extractor_version: Mapped[str] = mapped_column(String(32), default="street-v1")


class PromptSnapshot(Base):
    __tablename__ = "prompt_snapshots"
    __table_args__ = (UniqueConstraint("session_id", "revision_number", name="uq_prompt_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    interventions_json: Mapped[dict] = mapped_column(JSON)
    compiled_prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    construction_rules_version: Mapped[str] = mapped_column(String(32), default="brief-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    source_asset_id: Mapped[str] = mapped_column(String(36))
    prompt_snapshot_id: Mapped[str] = mapped_column(String(36))
    mode: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120))
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(80), default="")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_revision: Mapped[int] = mapped_column(Integer, default=0)
    stage_note: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DesignVersion(Base):
    __tablename__ = "design_versions"
    __table_args__ = (UniqueConstraint("session_id", "version_no", name="uq_session_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    generation_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    image_asset_id: Mapped[str] = mapped_column(String(36))
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    based_on_original: Mapped[bool] = mapped_column(Boolean, default=True)
    publish_status: Mapped[str] = mapped_column(String(32), default="published")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(64), default="original")
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_excerpt: Mapped[str] = mapped_column(Text, default="")
    demo_labeled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    version_id: Mapped[str] = mapped_column(String(36), index=True)
    evaluator_provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120), default="")
    rubric_version: Mapped[str] = mapped_column(String(64), default="alexander-street-v1")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evaluation_job_id: Mapped[str] = mapped_column(ForeignKey("evaluation_jobs.id"), index=True)
    version_id: Mapped[str] = mapped_column(String(36), index=True)
    model: Mapped[str] = mapped_column(String(120))
    rubric_version: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    score_aggregate: Mapped[float | None] = mapped_column(Float, nullable=True)
    assessed_count: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String(32), default="vision_model")
    limitations_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationPropertyScore(Base):
    __tablename__ = "evaluation_property_scores"
    __table_args__ = (UniqueConstraint("result_id", "property_key", name="uq_result_property"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    result_id: Mapped[str] = mapped_column(ForeignKey("evaluation_results.id"), index=True)
    property_key: Mapped[str] = mapped_column(String(64))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    applicability: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_text: Mapped[str] = mapped_column(Text)
    caveat_text: Mapped[str] = mapped_column(Text, default="")


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    public_id: Mapped[str] = mapped_column(String(16), unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    friendly_name: Mapped[str] = mapped_column(String(120))
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="offline")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    device_token_hash: Mapped[str] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerGrant(Base):
    __tablename__ = "worker_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PairingChallenge(Base):
    __tablename__ = "pairing_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), index=True)
    requester_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    challenge_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerLease(Base):
    __tablename__ = "worker_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    lease_token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    cost_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventOutbox(Base):
    __tablename__ = "events_outbox"
    __table_args__ = (UniqueConstraint("session_id", "seq_no", name="uq_session_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    seq_no: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsentRecord(Base):
    __tablename__ = "consent_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    processing_target: Mapped[str] = mapped_column(String(64))
    notice_version: Mapped[str] = mapped_column(String(64))
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
