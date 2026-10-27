from datetime import date
from typing import Optional

from pydantic import BaseModel


class Listing(BaseModel):
    site: str
    title: str
    url: str
    genre: Optional[str] = None
    subgenres: list[str] = []
    min_word_count: Optional[int] = None
    max_word_count: Optional[int] = None
    deadline: Optional[date] = None
    reading_period_open: Optional[date] = None
    reading_period_close: Optional[date] = None
    fee: Optional[str] = None
    simultaneous_submissions: Optional[bool] = None
    pay_rate: Optional[str] = None
    notes: Optional[str] = None
    fetched_at: Optional[str] = None
    # Rich venue signals (populated from Chill Subs API)
    magazine_name: Optional[str] = None
    magazine_url: Optional[str] = None
    genre_style: Optional[str] = None          # shortStory, flash, novella, etc.
    call_description: Optional[str] = None     # the call's own description (HTML stripped)
    pub_description: Optional[str] = None      # short publication blurb
    pub_description_full: Optional[str] = None # full publication description / aesthetic statement
    eligibility_text: Optional[str] = None     # exclusion criteria (e.g. "US residents only")
    submission_url: Optional[str] = None       # direct submission portal link (call.link from API)
    # Screenplay-specific fields (genre="screenplay")
    max_page_count: Optional[int] = None       # max pages (replaces word count for scripts)
    screenplay_format: Optional[str] = None    # "feature" | "short" | "pilot" | "tv" | "any"


class ListingCache(BaseModel):
    site: str
    fetched_at: str
    listings: list[Listing]
