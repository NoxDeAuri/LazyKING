"""
Timeline builder.

Routine (from spec):
  - 5-7 days/week writing sessions
  - Draft 1: handwritten, 2 pages/session, ~500 words/page
  - Draft 2: typed, 1000+ words/session
  - Drafts 3 & 4: one night each (revision passes, each shorter than the last)
  - Ready to submit after draft 4

Output is saved to data/timelines.json and optionally pushed to Tweek.
The work record in works.json is updated with the timeline_id when created.
"""

import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from . import tweek

WORKS_FILE = Path(__file__).parent.parent / "profile" / "works.json"
DATA_DIR = Path(__file__).parent.parent.parent / "data"
TIMELINES_FILE = DATA_DIR / "timelines.json"

WORDS_PER_HANDWRITTEN_PAGE = 500   # Trevor's estimate: 500 words/page
WORDS_PER_TYPED_SESSION    = 1000  # draft 2+ minimum
PAGES_PER_SESSION_DEFAULT  = 2
SESSIONS_PER_WEEK_DEFAULT  = 6     # midpoint of 5-7


def _load_works() -> list[dict]:
    if not WORKS_FILE.exists():
        return []
    return json.loads(WORKS_FILE.read_text()).get("works", [])


def _save_works(works: list[dict]) -> None:
    WORKS_FILE.write_text(json.dumps({"works": works}, indent=2))


def _load_timelines() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if not TIMELINES_FILE.exists():
        return {}
    return json.loads(TIMELINES_FILE.read_text())


