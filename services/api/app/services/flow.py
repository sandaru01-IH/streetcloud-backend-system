from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import AppContext
from app.core.errors import AppError
from app.core.security import sha256_text, sign_subject
from app.db.base import new_id, utcnow
from app.db.models import (
    AuditLog,
    Concept,
    ConsentRecord,
    DesignVersion,
    EvaluationJob,
    EvaluationPropertyScore,
    EvaluationResult,
    Feedback,
    FeedbackConcept,
    GenerationJob,
    PromptSnapshot,
    SceneAsset,
    Session,
    SessionInvite,
    SessionMember,
    UsageLedger,
    User,
    Worker,
    WorkerGrant,
)
from app.services.access import aware, iso
from app.services.brief import NEGATIVE_PROMPT, compile_prompt, mutate_brief
from app.services.concepts import (
    EXTRACTOR_VERSION,
    TAXONOMY_VERSION,
    detect_language,
    extract_concepts,
)
from app.services.events import push_event
from app.services.images import normalize_image, render_sample_street
from app.services.providers import resolve_generation, resolve_vision
from app.services.state_machine import ACTIVE_STATUSES

NOTICE_VERSION = "privacy-2026-10-10"


def _path(settings: Settings, purpose: str, subject: str) -> str:
    exp, sig = sign_subject(settings, purpose, subject).split(".", 1)
    if purpose == "upload":
        return f"/api/v1/uploads/{subject}?exp={exp}&sig={sig}"
    if purpose == "export":
        return f"/api/v1/versions/{subject}/export?exp={exp}&sig={sig}"
    return f"/api/v1/media/{subject}?exp={exp}&sig={sig}"


def audit(db: AsyncSession, actor_id: str | None, action: str, subject_type: str, subject_id: str, metadata: dict | None = None) -> None:
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            metadata_json=metadata or {},
        )
    )


async def _worker_usable(db: AsyncSession, user: User, worker_id: str | None) -> Worker | None:
    if not worker_id:
        return None
    worker = await db.scalar(select(Worker).where((Worker.id == worker_id) | (Worker.public_id == worker_id)))
    if worker is None or worker.revoked_at is not None:
        return None
    if worker.owner_id == user.id:
        return worker
    grant = await db.scalar(
        select(WorkerGrant).where(
            WorkerGrant.worker_id == worker.id,
            WorkerGrant.user_id == user.id,
            WorkerGrant.revoked_at.is_(None),
        )
    )
    return worker if grant else None


async def create_session_record(
    db: AsyncSession,
    ctx: AppContext,
    user: User,
    events: list[dict],
    *,
    title: str,
    mode: str,
    processing_mode: str,
    provider: str | None,
    worker_id: str | None,
    generation_policy: str,
    vision_provider: str | None,
    demo: bool,
    quality_preset: str,
) -> dict:
    worker = None
    if processing_mode == "private_gpu" and not demo:
        worker = await _worker_usable(db, user, worker_id)
    mode_name, provider_name, model_id = resolve_generation(
        ctx.settings,
        user,
        processing_mode=processing_mode,
        provider=provider,
        demo=demo,
        worker_ok=worker is not None,
    )
    vision = resolve_vision(ctx.settings, vision_provider, demo=demo)
    if mode == "collaborative" and user.role == "guest":
        raise AppError("NOT_AUTHORIZED", "Collaborative sessions require a registered account.", 403)
    session = Session(
        owner_user_id=user.id,
        title=title.strip()[:160] or "Untitled street",
        mode=mode,
        processing_mode=mode_name,
        provider=provider_name,
        model_id=model_id,
        worker_id=worker.id if worker else None,
        generation_policy="automatic" if mode == "individual" and generation_policy == "automatic" else generation_policy,
        quality_preset=quality_preset,
        vision_provider=vision,
        status="ready" if demo else "awaiting_photo",
        demo=demo,
        require_publish_approval=False,
    )
    if mode == "collaborative" and generation_policy not in {"automatic", "batched", "manual"}:
        raise AppError("PROVIDER_NOT_CONFIGURED", "Unknown generation policy.", 400)
    db.add(session)
    await db.flush()
    db.add(SessionMember(session_id=session.id, user_id=user.id, role="owner"))
    if demo:
        await _store_version_zero(db, ctx, session, user, render_sample_street(), demo_labeled=True, events=events)
        db.add(
            ConsentRecord(
                user_id=user.id,
                session_id=session.id,
                processing_target="demo_sample",
                notice_version=NOTICE_VERSION,
            )
        )
    audit(db, user.id, "session.create", "session", session.id, {"demo": demo, "provider": provider_name})
    push_event(db, session, events, "session.updated", {"status": session.status, "demo": demo})
    return await session_payload(db, ctx, session)


