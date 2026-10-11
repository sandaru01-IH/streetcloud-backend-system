from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Session, SessionMember, User


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def iso(value: datetime | None) -> str | None:
    value = aware(value)
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def member_role(db: AsyncSession, session: Session, user: User) -> str | None:
    if session.owner_user_id == user.id:
        return "owner"
    row = await db.scalar(
        select(SessionMember).where(
            SessionMember.session_id == session.id,
            SessionMember.user_id == user.id,
            SessionMember.state == "active",
        )
    )
    return row.role if row else None


async def require_session(
    db: AsyncSession,
    user: User,
    session_id: str,
    *,
    owner: bool = False,
) -> tuple[Session, str]:
    session = await db.get(Session, session_id)
    if session is None or session.status == "deleted":
        raise AppError("SESSION_NOT_FOUND", "Session not found.", 404)
    role = await member_role(db, session, user)
    if role is None:
        raise AppError("NOT_AUTHORIZED", "You do not have access to this session.", 403)
    if owner and role != "owner":
        raise AppError("NOT_AUTHORIZED", "Only the session owner can do that.", 403)
    return session, role
