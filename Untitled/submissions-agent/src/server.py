"""
Submissions Agent MCP Server

Entry point: `submissions-agent` (after pip install -e .)
or: python -m src.server

Tools registered:
  list_works, upsert_work, search_calls, match_works, find_boutique,
  record_submission, record_response, get_submission_context,
  create_timeline, adjust_timeline,
  log_instagram_post, suggest_instagram
"""

import asyncio
import json
import os
from datetime import date
from pathlib import Path

import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .instagram.log import log_post, suggest as instagram_suggest
from .match.matcher import find_boutique, match_works_to_listings
from .profile.builder import load_works, save_works
from .secrets.bitwarden import get_credentials, start_session
from .sites.chillsubs import load_cache
from .sites.chillsubs import search as chillsubs_search
from .sites.filmfreeway import search as filmfreeway_search, load_cache as ff_load_cache
from .sites.fellowships import get_fellowships
from .timeline.builder import adjust_timeline, create_timeline
from .events import schema as event_schema
from .events import store as event_store
from .events import handlers as _event_handlers  # noqa: F401 — registers handlers

load_dotenv()

WORKS_FILE = Path(__file__).parent / "profile" / "works.json"

app = Server("submissions-agent")

_bw_session: str | None = None


def _get_bw_session() -> str:
    global _bw_session
    if not _bw_session:
        _bw_session = start_session()
    return _bw_session


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_works",
            description="Return all works with status, word count, genre, and type.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="upsert_work",
            description=(
                "Add or update a work record. Must include 'id' and 'title'. "
                "All other fields are optional for updates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "work": {
                        "type": "object",
                        "description": "Work object with at least 'id' and 'title'.",
                    }
                },
                "required": ["work"],
            },
        ),
        types.Tool(
            name="search_calls",
            description=(
                "Search Chill Subs for open submission calls. "
                "No login required — calls are publicly accessible. "
                "Pass use_cache=true (default) to return last cached results instantly. "
                "Pass use_cache=false to fetch live from the Chill Subs API (~4300 calls, takes a minute)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "genre":                {"type": "string", "default": "fiction"},
                    "max_word_count":       {"type": "integer"},
                    "min_days_to_deadline": {"type": "integer", "default": 0},
                    "use_cache":            {"type": "boolean", "default": True},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="match_works",
            description=(
                "Match works to cached listings. "
                "Run search_calls first. Returns listings with matching works and reasons. "
                "Set include_drafting=true to also show works-in-progress that could fit if finished in time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "work_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Limit to these work IDs. Omit to check all eligible works.",
                    },
                    "min_days_to_deadline": {"type": "integer", "default": 0},
                    "include_drafting": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also match works with status 'drafting'. They appear in draft_matches, tagged for review.",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="search_screenplay_calls",
            description=(
                "Search for open screenplay competitions, fellowships, and labs. "
                "Returns FilmFreeway curated competitions (live deadlines) plus major annual programs. "
                "Use use_cache=false to fetch fresh data from FilmFreeway. "
                "Filter by format: feature, short, pilot, tv, or any (default)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format":               {"type": "string", "enum": ["feature", "short", "pilot", "tv", "any"], "default": "any"},
                    "min_days_to_deadline": {"type": "integer", "default": 0},
                    "use_cache":            {"type": "boolean", "default": True},
                    "include_fellowships":  {"type": "boolean", "default": True},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="find_boutique",
            description=(
                "Return open calls with max word count <= 5,000 "
                "and at least 14 days to deadline. Run search_calls first."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="record_submission",
            description=(
                "Log a submission after Trevor has explicitly approved it. "
                "This tool never submits anything automatically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "work_id": {"type": "string"},
                    "call":    {"type": "string", "description": "Name or URL of the call"},
                    "date":    {"type": "string", "description": "Submission date YYYY-MM-DD"},
                    "notes":   {"type": "string"},
                },
                "required": ["work_id", "call", "date"],
            },
        ),
        types.Tool(
            name="create_timeline",
            description=(
                "Build a draft timeline for a work using Trevor's routine. "
                "Saves to data/timelines.json and updates the work's timeline_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "work_id":            {"type": "string"},
                    "start_date":         {"type": "string", "description": "YYYY-MM-DD"},
                    "pages_per_session":  {"type": "integer", "default": 2},
                    "sessions_per_week":  {"type": "integer", "default": 6},
                    "write_to_tweek":     {"type": "boolean", "default": True},
                },
                "required": ["work_id", "start_date"],
            },
        ),
        types.Tool(
            name="adjust_timeline",
            description=(
                "Recalculate remaining timeline dates after a pace change. "
                "Completed sessions are preserved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "timeline_id": {"type": "string"},
                    "adjustment":  {
                        "type": "string",
                        "enum": ["faster", "slower", "custom"],
                        "description": (
                            "'faster' adds 1 session/week, 'slower' removes 1, "
                            "'custom' uses new_sessions_per_week directly"
                        ),
                    },
                    "from_date":             {"type": "string", "description": "Override today YYYY-MM-DD"},
                    "new_sessions_per_week": {"type": "integer"},
                },
                "required": ["timeline_id", "adjustment"],
            },
        ),
        types.Tool(
            name="log_instagram_post",
            description="Log an Instagram post Trevor pastes or describes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date":    {"type": "string", "description": "YYYY-MM-DD"},
                    "format":  {"type": "string", "description": "reel, carousel, single image, story, etc."},
                    "caption": {"type": "string"},
                    "metrics": {
                        "type": "object",
                        "description": "Any numbers: likes, comments, reach, saves, etc.",
                    },
                    "notes": {"type": "string"},
                },
                "required": ["date", "format", "caption"],
            },
        ),
        types.Tool(
            name="suggest_instagram",
            description=(
                "Suggest captions, image types, and schedule changes based on logged posts. "
                "Never rewrites Trevor's voice without approval. "
                "Reads data/schedule.md or accepts schedule_text directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_text": {
                        "type": "string",
                        "description": "Paste your schedule here, or leave blank to read data/schedule.md",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="record_response",
            description=(
                "Record a response (rejection, acceptance, waitlist, or withdrawal) "
                "to a previously logged submission. Appends a response_received event "
                "and updates the work's submission history."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "work_id":       {"type": "string"},
                    "call":          {"type": "string", "description": "Name or URL of the call"},
                    "response_type": {
                        "type": "string",
                        "enum": ["rejection", "acceptance", "waitlist", "withdrawn"],
                    },
                    "date":          {"type": "string", "description": "Response date YYYY-MM-DD"},
                    "notes":         {"type": "string"},
                },
                "required": ["work_id", "call", "response_type"],
            },
        ),
        types.Tool(
            name="get_submission_context",
            description=(
                "Retrieve past submission outcomes similar to a given work and call. "
                "Uses semantic search over submission history to surface relevant track record. "
                "Call this before recommending a match to give context on past patterns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "work_id":          {"type": "string"},
                    "listing_title":    {"type": "string"},
                    "call_description": {"type": "string", "description": "Optional call text for richer matching"},
                    "k":                {"type": "integer", "default": 5, "description": "Number of past submissions to retrieve"},
                },
                "required": ["work_id", "listing_title"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    args = arguments or {}
    try:
        result = await _dispatch(name, args)
    except Exception as exc:
        result = {"error": str(exc)}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _dispatch(name: str, args: dict) -> dict:

    # --- Works ---

    if name == "list_works":
        works = load_works().get("works", [])
        return {
            "count": len(works),
            "works": [
                {
                    "id":         w["id"],
                    "title":      w["title"],
                    "type":       w.get("type"),
                    "status":     w.get("status"),
                    "word_count": w.get("word_count"),
                    "genre":      w.get("genre"),
                }
                for w in works
            ],
        }

    if name == "upsert_work":
        work = args["work"]
        if "id" not in work or "title" not in work:
            raise ValueError("work must include 'id' and 'title'")
        _WORK_MAX_LENGTHS = {
            "title": 300, "genre": 100, "status": 50,
            "notes": 4000, "synopsis_short": 600,
        }
        for field, max_len in _WORK_MAX_LENGTHS.items():
            val = work.get(field)
            if isinstance(val, str) and len(val) > max_len:
                raise ValueError(f"'{field}' exceeds maximum length of {max_len} characters")
        data = load_works()
        works = data.get("works", [])
        idx = next((i for i, w in enumerate(works) if w["id"] == work["id"]), None)
        if idx is not None:
            works[idx].update(work)
            action = "updated"
        else:
            works.append(work)
            action = "added"
        save_works({"works": works})
        return {"action": action, "work_id": work["id"]}

    # --- Search & Match ---

    if name == "search_calls":
        cached = load_cache()
        if args.get("use_cache", True):
            if cached:
                return {
                    "source":     "cache",
                    "fetched_at": cached.fetched_at,
                    "count":      len(cached.listings),
                    "listings":   [l.model_dump(mode="json") for l in cached.listings],
                }
            else:
                return {
                    "error": (
                        "No cached listings available. "
                        "To fetch live results, call search_calls with use_cache=false. "
                        "This requires Bitwarden credentials (BW_CLIENTID and BW_CLIENTSECRET in .env) "
                        "and will prompt for the master password."
                    )
                }

        listings = await chillsubs_search(
            genre=args.get("genre", "fiction"),
            max_word_count=args.get("max_word_count"),
            min_days_to_deadline=args.get("min_days_to_deadline", 0),
        )
        return {
            "source":   "live",
            "count":    len(listings),
            "listings": [l.model_dump(mode="json") for l in listings],
        }

    if name == "match_works":
        cache = load_cache()
        if not cache:
            return {"error": "No cached listings. Run search_calls first."}
        results = match_works_to_listings(
            listings=cache.listings,
            work_ids=args.get("work_ids"),
            min_days_to_deadline=args.get("min_days_to_deadline", 0),
            include_drafting=args.get("include_drafting", False),
        )
        return {"match_count": len(results), "matches": results}

    if name == "search_screenplay_calls":
        fmt                 = args.get("format", "any") or "any"
        min_days            = args.get("min_days_to_deadline", 0) or 0
        use_cache           = args.get("use_cache", True)
        include_fellowships = args.get("include_fellowships", True)

        ff_listings = await filmfreeway_search(
            min_days_to_deadline=min_days,
            use_cache=use_cache,
        )
        if fmt != "any":
            ff_listings = [l for l in ff_listings if l.screenplay_format in (fmt, "any")]

        fellowships = get_fellowships(format_filter=fmt) if include_fellowships else []

        all_listings = ff_listings + fellowships
        return {
            "count":         len(all_listings),
            "competitions":  [l.model_dump(mode="json") for l in ff_listings],
            "fellowships":   [l.model_dump(mode="json") for l in fellowships],
        }

    if name == "find_boutique":
        cache = load_cache()
        if not cache:
            return {"error": "No cached listings. Run search_calls first."}
        results = find_boutique(cache.listings)
        return {"count": len(results), "boutique_calls": results}

    # --- Submissions ---

    if name == "record_submission":
        work_id = args["work_id"]
        data  = load_works()
        works = data.get("works", [])
        idx   = next((i for i, w in enumerate(works) if w["id"] == work_id), None)
        if idx is None:
            raise ValueError(f"Work '{work_id}' not found")
        entry = {
            "call":   args["call"],
            "date":   args["date"],
            "result": "pending",
            "notes":  args.get("notes", ""),
        }
        works[idx].setdefault("submission_history", []).append(entry)
        save_works({"works": works})
        event_store.append_event(event_schema.submission_sent(
            work_id=work_id,
            work_title=works[idx].get("title", work_id),
            call=args["call"],
            listing_url=args.get("listing_url", ""),
            date=args["date"],
            notes=args.get("notes", ""),
        ))
        return {"logged": True, "work_id": work_id, "call": args["call"]}

    if name == "record_response":
        work_id       = args["work_id"]
        call          = args["call"]
        response_type = args["response_type"]
        resp_date     = args.get("date", date.today().isoformat())
        notes         = args.get("notes", "")

        data  = load_works()
        works = data.get("works", [])
        idx   = next((i for i, w in enumerate(works) if w["id"] == work_id), None)
        if idx is None:
            raise ValueError(f"Work '{work_id}' not found")

        # Update existing submission history entry
        _result_map = {
            "rejection": "rejected", "acceptance": "accepted",
            "waitlist": "waitlisted", "withdrawn": "withdrawn",
        }
        for sub in works[idx].get("submission_history", []):
            if (sub.get("call") or "").lower() == call.lower():
                sub["result"] = _result_map.get(response_type, response_type)
                if notes:
                    sub["notes"] = notes
                break
        if response_type == "acceptance":
            works[idx]["status"] = "accepted"
        save_works({"works": works})

        if response_type == "withdrawn":
            event = event_schema.withdrawal_sent(
                work_id=work_id,
                work_title=works[idx].get("title", work_id),
                call=call,
                date=resp_date,
                notes=notes,
            )
        else:
            event = event_schema.response_received(
                work_id=work_id,
                work_title=works[idx].get("title", work_id),
                call=call,
                response_type=response_type,
                date=resp_date,
                notes=notes,
            )
        event_store.append_event(event)
        return {"recorded": True, "work_id": work_id, "call": call, "response_type": response_type}

    if name == "get_submission_context":
        try:
            from .rag.retriever import get_context
            work_id = args["work_id"]
            data    = load_works()
            work    = next((w for w in data.get("works", []) if w["id"] == work_id), None)
            if work is None:
                return {"error": f"Work '{work_id}' not found"}
            listing = {
                "listing_title":    args.get("listing_title", ""),
                "call_description": args.get("call_description", ""),
            }
            import asyncio
            context = asyncio.run(get_context(work, listing, k=args.get("k", 5)))
            return {"context": context or "No past submission history yet."}
        except ImportError:
            return {"error": "RAG not available — install chromadb and pull nomic-embed-text"}
        except RuntimeError as e:
            return {"context": f"RAG unavailable: {e}"}

    # --- Timelines ---

    if name == "create_timeline":
        timeline = create_timeline(
            work_id=args["work_id"],
            start_date=date.fromisoformat(args["start_date"]),
            pages_per_session=args.get("pages_per_session", 2),
            sessions_per_week=args.get("sessions_per_week", 6),
            write_to_tweek=args.get("write_to_tweek", False),
        )
        return timeline

    if name == "adjust_timeline":
        from_date = date.fromisoformat(args["from_date"]) if args.get("from_date") else None
        timeline  = adjust_timeline(
            timeline_id=args["timeline_id"],
            adjustment=args["adjustment"],
            from_date=from_date,
            new_sessions_per_week=args.get("new_sessions_per_week"),
        )
        return timeline

    # --- Instagram ---

    if name == "log_instagram_post":
        return log_post(
            post_date=args["date"],
            format=args["format"],
            caption=args["caption"],
            metrics=args.get("metrics"),
            notes=args.get("notes"),
        )

    if name == "suggest_instagram":
        return instagram_suggest(schedule_text=args.get("schedule_text"))

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run():
    asyncio.run(_main())


if __name__ == "__main__":
    run()
