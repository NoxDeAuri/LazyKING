"""
FastAPI web dashboard — local UI for the Submissions Agent.

Start with:
    web-server                              (after pip install -e .)
    uvicorn src.web.app:app --reload        (dev, auto-reload)

Open: http://localhost:8765
"""

import asyncio
import functools
import ipaddress
import json
import os
import re
import socket
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..timeline import tweek as tweek_mod
from ..timeline.tweek import log_submission_event as _tweek_log
from ..timeline.builder import (
    create_timeline as _create_timeline,
    adjust_timeline as _adjust_timeline,
    sync_tweek_on_adjust as _sync_tweek_on_adjust,
)

from ..match.matcher import find_boutique, match_works_to_listings
from ..match import scorer
from ..events import schema as event_schema
from ..events import store as event_store
from ..events import handlers as _event_handlers  # noqa: F401 — registers handlers
from ..profile.builder import load_works, save_works
from ..sites.chillsubs import load_cache
from ..sites.chillsubs import search as chillsubs_search
from ..sites import querytracker as qt_mod
from ..sites import grinder as grinder_mod
from ..sites import mswl as mswl_mod
from ..sites import pw_presses as pw_mod
from ..sites.filmfreeway import search as ff_search, load_cache as ff_load_cache
from ..sites.fellowships import get_fellowships
from ..match import novel_matcher
from ..instagram import log as ig_mod
from . import venue as venue_mod

load_dotenv()

_AGENT_SECRET = os.environ.get("AGENT_SECRET", "").strip()

# ── SSRF guard ────────────────────────────────────────────────────────────────

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_safe_url(url: str) -> bool:
    """Return False for URLs that resolve to private/loopback/link-local addresses."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if any(ip in net for net in _PRIVATE_NETS):
                return False
        return True
    except Exception:
        return False

# ── Slug validation helper ────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$")


def _assert_slug(work_id: str) -> str:
    if not _SLUG_RE.match(work_id):
        raise HTTPException(400, "work_id must be a slug (lowercase, digits, hyphens only)")
    return work_id

# ── Works write lock ─────────────────────────────────────────────────────────

_works_lock: asyncio.Lock  # initialised in startup

from ..telemetry.setup import setup_telemetry
setup_telemetry()

app = FastAPI(title="LazyKing")

# Auto-instrument FastAPI routes (no-op when OTel is disabled)
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
except ImportError:
    pass


@app.on_event("startup")
async def _init_lock():
    global _works_lock
    _works_lock = asyncio.Lock()


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Optional bearer-token auth for mutation endpoints. No-op when AGENT_SECRET is unset."""
    if _AGENT_SECRET and request.method not in ("GET", "HEAD", "OPTIONS"):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != _AGENT_SECRET:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


_STATIC = Path(__file__).parent / "static"
# Railway injects PORT; WEB_PORT is the local override; 8765 is the hard default
PORT = int(os.environ.get("PORT") or os.environ.get("WEB_PORT", "8765"))

# Mount MCP HTTP/SSE transport when running in a cloud deployment
if os.environ.get("DEPLOY", "").lower() == "true":
    try:
        from .mcp_http import create_mcp_app
        app.mount("/mcp", create_mcp_app())
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("MCP HTTP mount skipped: %s", _e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC / "index.html").read_text()


@app.get("/api/cache")
async def cache_info():
    cache = load_cache()
    if not cache:
        return {"available": False}
    return {
        "available": True,
        "count": len(cache.listings),
        "fetched_at": cache.fetched_at,
    }


@app.post("/api/listings/refresh")
async def refresh_listings(genre: str = "fiction"):
    """
    Fetch fresh listings from Chill Subs, streaming SSE progress events.

    Events:
      {"type":"progress","page":N,"collected":M,"total":T}
      {"type":"done","count":N}
      {"type":"error","message":"..."}
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _on_page(page_num: int, collected: int, total):
        await queue.put({
            "type": "progress",
            "page": page_num,
            "collected": collected,
            "total": total,
        })

    async def _run():
        try:
            listings = await chillsubs_search(genre=genre, on_page=_on_page)
            await queue.put({"type": "done", "count": len(listings)})
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})

    asyncio.create_task(_run())

    async def _stream():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=300)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","message":"Timeout — no response from Chill Subs"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/screenplay-calls")
async def get_screenplay_calls(
    format: str = "any",
    min_days: int = 0,
    include_fellowships: bool = True,
):
    """
    Return cached FilmFreeway competitions + curated fellowships.
    format: feature | short | pilot | tv | any
    """
    ff_cache = ff_load_cache()
    competitions = ff_cache.listings if ff_cache else []
    if format != "any":
        competitions = [l for l in competitions if l.screenplay_format in (format, "any")]
    if min_days > 0:
        from datetime import date
        cutoff = date.today()
        from datetime import timedelta
        cutoff = date.today() + timedelta(days=min_days)
        competitions = [l for l in competitions if not l.deadline or l.deadline >= cutoff]

    fellowships = get_fellowships(format_filter=format) if include_fellowships else []
    return {
        "competitions":      [l.model_dump(mode="json") for l in competitions],
        "fellowships":       [l.model_dump(mode="json") for l in fellowships],
        "ff_fetched_at":     ff_cache.fetched_at if ff_cache else None,
        "competition_count": len(competitions),
        "fellowship_count":  len(fellowships),
    }


@app.post("/api/screenplay-calls/refresh")
async def refresh_screenplay_calls(format: str = "any", min_days: int = 0):
    """Fetch fresh FilmFreeway screenwriting competition data (SSE stream)."""
    queue: asyncio.Queue = asyncio.Queue()

    async def _on_progress(event: dict):
        await queue.put(event)

    async def _run():
        try:
            listings = await ff_search(
                min_days_to_deadline=min_days,
                use_cache=False,
                on_progress=_on_progress,
            )
            await queue.put({"type": "done", "count": len(listings)})
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})

    asyncio.create_task(_run())

    async def _stream():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","message":"Timeout fetching FilmFreeway"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _get_listings(source: str = "chillsubs"):
    """
    Return (listings, fetched_at) for the requested source.
    source: "chillsubs" | "grinder" | "both"
    Raises HTTPException when the required cache is missing.
    """
    from ..sites.grinder import markets_to_listings
    cs_cache   = load_cache()        if source in ("chillsubs", "both") else None
    gr_cache   = grinder_mod.load_cache() if source in ("grinder",   "both") else None

    if source == "chillsubs":
        if not cs_cache:
            raise HTTPException(404, "No ChillSubs cache — refresh first.")
        return cs_cache.listings, cs_cache.fetched_at

    if source == "grinder":
        if not gr_cache:
            raise HTTPException(404, "No Grinder cache — refresh first.")
        return markets_to_listings(gr_cache["markets"]), gr_cache["fetched_at"]

    # both
    listings, ts = [], ""
    if cs_cache:
        listings += cs_cache.listings
        ts = cs_cache.fetched_at
    if gr_cache:
        listings += markets_to_listings(gr_cache["markets"])
        ts = gr_cache["fetched_at"] if not ts else ts
    if not listings:
        raise HTTPException(404, "No cached listings — refresh ChillSubs and/or Grinder first.")
    return listings, ts


@app.get("/api/matches")
async def get_matches(
    include_drafting: bool = False,
    min_days: int = 0,
    max_days: int = 14,
    source: str = "chillsubs",
):
    listings, fetched_at = _get_listings(source)
    results = match_works_to_listings(
        listings=listings,
        min_days_to_deadline=min_days,
        max_days_to_deadline=max_days if max_days > 0 else None,
        include_drafting=include_drafting,
    )
    return {"count": len(results), "matches": results, "fetched_at": fetched_at, "source": source}


@app.get("/api/boutique")
async def get_boutique(source: str = "chillsubs"):
    listings, fetched_at = _get_listings(source)
    results = find_boutique(listings)
    return {"count": len(results), "boutique": results, "fetched_at": fetched_at, "source": source}


@app.get("/api/works")
async def get_works():
    works = load_works().get("works", [])
    return {"count": len(works), "works": works}


@app.get("/api/score")
async def score_match(work_id: str, listing_url: str):
    """Use Claude to score work→listing fit (requires ANTHROPIC_API_KEY in .env)."""
    cache = load_cache()
    if not cache:
        raise HTTPException(404, "No cached listings — refresh first.")
    listing = next((l for l in cache.listings if l.url == listing_url), None)
    if not listing:
        raise HTTPException(404, f"Listing not found in cache.")
    return await scorer.score(work_id, listing)


@app.get("/api/brief")
async def get_brief(listing_url: str, research_venue: bool = False):
    """
    Generate a writer's brief for a boutique call.
    Set research_venue=true to also scrape the magazine's website (slower).
    Requires ANTHROPIC_API_KEY in .env.
    """
    cache = load_cache()
    if not cache:
        raise HTTPException(404, "No cached listings — refresh first.")
    listing = next((l for l in cache.listings if l.url == listing_url), None)
    if not listing:
        raise HTTPException(404, "Listing not found in cache.")

    venue_pages = None
    if research_venue and listing.magazine_url:
        result = await venue_mod.research(listing.magazine_url)
        if not result.get("error"):
            venue_pages = result.get("pages")

    return await scorer.brief(listing, venue_pages)


@app.get("/api/tweek/calendars")
async def get_tweek_calendars():
    """List Tweek calendars — use this to find your Queries calendar ID."""
    try:
        calendars = await asyncio.get_event_loop().run_in_executor(
            None, tweek_mod.list_calendars
        )
        return {"calendars": calendars}
    except EnvironmentError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Tweek API error: {e}")


@app.get("/api/venue")
async def research_venue(url: str):
    """Scrape a magazine's website to surface About/Submissions pages."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if not _is_safe_url(url):
        raise HTTPException(400, "url resolves to a private or reserved address")
    return await venue_mod.research(url)


