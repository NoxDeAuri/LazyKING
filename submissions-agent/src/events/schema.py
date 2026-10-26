"""
Event types for submission tracking.

Events are immutable facts: things that happened.
The current state of any submission is derived by replaying the event log.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


class EventType:
    SUBMISSION_SENT     = "submission_sent"
    RESPONSE_RECEIVED   = "response_received"
    WITHDRAWAL_SENT     = "withdrawal_sent"


class ResponseType:
    REJECTION  = "rejection"
    ACCEPTANCE = "acceptance"
    WAITLIST   = "waitlist"
    WITHDRAWN  = "withdrawn"


@dataclass
class Event:
    type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "id":        self.id,
            "type":      self.type,
            "timestamp": self.timestamp,
            "payload":   self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            id=d["id"],
            type=d["type"],
            timestamp=d["timestamp"],
            payload=d["payload"],
        )


# ---------------------------------------------------------------------------
# Payload constructors — keep payloads consistent across callers
# ---------------------------------------------------------------------------

def submission_sent(
    work_id: str,
    work_title: str,
    call: str,
    listing_url: str = "",
    date: str = "",
    notes: str = "",
) -> Event:
    return Event(
        type=EventType.SUBMISSION_SENT,
        payload={
            "work_id":     work_id,
            "work_title":  work_title,
            "call":        call,
            "listing_url": listing_url,
            "date":        date or datetime.now(timezone.utc).date().isoformat(),
            "notes":       notes,
        },
    )


def response_received(
    work_id: str,
    work_title: str,
    call: str,
    response_type: str,
    listing_url: str = "",
    date: str = "",
    notes: str = "",
) -> Event:
    return Event(
        type=EventType.RESPONSE_RECEIVED,
        payload={
            "work_id":       work_id,
            "work_title":    work_title,
            "call":          call,
            "listing_url":   listing_url,
            "response_type": response_type,
            "date":          date or datetime.now(timezone.utc).date().isoformat(),
            "notes":         notes,
        },
    )


def withdrawal_sent(
    work_id: str,
    work_title: str,
    call: str,
    listing_url: str = "",
    date: str = "",
    notes: str = "",
) -> Event:
    return Event(
        type=EventType.WITHDRAWAL_SENT,
        payload={
            "work_id":     work_id,
            "work_title":  work_title,
            "call":        call,
            "listing_url": listing_url,
            "date":        date or datetime.now(timezone.utc).date().isoformat(),
            "notes":       notes,
        },
    )
