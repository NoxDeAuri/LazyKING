"""
Tool Argument Validator — validates tool call arguments before execution.

Each tool has a Pydantic schema. validate_args() runs before _exec() fires the
tool, so malformed or adversarial arguments are blocked at the gate rather than
passed to the underlying tool or data store.

Validation rules shared across all tools:
  - String fields: max 2000 chars (blocks oversized injection payloads)
  - work_id: slug pattern only [a-z0-9-] — no path traversal
  - date fields: strict YYYY-MM-DD ISO format
  - No manuscript path patterns in any string argument
  - Enum fields: only accepted values pass
"""

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, field_validator, model_validator, ValidationError

# ── Shared constraints ────────────────────────────────────────────────────────

_MAX_STRING = 2000
_SLUG_RE    = re.compile(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$")
_DATE_RE    = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MANUSCRIPT_RE = re.compile(
    r"(manuscripts?/|data/manuscripts|\.docx|\.rtf|\.pages|\.odt)\b", re.I
)


def _check_string(v: str, field: str = "field") -> str:
    if not isinstance(v, str):
        raise ValueError(f"{field} must be a string")
    if len(v) > _MAX_STRING:
        raise ValueError(f"{field} exceeds {_MAX_STRING} characters")
    if _MANUSCRIPT_RE.search(v):
        raise ValueError(f"{field} contains a disallowed manuscript path reference")
    return v


def _check_slug(v: str, field: str = "work_id") -> str:
    v = _check_string(v, field)
    if not _SLUG_RE.match(v):
        raise ValueError(
            f"{field} must be a slug (lowercase letters, digits, hyphens only)"
        )
    return v


def _check_date(v: str, field: str = "date") -> str:
    v = _check_string(v, field)
    if not _DATE_RE.match(v):
        raise ValueError(f"{field} must be YYYY-MM-DD, got: {v!r}")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field} is not a valid calendar date: {v!r}")
    return v


# ── Per-tool schemas ─────────────────────────────────────────────────────────

class ListWorksArgs(BaseModel):
    pass  # no arguments


_ALLOWED_WORK_FIELDS = {
    "id", "title", "type", "status", "word_count", "genre",
    "themes", "comps", "synopsis_short", "synopsis_long",
    "content_flags", "notes", "call_deadline",
}


class UpsertWorkArgs(BaseModel):
    work: dict[str, Any]

    @field_validator("work")
    @classmethod
    def validate_work(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("work must be a dict")
        if "id" not in v:
            raise ValueError("work must include 'id'")
        if "title" not in v:
            raise ValueError("work must include 'title'")
        _check_slug(str(v["id"]), "work.id")
        _check_string(str(v["title"]), "work.title")
        # Block agent from overwriting submission history directly
        if "submission_history" in v:
            raise ValueError("submission_history may not be modified via upsert_work")
        # Block unknown fields
        extra = set(v.keys()) - _ALLOWED_WORK_FIELDS
        if extra:
            raise ValueError(f"Unknown work fields not allowed: {sorted(extra)}")
        # Validate all string values
        for k, val in v.items():
            if isinstance(val, str):
                _check_string(val, f"work.{k}")
        return v


class SearchCallsArgs(BaseModel):
    genre: Optional[str] = "fiction"
    max_word_count: Optional[int] = None
    min_days_to_deadline: Optional[int] = 0
    use_cache: Optional[bool] = True

    @field_validator("genre")
    @classmethod
    def validate_genre(cls, v):
        if v is not None:
            _check_string(v, "genre")
        return v

    @field_validator("max_word_count")
    @classmethod
    def validate_max_wc(cls, v):
        if v is not None and (v < 0 or v > 1_000_000):
            raise ValueError("max_word_count must be between 0 and 1,000,000")
        return v

    @field_validator("min_days_to_deadline")
    @classmethod
    def validate_min_days(cls, v):
        if v is not None and v < 0:
            raise ValueError("min_days_to_deadline must be >= 0")
        return v


class SearchScreenplayCallsArgs(BaseModel):
    format: Optional[str] = "any"
    min_days_to_deadline: Optional[int] = 0
    use_cache: Optional[bool] = True
    include_fellowships: Optional[bool] = True

    @field_validator("format")
    @classmethod
    def validate_format(cls, v):
        allowed = {"feature", "short", "pilot", "tv", "any", None}
        if v not in allowed:
            raise ValueError(f"format must be one of {allowed - {None}}, got {v!r}")
        return v

    @field_validator("min_days_to_deadline")
    @classmethod
    def validate_min_days(cls, v):
        if v is not None and v < 0:
            raise ValueError("min_days_to_deadline must be >= 0")
        return v


class MatchWorksArgs(BaseModel):
    work_ids: Optional[list[str]] = None
    min_days_to_deadline: Optional[int] = 0
    include_drafting: Optional[bool] = False

    @field_validator("work_ids")
    @classmethod
    def validate_work_ids(cls, v):
        if v is not None:
            if not isinstance(v, list):
                raise ValueError("work_ids must be a list")
            if len(v) > 50:
                raise ValueError("work_ids may contain at most 50 entries")
            for wid in v:
                _check_slug(str(wid), "work_ids[]")
        return v


class FindBoutiqueArgs(BaseModel):
    pass  # no arguments


class RecordSubmissionArgs(BaseModel):
    work_id: str
    call: str
    date: str
    listing_url: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("work_id")
    @classmethod
    def validate_work_id(cls, v): return _check_slug(v, "work_id")

    @field_validator("call")
    @classmethod
    def validate_call(cls, v): return _check_string(v, "call")

    @field_validator("date")
    @classmethod
    def validate_date(cls, v): return _check_date(v, "date")

    @field_validator("listing_url")
    @classmethod
    def validate_url(cls, v):
        if v is not None:
            _check_string(v, "listing_url")
            if not v.startswith(("http://", "https://")):
                raise ValueError("listing_url must start with http:// or https://")
        return v

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v):
        if v is not None:
            _check_string(v, "notes")
        return v


