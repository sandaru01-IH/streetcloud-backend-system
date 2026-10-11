import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.evaluation import run_evaluator
from app.adapters.generation import GenerationInput, run_adapter
from app.core.context import AppContext
from app.core.errors import AppError
from app.db.base import new_id, utcnow
from app.db.models import (
    DesignVersion,
    EvaluationJob,
    EvaluationPropertyScore,
    EvaluationResult,
    GenerationJob,
    PromptSnapshot,
    SceneAsset,
    Session,
    Worker,
    WorkerLease,
)
from app.services.access import aware
from app.services.events import push_event
from app.services.flow import _enqueue_evaluation, enqueue_generation, record_usage
from app.services.images import normalize_image
from app.services.providers import resolve_vision
from app.services.rubric import RUBRIC_VERSION
from app.services.state_machine import ACTIVE_STATUSES, assert_transition

logger = logging.getLogger("streetcloud.jobs")
LOCAL_PROVIDERS = {"mock", "openai", "gemini", "comfyui_hosted"}


async def process_until_idle(ctx: AppContext, limit: int = 12) -> None:
    for _ in range(limit):
        if not await process_once(ctx):
            return


async def process_once(ctx: AppContext) -> bool:
    events: list[dict] = []
    job_id: str | None = None
    eval_id: str | None = None
    scheduled = False
    async with ctx.sessionmaker() as db:
        scheduled = await _maintain(db, ctx, events)
        job_id = await _claim_generation(db)
        if job_id is None:
            eval_id = await _claim_evaluation(db)
        await db.commit()
    await ctx.hub.broadcast_many(events)
    if job_id:
        produced = await _run_generation(ctx, job_id)
        await ctx.hub.broadcast_many(produced)
        return True
    if eval_id:
        produced = await _run_evaluation(ctx, eval_id)
        await ctx.hub.broadcast_many(produced)
        return True
    return scheduled


async def _adopt_configured_vision(db: AsyncSession, ctx: AppContext) -> None:
    """Point an existing real session at a vision model that was configured later."""
    settings = ctx.settings
    if not (settings.openai_vision_ready or settings.gemini_vision_ready or settings.ollama_vision_ready):
        return
    sessions = (
        await db.scalars(
            select(Session).where(
                Session.demo.is_(False),
                Session.vision_provider.in_(("unconfigured", "private_vlm")),
            )
        )
    ).all()
    for session in sessions:
        resolved = resolve_vision(settings, "auto", demo=False)
        if resolved in {"unconfigured", "private_vlm", "disabled"}:
            continue
        session.vision_provider = resolved
        if not session.current_version_id:
            continue
        version = await db.get(DesignVersion, session.current_version_id)
        if version is None:
            continue
        existing = await db.scalar(select(EvaluationResult.id).where(EvaluationResult.version_id == version.id).limit(1))
        if existing:
            continue
        active = await db.scalar(
            select(EvaluationJob.id).where(
                EvaluationJob.version_id == version.id,
                EvaluationJob.status.in_(("queued", "running")),
            ).limit(1)
        )
        if active:
            continue
        latest = await db.scalar(
            select(EvaluationJob)
            .where(EvaluationJob.version_id == version.id)
            .order_by(EvaluationJob.created_at.desc())
            .limit(1)
        )
        if latest is not None and latest.status == "failed" and latest.error_code != "PROVIDER_NOT_CONFIGURED":
            continue
        _enqueue_evaluation(db, session, version, settings)


