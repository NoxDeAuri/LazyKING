"""
RAG indexer — builds submission embeddings from the event log.

Two entry points:
  index_event(event)   called by the response_received handler for new events
  index_all()          CLI / one-shot rebuild from the full event log

What gets indexed:
  Submissions with a recorded outcome (rejected / accepted / waitlisted /
  withdrawn). Pending submissions are not indexed — there's nothing to
  learn from yet.

What we embed:
  A text snippet combining synopsis, themes, and call description.
  This is the semantic query future retrievals will match against.
  The actual manuscript text is never touched (see security constraint).
"""

import asyncio
from pathlib import Path

from ..events.schema import EventType
from ..events.store import replay
from ..profile.builder import load_works
from . import embedder, store

_OUTCOME_MAP = {
    "rejection":  "rejected",
    "acceptance": "accepted",
    "waitlist":   "waitlisted",
    "withdrawn":  "withdrawn",
}


def _build_text(work: dict, call: str, call_description: str = "") -> str:
    """
    Compose the text we embed. Uses synopsis and themes — never manuscript text.
    """
    parts = []
    if work.get("synopsis_short"):
        parts.append(f"Synopsis: {work['synopsis_short']}")
    if work.get("themes"):
        parts.append(f"Themes: {', '.join(work['themes'])}")
    if work.get("genre"):
        parts.append(f"Genre: {work['genre']}")
    parts.append(f"Submitted to: {call}")
    if call_description:
        # Truncate to keep embeddings focused
        parts.append(f"Call description: {call_description[:300]}")
    return "\n".join(parts)


def _doc_id(work_id: str, call: str, listing_url: str = "") -> str:
    key = listing_url or call
    return f"{work_id}::{key}"


async def _index_one(
    work:          dict,
    call:          str,
    listing_url:   str,
    outcome:       str,
    response_date: str,
    call_description: str = "",
) -> None:
    text = _build_text(work, call, call_description)
    try:
        vector = await embedder.embed(text)
    except RuntimeError as e:
        print(f"[rag] embedding skipped — {e}")
        return

    doc_id = _doc_id(work["id"], call, listing_url)
    metadata = {
        "work_id":     work["id"],
        "work_title":  work.get("title", ""),
        "call":        call,
        "listing_url": listing_url,
        "outcome":     outcome,
        "date":        response_date,
    }
    store.upsert(doc_id=doc_id, text=text, embedding=vector, metadata=metadata)
    print(f"[rag] indexed  {work.get('title')!r} → {call!r}  outcome={outcome}")


def index_event(event) -> None:
    """
    Called synchronously from the response_received handler.
    Looks up the work profile and indexes the submission.
    """
    if event.type != EventType.RESPONSE_RECEIVED:
        return

    p       = event.payload
    outcome = _OUTCOME_MAP.get(p.get("response_type", ""), p.get("response_type", ""))

    works_data = load_works()
    work = next(
        (w for w in works_data.get("works", []) if w["id"] == p["work_id"]),
        None,
    )
    if work is None:
        print(f"[rag] work '{p['work_id']}' not found — skipping index")
        return

    asyncio.run(
        _index_one(
            work=work,
            call=p["call"],
            listing_url=p.get("listing_url", ""),
            outcome=outcome,
            response_date=p.get("date", ""),
        )
    )


async def index_all() -> int:
    """
    Rebuild the vector store from the full event log.
    Matches SubmissionSent events with ResponseReceived events by (work_id, call).
    Returns the number of documents indexed.
    """
    events = replay()
    works_data = load_works()
    works_by_id = {w["id"]: w for w in works_data.get("works", [])}

    # Pair sent events with response events
    sent:     dict[tuple, dict] = {}
    received: dict[tuple, dict] = {}

    for e in events:
        p   = e.payload
        key = (p["work_id"], p["call"])
        if e.type == EventType.SUBMISSION_SENT:
            sent[key] = p
        elif e.type in (EventType.RESPONSE_RECEIVED, EventType.WITHDRAWAL_SENT):
            received[key] = e

    indexed = 0
    for key, response_event in received.items():
        work_id = key[0]
        work    = works_by_id.get(work_id)
        if work is None:
            continue

        p       = response_event.payload
        rt      = p.get("response_type", "")
        outcome = _OUTCOME_MAP.get(rt, rt) if rt else "withdrawn"

        await _index_one(
            work=work,
            call=p["call"],
            listing_url=p.get("listing_url", ""),
            outcome=outcome,
            response_date=p.get("date", ""),
        )
        indexed += 1

    print(f"[rag] index_all complete — {indexed} documents indexed")
    return indexed