async def _store_version_zero(db, ctx, session, user, raw: bytes, demo_labeled: bool, events: list[dict]) -> DesignVersion:
    data, width, height, digest = normalize_image(
        raw, ctx.settings.max_upload_mb * 1024 * 1024, ctx.settings.max_megapixels
    )
    asset = SceneAsset(
        session_id=session.id,
        uploaded_by=user.id,
        object_key=f"sessions/{session.id}/{new_id()}.jpg",
        mime="image/jpeg",
        width=width,
        height=height,
        sha256=digest,
        byte_size=len(data),
        kind="original",
        status="stored",
        demo_labeled=demo_labeled,
    )
    db.add(asset)
    await db.flush()
    await ctx.storage.put(asset.object_key, data, "image/jpeg")
    version = DesignVersion(
        session_id=session.id,
        version_no=0,
        image_asset_id=asset.id,
        publish_status="published",
        checksum=digest,
        provider="original",
        model="",
        prompt_excerpt="",
        demo_labeled=demo_labeled,
        based_on_original=True,
    )
    db.add(version)
    await db.flush()
    session.current_version_id = version.id
    session.status = "ready"
    session.updated_at = utcnow()
    _enqueue_evaluation(db, session, version, ctx.settings)
    return version


def _enqueue_evaluation(
    db: AsyncSession,
    session: Session,
    version: DesignVersion,
    settings: Settings | None = None,
) -> EvaluationJob | None:
    if settings is not None and not session.demo and session.vision_provider in {"unconfigured", "private_vlm"}:
        from app.services.providers import resolve_vision

        session.vision_provider = resolve_vision(settings, "auto", demo=False)
    if session.vision_provider == "disabled":
        return None
    model = ""
    if settings is not None:
        if session.vision_provider == "ollama_vision":
            model = settings.ollama_vision_model
        elif session.vision_provider == "openai_vision":
            model = settings.openai_vision_model
        elif session.vision_provider == "gemini_vision":
            model = settings.gemini_vision_model
    job = EvaluationJob(
        session_id=session.id,
        version_id=version.id,
        evaluator_provider=session.vision_provider,
        model=model,
        status="queued",
    )
    db.add(job)
    return job


async def presign_source(db: AsyncSession, ctx: AppContext, user: User, session: Session) -> dict:
    if session.status == "deleted":
        raise AppError("SESSION_NOT_FOUND", "Session not found.", 404)
    asset = SceneAsset(
        session_id=session.id,
        uploaded_by=user.id,
        object_key=f"sessions/{session.id}/{new_id()}.jpg",
        kind="original",
        status="pending",
    )
    db.add(asset)
    await db.flush()
    return {
        "assetId": asset.id,
        "uploadPath": _path(ctx.settings, "upload", asset.id),
        "method": "PUT",
        "headers": {"Content-Type": "application/octet-stream"},
    }


