import logging
import secrets
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.generation import provider_catalog
from app.api.deps import current_user, get_db
from app.core.errors import AppError
from app.core.security import (
    decode_access_token,
    encode_access_token,
    hash_password,
    new_token,
    normalize_pairing_code,
    pairing_code,
    public_worker_code,
    sha256_text,
    verify_password,
    verify_subject,
)
from app.db.base import utcnow
from app.db.models import (
    DesignVersion,
    EventOutbox,
    Feedback,
    GenerationJob,
    PairingChallenge,
    PromptSnapshot,
    RefreshToken,
    SceneAsset,
    Session,
    User,
    UserSettings,
    Worker,
    WorkerGrant,
    WorkerLease,
)
from app.services.access import aware, iso, require_session
from app.services.flow import (
    audit,
    complete_upload,
    create_invite,
    create_session_record,
    enqueue_generation,
    evaluation_payload,
    presign_source,
    redeem_invite,
    session_payload,
    soft_delete_session,
    submit_feedback,
    versions_payload,
    wordcloud_payload,
)
from app.services.images import render_export
from app.services.providers import resolve_generation, resolve_vision
from app.services.state_machine import assert_transition

logger = logging.getLogger("streetcloud.api")
router = APIRouter()


class RegisterIn(BaseModel):
    email: str
    password: str
    display_name: str = Field(alias="displayName")
    model_config = ConfigDict(populate_by_name=True)


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str = Field(alias="refreshToken")
    model_config = ConfigDict(populate_by_name=True)


class SessionIn(BaseModel):
    title: str = "Untitled street"
    mode: str = "individual"
    processing_mode: str = Field(default="demo", alias="processingMode")
    provider: str | None = None
    worker_id: str | None = Field(default=None, alias="workerId")
    generation_policy: str = Field(default="automatic", alias="generationPolicy")
    vision_provider: str | None = Field(default=None, alias="visionProvider")
    demo: bool = False
    quality_preset: str = Field(default="balanced", alias="qualityPreset")
    model_config = ConfigDict(populate_by_name=True)


class FeedbackIn(BaseModel):
    text: str
    client_submission_id: str = Field(alias="clientSubmissionId")
    expected_session_revision: int = Field(alias="expectedSessionRevision")
    model_config = ConfigDict(populate_by_name=True)


class CompleteIn(BaseModel):
    asset_id: str = Field(alias="assetId")
    consent: bool
    model_config = ConfigDict(populate_by_name=True)


class PatchSessionIn(BaseModel):
    title: str | None = None
    processing_mode: str | None = Field(default=None, alias="processingMode")
    provider: str | None = None
    worker_id: str | None = Field(default=None, alias="workerId")
    generation_policy: str | None = Field(default=None, alias="generationPolicy")
    vision_provider: str | None = Field(default=None, alias="visionProvider")
    quality_preset: str | None = Field(default=None, alias="qualityPreset")
    require_publish_approval: bool | None = Field(default=None, alias="requirePublishApproval")
    model_config = ConfigDict(populate_by_name=True)


class PairIn(BaseModel):
    worker_id: str = Field(alias="workerId")
    pairing_code: str = Field(alias="pairingCode")
    model_config = ConfigDict(populate_by_name=True)


class WorkerIn(BaseModel):
    friendly_name: str = Field(alias="friendlyName")
    agent_version: str = Field(default="1.0.0", alias="agentVersion")
    model_config = ConfigDict(populate_by_name=True)


class InviteRedeemIn(BaseModel):
    code: str


class ModerateIn(BaseModel):
    action: str


def _tokens(settings, db: AsyncSession, user: User) -> tuple[str, str]:
    access = encode_access_token(settings, user.id, user.role)
    raw = new_token("scr_")
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=sha256_text(raw),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
        )
    )
    return access, raw


def _auth_body(user: User, access: str, refresh: str) -> dict:
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "user": {"id": user.id, "email": user.email, "displayName": user.display_name, "role": user.role},
    }


@router.post("/auth/register")
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise AppError("VALIDATION", "Enter a valid email address.", 400)
    if len(body.password) < 10:
        raise AppError("VALIDATION", "Use a password of at least 10 characters.", 400)
    if await db.scalar(select(User).where(User.email == email)):
        raise AppError("VALIDATION", "An account with that email already exists.", 409)
    user = User(
        email=email,
        display_name=body.display_name.strip()[:80] or "Street Cloud user",
        password_hash=hash_password(body.password),
        role="user",
    )
    db.add(user)
    await db.flush()
    db.add(UserSettings(user_id=user.id))
    access, refresh = _tokens(request.app.state.ctx.settings, db, user)
    audit(db, user.id, "auth.register", "user", user.id)
    return _auth_body(user, access, refresh)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == body.email.strip().lower(), User.status == "active"))
    if user is None or not user.password_hash or not verify_password(user.password_hash, body.password):
        raise AppError("AUTH_REQUIRED", "Email or password is incorrect.", 401)
    access, refresh = _tokens(request.app.state.ctx.settings, db, user)
    return _auth_body(user, access, refresh)


