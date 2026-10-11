import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import Settings, assert_runtime_settings, get_settings
from app.core.context import build_context
from app.core.errors import AppError
from app.db.base import Base
from app.services.jobs import process_once

logger = logging.getLogger("streetcloud")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    assert_runtime_settings(settings)
    ctx = build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_create_tables:
            async with ctx.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        stop = asyncio.Event()

        async def _worker_loop(flag: asyncio.Event) -> None:
            while not flag.is_set():
                try:
                    await process_once(ctx)
                except Exception:
                    logger.exception("embedded worker iteration failed")
                try:
                    await asyncio.wait_for(flag.wait(), timeout=0.5)
                except TimeoutError:
                    pass

        task = asyncio.create_task(_worker_loop(stop)) if settings.embed_worker else None
        yield
        stop.set()
        if task:
            await task
        await ctx.engine.dispose()

    app = FastAPI(title="Street Cloud API", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx
    origins = ["*"] if settings.cors_origins.strip() == "*" else [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "requestId": getattr(_request.state, "request_id", ""),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "VALIDATION", "message": "Check the submitted fields.", "details": {}},
                "requestId": getattr(_request.state, "request_id", ""),
            },
        )

    @app.get("/health")
    async def health():
        database = "ok"
        try:
            from sqlalchemy import text

            async with ctx.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            database = "error"
        return {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "storage": settings.storage_backend,
            "environment": settings.environment,
            "embeddedWorker": settings.embed_worker,
        }

    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
