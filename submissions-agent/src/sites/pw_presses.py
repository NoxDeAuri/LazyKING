"""
Poets & Writers Small Presses scraper.

URL: https://www.pw.org/small_presses
Filter: GET params — field_genres_value, subgenre, reading_period_status,
        items_per_page, unsolicited, fee

Fields available from the listing page (no login required):
  name, pw_url, description, reading_period, genres, subgenres,
  temporarily_closed

The detail page requires a P&W account, so submissions_url and other
contact details are not scraped here.

Cache: data/pw-cache.json  (7-day TTL)
"""

import asyncio
import json
import ssl
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx
import truststore
from bs4 import BeautifulSoup

DATA_DIR   = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "pw-cache.json"

_ssl_ctx  = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_BASE     = "https://www.pw.org"
_LIST_URL = f"{_BASE}/small_presses"
_HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CACHE_TTL_DAYS = 7

# Subgenre IDs on pw.org
SUBGENRE_IDS = {
    "Literary Fiction": "9677",
    "Experimental":     "9658",
    "Historical":       "9664",
    "BIPOC Voices":     "17330",
    "LGBTQ Voices":     "9661",
    "Cross-genre":      "9656",
}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class PressEntry:
    name: str
    slug: str               # e.g. "deep_vellum"
    pw_url: str             # full pw.org URL
    description: str
    reading_period: str     # e.g. "Jan 1 to Dec 31", or ""
    genres: list            # ["Fiction", "Poetry", ...]
    subgenres: list         # ["Literary Fiction", ...]
    temporarily_closed: bool
    fetched_at: str


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_cache() -> Optional[dict]:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.now() - fetched_at > timedelta(days=CACHE_TTL_DAYS):
            return None
        return data
    except Exception:
        return None


def save_cache(presses: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "fetched_at": datetime.now().isoformat(),
        "presses":    presses,
    }, indent=2))


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_presses(html: str) -> list[PressEntry]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[PressEntry] = []
    now = datetime.now().isoformat()

    for row in soup.select(".views-row"):
        title_a = row.select_one(".views-field-title a")
        if not title_a:
            continue

        name = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        slug = href.removeprefix("/small_presses/")

        desc_el = row.select_one(
            ".views-field-field-editorial-focus .field-content, "
            ".views-field-field-description .field-content"
        )
        description = desc_el.get_text(" ", strip=True) if desc_el else ""

        rp_el = row.select_one(".views-field-field-reading-period .field-content")
        reading_raw = rp_el.get_text(" ", strip=True) if rp_el else ""
        reading_period = reading_raw.removeprefix("Reading Period:").strip()

        genre_el = row.select_one(".views-field-field-genres .field-content")
        genres_raw = genre_el.get_text(" ", strip=True) if genre_el else ""
        genres = [g.strip() for g in genres_raw.removeprefix("Genre:").split(",") if g.strip()]

        sub_el = row.select_one(".views-field-taxonomy-vocabulary-20 .field-content")
        subs_raw = sub_el.get_text(" ", strip=True) if sub_el else ""
        subgenres = [s.strip() for s in subs_raw.removeprefix("Subgenres:").split(",") if s.strip()]

        closed_el = row.select_one(".views-field-field-temporarily-closed-1 .field-content")
        temporarily_closed = bool(closed_el and closed_el.get_text(strip=True))

        results.append(PressEntry(
            name=name,
            slug=slug,
            pw_url=f"{_BASE}/small_presses/{slug}",
            description=description,
            reading_period=reading_period,
            genres=genres,
            subgenres=subgenres,
            temporarily_closed=temporarily_closed,
            fetched_at=now,
        ))

    return results


# ---------------------------------------------------------------------------
# Async fetch
# ---------------------------------------------------------------------------

async def search(
    genre: str = "Fiction",
    subgenre_id: str = "",
    open_only: bool = False,
    on_progress: Optional[Callable] = None,
) -> list[dict]:
    """
    Fetch small presses from Poets & Writers.

    genre        : "Fiction", "Poetry", "Creative Nonfiction", "Translation", or "" for all
    subgenre_id  : one of the SUBGENRE_IDS values, e.g. "9677" for Literary Fiction
    open_only    : filter to presses with currently open reading periods

    Returns list of PressEntry dicts; caches to data/pw-cache.json.

    on_progress(event_dict) events:
        {"type": "fetching"}
        {"type": "fetched", "total": N, "open": M}
        {"type": "done", "presses": N}
        {"type": "error", "message": "..."}
    """
    params: dict = {"items_per_page": "All"}
    if genre:
        params["field_genres_value"] = genre
    if subgenre_id:
        params["subgenre"] = subgenre_id
    if open_only:
        params["reading_period_status"] = "1"

    if on_progress:
        await on_progress({"type": "fetching"})

    async with httpx.AsyncClient(verify=_ssl_ctx, timeout=30, headers=_HEADERS) as client:
        try:
            resp = await client.get(_LIST_URL, params=params, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            if on_progress:
                await on_progress({"type": "error", "message": f"Could not fetch P&W: {exc}"})
            return []

    presses = _parse_presses(resp.text)
    total = len(presses)
    open_count = sum(1 for p in presses if not p.temporarily_closed)

    if on_progress:
        await on_progress({"type": "fetched", "total": total, "open": open_count})

    serialized = [asdict(p) for p in presses]
    save_cache(serialized)

    if on_progress:
        await on_progress({"type": "done", "presses": total})

    return serialized
