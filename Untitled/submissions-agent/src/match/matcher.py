"""
Match works to listings and find boutique calls.

Complete works are always matched. Drafting works are included when
include_drafting=True — they appear with status='drafting' and a note
so you can evaluate whether you can finish them before the deadline.
"""

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import json

from ..sites.base import Listing

WORKS_FILE = Path(__file__).parent.parent / "profile" / "works.json"

_FEE_FREE_VALUES = {"", "none", "free", "$0", "no fee", "n/a"}


def _load_works() -> list[dict]:
    if not WORKS_FILE.exists():
        return []
    return json.loads(WORKS_FILE.read_text()).get("works", [])


def _has_fee(listing: Listing) -> bool:
    if not listing.fee:
        return False
    return listing.fee.strip().lower() not in _FEE_FREE_VALUES


# --- Individual match checks ---

def _words_fit(work: dict, listing: Listing) -> tuple[bool, str]:
    wc = work.get("word_count") or 0
    lo = listing.min_word_count
    hi = listing.max_word_count
    if hi and wc > hi:
        return False, f"word count {wc} exceeds limit {hi}"
    if lo and wc < lo:
        return False, f"word count {wc} below minimum {lo}"
    if hi:
        return True, f"word count {wc} fits within limit {hi}"
    return True, f"word count {wc} (no word limit stated)"


def _genre_matches(work: dict, listing: Listing) -> tuple[bool, str]:
    if not listing.genre:
        return True, "no genre restriction on listing"
    work_genre = (work.get("genre") or "").lower()
    listing_genre = listing.genre.lower()
    if work_genre in listing_genre or listing_genre in work_genre or "literary" in listing_genre:
        return True, f"genre '{work_genre}' matches '{listing.genre}'"
    for sg in listing.subgenres:
        if sg.lower() in work_genre or work_genre in sg.lower():
            return True, f"genre '{work_genre}' matches subgenre '{sg}'"
    return False, f"genre '{work_genre}' does not match '{listing.genre}'"


def _theme_alignment(work: dict, listing: Listing) -> tuple[bool, str]:
    """
    Soft check: compares work themes + synopsis keywords against the venue's
    subgenres, call description, and publication description.
    Always returns True (never blocks a match), but adds a human-readable
    explanation — either the overlap found, or a note that no themes were
    declared so alignment is unknown.
    """
    work_themes = [t.lower().strip() for t in (work.get("themes") or []) if t]
    synopsis = (work.get("synopsis_short") or "").lower()

    # Build a searchable corpus from the venue's own text
    venue_parts = [
        " ".join(listing.subgenres),
        listing.call_description or "",
        listing.pub_description or "",
        listing.pub_description_full or "",
    ]
    venue_text = " ".join(venue_parts).lower()

    if not venue_text.strip():
        return True, "no venue description to compare themes against"

    if not work_themes and not synopsis:
        return True, "no themes/synopsis declared — verify aesthetic fit manually"

    # Direct theme keyword hits
    matched = [t for t in work_themes if t in venue_text]
    if matched:
        return True, f"themes align: {', '.join(matched)}"

    # Fallback: content words from synopsis (nouns/adjectives, skip common words)
    _STOP = {"a","an","the","and","or","but","in","on","at","to","of","for","is","it",
              "its","with","this","that","are","was","has","had","be","as","from","by"}
    synopsis_words = [w for w in re.findall(r'\b[a-z]{4,}\b', synopsis) if w not in _STOP]
    synopsis_hits = [w for w in synopsis_words if w in venue_text]
    if synopsis_hits:
        return True, f"synopsis keywords found in venue description: {', '.join(synopsis_hits[:4])}"

    return True, "no theme overlap detected — review venue description manually"


def _eligibility_check(work: dict, listing: Listing) -> tuple[bool, str]:
    """
    Flag if eligibility text contains common hard exclusions.
    Returns False (blocks match) only for clear geography/nationality exclusions
    where the work doesn't fit. Otherwise returns True with a note.
    """
    text = (listing.eligibility_text or "").lower()
    if not text:
        return True, "no eligibility restrictions stated"
    # Surface the text for the human to review; don't auto-block
    return True, f"eligibility note: {listing.eligibility_text[:120]}"


def _content_flags_noted(work: dict, listing: Listing) -> tuple[bool, str]:
    flags = work.get("content_flags") or []
    if not flags:
        return True, "no content flags"
    return True, f"content flags present ({', '.join(flags)}) — verify against call requirements"


def _not_previously_submitted(work: dict, listing: Listing) -> tuple[bool, str]:
    for sub in work.get("submission_history") or []:
        call = (sub.get("call") or "").lower()
        if listing.title and listing.title.lower() in call:
            return False, f"previously submitted to this call (result: {sub.get('result', '?')})"
        if listing.url and listing.url.lower() in call:
            return False, "previously submitted (URL match)"
    return True, "no prior submission on record"


def _deadline_ok(listing: Listing, min_days: int = 0, max_days: Optional[int] = None) -> tuple[bool, str]:
    if listing.deadline is None:
        # When a max window is set (matches tab), exclude open-ended calls so they stay in boutique
        if max_days is not None:
            return False, "no deadline — goes to boutique"
        return True, "no deadline stated"
    days_left = (listing.deadline - date.today()).days
    if days_left < min_days:
        return False, f"deadline in {days_left} days (minimum required: {min_days})"
    if max_days is not None and days_left > max_days:
        return False, f"deadline in {days_left} days (beyond {max_days}-day window)"
    return True, f"deadline in {days_left} days ({listing.deadline})"