class RecordRequest(BaseModel):
    work_id: Optional[str] = None
    work_ids: Optional[list[str]] = Field(None, max_length=20)
    call: str = Field(..., max_length=500)
    listing_url: Optional[str] = Field(None, max_length=2000)
    notes: Optional[str] = Field("", max_length=4000)

    @model_validator(mode="after")
    def require_at_least_one_work(self) -> "RecordRequest":
        if not self.work_id and not self.work_ids:
            raise ValueError("Either work_id or work_ids must be provided")
        return self

    @property
    def all_work_ids(self) -> list[str]:
        ids: list[str] = []
        if self.work_ids:
            ids.extend(self.work_ids)
        if self.work_id and self.work_id not in ids:
            ids.append(self.work_id)
        return ids

    @field_validator("listing_url")
    @classmethod
    def validate_url(cls, v):
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("listing_url must start with http:// or https://")
        return v


@app.post("/api/record")
async def record_submission(req: RecordRequest, force: bool = False):
    """Log a submission and mark the work as submitted."""
    target_ids = req.all_work_ids
    today = date.today().isoformat()
    logged_works: list[dict] = []

    async with _works_lock:
        data = load_works()
        works = data.get("works", [])
        call_lower = req.call.lower()

        for wid in target_ids:
            idx = next((i for i, w in enumerate(works) if w["id"] == wid), None)
            if idx is None:
                raise HTTPException(404, f"Work '{wid}' not found")
            # Duplicate guard: warn on single-work requests (skip for multi-work bundles)
            if not force and len(target_ids) == 1:
                for sub in works[idx].get("submission_history") or []:
                    existing = (sub.get("call") or "").lower()
                    if existing and (existing in call_lower or call_lower in existing):
                        return {
                            "duplicate_warning": True,
                            "message": (
                                f"'{works[idx]['title']}' was already submitted to a call matching "
                                f"'{sub.get('call')}' (result: {sub.get('result','?')}). "
                                "POST again with ?force=true to record anyway."
                            ),
                            "existing": sub,
                        }
            entry: dict = {
                "call":   req.call,
                "date":   today,
                "result": "pending",
                "notes":  req.notes or "",
            }
            if req.listing_url:
                entry["listing_url"] = req.listing_url
            if len(target_ids) > 1:
                entry["bundled_with"] = [i for i in target_ids if i != wid]
            works[idx].setdefault("submission_history", []).append(entry)
            logged_works.append({"work_id": wid, "title": works[idx].get("title", wid)})

        save_works({"works": works})

    for lw in logged_works:
        event_store.append_event(event_schema.submission_sent(
            work_id=lw["work_id"],
            work_title=lw["title"],
            call=req.call,
            listing_url=req.listing_url or "",
            date=today,
            notes=req.notes or "",
        ))
        asyncio.get_event_loop().run_in_executor(
            None, _tweek_log,
            lw["title"], req.call, "submitted", today
        )

    return {
        "logged": True,
        "work_ids": [lw["work_id"] for lw in logged_works],
        "work_id": logged_works[0]["work_id"] if logged_works else None,
        "call": req.call,
    }


@app.get("/api/submissions")
async def get_all_submissions():
    """All submission history across every work, sorted newest first."""
    works = load_works().get("works", [])
    rows = []
    for w in works:
        for i, sub in enumerate(w.get("submission_history") or []):
            rows.append({
                "work_id":          w["id"],
                "work_title":       w["title"],
                "sub_index":        i,
                "call":             sub.get("call", ""),
                "date":             sub.get("date", ""),
                "result":           sub.get("result", "pending"),
                "notes":            sub.get("notes", ""),
                "listing_url":      sub.get("listing_url"),
                "rejection_reason": sub.get("rejection_reason", ""),
            })
    rows.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"count": len(rows), "submissions": rows}


class SubPatch(BaseModel):
    result: Literal["accepted", "rejected", "withdrawn", "waitlisted", "pending"]
    notes: Optional[str] = Field(None, max_length=4000)
    rejection_reason: Optional[str] = Field(None, max_length=500)


