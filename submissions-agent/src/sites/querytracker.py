"""
QueryTracker scraper — literary agents and small-press publishers.

URL pattern  : https://querytracker.net/agent/{id}  (profile pages, public)
List pages   : https://querytracker.net/agents/?page=N  (name + ID, public)
Publishers   : https://querytracker.net/publishers/?page=N  (same structure)

Genre filtering requires a free login, so we:
  1. Collect all IDs + names from the paginated list (fast — 24 pages for agents, 3 for publishers)
  2. Fetch each profile page for genre + status data (slower, 0.3 s/request)
  3. Keep only records whose genres match the target (default "Literary Fiction")
  4. Cache matching results in data/qt-cache.json

Subsequent calls use the cache (14-day TTL by default).
"""

import asyncio
import json
import re
import ssl
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx
import truststore

DATA_DIR   = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "qt-cache.json"

_ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_AGENT_LIST_URL = "https://querytracker.net/agents/"
_PUB_LIST_URL   = "https://querytracker.net/publishers/"

CACHE_TTL_DAYS  = 14
LIST_DELAY      = 0.15   # seconds between list-page requests
PROFILE_DELAY   = 0.35   # seconds between profile requests


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentEntry:
    id: int
    name: str
    agency: str
    status: str                       # "open" | "closed"
    genres: list[str] = field(default_factory=list)
    website: str = ""
    query_url: str = ""               # QueryTracker /query/{id} page

@dataclass
class PublisherEntry:
    id: int
    name: str
    status: str                       # "open" | "closed"
    genres: list[str] = field(default_factory=list)
    website: str = ""
    submission_url: str = ""


@dataclass
class QTCache:
    fetched_at: str
    agents: list[dict]
    publishers: list[dict]
    genre_filter: str


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_cache() -> Optional[QTCache]:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now() - fetched_at > timedelta(days=CACHE_TTL_DAYS):
            return None  # stale
        return QTCache(
            fetched_at=data["fetched_at"],
            agents=data.get("agents", []),
            publishers=data.get("publishers", []),
            genre_filter=data.get("genre_filter", ""),
        )
    except Exception:
        return None


def save_cache(cache: QTCache) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "fetched_at":   cache.fetched_at,
        "genre_filter": cache.genre_filter,
        "agents":       cache.agents,
        "publishers":   cache.publishers,
    }, indent=2))


# ---------------------------------------------------------------------------
# Parsing helpers (pure regex — no extra deps)
# ---------------------------------------------------------------------------

def _parse_ids(html: str, entity: str) -> list[dict]:
    """Extract id, name, agency/press from a list page."""
    pattern = rf"href=['\"]/{entity}/(\d+)['\"][^>]*>([^<]+)</a>"
    results = []
    for id_str, label in re.findall(pattern, html):
        parts = label.split(" - ", 1)
        results.append({
            "id":     int(id_str),
            "name":   parts[0].strip(),
            "agency": parts[1].strip() if len(parts) > 1 else "",
        })
    return results


def _total_pages(html: str) -> int:
    m = re.search(r"Page \d+ of (\d+)", html)
    return int(m.group(1)) if m else 1


def _parse_agent_profile(html: str, agent_id: int) -> Optional[AgentEntry]:
    # Name + agency from <title>Name - Agency | QueryTracker</title>
    title_m = re.search(r"<title>(.*?) \| QueryTracker</title>", html)
    if not title_m:
        return None
    raw_title = title_m.group(1)
    parts = raw_title.split(" - ", 1)
    name   = parts[0].strip()
    agency = parts[1].strip() if len(parts) > 1 else ""

    status = "closed" if "Closed to Unsolicited Queries" in html else "open"

    # External website (first non-QT, non-asset link in the page)
    _skip = {"querytracker.net", "fonts.googleapis.com", "fonts.gstatic.com",
             "twitter.com", "facebook.com", "blogspot.com", "instagram.com",
             "linkedin.com", "x.com"}
    website = ""
    for m in re.finditer(r'href=["\']?(https?://[^"\'>\s]+)["\']?', html):
        url = m.group(1)
        if not any(s in url for s in _skip):
            website = url
            break

    # QueryTracker query-submission link
    qm = re.search(r"href=['\"]?(https://querytracker\.net/query/\d+)['\"]?", html)
    query_url = qm.group(1) if qm else ""

    # Genres: text inside <div class="mb-md">…</div> in the genres section
    genres_m = re.search(
        r"seeking the following genres:(.*?)(?:Join Now|Known Clients|Author Comments)",
        html, re.DOTALL
    )
    genres = []
    if genres_m:
        genre_chunk = genres_m.group(1)
        raw = re.findall(r'<div class="mb-md">\s*(.*?)\s*</div>', genre_chunk, re.DOTALL)
        genres = [g.strip() for g in raw if g.strip() and g.strip() not in ("Fiction", "Non-Fiction")]

    return AgentEntry(
        id=agent_id, name=name, agency=agency,
        status=status, genres=genres, website=website, query_url=query_url,
    )


