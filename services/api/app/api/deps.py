from collections.abc import AsyncIterator

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import AppContext
from app.core.errors import AppError
from app.core.security import decode_access_token
from app.db.models import User


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    request.state.pending_events = []
    ctx: AppContext = request.app.state.ctx
    async with ctx.sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    for event in request.state.pending_events:
        await ctx.hub.broadcast(event)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise AppError("AUTH_REQUIRED", "Sign in to continue.", 401)
    token = header.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(request.app.state.ctx.settings, token)
    except jwt.PyJWTError as exc:
        raise AppError("AUTH_REQUIRED", "Sign in again to continue.", 401) from exc
    if payload.get("typ") != "access":
        raise AppError("AUTH_REQUIRED", "Sign in again to continue.", 401)
    user = await db.get(User, payload.get("sub"))
    if user is None or user.status != "active":
        raise AppError("AUTH_REQUIRED", "This account is not active.", 401)
    return user