@app.patch("/api/submissions/{work_id}/{sub_index}")
async def patch_submission(work_id: str, sub_index: int, patch: SubPatch):
    """Update a submission result (accepted / rejected / withdrawn / pending)."""
    _assert_slug(work_id)
    async with _works_lock:
        data = load_works()
        works = data.get("works", [])
        idx = next((i for i, w in enumerate(works) if w["id"] == work_id), None)
        if idx is None:
            raise HTTPException(404, f"Work '{work_id}' not found")
        subs = works[idx].get("submission_history", [])
        if sub_index >= len(subs):
            raise HTTPException(404, f"Submission index {sub_index} not found")
        subs[sub_index]["result"] = patch.result
        if patch.notes is not None:
            subs[sub_index]["notes"] = patch.notes
        if patch.rejection_reason is not None:
            subs[sub_index]["rejection_reason"] = patch.rejection_reason
        # Sync work status with submission outcome
        if patch.result == "accepted":
            works[idx]["status"] = "accepted"
        elif patch.result in ("rejected", "withdrawn"):
            still_pending = any(
                s.get("result") == "pending"
                for j, s in enumerate(subs) if j != sub_index
            )
            if not still_pending and works[idx].get("status") in ("submitted", "complete"):
                works[idx]["status"] = "complete"
        save_works({"works": works})
    # Emit event for responses and withdrawals (additive — works.json updated above)
    sub = subs[sub_index]
    _response_type_map = {
        "accepted":  "acceptance",
        "rejected":  "rejection",
        "waitlisted": "waitlist",
    }
    if patch.result == "withdrawn":
        event_store.append_event(event_schema.withdrawal_sent(
            work_id=work_id,
            work_title=works[idx].get("title", work_id),
            call=sub.get("call", ""),
            listing_url=sub.get("listing_url", ""),
            date=date.today().isoformat(),
            notes=patch.notes or "",
        ))
    elif patch.result in _response_type_map:
        event_store.append_event(event_schema.response_received(
            work_id=work_id,
            work_title=works[idx].get("title", work_id),
            call=sub.get("call", ""),
            response_type=_response_type_map[patch.result],
            listing_url=sub.get("listing_url", ""),
            date=date.today().isoformat(),
            notes=patch.notes or "",
        ))
    # Fire-and-forget Tweek log for feedback events
    if patch.result in ("accepted", "rejected", "withdrawn"):
        venue = sub.get("call", "unknown venue")
        asyncio.get_event_loop().run_in_executor(
            None, _tweek_log,
            works[idx]["title"], venue, patch.result, date.today().isoformat()
        )
    return {"updated": True, "submission": subs[sub_index]}


class WorkPatch(BaseModel):
    synopsis_short: Optional[str] = Field(None, max_length=600)
    themes:  Optional[list[str]] = None
    comps:   Optional[list[str]] = None
    status:  Optional[str] = Field(None, max_length=50)
    notes:   Optional[str] = Field(None, max_length=4000)


@app.patch("/api/works/{work_id}")
async def patch_work(work_id: str, patch: WorkPatch):
    """Update editable fields on a work record."""
    _assert_slug(work_id)
    async with _works_lock:
        data = load_works()
        works = data.get("works", [])
        idx = next((i for i, w in enumerate(works) if w["id"] == work_id), None)
        if idx is None:
            raise HTTPException(404, f"Work '{work_id}' not found")
        for field, value in patch.model_dump(exclude_none=True).items():
            works[idx][field] = value
        save_works({"works": works})
        return {"updated": True, "work": works[idx]}


class PlanRequest(BaseModel):
    story_title: str = Field(..., max_length=200)
    target_word_count: int
    start_date: str
    call_title: str = Field(..., max_length=300)
    listing_url: str = Field(..., max_length=500)
    deadline: Optional[str] = None


@app.post("/api/plan")
async def plan_story(req: PlanRequest):
    """
    Create a new drafting work entry and generate a writing timeline.
    Pushes sessions to Tweek if TWEEK_API_KEY + TWEEK_CALENDAR_ID are set.
    """
    async with _works_lock:
        data = load_works()
        works = data.get("works", [])
        work_id = uuid.uuid4().hex[:8]
        new_work: dict = {
            "id":             work_id,
            "title":          req.story_title,
            "status":         "drafting",
            "word_count":     req.target_word_count,
            "type":           "short story",
            "genre":          "literary fiction",
            "themes":         [],
            "comps":          [],
            "synopsis_short": "",
            "notes":          f"Planned for: {req.call_title}\n{req.listing_url}",
            "submission_history": [],
        }
        if req.deadline:
            new_work["call_deadline"] = req.deadline
        works.append(new_work)
        save_works({"works": works})

    start = date.fromisoformat(req.start_date)
    tweek_ok = False
    try:
        fn = functools.partial(_create_timeline, work_id=work_id, start_date=start, write_to_tweek=True)
        timeline = await asyncio.get_event_loop().run_in_executor(None, fn)
        tweek_ok = True
    except EnvironmentError:
        fn = functools.partial(_create_timeline, work_id=work_id, start_date=start, write_to_tweek=False)
        timeline = await asyncio.get_event_loop().run_in_executor(None, fn)
    except Exception as exc:
        raise HTTPException(500, f"Timeline creation failed: {exc}")

    # Persist target deadline in the saved timeline so the Works view can show it
    if req.deadline:
        _tl_file = Path(__file__).parent.parent.parent / "data" / "timelines.json"
        if _tl_file.exists():
            _all = json.loads(_tl_file.read_text())
            if timeline["id"] in _all:
                _all[timeline["id"]]["target_deadline"] = req.deadline
                _tl_file.write_text(json.dumps(_all, indent=2))
        timeline["target_deadline"] = req.deadline

    return {"work_id": work_id, "timeline": timeline, "tweek_ok": tweek_ok}


@app.get("/api/timelines")
async def get_all_timelines():
    """
    Summary of all timelines (without task lists).
    Keyed by work_id for easy lookup by the Works tab.
    """
    _tl_file = Path(__file__).parent.parent.parent / "data" / "timelines.json"
    if not _tl_file.exists():
        return {"timelines": {}}
    all_tl = json.loads(_tl_file.read_text())
    summaries: dict = {}
    for tl in all_tl.values():
        summaries[tl["work_id"]] = {
            "timeline_id":     tl["id"],
            "work_id":         tl["work_id"],
            "ready_to_submit": tl["ready_to_submit"],
            "start_date":      tl["start_date"],
            "word_count":      tl.get("word_count"),
            "target_deadline": tl.get("target_deadline"),
            "task_count":      len(tl.get("tasks", [])),
        }
    return {"timelines": summaries}


@app.get("/api/timeline/{work_id}")
async def get_work_timeline(work_id: str):
    """Full timeline with all tasks for a specific work."""
    _tl_file = Path(__file__).parent.parent.parent / "data" / "timelines.json"
    if not _tl_file.exists():
        raise HTTPException(404, "No timelines found")
    all_tl = json.loads(_tl_file.read_text())
    tl = next((t for t in all_tl.values() if t["work_id"] == work_id), None)
    if not tl:
        raise HTTPException(404, f"No timeline for work '{work_id}'")
    return tl


class TimelineAdjust(BaseModel):
    adjustment: str                          # "faster" | "slower" | "custom" | "target"
    from_date: Optional[str] = None          # YYYY-MM-DD; defaults to today
    sessions_per_week: Optional[int] = None
    pages_per_session: Optional[int] = None
    target_deadline: Optional[str] = None   # YYYY-MM-DD; used with adjustment="target"
    sync_tweek: bool = True