async def accept_upload(ctx: AppContext, asset_id: str, token: str, data: bytes) -> None:
    from app.core.security import verify_subject

    if not verify_subject(ctx.settings, "upload", asset_id, token):
        raise AppError("UPLOAD_FAILED", "The upload link is invalid or expired.", 403)
    async with ctx.sessionmaker() as db:
        asset = await db.get(SceneAsset, asset_id)
        if asset is None or asset.status not in {"pending", "uploaded"}:
            raise AppError("UPLOAD_FAILED", "This upload is no longer available.", 404)
        normalized, width, height, digest = normalize_image(
            data, ctx.settings.max_upload_mb * 1024 * 1024, ctx.settings.max_megapixels
        )
        await ctx.storage.put(asset.object_key, normalized, "image/jpeg")
        asset.mime = "image/jpeg"
        asset.width = width
        asset.height = height
        asset.sha256 = digest
        asset.byte_size = len(normalized)
        asset.status = "uploaded"
        await db.commit()


async def complete_upload(
    db: AsyncSession,
    ctx: AppContext,
    user: User,
    session: Session,
    asset_id: str,
    consent: bool,
    events: list[dict],
) -> dict:
    if not consent:
        raise AppError("NOT_AUTHORIZED", "Confirm third-party or local processing consent before upload.", 400)
    asset = await db.get(SceneAsset, asset_id)
    if asset is None or asset.session_id != session.id:
        raise AppError("UPLOAD_FAILED", "Unknown upload.", 404)
    existing = await db.scalar(
        select(DesignVersion).where(DesignVersion.session_id == session.id, DesignVersion.version_no == 0)
    )
    if existing:
        return {"versionId": existing.id, "versionNumber": 0, "imagePath": _path(ctx.settings, "media", existing.image_asset_id)}
    if asset.status != "uploaded":
        raise AppError("UPLOAD_FAILED", "The photo has not finished uploading.", 409)
    asset.status = "stored"
    version = DesignVersion(
        session_id=session.id,
        version_no=0,
        image_asset_id=asset.id,
        publish_status="published",
        checksum=asset.sha256,
        provider="original",
        based_on_original=True,
    )
    db.add(version)
    db.add(
        ConsentRecord(
            user_id=user.id,
            session_id=session.id,
            processing_target=session.provider,
            notice_version=NOTICE_VERSION,
        )
    )
    await db.flush()
    session.current_version_id = version.id
    session.status = "ready"
    session.updated_at = utcnow()
    _enqueue_evaluation(db, session, version, ctx.settings)
    push_event(db, session, events, "session.updated", {"status": "ready", "versionId": version.id})
    return {"versionId": version.id, "versionNumber": 0, "imagePath": _path(ctx.settings, "media", asset.id)}


async def _latest_snapshot(db: AsyncSession, session_id: str) -> PromptSnapshot | None:
    return await db.scalar(
        select(PromptSnapshot)
        .where(PromptSnapshot.session_id == session_id)
        .order_by(PromptSnapshot.revision_number.desc())
        .limit(1)
    )


async def _original_asset(db: AsyncSession, session_id: str) -> SceneAsset | None:
    return await db.scalar(
        select(SceneAsset).where(
            SceneAsset.session_id == session_id,
            SceneAsset.kind == "original",
            SceneAsset.status == "stored",
        )
    )


async def _active_job(db: AsyncSession, session_id: str) -> GenerationJob | None:
    return await db.scalar(
        select(GenerationJob)
        .where(GenerationJob.session_id == session_id, GenerationJob.status.in_(ACTIVE_STATUSES))
        .order_by(GenerationJob.created_at.desc())
        .limit(1)
    )


async def community_labels(db: AsyncSession, session_id: str) -> list[str]:
    payload = await wordcloud_payload(db, session_id, label="Community priorities")
    ranked = sorted(payload["concepts"], key=lambda item: (-item["submissionCount"], item["label"]))
    return [item["label"] for item in ranked[:8]]