class CreateTimelineArgs(BaseModel):
    work_id: str
    start_date: str
    pages_per_session: Optional[int] = 2
    sessions_per_week: Optional[int] = 6
    write_to_tweek: Optional[bool] = True

    @field_validator("work_id")
    @classmethod
    def validate_work_id(cls, v): return _check_slug(v, "work_id")

    @field_validator("start_date")
    @classmethod
    def validate_date(cls, v): return _check_date(v, "start_date")

    @field_validator("pages_per_session")
    @classmethod
    def validate_pps(cls, v):
        if v is not None and not (1 <= v <= 20):
            raise ValueError("pages_per_session must be 1–20")
        return v

    @field_validator("sessions_per_week")
    @classmethod
    def validate_spw(cls, v):
        if v is not None and not (1 <= v <= 7):
            raise ValueError("sessions_per_week must be 1–7")
        return v


class AdjustTimelineArgs(BaseModel):
    timeline_id: str
    adjustment: str
    from_date: Optional[str] = None
    new_sessions_per_week: Optional[int] = None

    @field_validator("timeline_id")
    @classmethod
    def validate_timeline_id(cls, v): return _check_slug(v, "timeline_id")

    @field_validator("adjustment")
    @classmethod
    def validate_adjustment(cls, v):
        allowed = {"faster", "slower", "custom"}
        if v not in allowed:
            raise ValueError(f"adjustment must be one of {allowed}, got {v!r}")
        return v

    @field_validator("from_date")
    @classmethod
    def validate_date(cls, v):
        if v is not None:
            return _check_date(v, "from_date")
        return v

    @model_validator(mode="after")
    def require_sessions_for_custom(self):
        if self.adjustment == "custom" and self.new_sessions_per_week is None:
            raise ValueError("new_sessions_per_week is required when adjustment='custom'")
        return self


class LogInstagramPostArgs(BaseModel):
    date: str
    format: str
    caption: str
    metrics: Optional[dict[str, Any]] = None
    notes: Optional[str] = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, v): return _check_date(v, "date")

    @field_validator("format")
    @classmethod
    def validate_format(cls, v): return _check_string(v, "format")

    @field_validator("caption")
    @classmethod
    def validate_caption(cls, v): return _check_string(v, "caption")

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v):
        if v is not None:
            _check_string(v, "notes")
        return v


class SuggestInstagramArgs(BaseModel):
    schedule_text: Optional[str] = None

    @field_validator("schedule_text")
    @classmethod
    def validate_schedule(cls, v):
        if v is not None:
            _check_string(v, "schedule_text")
        return v


class RecordResponseArgs(BaseModel):
    work_id: str
    call: str
    response_type: str
    date: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("work_id")
    @classmethod
    def validate_work_id(cls, v): return _check_slug(v, "work_id")

    @field_validator("call")
    @classmethod
    def validate_call(cls, v): return _check_string(v, "call")

    @field_validator("response_type")
    @classmethod
    def validate_response_type(cls, v):
        allowed = {"rejection", "acceptance", "waitlist", "withdrawn"}
        if v not in allowed:
            raise ValueError(f"response_type must be one of {allowed}, got {v!r}")
        return v

    @field_validator("date")
    @classmethod
    def validate_date(cls, v):
        if v is not None:
            return _check_date(v, "date")
        return v

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v):
        if v is not None:
            _check_string(v, "notes")
        return v


class GetSubmissionContextArgs(BaseModel):
    work_id: str
    listing_title: str
    call_description: Optional[str] = None
    k: Optional[int] = 5

    @field_validator("work_id")
    @classmethod
    def validate_work_id(cls, v): return _check_slug(v, "work_id")

    @field_validator("listing_title")
    @classmethod
    def validate_title(cls, v): return _check_string(v, "listing_title")

    @field_validator("call_description")
    @classmethod
    def validate_desc(cls, v):
        if v is not None:
            _check_string(v, "call_description")
        return v

    @field_validator("k")
    @classmethod
    def validate_k(cls, v):
        if v is not None and not (1 <= v <= 20):
            raise ValueError("k must be 1–20")
        return v


# ── Registry & public entry point ────────────────────────────────────────────

_SCHEMAS: dict[str, type[BaseModel]] = {
    "list_works":              ListWorksArgs,
    "upsert_work":             UpsertWorkArgs,
    "search_calls":            SearchCallsArgs,
    "search_screenplay_calls": SearchScreenplayCallsArgs,
    "match_works":             MatchWorksArgs,
    "find_boutique":           FindBoutiqueArgs,
    "record_submission":       RecordSubmissionArgs,
    "create_timeline":         CreateTimelineArgs,
    "adjust_timeline":         AdjustTimelineArgs,
    "log_instagram_post":      LogInstagramPostArgs,
    "suggest_instagram":       SuggestInstagramArgs,
    "record_response":         RecordResponseArgs,
    "get_submission_context":  GetSubmissionContextArgs,
}


def validate_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """
    Validate tool call arguments against the registered schema.

    Returns (ok, error_message).
    ok=True means args are safe to pass to the tool.
    ok=False means execution should be blocked; error_message explains why.

    Unknown tools return (True, "") — forward to executor to handle.
    """
    schema = _SCHEMAS.get(tool_name)
    if schema is None:
        return False, f"[GUARDRAIL] No validation schema registered for '{tool_name}' — blocked by default."

    try:
        schema.model_validate(args)
        return True, ""
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        )
        return False, f"[GUARDRAIL] Invalid args for {tool_name}: {errors}"
