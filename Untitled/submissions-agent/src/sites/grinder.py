"""
The Submission Grinder scraper — short story and fiction markets.

Base URL  : https://thegrinder.diabolicalplots.com
Session   : GET /Search/ByFilter?marketType=Fiction  → ARRAffinity cookie
Search    : POST /Search/Byfilter/Search  (JSON body, returns JSON array)
Market URL: /Pub/{MarketID}

Genre IDs (from the filter page):
  1=General  4=Fantasy  5=Horror  8=Science Fiction
  24=Literary  15=Historical  6=Mystery/Crime  9=Suspense/Thriller

LengthType values: "Flash", "Short Story", "Novelette", "Novella"
PaymentUnit: 0=per-word, 1=flat, 2=per-poem

Cache: data/grinder-cache.json  (7-day TTL)
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .base import Listing

DATA_DIR   = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "grinder-cache.json"
_PKG_ROOT  = Path(__file__).parent.parent.parent

_BASE = "https://thegrinder.diabolicalplots.com"

CACHE_TTL_DAYS = 7

# Genre IDs from the Grinder filter page
GENRE_LITERARY  = 24
GENRE_GENERAL   = 1
GENRE_FANTASY   = 4
GENRE_HORROR    = 5
GENRE_SCIFI     = 8
GENRE_MYSTERY   = 6

DEFAULT_GENRES  = [GENRE_LITERARY, GENRE_GENERAL]


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


def save_cache(markets: list[dict], genre_ids: list[int]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps({
        "fetched_at": datetime.now().isoformat(),
        "genre_ids":  genre_ids,
        "markets":    markets,
    }, indent=2))


# ---------------------------------------------------------------------------
# Market → Listing conversion
# ---------------------------------------------------------------------------

_PAYMENT_UNIT = {0: "per word", 1: "flat", 2: "per poem"}


def _market_to_listing(m: dict) -> Listing:
    mid  = m["MarketID"]
    url  = f"{_BASE}/Pub/{mid}"

    # Pay rate
    pay_rate = None
    pn = m.get("PayScaleNumeric") or 0
    if pn and pn > 0:
        unit = _PAYMENT_UNIT.get(m.get("PaymentUnit", 0), "per word")
        pay_rate = f"${pn:.3g} {unit}"

    # Word count from LengthDescription or LengthType
    length_desc = m.get("LengthDescription") or m.get("LengthType") or ""

    # Subgenres from comma-separated Genre field
    raw_genres = m.get("Genre") or ""
    subgenres  = [g.strip() for g in raw_genres.split(",") if g.strip()]

    return Listing(
        site="grinder",
        title=m.get("Name") or m.get("MarketNameMaybeWithType", ""),
        url=url,
        genre="Fiction",
        subgenres=subgenres,
        pay_rate=pay_rate,
        notes=length_desc or None,
        fetched_at=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# Subprocess fetch
# Grinder's ~650KB response takes ~32s. truststore uses macOS Security
# framework APIs that are main-thread-only, making it significantly slower
# (or broken) when called from asyncio's thread pool. Running in a subprocess
# gives truststore a clean main thread of its own.
# ---------------------------------------------------------------------------

async def _fetch_via_subprocess(genre_ids: list[int]) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "src.sites._grinder_fetch",
        *[str(g) for g in genre_ids],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_PKG_ROOT),
    )
    # asyncio.wait is safer than wait_for here: Python 3.12+ changed wait_for's
    # cancellation to wait for the coroutine to finish handling CancelledError,
    # which can block indefinitely when proc.communicate() is stuck on pipe I/O.
    # asyncio.wait simply returns done/pending without cancelling anything.
    comm_task = asyncio.create_task(proc.communicate())
    done, pending = await asyncio.wait({comm_task}, timeout=240)
    if pending:
        proc.kill()
        await proc.wait()
        for t in pending:
            t.cancel()
        raise RuntimeError("Fetch subprocess timed out after 240s")
    stdout, stderr = comm_task.result()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Fetch subprocess exited {proc.returncode}: {stderr.decode().strip()}"
        )
    return json.loads(stdout)


async def search(
    genre_ids: list[int] = None,
    open_only: bool = True,
    on_progress: Optional[Callable] = None,
) -> list[dict]:
    """
    Fetch fiction markets from The Submission Grinder.

    genre_ids defaults to [GENRE_LITERARY, GENRE_GENERAL].
    Returns list of raw market dicts; also caches to data/grinder-cache.json.

    on_progress(event_dict) events:
        {"type": "session"}
        {"type": "fetched", "total": N, "open": M}
        {"type": "done", "markets": N}
        {"type": "error", "message": "..."}
    """
    if genre_ids is None:
        genre_ids = list(DEFAULT_GENRES)

    if on_progress:
        await on_progress({"type": "session"})

    try:
        raw: list[dict] = await _fetch_via_subprocess(genre_ids)
    except Exception as exc:
        if on_progress:
            await on_progress({"type": "error", "message": f"Search request failed: {exc}"})
        return []

    if open_only:
        markets = [
            m for m in raw
            if not m.get("TempClosed") and not m.get("PermClosed") and not m.get("DNQ")
        ]
    else:
        markets = raw

    if on_progress:
        await on_progress({"type": "fetched", "total": len(raw), "open": len(markets)})

    save_cache(markets, genre_ids)

    if on_progress:
        await on_progress({"type": "done", "markets": len(markets)})

    return markets


def markets_to_listings(markets: list[dict]) -> list[Listing]:
    return [_market_to_listing(m) for m in markets]