async def submit_feedback(
    db: AsyncSession,
    ctx: AppContext,
    user: User,
    session: Session,
    role: str,
    text: str,
    client_submission_id: str,
    expected_revision: int,
    events: list[dict],
) -> dict:
    cleaned = text.strip()
    if not cleaned:
        raise AppError("FEEDBACK_EMPTY", "Enter a design idea before submitting.", 400)
    if len(cleaned) > 2000:
        raise AppError("FEEDBACK_EMPTY", "Keep the design note under 2000 characters.", 400)
    if role == "participant" and session.mode != "collaborative":
        raise AppError("NOT_AUTHORIZED", "You cannot add design notes to this session.", 403)
    existing = await db.scalar(
        select(Feedback).where(Feedback.session_id == session.id, Feedback.idempotency_key == client_submission_id)
    )
    if existing:
        if existing.raw_text != cleaned:
            raise AppError("SESSION_REVISION_CONFLICT", "This submission id was already used with different text.", 409)
        return existing.response_json or {"feedbackId": existing.id, "accepted": True}
    if session.revision != expected_revision:
        raise AppError(
            "SESSION_REVISION_CONFLICT",
            "This session changed. Refresh and submit again.",
            409,
            details={"sessionRevision": session.revision},
        )
    if session.status not in {"ready", "generating"}:
        raise AppError("UPLOAD_FAILED", "Add a street photo before submitting a design idea.", 409)
    recent = await db.scalar(
        select(func.count())
        .select_from(Feedback)
        .where(Feedback.user_id == user.id, Feedback.created_at >= utcnow() - timedelta(minutes=1))
    )
    if recent and recent > 20:
        raise AppError("FEEDBACK_RATE_LIMITED", "Too many submissions. Wait a moment and try again.", 429, retryable=True)

    feedback = Feedback(
        session_id=session.id,
        user_id=user.id,
        raw_text=cleaned,
        language=detect_language(cleaned),
        status="accepted",
        idempotency_key=client_submission_id,
    )
    db.add(feedback)
    await db.flush()
    for concept in extract_concepts(cleaned):
        row = await db.scalar(select(Concept).where(Concept.canonical_slug == concept["slug"]))
        if row is None:
            row = Concept(
                canonical_slug=concept["slug"],
                display_label=concept["label"],
                language=concept["language"],
                category=concept["category"],
                synonyms_json=[concept["source_phrase"]],
                taxonomy_version=TAXONOMY_VERSION,
            )
            db.add(row)
            await db.flush()
        db.add(
            FeedbackConcept(
                feedback_id=feedback.id,
                concept_id=row.id,
                confidence=concept["confidence"],
                source_phrase=concept["source_phrase"],
                extractor_version=EXTRACTOR_VERSION,
            )
        )
    previous = await _latest_snapshot(db, session.id)
    labels = await community_labels(db, session.id) if session.mode == "collaborative" else []
    brief = mutate_brief(previous.interventions_json if previous else None, cleaned, feedback.id, labels)
    session.revision += 1
    session.wordcloud_revision += 1
    session.updated_at = utcnow()
    brief["revision"] = session.revision
    snapshot = PromptSnapshot(
        session_id=session.id,
        revision_number=session.revision,
        interventions_json=brief,
        compiled_prompt=compile_prompt(brief),
        negative_prompt=NEGATIVE_PROMPT,
    )
    db.add(snapshot)
    await db.flush()
    generation = await _schedule_generation(db, ctx, user, session, snapshot)
    response = {
        "feedbackId": feedback.id,
        "accepted": True,
        "sessionRevision": session.revision,
        "wordcloudRevision": session.wordcloud_revision,
        "conflicts": brief.get("conflicts") or [],
        "generation": generation,
    }
    feedback.response_json = response
    push_event(db, session, events, "feedback.accepted", {"feedbackId": feedback.id})
    push_event(db, session, events, "wordcloud.updated", {"wordcloudRevision": session.wordcloud_revision})
    if generation.get("jobId"):
        push_event(
            db,
            session,
            events,
            "generation.queued" if generation.get("status") == "queued" else "generation.failed",
            {"jobId": generation["jobId"], "status": generation.get("status")},
        )
    return response


