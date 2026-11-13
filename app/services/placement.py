"""Placement mode: only placement mail gets deadline/event tracking (with aggressive reminders).

Defaults to PLACEMENT_MODE; the user can switch it in chat ("placement on" / "placement off").
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import kv
from app.config import get_settings

MODE_KEY = "placement_mode"


def is_on(session: Session) -> bool:
    value = kv.get(session, MODE_KEY)
    return get_settings().placement_mode if value is None else bool(value)


def set_mode(session: Session, on: bool) -> None:
    kv.put(session, MODE_KEY, on)
