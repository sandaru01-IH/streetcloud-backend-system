ALLOWED: dict[str, set[str]] = {
    "accepted": {"queued", "failed"},
    "queued": {"preparing", "running", "cancel_requested", "failed", "cancelled", "worker_disconnected"},
    "preparing": {"running", "finalizing", "failed", "cancel_requested", "cancelled", "worker_disconnected"},
    "running": {"finalizing", "failed", "cancel_requested", "cancelled", "worker_disconnected"},
    "finalizing": {"succeeded", "failed", "cancelled"},
    "cancel_requested": {"cancelled", "succeeded", "failed"},
    "worker_disconnected": {"queued", "failed", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}

ACTIVE_STATUSES = {"accepted", "queued", "preparing", "running", "finalizing", "cancel_requested"}


def can_transition(current: str, nxt: str) -> bool:
    return nxt in ALLOWED.get(current, set())


def assert_transition(current: str, nxt: str) -> None:
    if not can_transition(current, nxt):
        raise ValueError(f"Illegal job transition {current} -> {nxt}")