async def _schedule_generation(db, ctx, user, session, snapshot: PromptSnapshot) -> dict:
    if session.generation_policy == "manual":
        return {"policy": "manual", "jobId": None, "status": None}
    active = await _active_job(db, session.id)
    if session.generation_policy == "batched":
        session.batch_due_at = utcnow() + timedelta(seconds=session.batch_window_seconds)
        session.needs_followup = True
        return {
            "policy": "batched",
            "batchId": session.id,
            "nextEligibleAt": iso(session.batch_due_at),
            "status": "waiting",
        }
    if active:
        session.needs_followup = True
        return {"policy": "coalesced", "jobId": active.id, "status": active.status}
    running = await db.scalar(
        select(func.count())
        .select_from(GenerationJob)
        .join(Session, Session.id == GenerationJob.session_id)
        .where(Session.owner_user_id == user.id, GenerationJob.status.in_(ACTIVE_STATUSES))
    )
    if running and running >= ctx.settings.max_concurrent_jobs_per_user:
        return {
            "policy": "blocked",
            "error": {
                "code": "PROVIDER_QUOTA_EXCEEDED",
                "message": "A generation is already running for your account. It will stay in history; retry when it finishes.",
            },
        }
    return await enqueue_generation(db, ctx, session, snapshot)


async def enqueue_generation(db: AsyncSession, ctx: AppContext, session: Session, snapshot: PromptSnapshot) -> dict:
    asset = await _original_asset(db, session.id)
    if asset is None:
        raise AppError("UPLOAD_FAILED", "The original street photo is missing.", 409)
    status = "queued"
    error_code = None
    error_message = None
    if session.provider == "comfyui_private":
        worker = await db.get(Worker, session.worker_id) if session.worker_id else None
        if worker is None or worker.revoked_at is not None or not _worker_online(worker, ctx.settings):
            status = "worker_disconnected"
            error_code = "WORKER_OFFLINE"
            error_message = "The private GPU is offline. Reconnect it, then retry. The photo was not sent to a cloud provider."
    job = GenerationJob(
        session_id=session.id,
        source_asset_id=asset.id,
        prompt_snapshot_id=snapshot.id,
        mode=session.processing_mode,
        provider=session.provider,
        model=session.model_id,
        worker_id=session.worker_id,
        status=status,
        prompt_revision=snapshot.revision_number,
        error_code=error_code,
        error_message=error_message,
        stage_note="Queued" if status == "queued" else "Worker offline",
    )
    db.add(job)
    await db.flush()
    session.needs_followup = False
    session.batch_due_at = None
    session.status = "generating" if status == "queued" else "ready"
    return {
        "policy": "automatic",
        "jobId": job.id,
        "status": job.status,
        "error": None if not error_code else {"code": error_code, "message": error_message},
    }


def _worker_online(worker: Worker, settings: Settings) -> bool:
    if worker.status in {"revoked", "offline"}:
        return False
    if worker.last_seen is None:
        return False
    seen = aware(worker.last_seen)
    return seen is not None and seen >= utcnow() - timedelta(seconds=settings.worker_offline_seconds)


async def wordcloud_payload(db: AsyncSession, session_id: str, label: str) -> dict:
    rows = (
        await db.execute(
            select(Concept, Feedback)
            .join(FeedbackConcept, FeedbackConcept.concept_id == Concept.id)
            .join(Feedback, Feedback.id == FeedbackConcept.feedback_id)
            .where(Feedback.session_id == session_id, Feedback.status == "accepted")
        )
    ).all()
    bucket: dict[str, dict] = {}
    for concept, feedback in rows:
        item = bucket.setdefault(
            concept.id,
            {
                "conceptId": concept.id,
                "slug": concept.canonical_slug,
                "label": concept.display_label,
                "category": concept.category,
                "submissionIds": set(),
                "contributors": set(),
            },
        )
        item["submissionIds"].add(feedback.id)
        item["contributors"].add(feedback.user_id)
    concepts = []
    for item in bucket.values():
        count = len(item["submissionIds"])
        concepts.append(
            {
                "conceptId": item["conceptId"],
                "slug": item["slug"],
                "label": item["label"],
                "category": item["category"],
                "submissionCount": count,
                "contributorCount": len(item["contributors"]),
                "weight": count,
            }
        )
    concepts.sort(key=lambda row: (-row["submissionCount"], row["label"]))
    session = await db.get(Session, session_id)
    return {
        "revision": session.wordcloud_revision if session else 0,
        "label": label,
        "updatedAt": iso(session.updated_at) if session else None,
        "concepts": concepts,
    }