@app.patch("/api/timeline/{work_id}")
async def adjust_work_timeline(work_id: str, body: TimelineAdjust):
    """
    Recalculate remaining session dates for a work's timeline.
    adjustment: "faster" (+1 session/week), "slower" (-1), or "custom" (pass sessions_per_week).
    Syncs updated task dates to Tweek when TWEEK_API_KEY is configured.
    """
    _tl_file = Path(__file__).parent.parent.parent / "data" / "timelines.json"
    all_tl: dict = json.loads(_tl_file.read_text()) if _tl_file.exists() else {}
    tl = next((t for t in all_tl.values() if t["work_id"] == work_id), None)

    from_date = date.fromisoformat(body.from_date) if body.from_date else date.today()

    if not tl:
        if body.adjustment == "target" and body.target_deadline:
            # Auto-create timeline for existing work so target-deadline works in one click
            try:
                fn = functools.partial(
                    _create_timeline, work_id=work_id, start_date=from_date, write_to_tweek=False
                )
                await asyncio.get_event_loop().run_in_executor(None, fn)
                all_tl = json.loads(_tl_file.read_text()) if _tl_file.exists() else {}
                tl = next((t for t in all_tl.values() if t["work_id"] == work_id), None)
                if not tl:
                    raise HTTPException(500, "Timeline creation succeeded but entry not found")
            except ValueError as exc:
                raise HTTPException(422, f"Cannot create timeline: {exc}")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(500, f"Failed to create timeline: {exc}")
        else:
            raise HTTPException(404, f"No timeline for work '{work_id}'")

    adjustment = body.adjustment
    sessions_per_week = body.sessions_per_week

    if adjustment == "target" and body.target_deadline:
        deadline = date.fromisoformat(body.target_deadline)
        days_remaining = max(1, (deadline - from_date).days)
        # Count pending sessions in the timeline
        pending_sessions = sum(
            1 for t in tl["tasks"]
            if date.fromisoformat(t["date"]) >= from_date
            and t.get("type") == "writing"
        )
        if pending_sessions == 0:
            pending_sessions = sum(
                1 for t in tl["tasks"]
                if date.fromisoformat(t["date"]) >= from_date
            )
        weeks_remaining = max(1, days_remaining / 7)
        sessions_per_week = max(1, min(7, round(pending_sessions / weeks_remaining)))
        adjustment = "custom"
        # Save target_deadline into the timeline file
        all_tl[tl["id"]]["target_deadline"] = body.target_deadline
        _tl_file.write_text(json.dumps(all_tl, indent=2))

    fn = functools.partial(
        _adjust_timeline,
        timeline_id=tl["id"],
        adjustment=adjustment,
        from_date=from_date,
        new_sessions_per_week=sessions_per_week,
        new_pages_per_session=body.pages_per_session,
    )
    updated = await asyncio.get_event_loop().run_in_executor(None, fn)

    tweek_ok = False
    if body.sync_tweek:
        try:
            sync_fn = functools.partial(_sync_tweek_on_adjust, updated)
            await asyncio.get_event_loop().run_in_executor(None, sync_fn)
            tweek_ok = True
            # Save once more — sync may have added tweek_task_ids to pending tasks
            all_tl2 = json.loads(_tl_file.read_text())
            all_tl2[updated["id"]] = updated
            _tl_file.write_text(json.dumps(all_tl2, indent=2))
        except EnvironmentError:
            pass   # Tweek not configured; local adjustment still saved
        except Exception as exc:
            print(f"[tweek] adjust sync failed: {exc}")

    return {"timeline": updated, "tweek_ok": tweek_ok}


@app.get("/api/tweek/tasks")
async def get_tweek_tasks_range(date_from: str, date_to: str):
    """
    Fetch Tweek tasks between two dates (YYYY-MM-DD) for calendar conflict detection.
    Requires TWEEK_API_KEY and TWEEK_CALENDAR_ID in .env.
    """
    try:
        fn = functools.partial(tweek_mod.list_tasks, date_from, date_to)
        tasks = await asyncio.get_event_loop().run_in_executor(None, fn)
        return {"tasks": tasks}
    except EnvironmentError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Tweek API error: {e}")


# ---------------------------------------------------------------------------
# QueryTracker — agent / publisher listings
# ---------------------------------------------------------------------------

_QT_CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "qt-cache.json"


@app.get("/api/qt/cache")
async def qt_cache_info():
    cache = qt_mod.load_cache()
    if not cache:
        return {"available": False}
    return {
        "available":    True,
        "fetched_at":   cache.fetched_at,
        "agents":       len(cache.agents),
        "publishers":   len(cache.publishers),
        "genre_filter": cache.genre_filter,
    }


@app.get("/api/qt/listings")
async def qt_listings():
    """Return the cached agent + publisher listings, excluding blocked entries."""
    cache = qt_mod.load_cache()
    if not cache:
        raise HTTPException(404, "No QT cache — refresh first.")
    blocked = _load_blocked()
    return {
        "fetched_at":   cache.fetched_at,
        "genre_filter": cache.genre_filter,
        "agents":       [a for a in cache.agents    if a.get("url", "") not in blocked],
        "publishers":   [p for p in cache.publishers if p.get("url", "") not in blocked],
    }


