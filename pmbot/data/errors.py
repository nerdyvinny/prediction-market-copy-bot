"""Shared HTTP error handling for the data clients.

The clients retry on transport errors and `httpx.HTTPStatusError` (429 and
5xx: the server or the rate limiter had a bad moment). A plain 4xx means the
request itself is wrong — retrying it verbatim can only burn rate-limit
budget — so it raises `NonRetryableAPIError`, which the retry decorators do
not match.
"""

from __future__ import annotations

import httpx


class NonRetryableAPIError(Exception):
    """A 4xx response (other than 429): do not retry."""


def raise_for_status_smart(resp: httpx.Response) -> None:
    if 400 <= resp.status_code < 500 and resp.status_code != 429:
        raise NonRetryableAPIError(f"HTTP {resp.status_code} for {resp.request.url}")
    resp.raise_for_status()  # 429 / 5xx raise HTTPStatusError -> retried
