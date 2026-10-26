"""
RAG retriever — fetches past submission context for the matching agent.

Given a work dict and a listing dict, builds a semantic query and returns
a formatted string of the most similar past submission outcomes.

The matching agent passes this string as additional context so it can
reason about track record, not just word-count/genre fit.
"""

from . import embedder, store
from .indexer import _build_text


_OUTCOME_EMOJI = {
    "accepted":   "✓",
    "rejected":   "✗",
    "waitlisted": "~",
    "withdrawn":  "↩",
    "pending":    "…",
}


async def get_context(work: dict, listing: dict, k: int = 5) -> str:
    """
    Return formatted context about past submissions similar to this work+listing pair.
    Returns an empty string if the store is empty or ollama is unavailable.
    """
    if store.count() == 0:
        return ""

    query_text = _build_text(
        work=work,
        call=listing.get("listing_title") or listing.get("call", ""),
        call_description=listing.get("call_description", ""),
    )

    try:
        query_vec = await embedder.embed(query_text)
    except RuntimeError:
        return ""

    results = store.search(query_vec, k=k)
    if not results:
        return ""

    lines = ["Past submission outcomes (most similar first):"]
    for r in results:
        m       = r["metadata"]
        icon    = _OUTCOME_EMOJI.get(m.get("outcome", ""), "?")
        title   = m.get("work_title", m.get("work_id", "?"))
        call    = m.get("call", "?")
        outcome = m.get("outcome", "?")
        date    = m.get("date", "")
        date_s  = f"  ({date})" if date else ""
        lines.append(f"  {icon} {title!r} → {call!r}: {outcome}{date_s}")

    return "\n".join(lines)


def get_context_sync(work: dict, listing: dict, k: int = 5) -> str:
    """Synchronous wrapper for use in non-async contexts."""
    import asyncio
    return asyncio.run(get_context(work, listing, k))
