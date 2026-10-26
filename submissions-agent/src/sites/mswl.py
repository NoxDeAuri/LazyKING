"""
Manuscript Wishlist (MSWL) scraper — literary agents' public wishlists.

API base: https://manuscriptwishlist.com/wp-json/wp/v2/mswl-item
  - Custom WordPress post type; 96 items total (1 page at per_page=100)
  - Fields: id, title.rendered, slug, link, date, modified, content.rendered

Agent names are sometimes in the item title, sometimes self-identified in
the content. Items without a recognisable name are stored with title as the
display name and the MSWL link for the full page.

Search: free-text search via ?search= parameter; use "literary" to narrow
to ~14 relevant posts out of 96.

Cache: data/mswl-cache.json  (30-day TTL — wishlist posts change infrequently)
"""

import asyncio
import json
import re
import ssl
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx
import truststore

DATA_DIR   = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "mswl-cache.json"

_ssl_ctx  = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_API_BASE = "https://manuscriptwishlist.com/wp-json/wp/v2/mswl-item"
_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

CACHE_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class WishlistEntry:
    id: int
    title: str          # from post title — may be agent name, agency, or generic
    slug: str
    link: str           # URL to the MSWL page
    content: str        # plain-text wishlist body (HTML stripped)
    date: str           # ISO date posted / last modified
    modified: str


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


def save_cache(entries: list[dict], genre_filter: str) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "fetched_at":   datetime.now().isoformat(),
        "genre_filter": genre_filter,
        "entries":      entries,
    }, indent=2))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s{2,}")


def _strip_html(html: str) -> str:
    text = _HTML_TAG.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&#8217;", "'").replace("&#8220;", "“")
    text = text.replace("&#8221;", "”").replace("&amp;", "&").replace("&#x2716;&#xfe0f;", "✖")
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def _has_literary(entry: WishlistEntry, target: str = "literary") -> bool:
    t = target.lower()
    return t in entry.title.lower() or t in entry.content.lower()


# ---------------------------------------------------------------------------
# Async fetch
# ---------------------------------------------------------------------------

async def search(
    genre: str = "literary",
    on_progress: Optional[Callable] = None,
) -> list[dict]:
    """
    Fetch MSWL wishlist posts mentioning `genre`.

    Fetches all items in one request (96 total) and filters client-side.
    Returns a list of WishlistEntry dicts; also caches to data/mswl-cache.json.

    on_progress(event_dict) events:
        {"type": "fetched", "total": N}
        {"type": "filtered", "matches": M}
        {"type": "done", "entries": M}
        {"type": "error", "message": "..."}
    """
    async with httpx.AsyncClient(verify=_ssl_ctx, timeout=30) as client:
        try:
            resp = await client.get(
                _API_BASE,
                headers=_HEADERS,
                params={
                    "per_page": 100,
                    "_fields": "id,title,slug,link,date,modified,content",
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
            raw: list[dict] = resp.json()
        except Exception as exc:
            if on_progress:
                await on_progress({"type": "error", "message": f"Could not fetch MSWL: {exc}"})
            return []

    if on_progress:
        await on_progress({"type": "fetched", "total": len(raw)})

    entries: list[WishlistEntry] = []
    for item in raw:
        title    = _strip_html(item.get("title", {}).get("rendered", ""))
        content  = _strip_html(item.get("content", {}).get("rendered", ""))
        entry    = WishlistEntry(
            id       = item["id"],
            title    = title,
            slug     = item.get("slug", ""),
            link     = item.get("link", ""),
            content  = content,
            date     = (item.get("date") or "")[:10],
            modified = (item.get("modified") or "")[:10],
        )
        if _has_literary(entry, genre):
            entries.append(entry)

    if on_progress:
        await on_progress({"type": "filtered", "matches": len(entries)})

    serialized = [asdict(e) for e in entries]
    save_cache(serialized, genre)

    if on_progress:
        await on_progress({"type": "done", "entries": len(entries)})

    return serialized