async def _retry_failed_local_vision(db: AsyncSession, ctx: AppContext) -> None:
    if not ctx.settings.ollama_vision_ready:
        return
    sessions = (
        await db.scalars(select(Session).where(Session.demo.is_(False), Session.vision_provider == "ollama_vision"))
    ).all()
    for session in sessions:
        if not session.current_version_id:
            continue
        version = await db.get(DesignVersion, session.current_version_id)
        if version is None:
            continue
        existing = await db.scalar(select(EvaluationResult.id).where(EvaluationResult.version_id == version.id).limit(1))
        if existing:
            continue
        active = await db.scalar(
            select(EvaluationJob.id).where(
                EvaluationJob.version_id == version.id,
                EvaluationJob.status.in_(("queued", "running")),
            ).limit(1)
        )
        if active:
            continue
        latest = await db.scalar(
            select(EvaluationJob)
            .where(EvaluationJob.version_id == version.id)
            .order_by(EvaluationJob.created_at.desc())
            .limit(1)
        )
        if latest is None or latest.status != "failed" or latest.error_code != "VISION_EVALUATION_FAILED":
            continue
        if latest.attempt >= 4:
            continue
        job = _enqueue_evaluation(db, session, version, ctx.settings)
        if job is not None:
            job.attempt = latest.attempt + 1


async def _queue_scores_for_current_model(db: AsyncSession, ctx: AppContext) -> None:
    """Score generated images with the vision model configured on this computer."""
    settings = ctx.settings
    if not settings.ollama_vision_ready:
        return
    model = settings.ollama_vision_model
    sessions = (
        await db.scalars(select(Session).where(Session.demo.is_(False), Session.vision_provider == "ollama_vision"))
    ).all()
    for session in sessions:
        versions = (
            await db.scalars(
                select(DesignVersion).where(DesignVersion.session_id == session.id, DesignVersion.version_no > 0)
            )
        ).all()
        for version in versions:
            active = await db.scalar(
                select(EvaluationJob.id).where(
                    EvaluationJob.version_id == version.id,
                    EvaluationJob.status.in_(("queued", "running")),
                ).limit(1)
            )
            if active:
                continue
            result = await db.scalar(
                select(EvaluationResult)
                .where(EvaluationResult.version_id == version.id)
                .order_by(EvaluationResult.created_at.desc())
                .limit(1)
            )
            if result is not None and result.model == model and result.rubric_version == RUBRIC_VERSION:
                continue
            latest = await db.scalar(
                select(EvaluationJob)
                .where(EvaluationJob.version_id == version.id)
                .order_by(EvaluationJob.created_at.desc())
                .limit(1)
            )
            if (
                latest is not None
                and latest.model == model
                and latest.status == "failed"
                and latest.attempt >= 2
                and (result is None or result.rubric_version == RUBRIC_VERSION)
            ):
                continue
            job = _enqueue_evaluation(db, session, version, settings)
            if job is not None and latest is not None and latest.model == model:
                job.attempt = latest.attempt + 1


async def _maintain(db: AsyncSession, ctx: AppContext, events: list[dict]) -> bool:
    orphans = (await db.scalars(select(EvaluationJob).where(EvaluationJob.status == "running"))).all()
    for orphan in orphans:
        orphan.status = "queued"
        orphan.started_at = None
    await _adopt_configured_vision(db, ctx)
    await _retry_failed_local_vision(db, ctx)
    await _queue_scores_for_current_model(db, ctx)
    scheduled = False
    now = utcnow()
    leases = (
        await db.scalars(select(WorkerLease).where(WorkerLease.status == "active", WorkerLease.expires_at < now))
    ).all()
    for lease in leases:
        lease.status = "expired"
        job = await db.get(GenerationJob, lease.job_id)
        if job and job.status in ACTIVE_STATUSES:
            assert_transition(job.status, "worker_disconnected")
            job.status = "worker_disconnected"
            job.error_code = "WORKER_JOB_LEASE_EXPIRED"
            job.error_message = "The private GPU disconnected before the image was stored. Retry when it is back."
            job.completed_at = now
            session = await db.get(Session, job.session_id)
            if session:
                session.status = "ready"
                push_event(db, session, events, "generation.failed", {"jobId": job.id, "status": job.status, "errorCode": job.error_code})
    cutoff = now - timedelta(seconds=ctx.settings.worker_offline_seconds)
    workers = (await db.scalars(select(Worker).where(Worker.status.notin_(["offline", "revoked"])))).all()
    for worker in workers:
        seen = aware(worker.last_seen)
        if seen is None or seen < cutoff:
            worker.status = "offline"
    due = (
        await db.scalars(
            select(Session).where(Session.needs_followup.is_(True), Session.batch_due_at.is_not(None), Session.batch_due_at <= now)
        )
    ).all()
    for session in due:
        active = await db.scalar(
            select(GenerationJob).where(GenerationJob.session_id == session.id, GenerationJob.status.in_(ACTIVE_STATUSES)).limit(1)
        )
        if active:
            continue
        snapshot = await db.scalar(
            select(PromptSnapshot).where(PromptSnapshot.session_id == session.id).order_by(PromptSnapshot.revision_number.desc()).limit(1)
        )
        if snapshot is None:
            session.needs_followup = False
            continue
        result = await enqueue_generation(db, ctx, session, snapshot)
        scheduled = True
        if result.get("jobId"):
            push_event(db, session, events, "generation.queued", {"jobId": result["jobId"], "status": result["status"]})
    return scheduled