def _parse_publisher_profile(html: str, pub_id: int) -> Optional[PublisherEntry]:
    title_m = re.search(r"<title>(.*?) \| QueryTracker</title>", html)
    if not title_m:
        return None
    name = title_m.group(1).strip()

    status = "closed" if "Closed to Unsolicited Queries" in html else "open"

    _skip = {"querytracker.net", "fonts.googleapis.com", "fonts.gstatic.com",
             "twitter.com", "facebook.com", "blogspot.com", "instagram.com",
             "linkedin.com", "x.com"}
    website = ""
    for m in re.finditer(r'href=["\']?(https?://[^"\'>\s]+)["\']?', html):
        url = m.group(1)
        if not any(s in url for s in _skip):
            website = url
            break

    sub_m = re.search(r"href=['\"]?(https://querytracker\.net/query/\d+)['\"]?", html)
    submission_url = sub_m.group(1) if sub_m else ""

    genres_m = re.search(
        r"seeking the following genres:(.*?)(?:Join Now|Author Comments|Profile History)",
        html, re.DOTALL
    )
    genres = []
    if genres_m:
        raw = re.findall(r'<div class="mb-md">\s*(.*?)\s*</div>', genres_m.group(1), re.DOTALL)
        genres = [g.strip() for g in raw if g.strip() not in ("Fiction", "Non-Fiction", "Non-fiction")]
        # Fallback: broad category headings
        if not genres:
            cats = re.findall(r'<h3[^>]*>\s*(Fiction|Non-Fiction|Poetry)\s*</h3>', genres_m.group(1))
            genres = cats

    return PublisherEntry(
        id=pub_id, name=name,
        status=status, genres=genres, website=website, submission_url=submission_url,
    )


def _genre_match(genres: list[str], target: str) -> bool:
    t = target.lower()
    return any(t in g.lower() or g.lower() in t for g in genres)


# ---------------------------------------------------------------------------
# Async fetch pipeline
# ---------------------------------------------------------------------------

async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


async def search(
    genre: str = "Literary Fiction",
    on_progress: Optional[Callable] = None,
) -> dict:
    """
    Full fetch: collect all agent + publisher IDs, then fetch profiles and
    filter by genre.  on_progress(event_dict) is called with each event:

        {"type": "list_page",   "entity": "agent"|"publisher", "page": N, "total": T, "ids_found": K}
        {"type": "profile",     "entity": "agent"|"publisher", "entry": {...}}
        {"type": "done",        "agents": N, "publishers": N}
        {"type": "error",       "message": "..."}
    """
    agents:     list[dict] = []
    publishers: list[dict] = []

    async with httpx.AsyncClient(verify=_ssl_ctx, timeout=20) as client:

        # --- Phase 1: collect all IDs ---
        for entity, list_url in (("agent", _AGENT_LIST_URL), ("publisher", _PUB_LIST_URL)):
            try:
                first_html = await _fetch(client, list_url)
            except Exception as exc:
                if on_progress:
                    await on_progress({"type": "error", "message": f"Could not load {entity} list: {exc}"})
                continue

            total_pages = _total_pages(first_html)
            id_list: list[dict] = _parse_ids(first_html, entity)

            if on_progress:
                await on_progress({
                    "type": "list_page", "entity": entity,
                    "page": 1, "total": total_pages, "ids_found": len(id_list),
                })
            await asyncio.sleep(LIST_DELAY)

            for page in range(2, total_pages + 1):
                try:
                    html = await _fetch(client, f"{list_url}?page={page}")
                    new_ids = _parse_ids(html, entity)
                    id_list.extend(new_ids)
                    if on_progress:
                        await on_progress({
                            "type": "list_page", "entity": entity,
                            "page": page, "total": total_pages, "ids_found": len(id_list),
                        })
                except Exception as exc:
                    if on_progress:
                        await on_progress({"type": "error", "message": f"{entity} list page {page}: {exc}"})
                await asyncio.sleep(LIST_DELAY)

            # --- Phase 2: fetch profiles and filter ---
            for item in id_list:
                eid = item["id"]
                profile_url = f"https://querytracker.net/{entity}/{eid}"
                try:
                    html = await _fetch(client, profile_url)
                except Exception as exc:
                    await asyncio.sleep(PROFILE_DELAY)
                    continue

                if entity == "agent":
                    entry = _parse_agent_profile(html, eid)
                    if entry and _genre_match(entry.genres, genre):
                        d = asdict(entry)
                        agents.append(d)
                        if on_progress:
                            await on_progress({"type": "profile", "entity": "agent", "entry": d})
                else:
                    entry = _parse_publisher_profile(html, eid)
                    if entry and (
                        _genre_match(entry.genres, genre) or
                        _genre_match(entry.genres, "Fiction")
                    ):
                        d = asdict(entry)
                        publishers.append(d)
                        if on_progress:
                            await on_progress({"type": "profile", "entity": "publisher", "entry": d})

                await asyncio.sleep(PROFILE_DELAY)

    cache = QTCache(
        fetched_at=datetime.now().isoformat(),
        agents=agents,
        publishers=publishers,
        genre_filter=genre,
    )
    save_cache(cache)

    if on_progress:
        await on_progress({"type": "done", "agents": len(agents), "publishers": len(publishers)})

    return {"agents": agents, "publishers": publishers}
