"""
LLM-based match scorer.

Uses Claude (ANTHROPIC_API_KEY) when available, falls back to Ollama otherwise.
Ollama is slower but free — same prompts, same JSON output shape.
"""

import json
import os
import re

from ..sites.base import Listing
from ..guardrails import input_fence

_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5")


def _clean_json(raw: str) -> str:
    """Strip markdown fences and whitespace from an LLM response."""
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip()).rstrip("`").strip()
    # Some models wrap in extra text before the JSON; find the first { or [
    match = re.search(r"[\[{]", raw)
    if match:
        raw = raw[match.start():]
    return raw


async def _call_ollama(prompt: str, max_tokens: int = 400) -> str:
    import httpx
    payload = {
        "model":   _OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream":  False,
        "options": {"num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{_OLLAMA_BASE}/api/chat", json=payload)
    r.raise_for_status()
    return r.json()["message"]["content"]


async def _call_claude(prompt: str, max_tokens: int, api_key: str) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


async def _llm(prompt: str, max_tokens: int) -> tuple[str, str]:
    """
    Call the best available LLM. Returns (raw_text, backend_name).
    Prefers Claude; falls back to Ollama.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return await _call_claude(prompt, max_tokens, api_key), "claude"
    return await _call_ollama(prompt, max_tokens), "ollama"


def _build_venue_text(listing: Listing) -> str:
    return "\n".join(filter(None, [
        f"Call: {listing.title}",
        f"Magazine: {listing.magazine_name}" if listing.magazine_name else None,
        f"Subgenres: {', '.join(listing.subgenres)}" if listing.subgenres else None,
        f"Style: {listing.genre_style}" if listing.genre_style else None,
        f"Word limit: {listing.max_word_count}" if listing.max_word_count else None,
        f"Call description: {listing.call_description}" if listing.call_description else None,
        f"Publication: {listing.pub_description_full or listing.pub_description}"
            if (listing.pub_description_full or listing.pub_description) else None,
        f"Eligibility: {listing.eligibility_text}" if listing.eligibility_text else None,
    ]))


async def score(work_id: str, listing: Listing) -> dict:
    """
    Score how well work_id fits listing (1–10).

    Returns:
        {"score": int, "summary": str, "cautions": list[str], "backend": str}
      or
        {"error": str}
    """
    from ..profile.builder import load_works
    works = load_works().get("works", [])
    work = next((w for w in works if w["id"] == work_id), None)
    if not work:
        return {"error": f"Work '{work_id}' not found"}

    work_text = "\n".join(filter(None, [
        f"Title: {work['title']}",
        f"Word count: {work.get('word_count', '?')}",
        f"Genre: {work.get('genre', '?')}",
        f"Themes: {', '.join(work.get('themes') or [])}" if work.get('themes') else None,
        f"Synopsis: {work.get('synopsis_short', '')}" if work.get('synopsis_short') else None,
        f"Comps: {', '.join(work.get('comps') or [])}" if work.get('comps') else None,
    ]))

    # Word count fit is already a hard check in the matcher — only matched works appear here.
    # Tell the LLM explicitly so it doesn't hallucinate a word-count caution.
    word_count = work.get("word_count")
    max_words = listing.max_word_count
    wc_note = ""
    if word_count and max_words:
        wc_note = f"\nNote: word count ({word_count:,} words) is within the venue limit ({max_words:,}). Do NOT flag word count in cautions."

    prompt = f"""You are a literary magazine submissions expert. Score how well this short story fits this call.

STORY:
{work_text}

VENUE:
{_build_venue_text(listing)}{wc_note}

Score 1–10:
  9–10  Exceptional fit — aesthetic, theme, and tone all align
  7–8   Strong fit — core elements match
  5–6   Moderate fit — could work with the right framing
  3–4   Weak fit — significant mismatches
  1–2   Poor fit — genre/aesthetic clash

Return ONLY valid JSON (no markdown, no explanation before or after):
{{"score": <int>, "summary": "<one sentence>", "cautions": ["<issue>"]}}

cautions: 0–2 items, empty array if none, each ≤15 words."""

    try:
        raw, backend = await _llm(prompt, max_tokens=200)
        result = json.loads(_clean_json(raw))
        return {
            "score":    int(result.get("score", 0)),
            "summary":  result.get("summary", ""),
            "cautions": result.get("cautions", []),
            "backend":  backend,
        }
    except Exception as exc:
        return {"error": str(exc)}


async def brief(listing: Listing, venue_pages: list[dict] | None = None) -> dict:
    """
    Generate a writer's brief for a boutique call.

    Returns:
        {"brief": str, "keywords": list[str], "backend": str}
      or
        {"error": str}
    """
    extra = ""
    if venue_pages:
        raw_snippets = "\n\n".join(
            f"[{p['url'].split('/')[2]}] {p['text'][:400]}" for p in venue_pages[:2]
        )
        fenced, _ = input_fence.wrap_external("venue_scrape", raw_snippets)
        extra = "\n\n" + fenced

    prompt = f"""You are helping a short story writer decide what to write for this submission call.

CALL INFORMATION:
{_build_venue_text(listing)}{extra}

Write a writer's brief in 3–4 sentences covering:
1. What kind of story they want (subject matter, approach)
2. Tone and aesthetic they favor (e.g. spare, lyrical, weird, accessible)
3. What to avoid (explicitly or implicitly)

Then list 5–8 keyword tags capturing the aesthetic (single words or short phrases).

Return ONLY valid JSON (no markdown, no explanation before or after):
{{"brief": "<3-4 sentences>", "keywords": ["tag1", "tag2"]}}"""

    try:
        raw, backend = await _llm(prompt, max_tokens=400)
        result = json.loads(_clean_json(raw))
        return {
            "brief":    result.get("brief", ""),
            "keywords": result.get("keywords", []),
            "backend":  backend,
        }
    except Exception as exc:
        return {"error": str(exc)}
