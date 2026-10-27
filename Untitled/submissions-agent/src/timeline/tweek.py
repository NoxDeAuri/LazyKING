"""
Tweek REST API client.

Base URL : https://tweek.so/api/v1
Auth     : X-API-Key header (set TWEEK_API_KEY in .env)
Calendar : set TWEEK_CALENDAR_ID in .env — run list_calendars() once to find it.

Task fields that matter:
  text        (str, required)      — task title
  calendarId  (str, required)      — which calendar
  date        (str, YYYY-MM-DD)    — dated task; omit for someday list
  done        (bool, required)     — checked off?
  note        (str|null)           — free text
  checklist   (list|null)          — subtasks (paid feature)
"""

import os
import ssl
from typing import Optional

import httpx
import truststore

BASE_URL = "https://tweek.so/api/v1"

_ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _headers() -> dict:
    key = os.environ.get("TWEEK_API_KEY")
    if not key:
        raise EnvironmentError("TWEEK_API_KEY not set in .env")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _client() -> httpx.Client:
    return httpx.Client(verify=_ssl_ctx, timeout=15)


def _default_calendar_id() -> str:
    cid = os.environ.get("TWEEK_CALENDAR_ID")
    if not cid:
        raise EnvironmentError(
            "TWEEK_CALENDAR_ID not set in .env — run list_calendars() to find your calendar IDs"
        )
    return cid


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------

def list_calendars() -> list[dict]:
    """
    List all visible calendars.
    Each has: id, name, ownerId, role, isDefault, lists (someday lists).
    Run this once to find the ID of your 'Queries' calendar.
    """
    with _client() as client:
        resp = client.get(f"{BASE_URL}/calendars", headers=_headers())
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def list_tasks(day: str = None, date_to: str = None, calendar_id: str = None) -> list[dict]:
    """
    List tasks. day is YYYY-MM-DD; pass date_to for a range; omit both to get all.
    Use this first to verify auth and see the task shape.
    """
    params: dict = {"calendarId": calendar_id or _default_calendar_id()}
    if day:
        params["dateFrom"] = day
        params["dateTo"] = date_to or day
    with _client() as client:
        resp = client.get(f"{BASE_URL}/tasks", headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data


def create_task(
    title: str,
    day: str,
    calendar_id: str = None,
    checklist: Optional[list[str]] = None,
    notes: Optional[str] = None,
    done: bool = False,
) -> dict:
    """
    Create a task on the given date. Pass done=True to create it already checked off.
    Returns the created task dict (contains 'id').
    """
    payload: dict = {
        "text":       title,
        "calendarId": calendar_id or _default_calendar_id(),
        "date":       day,
        "done":       done,
    }
    if notes:
        payload["note"] = notes
    if checklist:
        payload["checklist"] = [
            {"id": str(i), "text": item, "done": done}
            for i, item in enumerate(checklist)
        ]
    with _client() as client:
        resp = client.post(f"{BASE_URL}/tasks", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


def update_task(task_id: str, **fields) -> dict:
    """Update specific fields on an existing task."""
    with _client() as client:
        resp = client.patch(
            f"{BASE_URL}/tasks/{task_id}", headers=_headers(), json=fields
        )
        resp.raise_for_status()
        return resp.json()


def complete_task(task_id: str) -> dict:
    return update_task(task_id, done=True)


def delete_task(task_id: str) -> None:
    with _client() as client:
        resp = client.delete(f"{BASE_URL}/tasks/{task_id}", headers=_headers())
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Submission event helpers
# ---------------------------------------------------------------------------

def log_submission_event(
    story_title: str,
    venue: str,
    event_type: str,
    date_str: str,
    calendar_id: str = None,
) -> Optional[dict]:
    """
    Create a completed task on the Queries calendar recording a submission event.

    event_type:
      "submitted"  → "Submitted: {story} → {venue}"
      "accepted"   → "Accepted: {story} ← {venue}"
      "rejected"   → "Rejected: {story} ← {venue}"
      "withdrawn"  → "Withdrawn: {story} from {venue}"

    Returns the created task dict, or None if TWEEK_API_KEY / TWEEK_CALENDAR_ID
    is not set (skips silently so it never breaks the submission flow).
    """
    try:
        arrows = {
            "submitted": f"Submitted: {story_title} → {venue}",
            "accepted":  f"Accepted: {story_title} ← {venue}",
            "rejected":  f"Rejected: {story_title} ← {venue}",
            "withdrawn": f"Withdrawn: {story_title} from {venue}",
        }
        title = arrows.get(event_type, f"{event_type.title()}: {story_title} — {venue}")
        return create_task(
            title=title,
            day=date_str,
            calendar_id=calendar_id,
            done=True,
        )
    except EnvironmentError:
        return None  # key / calendar not configured — skip silently
    except Exception as exc:
        # Log but never raise; Tweek failures must not break the submission flow
        print(f"[tweek] warning: could not log '{event_type}' event — {exc}")
        return None