async def session_payload(db: AsyncSession, ctx: AppContext, session: Session) -> dict:
    original = await _original_asset(db, session.id)
    current = await db.get(DesignVersion, session.current_version_id) if session.current_version_id else None
    active = await _active_job(db, session.id)
    latest = await _latest_snapshot(db, session.id)
    return {
        "sessionId": session.id,
        "title": session.title,
        "mode": session.mode,
        "demo": session.demo,
        "status": session.status,
        "processingMode": session.processing_mode,
        "provider": session.provider,
        "model": session.model_id,
        "workerId": session.worker_id,
        "visionProvider": session.vision_provider,
        "generationPolicy": session.generation_policy,
        "qualityPreset": session.quality_preset,
        "revision": session.revision,
        "wordcloudRevision": session.wordcloud_revision,
        "eventSequence": session.event_seq,
        "requirePublishApproval": session.require_publish_approval,
        "conflicts": (latest.interventions_json or {}).get("conflicts", []) if latest else [],
        "originalImagePath": _path(ctx.settings, "media", original.id) if original else None,
        "currentVersion": None
        if current is None
        else {
            "versionId": current.id,
            "versionNumber": current.version_no,
            "imagePath": _path(ctx.settings, "media", current.image_asset_id),
            "publishStatus": current.publish_status,
            "provider": current.provider,
            "demoLabeled": current.demo_labeled,
            "createdAt": iso(current.created_at),
            "promptExcerpt": current.prompt_excerpt,
        },
        "activeJob": None
        if active is None
        else {
            "jobId": active.id,
            "status": active.status,
            "stage": active.stage_note,
            "provider": active.provider,
            "errorCode": active.error_code,
            "errorMessage": active.error_message,
            "createdAt": iso(active.created_at),
            "startedAt": iso(active.started_at),
        },
        "updatedAt": iso(session.updated_at),
        "disclaimer": "AI-generated concept — not a surveyed or approved design.",
    }


async def versions_payload(db: AsyncSession, ctx: AppContext, session_id: str) -> dict:
    rows = (
        await db.scalars(
            select(DesignVersion)
            .where(DesignVersion.session_id == session_id, DesignVersion.deleted_at.is_(None))
            .order_by(DesignVersion.version_no)
        )
    ).all()
    return {
        "versions": [
            {
                "versionId": row.id,
                "versionNumber": row.version_no,
                "publishStatus": row.publish_status,
                "imagePath": _path(ctx.settings, "media", row.image_asset_id),
                "exportPath": _path(ctx.settings, "export", row.id),
                "provider": row.provider,
                "model": row.model,
                "demoLabeled": row.demo_labeled,
                "promptExcerpt": row.prompt_excerpt,
                "createdAt": iso(row.created_at),
            }
            for row in rows
        ]
    }


