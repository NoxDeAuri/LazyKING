"""
Event store — append-only log of submission lifecycle events.

Events are written to data/events.jsonl (one JSON object per line).
The file is never modified; new events are always appended.

Handlers are registered with subscribe() and fired synchronously
when append_event() is called. They receive the Event and can do
whatever side-effect work they need (update works.json, trigger RAG
indexing, etc.).

Replaying all events reconstructs current submission state without
touching works.json at all — useful for audits, debugging, and RAG
indexing that needs the full history.
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .schema import Event, EventType
from ..telemetry.spans import span as _span

EVENTS_FILE = Path(__file__).parent.parent.parent / "data" / "events.jsonl"

# handler registry: event_type -> list of callables
_handlers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)


def subscribe(event_type: str, handler: Callable[[Event], None]) -> None:
    """Register a handler to be called whenever an event of event_type is appended."""
    _handlers[event_type].append(handler)


def append_event(event: Event) -> Event:
    """
    Persist the event and fire all registered handlers for its type.
    Returns the event (with its generated id and timestamp).
    """
    with _span("event.append", event_type=event.type, event_id=event.id):
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

        for handler in _handlers.get(event.type, []):
            try:
                handler(event)
            except Exception as exc:
                # Handlers must never break the caller — log and continue
                print(f"[events] handler error ({handler.__name__}): {exc}")

    return event


def replay(event_type: str | None = None) -> list[Event]:
    """
    Return all events from the log, optionally filtered by type.
    Events are returned in chronological order.
    """
    if not EVENTS_FILE.exists():
        return []

    events: list[Event] = []
    with EVENTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                e = Event.from_dict(d)
                if event_type is None or e.type == event_type:
                    events.append(e)
            except (json.JSONDecodeError, KeyError):
                continue  # tolerate corrupt lines

    return events


def current_submissions() -> list[dict]:
    """
    Derive the current state of every submission by replaying the event log.

    Returns a list of submission records, each with:
      work_id, work_title, call, listing_url, sent_date,
      status (pending / rejected / accepted / waitlisted / withdrawn),
      response_date, notes
    """
    events = replay()

    # Build map: (work_id, call) → accumulated record
    subs: dict[tuple[str, str], dict] = {}

    for e in events:
        p = e.payload
        key = (p["work_id"], p["call"])

        if e.type == EventType.SUBMISSION_SENT:
            subs[key] = {
                "work_id":       p["work_id"],
                "work_title":    p.get("work_title", ""),
                "call":          p["call"],
                "listing_url":   p.get("listing_url", ""),
                "sent_date":     p.get("date", ""),
                "status":        "pending",
                "response_date": "",
                "notes":         p.get("notes", ""),
                "event_ids":     [e.id],
            }

        elif e.type == EventType.RESPONSE_RECEIVED and key in subs:
            rt = p.get("response_type", "")
            status_map = {
                "rejection":  "rejected",
                "acceptance": "accepted",
                "waitlist":   "waitlisted",
            }
            subs[key]["status"]        = status_map.get(rt, rt)
            subs[key]["response_date"] = p.get("date", "")
            if p.get("notes"):
                subs[key]["notes"] = p["notes"]
            subs[key]["event_ids"].append(e.id)

        elif e.type == EventType.WITHDRAWAL_SENT and key in subs:
            subs[key]["status"]        = "withdrawn"
            subs[key]["response_date"] = p.get("date", "")
            subs[key]["event_ids"].append(e.id)

    return sorted(subs.values(), key=lambda s: s["sent_date"], reverse=True)