@router.post("/auth/guest")
async def guest(request: Request, db: AsyncSession = Depends(get_db)):
    user = User(email=None, display_name="Guest", role="guest")
    db.add(user)
    await db.flush()
    db.add(UserSettings(user_id=user.id, preferred_image_provider="mock", mode_preference="demo"))
    access, refresh = _tokens(request.app.state.ctx.settings, db, user)
    return _auth_body(user, access, refresh)


@router.post("/auth/refresh")
async def refresh(body: RefreshIn, request: Request, db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == sha256_text(body.refresh_token)))
    expires = aware(row.expires_at) if row else None
    if row is None or row.revoked_at is not None or expires is None or expires < utcnow():
        raise AppError("AUTH_REQUIRED", "Sign in again to continue.", 401)
    user = await db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise AppError("AUTH_REQUIRED", "This account is not active.", 401)
    row.revoked_at = utcnow()
    access, refresh_token = _tokens(request.app.state.ctx.settings, db, user)
    return _auth_body(user, access, refresh_token)


@router.post("/auth/logout")
async def logout(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == sha256_text(body.refresh_token)))
    if row and row.revoked_at is None:
        row.revoked_at = utcnow()
    return {"ok": True}


@router.get("/me")
async def me(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    settings = request.app.state.ctx.settings
    prefs = await db.get(UserSettings, user.id)
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.display_name,
        "role": user.role,
        "settings": {
            "theme": prefs.theme if prefs else "system",
            "preferredImageProvider": prefs.preferred_image_provider if prefs else "mock",
            "preferredVisionProvider": prefs.preferred_vision_provider if prefs else "auto",
        },
        "quotas": {
            "maxConcurrentJobs": settings.max_concurrent_jobs_per_user,
            "retentionDays": settings.retention_days,
            "maxUploadMb": settings.max_upload_mb,
        },
        "cost": None,
        "costNote": "Provider cost is unknown until a tariff is configured.",
    }


@router.get("/me/settings")
async def get_settings_row(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(UserSettings, user.id)
    if row is None:
        row = UserSettings(user_id=user.id)
        db.add(row)
        await db.flush()
    return {
        "modePreference": row.mode_preference,
        "preferredImageProvider": row.preferred_image_provider,
        "preferredVisionProvider": row.preferred_vision_provider,
        "theme": row.theme,
        "retentionPolicy": row.retention_policy,
    }


@router.delete("/me")
async def delete_me(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    ctx = request.app.state.ctx
    sessions = (await db.scalars(select(Session).where(Session.owner_user_id == user.id, Session.status != "deleted"))).all()
    for session in sessions:
        await soft_delete_session(db, ctx, user, session)
    tokens = (await db.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id))).all()
    for token in tokens:
        token.revoked_at = utcnow()
    user.status = "deleted"
    user.email = None
    user.display_name = "Deleted user"
    user.password_hash = None
    audit(db, user.id, "auth.delete", "user", user.id)
    return {"ok": True}


@router.get("/providers/image")
async def image_providers(request: Request, user: User = Depends(current_user)):
    return provider_catalog(request.app.state.ctx.settings)


@router.get("/providers/evaluation")
async def evaluation_providers(request: Request, user: User = Depends(current_user)):
    return {"providers": provider_catalog(request.app.state.ctx.settings)["evaluation"]}


