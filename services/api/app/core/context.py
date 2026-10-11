import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.adapters.storage import Storage, build_storage
from app.core.config import Settings
from app.services.events import Hub

logger = logging.getLogger("streetcloud.db")


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    storage: Storage
    hub: Hub


def make_engine(url: str) -> AsyncEngine:
    _ensure_sqlite_directory(url)
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        bare = url.split("?", 1)[0].rstrip("/")
        if bare.endswith("sqlite+aiosqlite:") or ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    return create_async_engine(url, **kwargs)


def _ensure_sqlite_directory(url: str) -> None:
    if not url.startswith("sqlite") or ":memory:" in url:
        return
    path = url.split(":///", 1)[-1].split("?", 1)[0]
    if not path or path == ":memory:":
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def build_context(settings: Settings) -> AppContext:
    engine = make_engine(settings.database_url)
    return AppContext(
        settings=settings,
        engine=engine,
        sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
        storage=build_storage(settings),
        hub=Hub(),
    )
