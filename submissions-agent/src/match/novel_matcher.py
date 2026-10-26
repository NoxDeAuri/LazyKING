"""
Novel manuscript matcher.

Scores literary agents (QueryTracker), MSWL wishlists, and small presses
(Poets & Writers) against a novel's profile — themes, genre, synopsis,
and comps.

Scoring (additive, no hard exclusions — everything is surfaced for review):
  +4  Genre keyword match in wishlist/description
  +3  Comp title/author found in wishlist/description
  +2  Each theme keyword found
  +1  Each synopsis keyword found
  -1  "closed", "not accepting", "does not represent" signals
"""

import re
from pathlib import Path
import json

_STOP = {
    "a","an","the","and","or","but","in","on","at","to","of","for","is","it",
    "its","with","this","that","are","was","has","had","be","as","from","by",
    "not","no","i","we","they","he","she","you","my","your","their","our",
    "will","can","also","more","all","about","than","when","who","which",
}

_CLOSED_SIGNALS = [
    "not accepting", "closed to submissions", "does not represent",
    "not currently", "no longer", "temporarily closed",
]


def _keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"\b[a-z]{4,}\b", text.lower()) if w not in _STOP]


def _score_text(corpus: str, novel: dict) -> tuple[int, list[str]]:
    """Score how well a free-text corpus matches the novel profile."""
    corpus_l = corpus.lower()
    reasons: list[str] = []
    score = 0

    # Closed signals
    for sig in _CLOSED_SIGNALS:
        if sig in corpus_l:
            score -= 1
            reasons.append(f"⚠ '{sig}' detected — verify status")
            break

    # Genre match
    for keyword in ["literary fiction", "literary", "fiction", "novel"]:
        if keyword in corpus_l:
            score += 4
            reasons.append(f"genre match: '{keyword}'")
            break

    # Comp matches
    for comp in novel.get("comps") or []:
        comp_words = comp.lower().split()
        # Match any significant word from the comp title/author
        hits = [w for w in comp_words if len(w) > 4 and w not in _STOP and w in corpus_l]
        if hits:
            score += 3
            reasons.append(f"comp match: {comp} (via {', '.join(hits)})")

    # Theme matches
    for theme in novel.get("themes") or []:
        theme_words = [w for w in theme.lower().split() if w not in _STOP and len(w) > 3]
        hits = [w for w in theme_words if w in corpus_l]
        if hits:
            score += 2
            reasons.append(f"theme: {theme}")

    # Synopsis keyword matches
    synopsis = (novel.get("synopsis") or {})
    synopsis_text = f"{synopsis.get('short','')} {synopsis.get('long','')}".lower()
    synopsis_kw = set(_keywords(synopsis_text))
    corpus_kw   = set(_keywords(corpus_l))
    overlap = synopsis_kw & corpus_kw - {
        "chef", "paris", "father", "search"  # suppress overly-generic hits
    }
    if overlap:
        sample = sorted(overlap)[:4]
        score += min(len(overlap), 3)
        reasons.append(f"synopsis keywords: {', '.join(sample)}")

    return score, reasons


def match_agents(agents: list[dict], novel: dict) -> list[dict]:
    """Score QT agent records against the novel."""
    results = []
    for agent in agents:
        genres = agent.get("genres", [])
        corpus_parts = [
            agent.get("name", ""),
            agent.get("agency", ""),
            agent.get("bio", ""),
            " ".join(genres) if isinstance(genres, list) else genres,
            agent.get("wishlist", ""),
            " ".join(agent.get("categories", [])),
        ]
        corpus = " ".join(p for p in corpus_parts if p)
        score, reasons = _score_text(corpus, novel)
        if score <= 0:
            continue
        results.append({
            "type":          "agent",
            "name":          agent.get("name", ""),
            "agency":        agent.get("agency", ""),
            "url":           agent.get("url", ""),
            "query_url":     agent.get("query_url", ""),
            "status":        agent.get("status", ""),
            "score":         score,
            "reasons":       reasons,
        })
    results.sort(key=lambda x: -x["score"])
    return results


def match_wishlists(entries: list[dict], novel: dict) -> list[dict]:
    """Score MSWL wishlist entries against the novel."""
    results = []
    for entry in entries:
        corpus = f"{entry.get('title','')} {entry.get('content','')}"
        score, reasons = _score_text(corpus, novel)
        if score <= 0:
            continue
        results.append({
            "type":     "wishlist",
            "name":     entry.get("title", ""),
            "url":      entry.get("link", ""),
            "score":    score,
            "reasons":  reasons,
            "modified": entry.get("modified", ""),
        })
    results.sort(key=lambda x: -x["score"])
    return results


def match_presses(presses: list[dict], novel: dict) -> list[dict]:
    """Score P&W small press records against the novel."""
    results = []
    for press in presses:
        if press.get("temporarily_closed"):
            continue
        corpus_parts = [
            press.get("name", ""),
            press.get("description", ""),
            " ".join(press.get("subgenres", [])),
            " ".join(press.get("genres", [])),
        ]
        corpus = " ".join(p for p in corpus_parts if p)
        score, reasons = _score_text(corpus, novel)
        if score <= 0:
            continue
        results.append({
            "type":           "press",
            "name":           press.get("name", ""),
            "pw_url":         press.get("pw_url", ""),
            "reading_period": press.get("reading_period", ""),
            "subgenres":      press.get("subgenres", []),
            "score":          score,
            "reasons":        reasons,
        })
    results.sort(key=lambda x: -x["score"])
    return results


def match_novel(novel: dict, agents: list[dict], wishlists: list[dict], presses: list[dict]) -> dict:
    """
    Run all three matchers and return a combined ranked result set.
    Also returns a combined top-20 cross-source ranking for the dashboard.
    """
    agent_matches   = match_agents(agents, novel)
    wishlist_matches = match_wishlists(wishlists, novel)
    press_matches   = match_presses(presses, novel)

    combined = sorted(
        agent_matches + wishlist_matches + press_matches,
        key=lambda x: -x["score"],
    )[:20]

    return {
        "novel_id":    novel.get("id"),
        "novel_title": novel.get("title"),
        "agents":      agent_matches,
        "wishlists":   wishlist_matches,
        "presses":     press_matches,
        "top20":       combined,
    }
