"""
Standalone fetch helper for The Submission Grinder.
Called as a subprocess by grinder.py to avoid truststore/asyncio thread issues.

Usage: python -m src.sites._grinder_fetch [genre_id ...]
       (defaults to 24=Literary 1=General)

Output: JSON array of raw market dicts on stdout.
        On failure: writes error text to stderr and exits non-zero.
"""

import json
import ssl
import sys

import httpx
import truststore

BASE = "https://thegrinder.diabolicalplots.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def main() -> None:
    genre_ids = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else [24, 1]

    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    # Step 1: session cookie
    session_resp = httpx.get(
        f"{BASE}/Search/ByFilter",
        params={"marketType": "Fiction"},
        headers={"User-Agent": _HEADERS["User-Agent"]},
        verify=ssl_ctx,
        timeout=30,
        follow_redirects=True,
    )
    session_resp.raise_for_status()
    cookies = dict(session_resp.cookies)

    # Step 2: search POST (~650KB response; generous read timeout)
    post_headers = {
        **_HEADERS,
        "Content-Type": "application/json; charset=utf-8",
        "Referer": f"{BASE}/Search/ByFilter?marketType=Fiction",
    }
    resp = httpx.post(
        f"{BASE}/Search/Byfilter/Search",
        headers=post_headers,
        cookies=cookies,
        verify=ssl_ctx,
        timeout=httpx.Timeout(connect=15, read=180, write=15, pool=15),
        content=json.dumps({
            "SelectedGenres": genre_ids,
            "MarketType": "Fiction",
            "Paying": False,
        }),
    )
    resp.raise_for_status()
    print(json.dumps(resp.json()))


if __name__ == "__main__":
    main()
