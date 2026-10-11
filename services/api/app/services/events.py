import logging
from collections import defaultdict

from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id, utcnow
from app.db.models import EventOutbox, Session
from app.services.access import iso

logger = logging.getLogger("streetcloud.events")


class Hub:
    def __init__(self) -> None:
        self.rooms: dict[str, set[WebSocket]] = defaultdict(set)

    async def register(self, session_id: str, socket: WebSocket) -> None:
        self.rooms[session_id].add(socket)

    def unregister(self, session_id: str, socket: WebSocket) -> None:
        self.rooms[session_id].discard(socket)

    async def broadcast(self, event: dict) -> None:
        session_id = event.get("sessionId")
        dead: list[WebSocket] = []
        for socket in list(self.rooms.get(session_id, ())):
            try:
                await socket.send_json(event)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.unregister(session_id, socket)

    async def broadcast_many(self, events: list[dict]) -> None:
        for event in events:
            await self.broadcast(event)


def push_event(db: AsyncSession, session: Session, events: list[dict], event_type: str, payload: dict) -> dict:
    session.event_seq += 1
    env = {
        "eventId": new_id(),
        "eventType": event_type,
        "sessionId": session.id,
        "sequence": session.event_seq,
        "revision": session.revision,
        "emittedAt": iso(utcnow()),
        "payload": payload,
    }
    db.add(
        EventOutbox(
            id=env["eventId"],
            session_id=session.id,
            seq_no=session.event_seq,
            event_type=event_type,
            payload_json=env,
        )
    )
    events.append(env)
    return env