@app.get("/api/qt/refresh")
async def qt_refresh(genre: str = "Literary Fiction"):
    """
    Fetch agents + publishers from QueryTracker, stream SSE progress.
    WARNING: first fetch can take 30–60+ minutes depending on the total
    number of listings.  Results appear progressively as profiles are fetched.
    Subsequent calls use the 14-day cache and return instantly.

    Events:
      {"type":"list_page",  "entity":"agent"|"publisher", "page":N, "total":T, "ids_found":K}
      {"type":"profile",    "entity":"agent"|"publisher", "entry":{...}}
      {"type":"done",       "agents":N, "publishers":N}
      {"type":"error",      "message":"..."}
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _on_progress(event: dict):
        await queue.put(event)

    async def _run():
        try:
            await qt_mod.search(genre=genre, on_progress=_on_progress)
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})

    asyncio.create_task(_run())

    async def _stream():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=600)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","message":"Timeout — scrape took too long"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Manuscript query tracking (agents + publishers pipeline)
# ---------------------------------------------------------------------------

_QUERIES_FILE = Path(__file__).parent.parent.parent / "data" / "queries.json"


def _load_queries() -> list[dict]:
    if not _QUERIES_FILE.exists():
        return []
    return json.loads(_QUERIES_FILE.read_text()).get("queries", [])


def _save_queries(queries: list[dict]) -> None:
    Path(_QUERIES_FILE).parent.mkdir(exist_ok=True)
    _QUERIES_FILE.write_text(json.dumps({"queries": queries}, indent=2))


@app.get("/api/queries")
async def get_queries():
    """All manuscript queries (agents + publishers) sorted newest first."""
    queries = _load_queries()
    queries.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {"count": len(queries), "queries": queries}


class QueryRequest(BaseModel):
    type: str                     # "agent" | "publisher"
    name: str
    agency: str = ""              # agency name (agents) or press name (publishers)
    manuscript_id: Optional[str] = None
    manuscript_title: str = ""
    stage: str = "queried"        # default starting stage
    notes: str = ""
    url: str = ""                 # QueryTracker profile URL
    query_url: str = ""           # QT query submission link or external form


@app.post("/api/query")
async def record_query(req: QueryRequest):
    """Log a new agent query or publisher submission."""
    queries = _load_queries()
    entry = {
        "id":               uuid.uuid4().hex[:8],
        "type":             req.type,
        "name":             req.name,
        "agency":           req.agency,
        "manuscript_id":    req.manuscript_id or "",
        "manuscript_title": req.manuscript_title,
        "date":             date.today().isoformat(),
        "date_updated":     date.today().isoformat(),
        "stage":            req.stage,
        "notes":            req.notes,
        "url":              req.url,
        "query_url":        req.query_url,
    }
    queries.append(entry)
    _save_queries(queries)
    return {"logged": True, "query": entry}


class QueryPatch(BaseModel):
    stage: Optional[str] = None
    notes: Optional[str] = None


@app.patch("/api/query/{query_id}")
async def patch_query(query_id: str, patch: QueryPatch):
    """Update stage or notes on a tracked query."""
    queries = _load_queries()
    idx = next((i for i, q in enumerate(queries) if q["id"] == query_id), None)
    if idx is None:
        raise HTTPException(404, f"Query '{query_id}' not found")
    if patch.stage is not None:
        queries[idx]["stage"] = patch.stage
        queries[idx]["date_updated"] = date.today().isoformat()
    if patch.notes is not None:
        queries[idx]["notes"] = patch.notes
    _save_queries(queries)
    return {"updated": True, "query": queries[idx]}


@app.delete("/api/query/{query_id}")
async def delete_query(query_id: str):
    """Remove a tracked query."""
    queries = _load_queries()
    original_len = len(queries)
    queries = [q for q in queries if q["id"] != query_id]
    if len(queries) == original_len:
        raise HTTPException(404, f"Query '{query_id}' not found")
    _save_queries(queries)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# The Submission Grinder — short story / fiction markets
# ---------------------------------------------------------------------------

@app.get("/api/grinder/cache")
async def grinder_cache_info():
    cache = grinder_mod.load_cache()
    if not cache:
        return {"available": False}
    return {
        "available":  True,
        "fetched_at": cache["fetched_at"],
        "markets":    len(cache["markets"]),
        "genre_ids":  cache.get("genre_ids", []),
    }


@app.get("/api/grinder/listings")
async def grinder_listings():
    cache = grinder_mod.load_cache()
    if not cache:
        raise HTTPException(404, "No Grinder cache — refresh first.")
    from ..sites.grinder import markets_to_listings
    listings = [l.model_dump() for l in markets_to_listings(cache["markets"])]
    return {"fetched_at": cache["fetched_at"], "count": len(listings), "listings": listings}


@app.post("/api/grinder/refresh")
async def grinder_refresh():
    """
    Fetch fiction markets from The Submission Grinder, streaming SSE progress.

    Events:
      {"type":"session"}
      {"type":"fetched","total":N,"open":M}
      {"type":"done","markets":N}
      {"type":"error","message":"..."}
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _on_progress(event: dict):
        await queue.put(event)

    async def _run():
        try:
            await grinder_mod.search(on_progress=_on_progress)
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})

    asyncio.create_task(_run())

    async def _stream():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=120)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","message":"Timeout — no response from The Submission Grinder"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# MSWL — Manuscript Wishlist agent/editor posts
# ---------------------------------------------------------------------------

@app.get("/api/mswl/cache")
async def mswl_cache_info():
    cache = mswl_mod.load_cache()
    if not cache:
        return {"available": False}
    return {
        "available":    True,
        "fetched_at":   cache["fetched_at"],
        "entries":      len(cache["entries"]),
        "genre_filter": cache.get("genre_filter", ""),
    }


@app.get("/api/mswl/listings")
async def mswl_listings():
    cache = mswl_mod.load_cache()
    if not cache:
        raise HTTPException(404, "No MSWL cache — refresh first.")
    return {
        "fetched_at":   cache["fetched_at"],
        "genre_filter": cache.get("genre_filter", ""),
        "count":        len(cache["entries"]),
        "entries":      cache["entries"],
    }


@app.post("/api/mswl/refresh")
async def mswl_refresh(genre: str = "literary"):
    """
    Fetch MSWL wishlist posts, streaming SSE progress.

    Events:
      {"type":"fetched","total":N}
      {"type":"filtered","matches":M}
      {"type":"done","entries":M}
      {"type":"error","message":"..."}
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _on_progress(event: dict):
        await queue.put(event)

    async def _run():
        try:
            await mswl_mod.search(genre=genre, on_progress=_on_progress)
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})

    asyncio.create_task(_run())

    async def _stream():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","message":"Timeout — no response from MSWL"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Poets & Writers — Small Presses
# ---------------------------------------------------------------------------

@app.get("/api/pw/cache")
async def pw_cache_info():
    cache = pw_mod.load_cache()
    if not cache:
        return {"available": False}
    return {
        "available":  True,
        "fetched_at": cache["fetched_at"],
        "presses":    len(cache["presses"]),
    }


@app.get("/api/pw/listings")
async def pw_listings():
    cache = pw_mod.load_cache()
    if not cache:
        raise HTTPException(404, "No P&W cache — refresh first.")
    return {
        "fetched_at": cache["fetched_at"],
        "count":      len(cache["presses"]),
        "presses":    cache["presses"],
    }


@app.post("/api/pw/refresh")
async def pw_refresh(genre: str = "Fiction", subgenre_id: str = "9677", open_only: bool = False):
    """
    Fetch small press listings from Poets & Writers, streaming SSE progress.

    Events:
      {"type":"fetching"}
      {"type":"fetched","total":N,"open":M}
      {"type":"done","presses":N}
      {"type":"error","message":"..."}
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _on_progress(event: dict):
        await queue.put(event)

    async def _run():
        try:
            await pw_mod.search(
                genre=genre,
                subgenre_id=subgenre_id,
                open_only=open_only,
                on_progress=_on_progress,
            )
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})

    asyncio.create_task(_run())

    async def _stream():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60)
            except asyncio.TimeoutError:
                yield 'data: {"type":"error","message":"Timeout — no response from Poets & Writers"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Novel manuscript matching
# ---------------------------------------------------------------------------

@app.get("/api/novel/matches")
async def novel_matches(novel_id: str = "creme-brulee"):
    """
    Score all cached agents (QT), MSWL wishlists, and P&W presses against
    the specified novel work. Returns ranked lists per source + combined top-20.
    """
    works = load_works().get("works", [])
    novel = next((w for w in works if w["id"] == novel_id and w.get("type") == "novel"), None)
    if not novel:
        # Fall back to first novel found
        novel = next((w for w in works if w.get("type") == "novel"), None)
    if not novel:
        raise HTTPException(404, "No novel found in works — add one with build-profile.")

    blocked = _load_blocked()

    qt_cache = qt_mod.load_cache()
    agents    = qt_cache.agents    if qt_cache else []
    # QT agents are dataclasses — convert to dicts
    if agents and hasattr(agents[0], "__dataclass_fields__"):
        from dataclasses import asdict as dc_asdict
        agents = [dc_asdict(a) for a in agents]
    agents = [a for a in agents if a.get("url", "") not in blocked]

    mswl_cache = mswl_mod.load_cache()
    wishlists  = mswl_cache["entries"] if mswl_cache else []
    wishlists  = [w for w in wishlists if w.get("url", w.get("link", "")) not in blocked]

    pw_cache = pw_mod.load_cache()
    presses  = pw_cache["presses"] if pw_cache else []
    presses  = [p for p in presses if p.get("pw_url", "") not in blocked]

    result = novel_matcher.match_novel(novel, agents, wishlists, presses)
    result["sources"] = {
        "agents":    len(agents),
        "wishlists": len(wishlists),
        "presses":   len(presses),
    }
    return result


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Blocked entries (agents, presses, wishlists)
# ---------------------------------------------------------------------------

