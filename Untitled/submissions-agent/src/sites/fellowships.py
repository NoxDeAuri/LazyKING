"""
Curated list of major annual screenplay fellowships and competitions.

These programs have predictable annual cycles but change their exact deadlines
each year. Deadlines are set to None; the notes field carries the typical window.
Run filmfreeway.search() alongside this for competitions with live deadline data.

No scraping — returns a static list immediately.
"""

from datetime import date
from .base import Listing

_FETCHED_AT = "2026-10-26T00:00:00"

# Each entry: (title, url, format, prize_or_fee, notes)
_FELLOWSHIPS: list[tuple] = [
    (
        "Sundance Screenwriters Lab",
        "https://labs.sundance.org/",
        "feature",
        "Prize: Residency + development support",
        "Fellowship, not competition. Annual. Typical window: April–May. "
        "Feature film scripts only; drama/independent focus. "
        "One of the most prestigious development programs in the industry.",
    ),
    (
        "Austin Film Festival Screenplay Competition",
        "https://austinfilmfestival.com/submit/screenwriters/",
        "any",
        "Prize: $5,000 + industry access",
        "Annual. Typical window: January–July. Accepts features and pilots. "
        "Notable for strong industry connections and conference access.",
    ),
    (
        "Academy Nicholl Fellowships in Screenwriting",
        "https://nicholl.oscars.org/",
        "feature",
        "Prize: Up to $35,000 fellowship",
        "Annual. Changed to recommendation-only model in 2026 — "
        "general public entries no longer accepted directly. "
        "Apply via official Nicholl partner programs.",
    ),
    (
        "BlueCat Screenplay Competition",
        "https://bluecatscreenplay.com/",
        "any",
        "Fee-based. Prize: $10,000 + development",
        "Year-round with multiple deadline tiers. Accepts features, shorts, and pilots. "
        "All submissions receive written coverage. Annual top prize $10,000.",
    ),
    (
        "Shore Scripts Feature Screenplay Contest",
        "https://www.shorescripts.com/",
        "feature",
        "Fee-based. Prize: $5,000 + industry reads",
        "Annual. Well-reviewed. Feature scripts only. "
        "Known for strong industry judge panel including producers and agents.",
    ),
    (
        "Page International Screenwriting Awards",
        "https://pageawards.com/",
        "any",
        "Fee-based. Prize: $25,000 grand prize",
        "Annual. Typical window: January–May. Accepts features, shorts, TV pilots, "
        "and theatre scripts. One of the largest cash prizes in screenwriting.",
    ),
    (
        "Finish Line Script Competition",
        "https://filmfreeway.com/FinishLineScriptCompetition",
        "feature",
        "Fee-based",
        "Annual. Strong reputation for fair coverage. Features. "
        "Current deadline visible on FilmFreeway.",
    ),
    (
        "ISA Screenwriting Competition",
        "https://www.networkisa.org/competitions",
        "any",
        "Fee-based",
        "Annual. International Screenwriters' Association. "
        "Multiple categories: feature, pilot, short, webseries.",
    ),
    (
        "NBC Writers on the Verge",
        "https://www.nbcuniversalworkshops.com/",
        "tv",
        "Prize: Paid NBC writing fellowship",
        "Annual. Pilot scripts only. TV drama and comedy. "
        "Paid 9-month fellowship for selected writers. Highly competitive.",
    ),
    (
        "Disney/ABC Writing Program",
        "https://studioentertainment.disney.com/writing-program",
        "tv",
        "Prize: Paid Disney TV writing fellowship",
        "Annual. TV pilot scripts. Both drama and comedy. "
        "One-year paid program placing writers on Disney/ABC productions.",
    ),
    (
        "Humanitas Prize",
        "https://www.humanitasprize.org/",
        "any",
        "No entry fee. Prize: $10,000–$25,000",
        "Annual. Recognizes scripts that affirm human dignity — "
        "features, TV drama, TV comedy, and children's content. "
        "No fee to enter. Deadline typically spring.",
    ),
]


def get_fellowships(format_filter: str = "any") -> list[Listing]:
    """
    Return the curated fellowship / major competition list.

    format_filter: "feature" | "short" | "pilot" | "tv" | "any" (default = all)
    """
    results: list[Listing] = []
    for title, url, fmt, prize_fee, notes in _FELLOWSHIPS:
        if format_filter != "any" and fmt != "any" and fmt != format_filter:
            continue
        results.append(Listing(
            site="fellowships",
            title=title,
            url=url,
            genre="screenplay",
            screenplay_format=fmt,
            genre_style=fmt,
            deadline=None,
            pay_rate=prize_fee,
            notes=notes,
            fetched_at=_FETCHED_AT,
            submission_url=url,
        ))
    return results