async def _claim_generation(db: AsyncSession) -> str | None:
    job = await db.scalar(
        select(GenerationJob)
        .where(GenerationJob.status == "queued", GenerationJob.provider.in_(LOCAL_PROVIDERS))
        .order_by(GenerationJob.created_at)
        .limit(1)
    )
    if job is None:
        return None
    assert_transition(job.status, "preparing")
    job.status = "preparing"
    job.started_at = utcnow()
    job.stage_note = "Preparing"
    return job.id


async def _claim_evaluation(db: AsyncSession) -> str | None:
    current_first = EvaluationJob.version_id == Session.current_version_id
    job = await db.scalar(
        select(EvaluationJob)
        .join(Session, Session.id == EvaluationJob.session_id)
        .where(EvaluationJob.status == "queued")
        .order_by(current_first.desc(), EvaluationJob.created_at)
        .limit(1)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = utcnow()
    return job.id


async def _run_generation(ctx: AppContext, job_id: str) -> list[dict]:
    events: list[dict] = []
    async with ctx.sessionmaker() as db:
        job = await db.get(GenerationJob, job_id)
        session = await db.get(Session, job.session_id) if job else None
        if job is None or session is None:
            return []
        if job.status == "cancel_requested":
            job.status = "cancelled"
            job.completed_at = utcnow()
            session.status = "ready"
            push_event(db, session, events, "generation.cancelled", {"jobId": job.id, "status": "cancelled"})
            await db.commit()
            return events
        try:
            assert_transition(job.status, "running")
            job.status = "running"
            job.stage_note = "Generating"
            push_event(db, session, events, "generation.started", {"jobId": job.id, "status": "running"})
            snapshot = await db.get(PromptSnapshot, job.prompt_snapshot_id)
            asset = await db.get(SceneAsset, job.source_asset_id)
            if snapshot is None or asset is None:
                raise AppError("GENERATION_INVALID_OUTPUT", "The source photo or prompt snapshot is missing.", 409)
            source = await ctx.storage.get(asset.object_key)
            version_no = await _next_version_no(db, session.id)
            started = utcnow()
            output = await run_adapter(
                job.provider,
                ctx.settings,
                GenerationInput(
                    job_id=job.id,
                    source_bytes=source,
                    compiled_prompt=snapshot.compiled_prompt,
                    negative_prompt=snapshot.negative_prompt,
                    resolution_preset="832x512",
                    model_id=job.model,
                    quality_preset=session.quality_preset,
                    provider=job.provider,
                ),
                version_no,
            )
            await db.refresh(job)
            if job.status == "cancel_requested":
                job.status = "cancelled"
                job.completed_at = utcnow()
                session.status = "ready"
                push_event(db, session, events, "generation.cancelled", {"jobId": job.id, "status": "cancelled"})
                await db.commit()
                return events
            data, width, height, digest = normalize_image(
                output.image_bytes, ctx.settings.max_upload_mb * 1024 * 1024, ctx.settings.max_megapixels
            )
            stored = SceneAsset(
                session_id=session.id,
                uploaded_by=session.owner_user_id,
                object_key=f"sessions/{session.id}/{new_id()}.jpg",
                mime="image/jpeg",
                width=width,
                height=height,
                sha256=digest,
                byte_size=len(data),
                kind="generated",
                status="stored",
                demo_labeled=output.demo_labeled,
            )
            db.add(stored)
            await db.flush()
            await ctx.storage.put(stored.object_key, data, "image/jpeg")
            assert_transition(job.status, "finalizing")
            job.status = "finalizing"
            job.stage_note = "Finalizing"
            parent = await db.get(DesignVersion, session.current_version_id) if session.current_version_id else None
            publish = "candidate" if session.require_publish_approval else "published"
            version = DesignVersion(
                session_id=session.id,
                version_no=version_no,
                generation_job_id=job.id,
                image_asset_id=stored.id,
                parent_version_id=parent.id if parent else None,
                based_on_original=True,
                publish_status=publish,
                checksum=digest,
                provider=output.provider,
                model=output.model_id,
                prompt_excerpt=snapshot.compiled_prompt[:280],
                demo_labeled=output.demo_labeled,
            )
            db.add(version)
            await db.flush()
            assert_transition(job.status, "succeeded")
            job.status = "succeeded"
            job.completed_at = utcnow()
            job.stage_note = "Ready"
            if publish == "published":
                session.current_version_id = version.id
            session.status = "ready"
            session.updated_at = utcnow()
            duration_ms = int((job.completed_at - started).total_seconds() * 1000)
            await record_usage(db, session.owner_user_id, job.id, job.provider, "succeeded", duration_ms)
            _enqueue_evaluation(db, session, version, ctx.settings)
            push_event(
                db,
                session,
                events,
                "generation.succeeded",
                {"jobId": job.id, "versionId": version.id, "versionNumber": version.version_no, "demoLabeled": version.demo_labeled},
            )
            if session.needs_followup and session.generation_policy == "automatic":
                latest = await db.scalar(
                    select(PromptSnapshot)
                    .where(PromptSnapshot.session_id == session.id)
                    .order_by(PromptSnapshot.revision_number.desc())
                    .limit(1)
                )
                if latest and latest.id != job.prompt_snapshot_id:
                    follow = await enqueue_generation(db, ctx, session, latest)
                    if follow.get("jobId"):
                        push_event(db, session, events, "generation.queued", {"jobId": follow["jobId"], "status": follow["status"]})
        except AppError as exc:
            await _fail_job(db, job, session, events, exc.code, exc.message)
        except Exception:
            logger.exception("generation failed job=%s", job_id)
            await _fail_job(db, job, session, events, "GENERATION_INVALID_OUTPUT", "Image generation failed before a result was stored.")
        await db.commit()
    return events


async def _fail_job(db, job: GenerationJob, session: Session, events: list[dict], code: str, message: str) -> None:
    if job.status not in {"failed", "succeeded", "cancelled"}:
        try:
            assert_transition(job.status, "failed")
        except ValueError:
            job.status = "failed"
        else:
            job.status = "failed"
    job.error_code = code
    job.error_message = message
    job.completed_at = utcnow()
    job.stage_note = "Failed"
    session.status = "ready"
    push_event(db, session, events, "generation.failed", {"jobId": job.id, "status": "failed", "errorCode": code, "errorMessage": message})
    if session.needs_followup and session.generation_policy == "automatic":
        session.needs_followup = True


async def _run_evaluation(ctx: AppContext, eval_id: str) -> list[dict]:
    events: list[dict] = []
    target: bytes | None = None
    baseline: bytes | None = None
    provider = ""
    model_id = ""
    version_id = ""
    async with ctx.sessionmaker() as db:
        job = await db.get(EvaluationJob, eval_id)
        session = await db.get(Session, job.session_id) if job else None
        version = await db.get(DesignVersion, job.version_id) if job else None
        if job is None or session is None or version is None:
            return []
        version_id = version.id
        busy = await db.scalar(
            select(GenerationJob.id).where(
                GenerationJob.session_id == session.id,
                GenerationJob.status.in_(("preparing", "running", "finalizing")),
            ).limit(1)
        )
        if busy:
            job.status = "queued"
            job.started_at = None
            await db.commit()
            return []
        provider = job.evaluator_provider
        if provider == "openai_vision":
            model_id = ctx.settings.openai_vision_model
        elif provider == "gemini_vision":
            model_id = ctx.settings.gemini_vision_model
        elif provider == "ollama_vision":
            model_id = job.model or ctx.settings.ollama_vision_model
        push_event(db, session, events, "evaluation.started", {"evaluationJobId": job.id, "versionId": version.id})
        try:
            asset = await db.get(SceneAsset, version.image_asset_id)
            if asset is None:
                raise AppError("VISION_EVALUATION_FAILED", "The version image is missing.", 409)
            target = await ctx.storage.get(asset.object_key)
            if version.version_no != 0:
                original = await db.scalar(
                    select(DesignVersion).where(DesignVersion.session_id == session.id, DesignVersion.version_no == 0)
                )
                if original:
                    original_asset = await db.get(SceneAsset, original.image_asset_id)
                    if original_asset is not None:
                        baseline = await ctx.storage.get(original_asset.object_key)
        except AppError as exc:
            await _fail_evaluation(db, job, session, events, exc.code, exc.message)
            await db.commit()
            return events
        await db.commit()
    try:
        parsed = await run_evaluator(provider, ctx.settings, target or b"", baseline, model_id)
    except AppError as exc:
        async with ctx.sessionmaker() as db:
            await _record_evaluation_failure(db, events, eval_id, exc.code, exc.message)
            await db.commit()
        return events
    except Exception:
        logger.exception("evaluation failed job=%s", eval_id)
        async with ctx.sessionmaker() as db:
            await _record_evaluation_failure(
                db,
                events,
                eval_id,
                "VISION_EVALUATION_FAILED",
                "Evaluation failed before a valid 15-property result was stored.",
            )
            await db.commit()
        return events
    async with ctx.sessionmaker() as db:
        job = await db.get(EvaluationJob, eval_id)
        session = await db.get(Session, job.session_id) if job else None
        if job is None or session is None:
            return events
        result = EvaluationResult(
            evaluation_job_id=job.id,
            version_id=version_id,
            model=parsed["model"],
            rubric_version=parsed["rubric_version"],
            summary=parsed["summary"],
            score_aggregate=parsed["mean"],
            assessed_count=parsed["assessed_count"],
            origin=parsed["origin"],
            limitations_json=parsed["limitations"],
        )
        db.add(result)
        await db.flush()
        for item in parsed["assessments"]:
            db.add(
                EvaluationPropertyScore(
                    result_id=result.id,
                    property_key=item["key"],
                    score=item["score"],
                    applicability=item["applicability"],
                    confidence=item["confidence"],
                    evidence_text=item["evidence"],
                    caveat_text=item["caveat"],
                )
            )
        job.status = "succeeded"
        job.model = parsed["model"]
        job.completed_at = utcnow()
        push_event(
            db,
            session,
            events,
            "evaluation.succeeded",
            {"evaluationId": result.id, "versionId": version_id, "origin": result.origin},
        )
        await db.commit()
    return events


async def _fail_evaluation(db: AsyncSession, job: EvaluationJob, session: Session, events: list[dict], code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = code
    job.error_message = message
    job.completed_at = utcnow()
    push_event(
        db,
        session,
        events,
        "evaluation.failed",
        {"evaluationJobId": job.id, "versionId": job.version_id, "errorCode": code, "errorMessage": message},
    )


async def _record_evaluation_failure(
    db: AsyncSession,
    events: list[dict],
    eval_id: str,
    code: str,
    message: str,
) -> None:
    job = await db.get(EvaluationJob, eval_id)
    if job is None:
        return
    session = await db.get(Session, job.session_id)
    if session is None:
        return
    await _fail_evaluation(db, job, session, events, code, message)


async def _next_version_no(db: AsyncSession, session_id: str) -> int:
    from sqlalchemy import func

    current = await db.scalar(select(func.max(DesignVersion.version_no)).where(DesignVersion.session_id == session_id))
    return 0 if current is None else int(current) + 1
