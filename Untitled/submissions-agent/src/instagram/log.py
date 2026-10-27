"""
Instagram activity log and suggestions.

No automated reading — Trevor pastes post details or insight numbers.
Suggestions are structural only; captions are never rewritten without approval.
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

DATA_DIR   = Path(__file__).parent.parent.parent / "data"
LOG_FILE   = DATA_DIR / "instagram-log.json"
SCHEDULE_FILE = DATA_DIR / "schedule.md"


def _load_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    return json.loads(LOG_FILE.read_text())


def _save_log(log: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    LOG_FILE.write_text(json.dumps(log, indent=2))


def log_post(
    post_date: str,
    format: str,
    caption: str,
    metrics: Optional[dict] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Log one Instagram post.

    post_date: YYYY-MM-DD
    format: 'reel', 'carousel', 'single image', 'story', etc.
    caption: full caption text
    metrics: any numbers Trevor provides (likes, comments, reach, saves, etc.)
    """
    log = _load_log()
    entry = {
        "id": f"ig-{len(log) + 1:04d}",
        "date": post_date,
        "format": format,
        "caption": caption,
        "metrics": metrics or {},
        "notes": notes or "",
        "logged_at": datetime.now().isoformat(),
    }
    log.append(entry)
    _save_log(log)
    return entry


def suggest(schedule_text: Optional[str] = None) -> dict:
    """
    Compare the log against the schedule and return structural suggestions.

    Reads data/schedule.md unless schedule_text is passed directly.
    Never rewrites Trevor's caption voice without approval.
    Flags gaps but does not auto-correct them.
    """
    log = _load_log()

    if not schedule_text and SCHEDULE_FILE.exists():
        schedule_text = SCHEDULE_FILE.read_text()

    if not schedule_text:
        return {
            "error": (
                "No schedule found. "
                "Paste your Instagram schedule into data/schedule.md "
                "or pass it directly as schedule_text."
            )
        }

    if not log:
        return {
            "log_entries": 0,
            "suggestions": ["No posts logged yet. Use log_instagram_post to add posts."],
            "gaps": [],
        }

    post_dates = sorted(e["date"] for e in log)
    last_post  = post_dates[-1]
    days_since = (date.today() - date.fromisoformat(last_post)).days

    formats_used: dict[str, int] = {}
    for e in log:
        formats_used[e["format"]] = formats_used.get(e["format"], 0) + 1

    suggestions: list[str] = []
    gaps: list[str] = []

    # Recency
    if days_since > 7:
        gaps.append(f"No post in {days_since} days (last: {last_post})")
    elif days_since > 3:
        suggestions.append(f"Last post was {days_since} days ago — consider posting soon")

    # Format variety
    if formats_used:
        dominant = max(formats_used, key=lambda k: formats_used[k])
        if len(log) >= 3 and formats_used[dominant] / len(log) > 0.8:
            others = [f for f in ["reel", "carousel", "single image", "story"] if f != dominant]
            suggestions.append(
                f"Most posts ({formats_used[dominant]}/{len(log)}) are '{dominant}'. "
                f"Consider mixing in: {', '.join(others[:2])}."
            )

    # Short captions on recent posts (flag, don't rewrite)
    for entry in log[-3:]:
        if len(entry["caption"]) < 60:
            suggestions.append(
                f"Post on {entry['date']} has a short caption ({len(entry['caption'])} chars). "
                "Flag for review — expand if you want more reach, but only in your voice."
            )

    suggestions.append(
        "Caption voice: all suggestions above are structural. "
        "Approve any rewrites before publishing."
    )

    return {
        "log_entries": len(log),
        "last_post": last_post,
        "days_since_last_post": days_since,
        "formats_used": formats_used,
        "schedule_loaded": bool(schedule_text),
        "suggestions": suggestions,
        "gaps": gaps,
    }