@router.get("/usage")
async def usage(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.db.models import UsageLedger

    rows = (
        await db.scalars(select(UsageLedger).where(UsageLedger.actor_id == user.id).order_by(UsageLedger.created_at.desc()).limit(20))
    ).all()
    return {
        "costNote": "No tariff is configured. Amounts are null rather than estimated.",
        "jobs": [
            {
                "jobId": row.job_id,
                "provider": row.provider,
                "status": row.status,
                "durationMs": row.duration_ms,
                "costEstimate": None,
                "costActual": None,
                "createdAt": iso(row.created_at),
            }
            for row in rows
        ],
    }


@router.post("/sessions")
async def create_session(body: SessionIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if body.mode not in {"individual", "collaborative"}:
        raise AppError("VALIDATION", "Mode must be individual or collaborative.", 400)
    if body.quality_preset not in {"fast", "balanced", "higher"}:
        raise AppError("VALIDATION", "Unknown quality preset.", 400)
    events: list[dict] = []
    payload = await create_session_record(
        db,
        request.app.state.ctx,
        user,
        events,
        title=body.title,
        mode=body.mode,
        processing_mode=body.processing_mode,
        provider=body.provider,
        worker_id=body.worker_id,
        generation_policy=body.generation_policy,
        vision_provider=body.vision_provider,
        demo=body.demo,
        quality_preset=body.quality_preset,
    )
    request.state.pending_events.extend(events)
    return payload


@router.get("/sessions")
async def list_sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owned = (await db.scalars(select(Session).where(Session.owner_user_id == user.id, Session.status != "deleted"))).all()
    from app.db.models import SessionMember

    member_ids = (
        await db.scalars(select(SessionMember.session_id).where(SessionMember.user_id == user.id, SessionMember.state == "active"))
    ).all()
    extra = []
    if member_ids:
        extra = (
            await db.scalars(select(Session).where(Session.id.in_(member_ids), Session.status != "deleted", Session.owner_user_id != user.id))
        ).all()
    rows = sorted([*owned, *extra], key=lambda row: row.updated_at, reverse=True)
    return {
        "sessions": [
            {
                "sessionId": row.id,
                "title": row.title,
                "mode": row.mode,
                "demo": row.demo,
                "status": row.status,
                "provider": row.provider,
                "processingMode": row.processing_mode,
                "revision": row.revision,
                "updatedAt": iso(row.updated_at),
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id)
    return await session_payload(db, request.app.state.ctx, session)


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: str, body: PatchSessionIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    ctx = request.app.state.ctx
    if body.title:
        session.title = body.title.strip()[:160]
    if body.generation_policy:
        if body.generation_policy not in {"automatic", "batched", "manual"}:
            raise AppError("VALIDATION", "Unknown generation policy.", 400)
        session.generation_policy = body.generation_policy
    if body.quality_preset:
        session.quality_preset = body.quality_preset
    if body.require_publish_approval is not None:
        session.require_publish_approval = body.require_publish_approval
    if body.vision_provider:
        session.vision_provider = resolve_vision(ctx.settings, body.vision_provider, demo=session.demo)
    if body.processing_mode or body.provider or body.worker_id:
        mode, provider, model = resolve_generation(
            ctx.settings,
            user,
            processing_mode=body.processing_mode or session.processing_mode,
            provider=body.provider or session.provider,
            demo=session.demo,
            worker_ok=True if (body.processing_mode or session.processing_mode) != "private_gpu" else body.worker_id is not None or session.worker_id is not None,
        )
        if mode == "private_gpu":
            from app.services.flow import _worker_usable

            worker = await _worker_usable(db, user, body.worker_id or session.worker_id)
            if worker is None:
                raise AppError("WORKER_UNAUTHORIZED", "Pair a private GPU server before using it.", 403)
            session.worker_id = worker.id
        session.processing_mode = mode
        session.provider = provider
        session.model_id = model
    session.updated_at = utcnow()
    events: list[dict] = []
    from app.services.events import push_event

    push_event(db, session, events, "session.updated", {"provider": session.provider, "processingMode": session.processing_mode})
    request.state.pending_events.extend(events)
    return await session_payload(db, ctx, session)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    await soft_delete_session(db, request.app.state.ctx, user, session)
    return {"ok": True}


@router.post("/sessions/{session_id}/source-upload")
async def source_upload(session_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    return await presign_source(db, request.app.state.ctx, user, session)


@router.post("/sessions/{session_id}/source-upload/complete")
async def source_complete(session_id: str, body: CompleteIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    events: list[dict] = []
    payload = await complete_upload(db, request.app.state.ctx, user, session, body.asset_id, body.consent, events)
    request.state.pending_events.extend(events)
    return payload


@router.post("/sessions/{session_id}/feedback")
async def post_feedback(session_id: str, body: FeedbackIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, role = await require_session(db, user, session_id)
    events: list[dict] = []
    payload = await submit_feedback(
        db,
        request.app.state.ctx,
        user,
        session,
        role,
        body.text,
        body.client_submission_id,
        body.expected_session_revision,
        events,
    )
    request.state.pending_events.extend(events)
    return payload


@router.get("/sessions/{session_id}/feedback")
async def list_feedback(session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id)
    rows = (
        await db.scalars(select(Feedback).where(Feedback.session_id == session.id).order_by(Feedback.created_at))
    ).all()
    return {
        "feedback": [
            {
                "feedbackId": row.id,
                "text": row.raw_text,
                "status": row.status,
                "language": row.language,
                "createdAt": iso(row.created_at),
                "mine": row.user_id == user.id,
            }
            for row in rows
        ]
    }


@router.post("/sessions/{session_id}/feedback/{feedback_id}/moderate")
async def moderate(session_id: str, feedback_id: str, body: ModerateIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    row = await db.get(Feedback, feedback_id)
    if row is None or row.session_id != session.id:
        raise AppError("SESSION_NOT_FOUND", "Submission not found.", 404)
    if body.action != "reject":
        raise AppError("VALIDATION", "Supported action is reject.", 400)
    row.status = "rejected"
    session.wordcloud_revision += 1
    session.updated_at = utcnow()
    events: list[dict] = []
    from app.services.events import push_event

    push_event(db, session, events, "wordcloud.updated", {"wordcloudRevision": session.wordcloud_revision})
    request.state.pending_events.extend(events)
    return {"ok": True, "wordcloudRevision": session.wordcloud_revision}


@router.get("/sessions/{session_id}/wordcloud")
async def wordcloud(session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id)
    label = "My design priorities" if session.mode == "individual" else "Community priorities"
    return await wordcloud_payload(db, session.id, label)


@router.get("/sessions/{session_id}/versions")
async def list_versions(session_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id)
    return await versions_payload(db, request.app.state.ctx, session.id)


@router.post("/sessions/{session_id}/versions/{version_id}/publish")
async def publish_version(session_id: str, version_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    version = await db.get(DesignVersion, version_id)
    if version is None or version.session_id != session.id:
        raise AppError("SESSION_NOT_FOUND", "Version not found.", 404)
    version.publish_status = "published"
    session.current_version_id = version.id
    session.updated_at = utcnow()
    events: list[dict] = []
    from app.services.events import push_event

    push_event(db, session, events, "version.published", {"versionId": version.id, "versionNumber": version.version_no})
    request.state.pending_events.extend(events)
    return {"versionId": version.id, "publishStatus": "published"}


@router.post("/sessions/{session_id}/generations")
async def manual_generation(session_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    active = await db.scalar(
        select(GenerationJob).where(GenerationJob.session_id == session.id, GenerationJob.status.in_(["queued", "preparing", "running", "finalizing"]))
    )
    if active:
        return {"policy": "already_running", "jobId": active.id, "status": active.status}
    snapshot = await db.scalar(
        select(PromptSnapshot).where(PromptSnapshot.session_id == session.id).order_by(PromptSnapshot.revision_number.desc()).limit(1)
    )
    if snapshot is None:
        raise AppError("FEEDBACK_EMPTY", "Submit a design idea before generating.", 400)
    result = await enqueue_generation(db, request.app.state.ctx, session, snapshot)
    events: list[dict] = []
    from app.services.events import push_event

    if result.get("jobId"):
        push_event(db, session, events, "generation.queued", {"jobId": result["jobId"], "status": result.get("status")})
    request.state.pending_events.extend(events)
    return result


@router.post("/sessions/{session_id}/invites")
async def invites(session_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session, _role = await require_session(db, user, session_id, owner=True)
    if session.mode != "collaborative":
        raise AppError("VALIDATION", "Switch this session to collaborative before inviting people.", 400)
    code = secrets.token_hex(3).upper()
    return await create_invite(db, session, code)


@router.post("/invitations/redeem")
async def redeem(body: InviteRedeemIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    session = await redeem_invite(db, user, body.code)
    return await session_payload(db, request.app.state.ctx, session)


@router.post("/generations/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    job = await db.get(GenerationJob, job_id)
    if job is None:
        raise AppError("SESSION_NOT_FOUND", "Generation not found.", 404)
    session, _role = await require_session(db, user, job.session_id, owner=True)
    try:
        assert_transition(job.status, "cancel_requested")
    except ValueError as exc:
        raise AppError("VALIDATION", "This generation can no longer be cancelled.", 409) from exc
    job.status = "cancel_requested"
    job.stage_note = "Stopping"
    events: list[dict] = []
    from app.services.events import push_event

    push_event(db, session, events, "generation.cancelled", {"jobId": job.id, "status": "cancel_requested"})
    request.state.pending_events.extend(events)
    return {"jobId": job.id, "status": "cancel_requested"}


@router.get("/generations/{job_id}")
async def get_job(job_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    job = await db.get(GenerationJob, job_id)
    if job is None:
        raise AppError("SESSION_NOT_FOUND", "Generation not found.", 404)
    await require_session(db, user, job.session_id)
    return {
        "jobId": job.id,
        "status": job.status,
        "stage": job.stage_note,
        "provider": job.provider,
        "model": job.model,
        "errorCode": job.error_code,
        "errorMessage": job.error_message,
        "createdAt": iso(job.created_at),
        "startedAt": iso(job.started_at),
        "completedAt": iso(job.completed_at),
    }


@router.post("/versions/{version_id}/evaluations")
async def request_evaluation(version_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    version = await db.get(DesignVersion, version_id)
    if version is None:
        raise AppError("SESSION_NOT_FOUND", "Version not found.", 404)
    session, _role = await require_session(db, user, version.session_id, owner=True)
    from app.services.flow import _enqueue_evaluation

    job = _enqueue_evaluation(db, session, version, request.app.state.ctx.settings)
    if job is None:
        raise AppError("PROVIDER_NOT_CONFIGURED", "Evaluation is disabled for this session.", 409)
    events: list[dict] = []
    from app.services.events import push_event

    push_event(db, session, events, "evaluation.queued", {"versionId": version.id})
    request.state.pending_events.extend(events)
    return {"evaluationJobId": job.id, "status": "queued", "versionId": version.id}


@router.get("/versions/{version_id}/evaluations")
async def get_evaluation(version_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    version = await db.get(DesignVersion, version_id)
    if version is None:
        raise AppError("SESSION_NOT_FOUND", "Version not found.", 404)
    await require_session(db, user, version.session_id)
    return await evaluation_payload(db, version.id)


@router.post("/workers/register")
async def register_worker(body: WorkerIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.role == "guest":
        raise AppError("NOT_AUTHORIZED", "Register an account before adding a GPU server.", 403)
    device = new_token("scw_")
    worker = Worker(
        public_id=public_worker_code(),
        owner_id=user.id,
        friendly_name=body.friendly_name.strip()[:120] or "Private GPU",
        device_token_hash=sha256_text(device),
        agent_version=body.agent_version,
        status="offline",
        capabilities_json={},
    )
    db.add(worker)
    await db.flush()
    audit(db, user.id, "worker.register", "worker", worker.id)
    return {
        "workerId": worker.id,
        "publicId": worker.public_id,
        "deviceToken": device,
        "notice": "Store the device token in the agent. It is shown once.",
    }


@router.post("/workers/{worker_id}/pairing-challenges")
async def create_pairing(worker_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    worker = await _owned_worker(db, user, worker_id)
    code = pairing_code()
    ttl = request.app.state.ctx.settings.pairing_ttl_seconds
    expires = utcnow() + timedelta(seconds=ttl)
    db.add(
        PairingChallenge(
            worker_id=worker.id,
            requester_id=user.id,
            challenge_hash=sha256_text(normalize_pairing_code(code)),
            expires_at=expires,
        )
    )
    audit(db, user.id, "worker.pairing_issued", "worker", worker.id)
    return {"workerId": worker.id, "publicId": worker.public_id, "pairingCode": code, "expiresAt": iso(expires)}


@router.post("/workers/pair")
async def pair_worker(body: PairIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.role == "guest":
        raise AppError("NOT_AUTHORIZED", "Register an account before pairing a GPU server.", 403)
    worker = await db.scalar(select(Worker).where((Worker.id == body.worker_id) | (Worker.public_id == body.worker_id.upper())))
    if worker is None or worker.revoked_at is not None:
        raise AppError("WORKER_UNAUTHORIZED", "No server matches that id.", 404)
    code_hash = sha256_text(normalize_pairing_code(body.pairing_code))
    challenge = await db.scalar(
        select(PairingChallenge)
        .where(PairingChallenge.worker_id == worker.id, PairingChallenge.challenge_hash == code_hash)
        .order_by(PairingChallenge.created_at.desc())
        .limit(1)
    )
    recent_failures = (
        await db.scalar(
            select(PairingChallenge).where(PairingChallenge.worker_id == worker.id).order_by(PairingChallenge.created_at.desc()).limit(1)
        )
    )
    if recent_failures and recent_failures.attempts >= 8:
        raise AppError("PAIRING_RATE_LIMITED", "Too many pairing attempts. Issue a new code.", 429)
    if challenge is None:
        if recent_failures:
            recent_failures.attempts += 1
        audit(db, user.id, "worker.pair_failed", "worker", worker.id)
        raise AppError("PAIRING_CODE_INVALID", "That pairing code is not valid.", 400)
    if challenge.consumed_at is not None:
        raise AppError("PAIRING_CODE_INVALID", "That pairing code was already used.", 400)
    if aware(challenge.expires_at) < utcnow():
        raise AppError("PAIRING_CODE_EXPIRED", "That pairing code has expired. Create a new one on the server.", 400)
    challenge.consumed_at = utcnow()
    grant = WorkerGrant(worker_id=worker.id, user_id=user.id, scopes_json=["run_generation", "view_worker_status"])
    db.add(grant)
    await db.flush()
    audit(db, user.id, "worker.pair", "worker", worker.id, {"grantId": grant.id})
    return {
        "workerGrantId": grant.id,
        "workerId": worker.id,
        "publicId": worker.public_id,
        "friendlyName": worker.friendly_name,
        "status": worker.status,
        "capabilities": worker.capabilities_json,
    }


@router.get("/workers")
async def list_workers(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owned = (await db.scalars(select(Worker).where(Worker.owner_id == user.id, Worker.revoked_at.is_(None)))).all()
    grants = (
        await db.scalars(select(WorkerGrant).where(WorkerGrant.user_id == user.id, WorkerGrant.revoked_at.is_(None)))
    ).all()
    granted = []
    for grant in grants:
        worker = await db.get(Worker, grant.worker_id)
        if worker and worker.revoked_at is None and worker.owner_id != user.id:
            granted.append(worker)
    return {"workers": [_worker_json(worker, owned=worker.owner_id == user.id) for worker in [*owned, *granted]]}


@router.get("/workers/{worker_id}/health")
async def worker_health(worker_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    worker = await _visible_worker(db, user, worker_id)
    return _worker_json(worker, owned=worker.owner_id == user.id)


@router.post("/workers/{worker_id}/unpair")
async def unpair(worker_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    worker = await _visible_worker(db, user, worker_id)
    now = utcnow()
    if worker.owner_id == user.id:
        worker.revoked_at = now
        worker.status = "offline"
        grants = (
            await db.scalars(select(WorkerGrant).where(WorkerGrant.worker_id == worker.id, WorkerGrant.revoked_at.is_(None)))
        ).all()
    else:
        grants = (
            await db.scalars(
                select(WorkerGrant).where(
                    WorkerGrant.worker_id == worker.id,
                    WorkerGrant.user_id == user.id,
                    WorkerGrant.revoked_at.is_(None),
                )
            )
        ).all()
    for grant in grants:
        grant.revoked_at = now
    audit(db, user.id, "worker.unpair", "worker", worker.id)
    return {"ok": True, "removed": True}


@router.post("/workers/{worker_id}/test")
async def test_worker(worker_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    worker = await _visible_worker(db, user, worker_id)
    return {
        "workerId": worker.id,
        "status": worker.status,
        "capabilities": worker.capabilities_json,
        "notice": "No test image was sent. This only reports the last heartbeat.",
    }


def _worker_json(worker: Worker, owned: bool) -> dict:
    return {
        "workerId": worker.id,
        "publicId": worker.public_id,
        "friendlyName": worker.friendly_name,
        "status": worker.status,
        "owned": owned,
        "lastSeen": iso(worker.last_seen),
        "agentVersion": worker.agent_version,
        "capabilities": worker.capabilities_json,
    }


async def _owned_worker(db: AsyncSession, user: User, worker_id: str) -> Worker:
    worker = await db.scalar(select(Worker).where((Worker.id == worker_id) | (Worker.public_id == worker_id.upper())))
    if worker is None or worker.revoked_at is not None or worker.owner_id != user.id:
        raise AppError("NOT_AUTHORIZED", "Only the server owner can do that.", 403)
    return worker


async def _visible_worker(db: AsyncSession, user: User, worker_id: str) -> Worker:
    worker = await db.scalar(select(Worker).where((Worker.id == worker_id) | (Worker.public_id == worker_id.upper())))
    if worker is None or worker.revoked_at is not None:
        raise AppError("WORKER_UNAUTHORIZED", "Server not found.", 404)
    if worker.owner_id == user.id:
        return worker
    grant = await db.scalar(
        select(WorkerGrant).where(WorkerGrant.worker_id == worker.id, WorkerGrant.user_id == user.id, WorkerGrant.revoked_at.is_(None))
    )
    if grant is None:
        raise AppError("NOT_AUTHORIZED", "You do not have access to that server.", 403)
    return worker


async def _agent_worker(request: Request, db: AsyncSession) -> Worker:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise AppError("WORKER_UNAUTHORIZED", "Agent credential missing.", 401)
    token = header.split(" ", 1)[1].strip()
    worker = await db.scalar(select(Worker).where(Worker.device_token_hash == sha256_text(token), Worker.revoked_at.is_(None)))
    if worker is None:
        raise AppError("WORKER_UNAUTHORIZED", "Agent credential was rejected.", 401)
    return worker


@router.post("/agent/v1/heartbeat")
async def heartbeat(request: Request, db: AsyncSession = Depends(get_db)):
    worker = await _agent_worker(request, db)
    body = await request.json()
    worker.last_seen = utcnow()
    worker.status = "available" if body.get("available", True) else "degraded"
    worker.capabilities_json = body.get("capabilities") or worker.capabilities_json
    worker.agent_version = body.get("agentVersion") or worker.agent_version
    return {"ok": True, "workerId": worker.id, "status": worker.status}


@router.post("/agent/v1/jobs/lease")
async def lease_job(request: Request, db: AsyncSession = Depends(get_db)):
    worker = await _agent_worker(request, db)
    if worker.status == "offline":
        worker.status = "available"
    worker.last_seen = utcnow()
    job = await db.scalar(
        select(GenerationJob)
        .where(GenerationJob.worker_id == worker.id, GenerationJob.status == "queued", GenerationJob.provider == "comfyui_private")
        .order_by(GenerationJob.created_at)
        .limit(1)
    )
    if job is None:
        return {"job": None}
    snapshot = await db.get(PromptSnapshot, job.prompt_snapshot_id)
    asset = await db.get(SceneAsset, job.source_asset_id)
    lease_token = new_token("lease_")
    ttl = request.app.state.ctx.settings.lease_ttl_seconds
    db.add(
        WorkerLease(
            worker_id=worker.id,
            job_id=job.id,
            lease_token_hash=sha256_text(lease_token),
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
    )
    assert_transition(job.status, "preparing")
    job.status = "preparing"
    job.started_at = utcnow()
    job.stage_note = "Preparing on private GPU"
    worker.status = "busy"
    from app.adapters.evaluation import unload_ollama_models
    from app.services.flow import _path

    await unload_ollama_models(request.app.state.ctx.settings)

    return {
        "job": {
            "jobId": job.id,
            "leaseToken": lease_token,
            "prompt": snapshot.compiled_prompt if snapshot else "",
            "negativePrompt": snapshot.negative_prompt if snapshot else "",
            "workflowId": "street-img2img-v1",
            "sourceImagePath": _path(request.app.state.ctx.settings, "media", asset.id) if asset else None,
            "outputPath": f"/api/v1/agent/v1/jobs/{job.id}/output",
            "params": {"steps": 20, "cfg": 6.5, "denoise": 0.45, "width": 832, "height": 512},
        }
    }


@router.post("/agent/v1/jobs/{job_id}/output")
async def agent_output(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    worker = await _agent_worker(request, db)
    lease_token = request.headers.get("x-lease-token", "")
    lease = await db.scalar(
        select(WorkerLease).where(
            WorkerLease.job_id == job_id,
            WorkerLease.worker_id == worker.id,
            WorkerLease.lease_token_hash == sha256_text(lease_token),
            WorkerLease.status == "active",
        )
    )
    if lease is None or aware(lease.expires_at) < utcnow():
        raise AppError("WORKER_JOB_LEASE_EXPIRED", "The job lease is not active.", 403)
    job = await db.get(GenerationJob, job_id)
    if job is None or job.worker_id != worker.id:
        raise AppError("WORKER_UNAUTHORIZED", "Job not found for this server.", 404)
    if job.status == "succeeded":
        return {"ok": True, "status": "succeeded"}
    data = await request.body()
    ctx = request.app.state.ctx
    from app.db.base import new_id
    from app.services.events import push_event
    from app.services.flow import _enqueue_evaluation
    from app.services.images import normalize_image
    from app.services.jobs import _next_version_no

    normalized, width, height, digest = normalize_image(data, ctx.settings.max_upload_mb * 1024 * 1024, ctx.settings.max_megapixels)
    # Private worker results are real only when the agent did not mark them as mock.
    demo_labeled = request.headers.get("x-streetcloud-demo", "") == "1"
    asset = SceneAsset(
        session_id=job.session_id,
        uploaded_by=worker.owner_id,
        object_key=f"sessions/{job.session_id}/{new_id()}.jpg",
        mime="image/jpeg",
        width=width,
        height=height,
        sha256=digest,
        byte_size=len(normalized),
        kind="generated",
        status="stored",
        demo_labeled=demo_labeled,
    )
    db.add(asset)
    await db.flush()
    await ctx.storage.put(asset.object_key, normalized, "image/jpeg")
    session = await db.get(Session, job.session_id)
    version_no = await _next_version_no(db, job.session_id)
    snapshot = await db.get(PromptSnapshot, job.prompt_snapshot_id)
    version = DesignVersion(
        session_id=job.session_id,
        version_no=version_no,
        generation_job_id=job.id,
        image_asset_id=asset.id,
        publish_status="candidate" if session and session.require_publish_approval else "published",
        checksum=digest,
        provider="comfyui_private",
        model=job.model,
        prompt_excerpt=(snapshot.compiled_prompt[:280] if snapshot else ""),
        demo_labeled=demo_labeled,
        based_on_original=True,
    )
    db.add(version)
    await db.flush()
    assert_transition(job.status, "finalizing")
    job.status = "finalizing"
    assert_transition(job.status, "succeeded")
    job.status = "succeeded"
    job.completed_at = utcnow()
    job.stage_note = "Ready"
    lease.status = "completed"
    worker.status = "available"
    worker.last_seen = utcnow()
    events: list[dict] = []
    if session:
        if version.publish_status == "published":
            session.current_version_id = version.id
        session.status = "ready"
        session.updated_at = utcnow()
        _enqueue_evaluation(db, session, version, request.app.state.ctx.settings)
        push_event(db, session, events, "generation.succeeded", {"jobId": job.id, "versionId": version.id, "versionNumber": version.version_no, "demoLabeled": demo_labeled})
        request.state.pending_events.extend(events)
    return {"ok": True, "versionId": version.id, "demoLabeled": demo_labeled}


@router.post("/agent/v1/jobs/{job_id}/fail")
async def agent_fail(job_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    worker = await _agent_worker(request, db)
    job = await db.get(GenerationJob, job_id)
    if job is None or job.worker_id != worker.id:
        raise AppError("WORKER_UNAUTHORIZED", "Job not found for this server.", 404)
    body = await request.json()
    if job.status not in {"succeeded", "failed", "cancelled"}:
        job.status = "failed"
        job.error_code = str(body.get("errorCode") or "GENERATION_INVALID_OUTPUT")[:64]
        job.error_message = str(body.get("message") or "The private GPU could not finish the image.")[:400]
        job.completed_at = utcnow()
    worker.status = "available"
    session = await db.get(Session, job.session_id)
    if session:
        session.status = "ready"
        events: list[dict] = []
        from app.services.events import push_event

        push_event(db, session, events, "generation.failed", {"jobId": job.id, "errorCode": job.error_code, "errorMessage": job.error_message})
        request.state.pending_events.extend(events)
    return {"ok": True}


@router.put("/uploads/{asset_id}")
async def upload_bytes(asset_id: str, request: Request, exp: int, sig: str):
    from app.services.flow import accept_upload

    await accept_upload(request.app.state.ctx, asset_id, f"{exp}.{sig}", await request.body())
    return Response(status_code=204)


@router.get("/media/{asset_id}")
async def media(asset_id: str, request: Request, exp: int, sig: str):
    if not verify_subject(request.app.state.ctx.settings, "media", asset_id, f"{exp}.{sig}"):
        raise AppError("NOT_AUTHORIZED", "This image link has expired.", 403)
    ctx = request.app.state.ctx
    async with ctx.sessionmaker() as db:
        asset = await db.get(SceneAsset, asset_id)
        if asset is None or asset.status not in {"stored", "uploaded"}:
            raise AppError("SESSION_NOT_FOUND", "Image not found.", 404)
        data = await ctx.storage.get(asset.object_key)
        mime = asset.mime
    return Response(content=data, media_type=mime)


@router.get("/versions/{version_id}/export")
async def export_version(version_id: str, request: Request, exp: int, sig: str):
    if not verify_subject(request.app.state.ctx.settings, "export", version_id, f"{exp}.{sig}"):
        raise AppError("NOT_AUTHORIZED", "This export link has expired.", 403)
    ctx = request.app.state.ctx
    async with ctx.sessionmaker() as db:
        version = await db.get(DesignVersion, version_id)
        if version is None:
            raise AppError("SESSION_NOT_FOUND", "Version not found.", 404)
        asset = await db.get(SceneAsset, version.image_asset_id)
        data = await ctx.storage.get(asset.object_key)
    exported = render_export(data)
    return Response(content=exported, media_type="image/jpeg")


@router.websocket("/events")
async def events_socket(websocket: WebSocket):
    await websocket.accept()
    ctx = websocket.app.state.ctx
    try:
        message = await websocket.receive_json()
    except Exception:
        await websocket.close(code=4401)
        return
    token = message.get("token") if isinstance(message, dict) else None
    session_id = message.get("sessionId") if isinstance(message, dict) else None
    last_sequence = int(message.get("lastSequence") or 0)
    try:
        payload = decode_access_token(ctx.settings, token or "")
    except jwt.PyJWTError:
        await websocket.send_json({"error": {"code": "AUTH_REQUIRED", "message": "Sign in again to continue."}})
        await websocket.close(code=4401)
        return
    async with ctx.sessionmaker() as db:
        user = await db.get(User, payload.get("sub"))
        if user is None:
            await websocket.close(code=4401)
            return
        try:
            session, _role = await require_session(db, user, session_id)
        except AppError:
            await websocket.send_json({"error": {"code": "NOT_AUTHORIZED", "message": "You do not have access to this session."}})
            await websocket.close(code=4403)
            return
        missed = (
            await db.scalars(
                select(EventOutbox)
                .where(EventOutbox.session_id == session.id, EventOutbox.seq_no > last_sequence)
                .order_by(EventOutbox.seq_no)
            )
        ).all()
        replay = [row.payload_json for row in missed]
    await ctx.hub.register(session_id, websocket)
    try:
        for event in replay:
            await websocket.send_json(event)
        while True:
            incoming = await websocket.receive_json()
            if isinstance(incoming, dict) and incoming.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        ctx.hub.unregister(session_id, websocket)