_BLOCKED_FILE = Path(__file__).parent.parent.parent / "data" / "blocked.json"


def _load_blocked() -> dict:
    if not _BLOCKED_FILE.exists():
        return {}
    return json.loads(_BLOCKED_FILE.read_text())


def _save_blocked(blocked: dict) -> None:
    _BLOCKED_FILE.parent.mkdir(exist_ok=True)
    _BLOCKED_FILE.write_text(json.dumps(blocked, indent=2))


class BlockRequest(BaseModel):
    url: str
    name: str
    entry_type: str   # "agent" | "press" | "wishlist" | "publisher"
    reason: str = ""


@app.get("/api/blocked")
async def get_blocked():
    return _load_blocked()


@app.post("/api/blocked")
async def add_blocked(body: BlockRequest):
    blocked = _load_blocked()
    blocked[body.url] = {
        "name":       body.name,
        "type":       body.entry_type,
        "reason":     body.reason,
        "blocked_at": date.today().isoformat(),
    }
    _save_blocked(blocked)
    return {"blocked": True, "url": body.url}


@app.delete("/api/blocked")
async def remove_blocked(url: str):
    blocked = _load_blocked()
    blocked.pop(url, None)
    _save_blocked(blocked)
    return {"unblocked": True}


# ---------------------------------------------------------------------------
# Manual publishers (Deep Vellum etc.)
# ---------------------------------------------------------------------------

_MANUAL_PUBLISHERS_FILE = Path(__file__).parent.parent.parent / "data" / "publishers-manual.json"


def _load_manual_publishers() -> list[dict]:
    if not _MANUAL_PUBLISHERS_FILE.exists():
        return []
    return json.loads(_MANUAL_PUBLISHERS_FILE.read_text()).get("publishers", [])


def _save_manual_publishers(publishers: list[dict]) -> None:
    Path(_MANUAL_PUBLISHERS_FILE).parent.mkdir(exist_ok=True)
    _MANUAL_PUBLISHERS_FILE.write_text(json.dumps({"publishers": publishers}, indent=2))


@app.get("/api/publishers/manual")
async def get_manual_publishers():
    return {"publishers": _load_manual_publishers()}


class ManualPublisherRequest(BaseModel):
    name: str
    url: str = ""
    submissions_url: str = ""
    genres: list[str] = []
    notes: str = ""
    reading_period: str = ""
    temporarily_closed: bool = False


@app.post("/api/publishers/manual")
async def add_manual_publisher(req: ManualPublisherRequest):
    publishers = _load_manual_publishers()
    entry = {
        "id":                 uuid.uuid4().hex[:8],
        "name":               req.name,
        "url":                req.url,
        "submissions_url":    req.submissions_url,
        "genres":             req.genres,
        "notes":              req.notes,
        "reading_period":     req.reading_period,
        "temporarily_closed": req.temporarily_closed,
        "added_at":           date.today().isoformat(),
    }
    publishers.append(entry)
    _save_manual_publishers(publishers)
    return {"added": True, "publisher": entry}


