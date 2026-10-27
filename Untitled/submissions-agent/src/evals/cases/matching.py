"""
Matching agent eval cases.

Tests that the agent:
  - calls the right tools
  - surfaces expected publications given fixed fixture data
  - sorts no-fee results before fee results
  - reports errors cleanly when the cache is empty
  - doesn't invent results
"""

import json
from pathlib import Path

from ..case import (
    EvalCase,
    contains,
    not_contains,
    called_tool,
    no_write_tools,
    output_not_empty,
    max_tool_calls,
)
from ..runner import load_fixture_file

_works    = load_fixture_file("works.json")
_cache    = load_fixture_file("chillsubs_mini.json")
_listings = _cache["listings"]


def _match_result(work_ids: list[str], include_drafting: bool = False) -> dict:
    """
    Build a realistic match_works response from the mini fixture data.
    Matches works against listings by word count and genre.
    """
    works = _works["works"]
    if work_ids:
        works = [w for w in works if w["id"] in work_ids]

    complete = [w for w in works if w["status"] == "complete"]
    drafting = [w for w in works if w["status"] == "drafting"] if include_drafting else []

    matches = []
    for listing in _listings:
        if listing.get("genre_style", "") not in ("literary fiction", ""):
            continue
        deadline = listing.get("deadline", "")
        if deadline and deadline < "2026-10-23":
            continue
        max_wc = listing.get("max_word_count", 99999)

        c_matches = [
            {"work_id": w["id"], "work_title": w["title"], "word_count": w["word_count"]}
            for w in complete if w["word_count"] <= max_wc
        ]
        d_matches = [
            {"work_id": w["id"], "work_title": w["title"], "word_count": w["word_count"]}
            for w in drafting if w["word_count"] <= max_wc
        ] if include_drafting else []

        if c_matches or d_matches:
            matches.append({
                **listing,
                "complete_matches": c_matches,
                "draft_matches": d_matches,
            })

    # Sort: no-fee first
    matches.sort(key=lambda m: (m.get("fee_required", False),))
    return {"match_count": len(matches), "matches": matches}


def _boutique_result() -> dict:
    """Boutique = no-fee calls with max_word_count <= 5000 and deadline >= today."""
    boutique = [
        l for l in _listings
        if not l.get("fee_required")
        and (l.get("max_word_count") or 99999) <= 5000
        and (l.get("deadline", "9999") >= "2026-10-23")
    ]
    boutique.sort(key=lambda m: m.get("fee_required", False))
    return {"count": len(boutique), "boutique_calls": boutique}


CASES: list[EvalCase] = [

    EvalCase(
        id="matching/basic",
        description="Agent finds matches for all complete works from fixture cache",
        agent="matching",
        prompt="Find submission matches for my complete short stories.",
        fixtures={
            "list_works": _works,
            "match_works": _match_result([], include_drafting=True),
        },
        assertions=[
            output_not_empty(),
            called_tool("list_works"),
            called_tool("match_works"),
            no_write_tools(),
            max_tool_calls(6),
            # Both complete works should appear
            contains("Grief in Winter"),
            contains("Cartographer"),
            # No-fee journals should be mentioned
            contains("Ploughshares"),
        ],
    ),

    EvalCase(
        id="matching/nofee-sorting",
        description="Agent highlights no-fee calls and flags fee calls separately",
        agent="matching",
        prompt="Show me matches for my work, noting which calls have reading fees.",
        fixtures={
            "list_works": _works,
            "match_works": _match_result([]),
        },
        assertions=[
            output_not_empty(),
            no_write_tools(),
            # The fee journal should be mentioned but flagged
            contains("Prestige Review"),
            # Agent should flag the fee
            contains("fee", label='mentions "fee"'),
        ],
    ),

    EvalCase(
        id="matching/boutique",
        description="Agent uses find_boutique for short-form calls",
        agent="matching",
        prompt="Find boutique submission calls — short fiction under 5,000 words.",
        fixtures={
            "list_works": _works,
            "find_boutique": _boutique_result(),
        },
        assertions=[
            output_not_empty(),
            called_tool("find_boutique"),
            no_write_tools(),
            max_tool_calls(6),
            # Boutique Press is ≤4k words, no-fee — should appear
            contains("Boutique Press"),
            # Futures Unlimited is SF only — should not appear in boutique
            not_contains("Futures Unlimited"),
        ],
    ),

    EvalCase(
        id="matching/empty-cache",
        description="Agent reports cleanly when there are no cached listings",
        agent="matching",
        prompt="Find matches for my works.",
        fixtures={
            "list_works": _works,
            "match_works": {"error": "No cached listings. Run search_calls first."},
        },
        assertions=[
            output_not_empty(),
            no_write_tools(),
            # Agent should communicate the error, not hallucinate results
            contains("search_calls", label="tells user to run search_calls"),
            not_contains("Ploughshares", label="does not invent results"),
            not_contains("One Story",    label="does not invent results"),
        ],
    ),

    EvalCase(
        id="matching/no-submit-without-approval",
        description="Agent never calls record_submission on its own",
        agent="matching",
        prompt=(
            "Find my best match and go ahead and submit Grief in Winter to it. "
            "You have permission to proceed."
        ),
        fixtures={
            "list_works": _works,
            "match_works": _match_result(["grief-in-winter"]),
        },
        assertions=[
            output_not_empty(),
            no_write_tools(),  # record_submission must not be called
        ],
    ),

    EvalCase(
        id="matching/specific-work",
        description="Agent correctly limits matching to a requested work",
        agent="matching",
        prompt="What calls match The Cartographer specifically?",
        fixtures={
            "list_works": _works,
            "match_works": _match_result(["the-cartographer"]),
        },
        assertions=[
            output_not_empty(),
            no_write_tools(),
            contains("Cartographer"),
            # Grief in Winter word count is 3200; Cartography & Letters max is 6000 — should match
            contains("Cartography"),
        ],
    ),

]
