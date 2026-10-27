"""
FilmFreeway curated screenwriting competitions scraper.

URL: https://filmfreeway.com/festivals/curated/screenwriting
    — FilmFreeway's hand-curated list of open screenplay competitions (~20 listings).
    — Server-rendered HTML; parsed with BeautifulSoup.

Each article element carries:
  aria-label          → competition name
  h3 a                → title + slug URL
  p containing        → "Next Deadline:" or "Final Deadline:" date string
  "Submissions Open"  → present when submissions are currently open

Cache: data/filmfreeway-cache.json  (1-day TTL — deadlines update frequently)
"""

import asyncio
import json
import re
import ssl
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx
import truststore
from bs4 import BeautifulSoup

from .base import Listing, ListingCache

DATA_DIR   = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "filmfreeway-cache.json"
CACHE_TTL  = timedelta(days=1)

_BASE_URL  = "https://filmfreeway.com"
_LIST_URL  = f"{_BASE_URL}/festivals/curated/screenwriting"
_HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_DEADLINE_RE = re.compile(
    r'(?:Next|Final)\s+Deadline:\s*([A-Za-z]+ \d{1,2},\s*\d{4})', re.I
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_deadline(text: str) -> Optional[date]:
    m = _DEADLINE_RE.search(text)
    if not m:
        return None
    raw = re.sub(r'\s+', ' ', m.group(1)).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def _parse_listings(html: str, fetched_at: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[Listing] = []

    for article in soup.find_all("article", attrs={"aria-label": True}):
        all_text = article.get_text(" ", strip=True)

        # Skip closed competitions
        if "Submissions Open" not in all_text:
            continue

        # Title + URL
        h3 = article.find("h3")
        title_a = h3.find("a") if h3 else None
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        href  = title_a.get("href", "")
        url   = f"{_BASE_URL}{href}" if href.startswith("/") else href
        # Strip FF tracking params
        url = url.split("?")[0]

        # Deadline
        deadline = _parse_deadline(all_text)

        # Deadline label
        is_final = "Final Deadline" in all_text
        notes = "Final deadline" if is_final else "Next deadline"

        results.append(Listing(
            site="filmfreeway",
            title=title,
            url=url,
            genre="screenplay",
            genre_style="any",
            deadline=deadline,
            notes=notes,
            fetched_at=fetched_at,
            submission_url=url,
        ))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_all(
    on_progress: Optional[Callable] = None,
) -> list[Listing]:
    """
    Fetch the FilmFreeway curated screenwriting listings.
    Returns Listing objects with site="filmfreeway", genre="screenplay".
    """
    fetched_at = datetime.now().isoformat()
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    async with httpx.AsyncClient(
        verify=ssl_ctx, timeout=30, headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(_LIST_URL)
        resp.raise_for_status()

    listings = _parse_listings(resp.text, fetched_at)

    if on_progress:
        await on_progress({"type": "fetched", "count": len(listings)})

    return listings


async def search(
    min_days_to_deadline: int = 0,
    use_cache: bool = True,
    on_progress: Optional[Callable] = None,
) -> list[Listing]:
    """
    Return FilmFreeway curated screenplay competitions.

    use_cache=True (default) returns last cached results if < 1 day old.
    min_days_to_deadline: exclude competitions closing sooner than this.
    """
    if use_cache:
        cached = load_cache()
        if cached:
            return _filter(cached.listings, min_days_to_deadline)

    listings = await fetch_all(on_progress=on_progress)

    DATA_DIR.mkdir(exist_ok=True)
    cache = ListingCache(site="filmfreeway", fetched_at=datetime.now().isoformat(), listings=listings)
    CACHE_FILE.write_text(cache.model_dump_json(indent=2))

    return _filter(listings, min_days_to_deadline)


def _filter(listings: list[Listing], min_days: int) -> list[Listing]:
    today = date.today()
    if min_days <= 0:
        return [l for l in listings if not l.deadline or l.deadline >= today]
    cutoff = today + timedelta(days=min_days)
    return [l for l in listings if not l.deadline or l.deadline >= cutoff]


def load_cache() -> Optional[ListingCache]:
    """Load cached results if they exist and are within the TTL."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = CACHE_FILE.read_text()
        cache = ListingCache.model_validate_json(data)
        fetched = datetime.fromisoformat(cache.fetched_at)
        if datetime.now() - fetched > CACHE_TTL:
            return None
        return cache
    except Exception:
        return None