def _save_timelines(timelines: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    TIMELINES_FILE.write_text(json.dumps(timelines, indent=2))


def _session_dates(start: date, count: int, sessions_per_week: int) -> list[date]:
    """
    Generate `count` session dates starting from `start`.
    Schedules `sessions_per_week` sessions per week, one per day,
    then skips to the following Monday.
    """
    dates: list[date] = []
    current = start
    sessions_this_week = 0

    while len(dates) < count:
        dates.append(current)
        sessions_this_week += 1
        if sessions_this_week >= sessions_per_week:
            # Advance to next Monday
            days_to_monday = 7 - current.weekday()
            current = current + timedelta(days=days_to_monday)
            sessions_this_week = 0
        else:
            current = current + timedelta(days=1)

    return dates


def create_timeline(
    work_id: str,
    start_date: date,
    pages_per_session: int = PAGES_PER_SESSION_DEFAULT,
    sessions_per_week: int = SESSIONS_PER_WEEK_DEFAULT,
    word_count_override: Optional[int] = None,
    write_to_tweek: bool = True,
) -> dict:
    """
    Build a full draft timeline for a work and save it.

    Returns the timeline dict. Also updates works.json with the timeline_id.
    Tasks are written to Tweek by default (requires TWEEK_API_KEY).
    Pass write_to_tweek=False to skip Tweek and only save locally.
    """
    works = _load_works()
    work = next((w for w in works if w["id"] == work_id), None)
    if not work:
        raise ValueError(f"Work '{work_id}' not found in works.json")

    word_count = word_count_override or work.get("word_count")
    if not word_count:
        raise ValueError(f"Work '{work_id}' has no word count set")

    words_per_d1_session = pages_per_session * WORDS_PER_HANDWRITTEN_PAGE

    # Ceiling division: how many sessions to cover word_count at given rate
    d1_sessions = -(-word_count // words_per_d1_session)
    d2_sessions = -(-word_count // WORDS_PER_TYPED_SESSION)

    d1_dates = _session_dates(start_date, d1_sessions, sessions_per_week)
    d2_start  = d1_dates[-1] + timedelta(days=1)
    d2_dates  = _session_dates(d2_start, d2_sessions, sessions_per_week)
    d3_date   = d2_dates[-1] + timedelta(days=1)
    d4_date   = d3_date + timedelta(days=1)

    tasks: list[dict] = []

    for i, d in enumerate(d1_dates, 1):
        tasks.append({
            "draft": 1,
            "session": i,
            "date": d.isoformat(),
            "type": "handwritten",
            "target": f"{pages_per_session} pages (~{words_per_d1_session} words)",
            "tweek_task_id": None,
        })

    for i, d in enumerate(d2_dates, 1):
        tasks.append({
            "draft": 2,
            "session": i,
            "date": d.isoformat(),
            "type": "typed",
            "target": f"{WORDS_PER_TYPED_SESSION}+ words",
            "tweek_task_id": None,
        })

    tasks.append({
        "draft": 3,
        "session": 1,
        "date": d3_date.isoformat(),
        "type": "revision",
        "target": "Full revision pass (one night)",
        "tweek_task_id": None,
    })

    tasks.append({
        "draft": 4,
        "session": 1,
        "date": d4_date.isoformat(),
        "type": "revision",
        "target": "Final polish pass (one night)",
        "tweek_task_id": None,
    })

    timeline_id = str(uuid.uuid4())[:8]
    timeline = {
        "id": timeline_id,
        "work_id": work_id,
        "work_title": work["title"],
        "word_count": word_count,
        "pages_per_session": pages_per_session,
        "sessions_per_week": sessions_per_week,
        "start_date": start_date.isoformat(),
        "ready_to_submit": d4_date.isoformat(),
        "adjustment_log": [],
        "tasks": tasks,
    }

    if write_to_tweek:
        _push_to_tweek(timeline)

    timelines = _load_timelines()
    timelines[timeline_id] = timeline
    _save_timelines(timelines)

    work["timeline_id"] = timeline_id
    _save_works(works)

    return timeline


def adjust_timeline(
    timeline_id: str,
    adjustment: str,
    from_date: Optional[date] = None,
    new_sessions_per_week: Optional[int] = None,
    new_pages_per_session: Optional[int] = None,
) -> dict:
    """
    Recalculate remaining task dates after a pace change.

    adjustment: 'faster' (+1 session/week), 'slower' (-1), or 'custom'
    Completed tasks (dates before from_date/today) are kept as-is.
    Returns the updated timeline and shows the change in ready_to_submit.
    """
    timelines = _load_timelines()
    timeline = timelines.get(timeline_id)
    if not timeline:
        raise ValueError(f"Timeline '{timeline_id}' not found")

    today = from_date or date.today()
    spw   = new_sessions_per_week or timeline["sessions_per_week"]
    pps   = new_pages_per_session or timeline["pages_per_session"]

    if adjustment == "faster":
        spw = min(7, spw + 1)
    elif adjustment == "slower":
        spw = max(1, spw - 1)
    # 'custom': uses new_sessions_per_week directly

    completed = [t for t in timeline["tasks"] if date.fromisoformat(t["date"]) < today]
    pending   = [t for t in timeline["tasks"] if date.fromisoformat(t["date"]) >= today]

    if not pending:
        return timeline  # nothing left to adjust

    remaining = {1: [], 2: [], 3: [], 4: []}
    for t in pending:
        remaining[t["draft"]].append(t)

    new_tasks  = list(completed)
    cursor     = today

    for draft_num in (1, 2, 3, 4):
        bucket = remaining[draft_num]
        if not bucket:
            continue

        if draft_num in (3, 4):
            task = bucket[0].copy()
            task["date"] = cursor.isoformat()
            new_tasks.append(task)
            cursor = cursor + timedelta(days=1)
        else:
            new_dates = _session_dates(cursor, len(bucket), spw)
            for task, d in zip(bucket, new_dates):
                t = task.copy()
                t["date"] = d.isoformat()
                new_tasks.append(t)
            cursor = new_dates[-1] + timedelta(days=1)

    old_ready = timeline["ready_to_submit"]
    new_ready = new_tasks[-1]["date"]

    timeline["tasks"]            = new_tasks
    timeline["sessions_per_week"] = spw
    timeline["pages_per_session"] = pps
    timeline["ready_to_submit"]  = new_ready
    timeline["adjustment_log"].append({
        "adjustment": adjustment,
        "from_date": today.isoformat(),
        "old_ready": old_ready,
        "new_ready": new_ready,
        "new_sessions_per_week": spw,
    })

    timelines[timeline_id] = timeline
    _save_timelines(timelines)

    return timeline


def _push_to_tweek(timeline: dict) -> None:
    """Create Tweek tasks for all sessions that don't already have a tweek_task_id."""
    for task in timeline["tasks"]:
        if task.get("tweek_task_id"):
            continue
        title = (
            f"{timeline['work_title']} — "
            f"Draft {task['draft']}"
            + (f", Session {task['session']}" if task["draft"] < 3 else "")
        )
        result = tweek.create_task(
            title=title,
            day=task["date"],
            checklist=[task["target"]],
            done=False,
        )
        task["tweek_task_id"] = result.get("id")


def sync_tweek_on_adjust(timeline: dict) -> None:
    """
    After adjust_timeline(), move pending Tweek tasks to their new dates.

    Tasks before today are left alone (already done or past).
    Tasks with no tweek_task_id get a fresh Tweek task created.
    Modifies the timeline dict in place (tweek_task_id fields updated).
    """
    today = date.today().isoformat()
    for task in timeline["tasks"]:
        if task["date"] < today:
            continue
        tweek_id = task.get("tweek_task_id")
        if tweek_id:
            try:
                tweek.update_task(tweek_id, date=task["date"])
            except Exception as exc:
                print(f"[tweek] could not reschedule task {tweek_id}: {exc}")
        else:
            title = (
                f"{timeline['work_title']} — Draft {task['draft']}"
                + (f", Session {task['session']}" if task["draft"] < 3 else "")
            )
            try:
                result = tweek.create_task(
                    title=title, day=task["date"], checklist=[task["target"]], done=False
                )
                task["tweek_task_id"] = result.get("id")
            except Exception as exc:
                print(f"[tweek] could not create task for {task['date']}: {exc}")