def _build_match(work: dict, listing: Listing, deadline_reason: str) -> Optional[dict]:
    """Run all checks and return a match dict if passing, else None."""
    # Hard checks: any failure blocks the match
    hard_checks = [
        _words_fit(work, listing),
        _genre_matches(work, listing),
        _not_previously_submitted(work, listing),
    ]
    if not all(ok for ok, _ in hard_checks):
        return None

    # Soft checks: always pass, add context to reasons
    soft_checks = [
        _content_flags_noted(work, listing),
        _theme_alignment(work, listing),
    ]
    if listing.eligibility_text:
        soft_checks.append(_eligibility_check(work, listing))

    all_reasons = [msg for _, msg in hard_checks + soft_checks] + [deadline_reason]
    m = {
        "work_id":    work["id"],
        "work_title": work["title"],
        "word_count": work.get("word_count"),
        "status":     work.get("status"),
        "reasons":    all_reasons,
    }
    if work.get("status") == "drafting":
        m["note"] = "work in progress — confirm it will be complete before the deadline"
    return m


# --- Public API ---

def match_works_to_listings(
    listings: list[Listing],
    work_ids: Optional[list[str]] = None,
    min_days_to_deadline: int = 0,
    max_days_to_deadline: Optional[int] = None,
    include_drafting: bool = False,
) -> list[dict]:
    """
    For each listing, return which works match and why.

    Complete works are always included. Set include_drafting=True to also
    surface works-in-progress that fit by word count and genre, tagged with
    status='drafting' so you can judge whether you have time to finish them.
    """
    works = _load_works()
    if work_ids:
        works = [w for w in works if w["id"] in work_ids]

    statuses = {"complete"}
    if include_drafting:
        statuses.add("drafting")
    eligible = [w for w in works if w.get("status") in statuses]

    results = []
    for listing in listings:
        deadline_ok, deadline_reason = _deadline_ok(listing, min_days_to_deadline, max_days_to_deadline)
        if not deadline_ok:
            continue

        complete_matches = []
        draft_matches = []
        for work in eligible:
            m = _build_match(work, listing, deadline_reason)
            if m is None:
                continue
            if work.get("status") == "drafting":
                draft_matches.append(m)
            else:
                complete_matches.append(m)

        if complete_matches or draft_matches:
            results.append({
                "listing_title":         listing.title,
                "listing_url":           listing.url,
                "submission_url":        listing.submission_url or listing.url,
                "magazine_name":         listing.magazine_name,
                "magazine_url":          listing.magazine_url,
                "pub_description":       listing.pub_description,
                "pub_description_full":  listing.pub_description_full,
                "call_description":      listing.call_description,
                "genre_style":           listing.genre_style,
                "deadline":              listing.deadline.isoformat() if listing.deadline else None,
                "fee":                   listing.fee,
                "fee_required":          _has_fee(listing),
                "max_word_count":        listing.max_word_count,
                "simultaneous_submissions": listing.simultaneous_submissions,
                "pay_rate":              listing.pay_rate,
                "complete_matches":      complete_matches,
                "draft_matches":         draft_matches,
            })

    # No-fee calls first, then by deadline (None deadlines last)
    results.sort(key=lambda x: (x["fee_required"], x["deadline"] or "9999-99-99"))
    return results


def find_boutique(listings: list[Listing]) -> list[dict]:
    """
    Return calls with at least 14 days to deadline, or open-ended (no deadline).

    This is the planning/discovery view — longer horizon than the urgent Matches tab.
    Each result includes complete_matches and draft_matches so you can see
    both ready-to-go works and pieces you could finish in time.
    No-fee calls sort first, then by deadline (open-ended last).
    """
    today = date.today()
    min_deadline = today + timedelta(days=14)

    boutique = [
        l for l in listings
        if l.deadline is None or l.deadline >= min_deadline
    ]

    # Reuse match logic with drafting included; no max_days so open-ended are kept
    matched = match_works_to_listings(
        boutique,
        min_days_to_deadline=14,
        include_drafting=True,
    )

    # Boutique is for in-progress/drafting works only; complete stories belong in Matches
    for r in matched:
        r["complete_matches"] = []

    # Add boutique-specific fields to each result
    for r in matched:
        if r["deadline"]:
            r["days_to_deadline"] = (date.fromisoformat(r["deadline"]) - today).days
        else:
            r["days_to_deadline"] = None  # open-ended

    # Also include boutique listings with no work matches yet (so nothing is hidden)
    matched_urls = {r["listing_url"] for r in matched}
    for listing in boutique:
        if listing.url in matched_urls:
            continue
        r = {
            "listing_title":            listing.title,
            "listing_url":              listing.url,
            "submission_url":           listing.submission_url or listing.url,
            "magazine_name":            listing.magazine_name,
            "magazine_url":             listing.magazine_url,
            "pub_description":          listing.pub_description,
            "pub_description_full":     listing.pub_description_full,
            "call_description":         listing.call_description,
            "genre_style":              listing.genre_style,
            "deadline":                 listing.deadline.isoformat() if listing.deadline else None,
            "days_to_deadline":         (listing.deadline - today).days if listing.deadline else None,
            "fee":                      listing.fee,
            "fee_required":             _has_fee(listing),
            "max_word_count":           listing.max_word_count,
            "simultaneous_submissions": listing.simultaneous_submissions,
            "pay_rate":                 listing.pay_rate,
            "genre":                    listing.genre,
            "complete_matches":         [],
            "draft_matches":            [],
        }
        matched.append(r)

    # No-fee first, then by deadline (open-ended / None sorts last)
    matched.sort(key=lambda x: (x["fee_required"], x["deadline"] or "9999-99-99"))
    return matched
