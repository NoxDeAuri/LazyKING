"""
Chill Subs integration — uses the public tRPC API (no login required).

The /submission-calls page exposes all 4300+ calls without authentication.
API: GET https://www.chillsubs.com/api/trpc/opportunities.getFeed
     ?batch=1&input=<urlencoded-json>

Input:  {"0": {"json": {"limit": 50, "cursor": <object-or-null>}}}
Output: [{"result": {"data": {"json": {"items": [...], "nextCursor": {...}, "total": N}}}}]

Cursor: {"callId": str, "createdAtTimestamp": int, "deadlineSortKey": int, "feedSortKey": int}
"""

import json
import re
import ssl
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import httpx
import truststore

from .base import Listing, ListingCache


DATA_DIR = Path(__file__).parent.parent.parent / "data"
_WORKS_FILE = Path(__file__).parent.parent / "profile" / "works.json"
CACHE_FILE = DATA_DIR / "chillsubs-cache.json"

_FEED_URL = "https://www.chillsubs.com/api/trpc/opportunities.getFeed"
_CALL_URL = "https://www.chillsubs.com/submission-calls"
_LIMIT = 50  # max the API allows


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r'<[^>]+>')

def _strip_html(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    clean = _HTML_TAG_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', clean).strip() or None

def _parse_genre(genre: dict) -> Optional[str]:
    for key in ("fiction", "poetry", "nonfiction", "hybrid", "multimedia"):
        if genre.get(key):
            return key
    return None


def _parse_fee(fee: dict) -> str:
    if not fee.get("charges"):
        return "No fee"
    amount = fee.get("amount") or 0
    currency = fee.get("currency", "USD")
    if amount > 0:
        return f"${amount}" if currency == "USD" else f"{amount} {currency}"
    return "Fee"


def _parse_pay(payment: dict) -> Optional[str]:
    if not payment.get("pays"):
        return "No pay"
    prize = (payment.get("prize") or {}).get("totalCashPrize") or 0
    if prize:
        return f"Prize: ${prize}"
    amt = payment.get("amount") or {}
    upper = amt.get("upper") or 0
    lower = amt.get("lower") or 0
    currency = payment.get("currency", "USD")
    sym = "$" if currency == "USD" else f"{currency} "
    if upper and lower and upper != lower:
        return f"{sym}{lower}–{sym}{upper}"
    if upper:
        return f"{sym}{upper}"
    return "Pays"


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _parse_deadline(reading_period: dict) -> Optional[date]:
    if reading_period.get("callPeriod", {}).get("alwaysOpen"):
        return None
    windows = reading_period.get("subWindows") or []
    if not windows:
        return None
    return _parse_date(windows[0].get("closeDate"))


def _item_to_listing(item: dict, fetched_at: str) -> Listing:
    call = item["cardData"]["call"]
    listing_info = item["cardData"].get("listing") or {}
    call_id = item["callId"]

    listing_key = listing_info.get("key", "")
    url = (
        f"{_CALL_URL}/{listing_key}/{call_id}"
        if listing_key
        else f"{_CALL_URL}/{call_id}"
    )

    reading_period = call.get("readingPeriod") or {}
    windows = reading_period.get("subWindows") or []
    rp_open = _parse_date(windows[0].get("openDate")) if windows else None
    rp_close = _parse_deadline(reading_period)

    length = call.get("length") or {}
    subgenres = [sg for sg in (call.get("subGenre") or []) if sg]

    return Listing(
        site="chillsubs",
        title=call.get("title", ""),
        url=url,
        genre=_parse_genre(call.get("genre") or {}),
        subgenres=subgenres,
        min_word_count=length.get("minimumWordCount"),
        max_word_count=length.get("maximumWordCount"),
        deadline=rp_close,
        reading_period_open=rp_open,
        reading_period_close=rp_close,
        fee=_parse_fee(call.get("fee") or {}),
        simultaneous_submissions=None,
        pay_rate=_parse_pay(call.get("payment") or {}),
        notes=None,
        fetched_at=fetched_at,
        magazine_name=listing_info.get("name"),
        magazine_url=listing_info.get("website"),
        genre_style=call.get("genreStyle"),
        call_description=_strip_html(call.get("description")),
        pub_description=listing_info.get("description"),
        pub_description_full=listing_info.get("descriptionFull"),
        eligibility_text=_strip_html(call.get("eligibilityText")),
        submission_url=call.get("link") or None,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_all(
    genre_filter: Optional[str] = None,
    max_pages: Optional[int] = None,
    progress: bool = True,
    on_page: Optional[Callable] = None,
) -> list[Listing]:
    """
    Fetch all open calls from the Chill Subs public tRPC API.
    No login required. Paginates automatically (50 items per page).

    genre_filter: "fiction" | "poetry" | "nonfiction" | "hybrid" | "multimedia" | None
                  Client-side filter applied after fetching.
    max_pages: cap pages for testing (50 items each).
    """
    fetched_at = datetime.now().isoformat()
    listings: list[Listing] = []
    cursor = None
    page_num = 0

    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    async with httpx.AsyncClient(verify=ssl_ctx, timeout=30) as client:
        while True:
            if max_pages is not None and page_num >= max_pages:
                break

            payload: dict = {"0": {"json": {"limit": _LIMIT}}}
            if cursor:
                payload["0"]["json"]["cursor"] = cursor

            encoded = quote(json.dumps(payload))
            resp = await client.get(
                f"{_FEED_URL}?batch=1&input={encoded}",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            if data[0].get("error"):
                raise RuntimeError(data[0]["error"]["json"]["message"])

            page = data[0]["result"]["data"]["json"]
            items = page.get("items", [])

            for item in items:
                try:
                    call_status = item["cardData"]["call"].get("status", "open")
                    if call_status != "open":
                        continue
                    listing = _item_to_listing(item, fetched_at)
                    if genre_filter is None or listing.genre == genre_filter:
                        listings.append(listing)
                except Exception:
                    pass

            page_num += 1
            total = page.get("total", "?")
            if on_page:
                await on_page(page_num, len(listings), total)
            elif progress:
                print(f"  page {page_num}: {len(listings)} collected (API total: {total})")

            cursor = page.get("nextCursor")
            if not cursor:
                break

    return listings


def _works_word_count_range() -> tuple[int, int]:
    """
    Return (min_wc, max_wc) across all complete and drafting works.
    Used to pre-filter calls so the cache only holds listings that at least
    one of Trevor's pieces could potentially fit.
    Falls back to (0, 100_000) if the profile is missing or unreadable.
    """
    try:
        works = json.loads(_WORKS_FILE.read_text()).get("works", [])
        counts = [
            w["word_count"]
            for w in works
            if w.get("status") in ("complete", "drafting")
            and isinstance(w.get("word_count"), int)
            and w["word_count"] > 0
        ]
        if counts:
            return min(counts), max(counts)
    except Exception:
        pass
    return 0, 100_000


async def search(
    credentials: Optional[dict] = None,
    genre: str = "fiction",
    max_word_count: Optional[int] = None,
    min_days_to_deadline: int = 0,
    max_pages: Optional[int] = None,
    on_page: Optional[Callable] = None,
) -> list[Listing]:
    """
    Fetch, filter, and cache Chill Subs listings. No login required.
    Saves results to data/chillsubs-cache.json.

    Word-count bounds are auto-derived from your works profile:
    a call is kept only if at least one of your works could fit its range.
    Calls with no stated word limit are always kept.

    credentials: ignored (kept for API compatibility)
    genre: "fiction" | "poetry" | "nonfiction" | "hybrid" | "multimedia" | "all"
    max_word_count: explicit upper cap (overrides per-call limit check)
    min_days_to_deadline: exclude calls with fewer days left
    max_pages: limit pages fetched (50 items each) — for testing
    """
    genre_filter = None if genre == "all" else genre
    all_listings = await fetch_all(genre_filter=genre_filter, max_pages=max_pages, on_page=on_page)

    work_wc_min, work_wc_max = _works_word_count_range()

    today = date.today()
    results: list[Listing] = []
    for listing in all_listings:
        # Drop calls that have already closed
        if listing.deadline and listing.deadline < today:
            continue
        # Drop calls whose reading window hasn't opened yet
        if listing.reading_period_open and listing.reading_period_open > today:
            continue
        # Drop calls where every one of Trevor's works is too long for the limit
        if listing.max_word_count and listing.max_word_count < work_wc_min:
            continue
        # Drop calls that require more words than Trevor's longest eligible piece
        if listing.min_word_count and listing.min_word_count > work_wc_max:
            continue
        # Honour an explicit max_word_count from the caller
        if max_word_count and listing.max_word_count and listing.max_word_count > max_word_count:
            continue
        if listing.deadline and min_days_to_deadline > 0:
            if (listing.deadline - today).days < min_days_to_deadline:
                continue
        results.append(listing)

    # Deduplicate by URL (handles merged multi-genre fetches or API duplicates)
    seen_urls: set[str] = set()
    deduped: list[Listing] = []
    for listing in results:
        if listing.url not in seen_urls:
            seen_urls.add(listing.url)
            deduped.append(listing)
    results = deduped

    DATA_DIR.mkdir(exist_ok=True)
    cache = ListingCache(
        site="chillsubs",
        fetched_at=datetime.now().isoformat(),
        listings=results,
    )
    CACHE_FILE.write_text(cache.model_dump_json(indent=2))
    return results


def load_cache() -> Optional[ListingCache]:
    """Load the most recent cached results, if any."""
    if not CACHE_FILE.exists():
        return None
    return ListingCache.model_validate_json(CACHE_FILE.read_text())