async def evaluation_payload(db: AsyncSession, version_id: str) -> dict | None:
    result = await db.scalar(
        select(EvaluationResult)
        .where(EvaluationResult.version_id == version_id)
        .order_by(EvaluationResult.created_at.desc())
        .limit(1)
    )
    job = await db.scalar(
        select(EvaluationJob)
        .where(EvaluationJob.version_id == version_id)
        .order_by(EvaluationJob.created_at.desc())
        .limit(1)
    )
    if job is not None and job.status in {"queued", "running"} and (result is None or job.created_at >= result.created_at):
        return {
            "versionId": version_id,
            "status": job.status,
            "errorCode": None,
            "errorMessage": None,
            "provider": job.evaluator_provider,
            "model": job.model or None,
            "result": None,
        }
    if result is None:
        return {
            "versionId": version_id,
            "status": job.status if job else "not_requested",
            "errorCode": job.error_code if job else None,
            "errorMessage": job.error_message if job else None,
            "provider": job.evaluator_provider if job else None,
            "model": job.model or None if job else None,
            "result": None,
        }
    scores = (
        await db.scalars(select(EvaluationPropertyScore).where(EvaluationPropertyScore.result_id == result.id))
    ).all()
    order = {row.property_key: row for row in scores}
    from app.services.rubric import PROPERTY_BY_KEY, PROPERTY_KEYS

    assessments = []
    for key in PROPERTY_KEYS:
        row = order.get(key)
        if row is None:
            continue
        assessments.append(
            {
                "key": key,
                "name": PROPERTY_BY_KEY[key]["name"],
                "applicability": row.applicability,
                "score": row.score,
                "confidence": row.confidence,
                "evidence": row.evidence_text,
                "caveat": row.caveat_text,
            }
        )
    return {
        "versionId": version_id,
        "status": "succeeded",
        "evaluationId": result.id,
        "rubricVersion": result.rubric_version,
        "origin": result.origin,
        "model": result.model,
        "summary": result.summary,
        "limitations": result.limitations_json,
        "meanAssessableScore": result.score_aggregate,
        "assessedCount": result.assessed_count,
        "assessments": assessments,
        "disclaimer": "Exploratory image-based indicators. Not a validated urban-quality score.",
    }


async def soft_delete_session(db: AsyncSession, ctx: AppContext, user: User, session: Session) -> None:
    session.status = "deleted"
    session.updated_at = utcnow()
    jobs = (
        await db.scalars(select(GenerationJob).where(GenerationJob.session_id == session.id, GenerationJob.status.in_(ACTIVE_STATUSES)))
    ).all()
    for job in jobs:
        job.status = "cancelled"
        job.error_code = "SESSION_NOT_FOUND"
        job.completed_at = utcnow()
    assets = (await db.scalars(select(SceneAsset).where(SceneAsset.session_id == session.id))).all()
    for asset in assets:
        if asset.object_key:
            await ctx.storage.delete(asset.object_key)
        asset.status = "deleted"
    audit(db, user.id, "session.delete", "session", session.id)


async def record_usage(db: AsyncSession, actor_id: str, job_id: str, provider: str, status: str, duration_ms: int | None) -> None:
    db.add(
        UsageLedger(
            actor_id=actor_id,
            job_id=job_id,
            provider=provider,
            cost_estimate=None,
            cost_actual=None,
            duration_ms=duration_ms,
            status=status,
        )
    )


async def create_invite(db: AsyncSession, session: Session, raw_code: str) -> dict:
    normalized = raw_code.strip().upper()
    invite = SessionInvite(
        session_id=session.id,
        token_hash=sha256_text(normalized),
        expires_at=utcnow() + timedelta(days=7),
        max_uses=25,
    )
    db.add(invite)
    await db.flush()
    return {"inviteId": invite.id, "code": normalized, "expiresAt": iso(invite.expires_at)}


async def redeem_invite(db: AsyncSession, user: User, raw_code: str) -> Session:
    if user.role == "guest":
        raise AppError("NOT_AUTHORIZED", "Register before joining a collaborative session.", 403)
    invite = await db.scalar(select(SessionInvite).where(SessionInvite.token_hash == sha256_text(raw_code.strip().upper())))
    expires = aware(invite.expires_at) if invite else None
    if (
        invite is None
        or invite.revoked_at is not None
        or expires is None
        or expires < utcnow()
        or invite.uses >= invite.max_uses
    ):
        raise AppError("NOT_AUTHORIZED", "That join code is invalid or expired.", 404)
    session = await db.get(Session, invite.session_id)
    if session is None or session.status == "deleted":
        raise AppError("SESSION_NOT_FOUND", "Session not found.", 404)
    existing = await db.scalar(
        select(SessionMember).where(SessionMember.session_id == session.id, SessionMember.user_id == user.id)
    )
    if existing is None:
        db.add(SessionMember(session_id=session.id, user_id=user.id, role="participant"))
        invite.uses += 1
    elif existing.state != "active":
        existing.state = "active"
        invite.uses += 1
    return session