class ManualPublisherPatch(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    submissions_url: Optional[str] = None
    genres: Optional[list[str]] = None
    notes: Optional[str] = None
    reading_period: Optional[str] = None
    temporarily_closed: Optional[bool] = None


@app.patch("/api/publishers/manual/{pub_id}")
async def patch_manual_publisher(pub_id: str, patch: ManualPublisherPatch):
    publishers = _load_manual_publishers()
    idx = next((i for i, p in enumerate(publishers) if p["id"] == pub_id), None)
    if idx is None:
        raise HTTPException(404, f"Publisher '{pub_id}' not found")
    for field, value in patch.model_dump(exclude_none=True).items():
        publishers[idx][field] = value
    _save_manual_publishers(publishers)
    return {"updated": True, "publisher": publishers[idx]}


@app.delete("/api/publishers/manual/{pub_id}")
async def delete_manual_publisher(pub_id: str):
    publishers = _load_manual_publishers()
    original = len(publishers)
    publishers = [p for p in publishers if p["id"] != pub_id]
    if len(publishers) == original:
        raise HTTPException(404, f"Publisher '{pub_id}' not found")
    _save_manual_publishers(publishers)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Instagram log
# ---------------------------------------------------------------------------

@app.get("/api/instagram/log")
async def get_instagram_log():
    log = ig_mod._load_log()
    return {"count": len(log), "entries": log}


class IgLogRequest(BaseModel):
    post_date: str
    format: str
    caption: str
    metrics: Optional[dict] = None
    notes: Optional[str] = ""


@app.post("/api/instagram/log")
async def log_instagram_post(req: IgLogRequest):
    entry = ig_mod.log_post(
        post_date=req.post_date,
        format=req.format,
        caption=req.caption,
        metrics=req.metrics,
        notes=req.notes,
    )
    return {"logged": True, "entry": entry}


@app.delete("/api/instagram/log/{entry_id}")
async def delete_instagram_entry(entry_id: str):
    log = ig_mod._load_log()
    original = len(log)
    log = [e for e in log if e["id"] != entry_id]
    if len(log) == original:
        raise HTTPException(404, f"Entry '{entry_id}' not found")
    ig_mod._save_log(log)
    return {"deleted": True}


@app.get("/api/instagram/suggest")
async def instagram_suggest():
    result = ig_mod.suggest()
    return result


@app.get("/api/instagram/schedule")
async def get_instagram_schedule():
    if not ig_mod.SCHEDULE_FILE.exists():
        return {"available": False, "content": ""}
    return {"available": True, "content": ig_mod.SCHEDULE_FILE.read_text()}


class ScheduleBody(BaseModel):
    content: str = Field("", max_length=100_000)


@app.post("/api/instagram/schedule")
async def save_instagram_schedule(body: ScheduleBody):
    ig_mod.DATA_DIR.mkdir(exist_ok=True)
    ig_mod.SCHEDULE_FILE.write_text(body.content)
    return {"saved": True}


# ---------------------------------------------------------------------------
# Works export / backup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Manuscript storage  (plain text only — never passed to AI)
# ---------------------------------------------------------------------------

_MANUSCRIPTS_DIR = Path(__file__).parent.parent.parent / "data" / "manuscripts"


@app.get("/api/works/{work_id}/manuscript")
async def get_manuscript(work_id: str):
    _assert_slug(work_id)
    path = _MANUSCRIPTS_DIR / f"{work_id}.txt"
    if not path.exists():
        return {"text": "", "work_id": work_id}
    return {"text": path.read_text(encoding="utf-8"), "work_id": work_id}


class ManuscriptBody(BaseModel):
    text: str = Field(..., max_length=600_000)  # ~120k words — generous for a short story


@app.put("/api/works/{work_id}/manuscript")
async def save_manuscript(work_id: str, body: ManuscriptBody):
    _assert_slug(work_id)
    works = load_works().get("works", [])
    if not any(w["id"] == work_id for w in works):
        raise HTTPException(404, f"Work '{work_id}' not found")
    _MANUSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    (_MANUSCRIPTS_DIR / f"{work_id}.txt").write_text(body.text, encoding="utf-8")
    return {"saved": True, "work_id": work_id}


# ---------------------------------------------------------------------------
# Formatting presets
# ---------------------------------------------------------------------------

_PRESETS_FILE = Path(__file__).parent.parent.parent / "data" / "formatting-presets.json"

_DEFAULT_PRESETS = {
    "Standard Manuscript": {"font": "'Times New Roman', Times, serif", "size": 12, "spacing": "2.0"},
    "Shunn Modern":        {"font": "'Times New Roman', Times, serif", "size": 12, "spacing": "2.0"},
    "Courier Draft":       {"font": "'Courier New', Courier, monospace", "size": 12, "spacing": "2.0"},
}


def _load_presets() -> dict:
    if not _PRESETS_FILE.exists():
        return dict(_DEFAULT_PRESETS)
    return json.loads(_PRESETS_FILE.read_text())


def _save_presets(p: dict) -> None:
    _PRESETS_FILE.parent.mkdir(exist_ok=True)
    _PRESETS_FILE.write_text(json.dumps(p, indent=2))


@app.get("/api/formatting-presets")
async def get_presets():
    return _load_presets()


class PresetBody(BaseModel):
    name: str
    font: str
    size: float
    spacing: str


@app.put("/api/formatting-presets")
async def upsert_preset(body: PresetBody):
    presets = _load_presets()
    presets[body.name] = {"font": body.font, "size": body.size, "spacing": body.spacing}
    _save_presets(presets)
    return {"saved": True, "name": body.name}


@app.delete("/api/formatting-presets/{name}")
async def delete_preset(name: str):
    presets = _load_presets()
    presets.pop(name, None)
    _save_presets(presets)
    return {"deleted": True, "name": name}


# ---------------------------------------------------------------------------
# Author profile  (bio, credits — used in cover letter builder)
# ---------------------------------------------------------------------------

_AUTHOR_PROFILE_FILE = Path(__file__).parent.parent.parent / "data" / "author-profile.json"


def _load_author_profile() -> dict:
    if not _AUTHOR_PROFILE_FILE.exists():
        return {"name": "", "bio": "", "credits": ""}
    return json.loads(_AUTHOR_PROFILE_FILE.read_text())


@app.get("/api/author-profile")
async def get_author_profile():
    return _load_author_profile()


class AuthorProfileBody(BaseModel):
    name: str = ""
    bio: str = ""
    credits: str = ""


@app.put("/api/author-profile")
async def save_author_profile(body: AuthorProfileBody):
    _AUTHOR_PROFILE_FILE.parent.mkdir(exist_ok=True)
    _AUTHOR_PROFILE_FILE.write_text(
        json.dumps({"name": body.name, "bio": body.bio, "credits": body.credits}, indent=2)
    )
    return {"saved": True}


@app.get("/api/works/export")
async def export_works():
    """Download works.json as a file attachment."""
    from fastapi.responses import Response
    data = load_works()
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=works-backup.json"},
    )


# ---------------------------------------------------------------------------
# Cache health (for expiry notifications)
# ---------------------------------------------------------------------------

