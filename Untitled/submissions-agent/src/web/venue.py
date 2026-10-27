"""
Venue research — scrapes a magazine's own website to surface aesthetic signals.

Fetches the homepage and any About / Submissions pages it can find,
strips HTML, and returns the most useful text.  Results are lightweight
(< 4 KB total) and intended to guide a submission decision, not as a
complete mirror of the site.
"""

import re
import ssl
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import truststore

_USEFUL_PATHS = [
    "/about", "/about-us", "/about/",
    "/submit", "/submissions", "/submit/", "/submissions/",
    "/guidelines", "/submission-guidelines",
    "/masthead", "/mission",
]

_KEYWORD_LINKS = {"about", "submit", "guideline", "mission", "masthead", "contribute"}

_STRIP_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

_HEADERS = {"User-Agent": "submissions-agent/1.0 (literary research tool; read-only)"}


def _clean(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    # Remove script / style blocks entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = _STRIP_TAGS.sub(" ", html)
    return _WHITESPACE.sub(" ", text).strip()


def _extract_links(html: str, base: str) -> list[str]:
    """Return candidate About/Submissions links from the homepage HTML."""
    hrefs = re.findall(r'href=["\']([^"\'#?][^"\']*)["\']', html, re.IGNORECASE)
    base_domain = urlparse(base).netloc
    results = []
    for href in hrefs:
        href_lower = href.lower()
        if not any(kw in href_lower for kw in _KEYWORD_LINKS):
            continue
        if href.startswith("http"):
            # Only follow links on the same domain
            if urlparse(href).netloc != base_domain:
                continue
            results.append(href)
        elif href.startswith("/"):
            results.append(urljoin(base, href))
    # Deduplicate preserving order
    seen = set()
    unique = []
    for u in results:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique[:6]


def _truncate(text: str, chars: int = 1200) -> str:
    if len(text) <= chars:
        return text
    return text[:chars].rsplit(" ", 1)[0] + "…"


async def research(magazine_url: str) -> dict:
    """
    Fetch and summarise useful pages from the magazine's website.

    Returns:
        {
          "homepage_text": str,    # cleaned homepage text (truncated)
          "pages": [               # up to 4 additional pages
            {"url": str, "text": str}
          ],
          "error": str | None
        }
    """
    if not magazine_url or not magazine_url.startswith("http"):
        return {"homepage_text": "", "pages": [], "error": "No valid magazine URL"}

    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    async def _get(url: str) -> Optional[str]:
        try:
            async with httpx.AsyncClient(
                verify=ssl_ctx,
                timeout=15,
                follow_redirects=True,
                headers=_HEADERS,
            ) as client:
                r = await client.get(url)
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                    return r.text
        except Exception:
            pass
        return None

    # Fetch homepage
    homepage_html = await _get(magazine_url)
    if not homepage_html:
        return {"homepage_text": "", "pages": [], "error": f"Could not fetch {magazine_url}"}

    homepage_text = _truncate(_clean(homepage_html))

    # Find candidate sub-pages
    candidate_urls = _extract_links(homepage_html, magazine_url)

    # Also try common paths directly
    base = magazine_url.rstrip("/")
    for path in _USEFUL_PATHS:
        url = base + path
        if url not in candidate_urls:
            candidate_urls.append(url)

    # Fetch up to 4 pages
    pages = []
    for url in candidate_urls[:8]:  # try 8, keep first 4 that respond
        if len(pages) >= 4:
            break
        html = await _get(url)
        if html:
            text = _truncate(_clean(html))
            if len(text) > 100:
                pages.append({"url": url, "text": text})

    return {"homepage_text": homepage_text, "pages": pages, "error": None}