@app.get("/api/health/caches")
async def cache_health():
    """
    Returns age in days for each cache so the UI can show expiry warnings.
    """
    from datetime import datetime

    def _age(fetched_at_str: str) -> Optional[float]:
        try:
            dt = datetime.fromisoformat(fetched_at_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return None

    result = {}
    cs = load_cache()
    result["chillsubs"] = {
        "available": bool(cs),
        "fetched_at": cs.fetched_at if cs else None,
        "age_days": _age(cs.fetched_at) if cs else None,
        "ttl_days": 1,
    }
    gr = grinder_mod.load_cache()
    result["grinder"] = {
        "available": bool(gr),
        "fetched_at": gr["fetched_at"] if gr else None,
        "age_days": _age(gr["fetched_at"]) if gr else None,
        "ttl_days": 7,
    }
    qt = qt_mod.load_cache()
    result["querytracker"] = {
        "available": bool(qt),
        "fetched_at": qt.fetched_at if qt else None,
        "age_days": _age(qt.fetched_at) if qt else None,
        "ttl_days": 14,
    }
    ms = mswl_mod.load_cache()
    result["mswl"] = {
        "available": bool(ms),
        "fetched_at": ms["fetched_at"] if ms else None,
        "age_days": _age(ms["fetched_at"]) if ms else None,
        "ttl_days": 30,
    }
    pw = pw_mod.load_cache()
    result["pw"] = {
        "available": bool(pw),
        "fetched_at": pw["fetched_at"] if pw else None,
        "age_days": _age(pw["fetched_at"]) if pw else None,
        "ttl_days": 7,
    }
    return result


# ---------------------------------------------------------------------------
# Saved calls / agents / presses (save-for-later)
# ---------------------------------------------------------------------------

_SAVED_FILE = Path(__file__).parent.parent.parent / "data" / "saved-calls.json"


def _load_saved() -> dict:
    if not _SAVED_FILE.exists():
        return {}
    return json.loads(_SAVED_FILE.read_text())


def _write_saved(data: dict) -> None:
    _SAVED_FILE.parent.mkdir(exist_ok=True)
    _SAVED_FILE.write_text(json.dumps(data, indent=2))


class SavedItemRequest(BaseModel):
    type: str = Field(..., max_length=20)        # "call" | "agent" | "publisher" | "press" | "wishlist"
    name: str = Field(..., max_length=300)
    url:  str = Field("",  max_length=500)
    notes: str = Field("", max_length=2000)
    metadata: Optional[dict] = None


class SavedItemPatch(BaseModel):
    name: Optional[str] = Field(None, max_length=300)
    url: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = Field(None, max_length=2000)
    metadata: Optional[dict] = None
    targeted_works: Optional[list[dict]] = None


@app.get("/api/saved")
async def get_saved():
    data = _load_saved()
    return {"count": len(data), "items": list(data.values())}


@app.post("/api/saved")
async def add_saved(req: SavedItemRequest):
    data = _load_saved()
    # Deduplicate by URL
    existing = next((v for v in data.values() if v["url"] == req.url), None)
    if existing:
        return {"saved": True, "item": existing, "duplicate": True}
    item_id = uuid.uuid4().hex[:8]
    entry = {
        "id":       item_id,
        "type":     req.type,
        "name":     req.name,
        "url":      req.url,
        "notes":    req.notes,
        "metadata": req.metadata or {},
        "saved_at": date.today().isoformat(),
    }
    data[item_id] = entry
    _write_saved(data)
    return {"saved": True, "item": entry}


@app.patch("/api/saved/{item_id}")
async def patch_saved(item_id: str, patch: SavedItemPatch):
    data = _load_saved()
    if item_id not in data:
        raise HTTPException(404, f"Saved item '{item_id}' not found")
    if patch.name is not None:
        data[item_id]["name"] = patch.name
    if patch.url is not None:
        data[item_id]["url"] = patch.url
    if patch.notes is not None:
        data[item_id]["notes"] = patch.notes
    if patch.metadata is not None:
        data[item_id]["metadata"] = {**(data[item_id].get("metadata") or {}), **patch.metadata}
    if patch.targeted_works is not None:
        data[item_id]["targeted_works"] = patch.targeted_works
    _write_saved(data)
    return {"updated": True, "item": data[item_id]}


class WorkTargetRequest(BaseModel):
    work_id: str = Field(..., max_length=100)
    work_title: str = Field(..., max_length=300)
    word_count: Optional[int] = None
    status: Optional[str] = Field(None, max_length=50)


@app.post("/api/saved/{item_id}/works")
async def add_target_work(item_id: str, req: WorkTargetRequest):
    """Target a work towards a saved call. Multiple works can be targeted per call."""
    data = _load_saved()
    if item_id not in data:
        raise HTTPException(404, f"Saved item '{item_id}' not found")
    targeted = data[item_id].setdefault("targeted_works", [])
    if any(w["work_id"] == req.work_id for w in targeted):
        return {"added": False, "duplicate": True, "item": data[item_id]}
    targeted.append({
        "work_id":     req.work_id,
        "work_title":  req.work_title,
        "word_count":  req.word_count,
        "status":      req.status or "unknown",
        "targeted_at": date.today().isoformat(),
    })
    _write_saved(data)
    return {"added": True, "item": data[item_id]}


@app.delete("/api/saved/{item_id}/works/{work_id}")
async def remove_target_work(item_id: str, work_id: str):
    """Remove a work from a saved call's target list."""
    data = _load_saved()
    if item_id not in data:
        raise HTTPException(404, f"Saved item '{item_id}' not found")
    original = data[item_id].get("targeted_works", [])
    data[item_id]["targeted_works"] = [w for w in original if w["work_id"] != work_id]
    _write_saved(data)
    return {"removed": True, "item": data[item_id]}


@app.get("/api/glance")
async def get_glance():
    """
    At-a-glance summary: all saved calls cross-referenced with targeted works
    and existing submission history.
    """
    saved_items = list(_load_saved().values())
    works_data  = load_works().get("works", [])
    sub_lookup: dict[str, list] = {}
    for w in works_data:
        sub_lookup[w["id"]] = w.get("submission_history", [])

    result = []
    for item in saved_items:
        targeted = item.get("targeted_works", [])
        enriched_targets = []
        for tw in targeted:
            subs = sub_lookup.get(tw["work_id"], [])
            submitted_to_this = [
                s for s in subs
                if item["url"] and (
                    s.get("listing_url") == item["url"]
                    or item["name"].lower() in (s.get("call") or "").lower()
                )
            ]
            enriched_targets.append({
                **tw,
                "submissions": submitted_to_this,
                "any_pending":  any(s.get("result") == "pending" for s in submitted_to_this),
                "any_accepted": any(s.get("result") == "accepted" for s in submitted_to_this),
            })
        result.append({**item, "targeted_works": enriched_targets})

    targeted_count = sum(len(i.get("targeted_works", [])) for i in result)
    pending_count  = sum(
        1 for i in result
        for tw in i.get("targeted_works", [])
        if tw.get("any_pending")
    )
    return {
        "items":           result,
        "total_saved":     len(result),
        "total_targeted":  targeted_count,
        "total_pending":   pending_count,
    }


@app.delete("/api/saved/{item_id}")
async def delete_saved(item_id: str):
    data = _load_saved()
    if item_id not in data:
        raise HTTPException(404, f"Saved item '{item_id}' not found")
    del data[item_id]
    _write_saved(data)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Agent write-confirmation (LLM06) — web path pending-action endpoints
# ---------------------------------------------------------------------------

_pending_actions: dict[str, dict] = {}
_PENDING_TTL_SECS = 300  # auto-expire after 5 minutes
# Each entry: {"tool": str, "args": dict, "event": asyncio.Event,
#              "approved": bool, "created_at": time.monotonic()}


class ConfirmBody(BaseModel):
    action_id: str
    approved: bool = False


@app.get("/api/agent/pending-actions")
async def list_pending_actions():
    """Actions the agent loop is waiting for Trevor to approve or deny."""
    now = time.monotonic()
    return [
        {"id": k, "tool": v["tool"], "args": v["args"]}
        for k, v in _pending_actions.items()
        if not v["event"].is_set() and (now - v["created_at"]) < _PENDING_TTL_SECS
    ]


@app.post("/api/agent/confirm-action")
async def confirm_action(body: ConfirmBody):
    """Approve or deny a pending write-tool call from the agent."""
    entry = _pending_actions.get(body.action_id)
    if not entry:
        raise HTTPException(404, "Pending action not found")
    if (time.monotonic() - entry["created_at"]) >= _PENDING_TTL_SECS:
        entry["event"].set()  # unblock the waiting coroutine
        _pending_actions.pop(body.action_id, None)
        raise HTTPException(410, "Action expired — the agent has moved on")
    entry["approved"] = body.approved
    entry["event"].set()
    _pending_actions.pop(body.action_id, None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Demo: Input Fence
# ---------------------------------------------------------------------------

class FenceDemoRequest(BaseModel):
    text: str = Field(max_length=4000)


@app.post("/api/demo/fence")
async def demo_fence(req: FenceDemoRequest):
    """Run text through the input fence and return scan results for the demo tab."""
    from ..guardrails.input_fence import scan
    result = scan(req.text, source="demo")
    return {
        "annotated": result.text,
        "injections": result.injections,
        "manuscript_refs": result.manuscript_refs,
        "clean": result.clean,
    }


# ---------------------------------------------------------------------------
# Events feed
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def get_events_feed(limit: int = 100):
    """Return recent submission lifecycle events from the append-only log."""
    from ..events.store import replay
    events = replay()
    return [e.to_dict() for e in events[-limit:]]


# ---------------------------------------------------------------------------
# Eval status
# ---------------------------------------------------------------------------

_EVAL_CACHE = Path(__file__).parent.parent.parent / "data" / "eval-results.json"


@app.get("/api/evals/status")
async def get_evals_status():
    """Return eval case metadata and last-run results if the cache file exists."""
    cases: list[dict] = []
    try:
        from ..evals.cases import ALL_CASES
        cases = [
            {
                "id": c.id,
                "description": c.description,
                "skip": bool(getattr(c, "skip", False)),
            }
            for c in ALL_CASES
        ]
    except Exception:
        pass

    last_run = None
    if _EVAL_CACHE.exists():
        try:
            last_run = json.loads(_EVAL_CACHE.read_text())
        except Exception:
            pass

    return {"cases": cases, "last_run": last_run}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    host = "0.0.0.0" if os.environ.get("DEPLOY") else "127.0.0.1"
    uvicorn.run(app, host=host, port=PORT, reload=False)
