"""
Resilient drop-in replacement for the `requests` module.

Modules that talk to the Notion/OpenAI/HTTP APIs import this as:

    from utils import resilient_http as http_requests

and keep calling `http_requests.get/post/patch/...` exactly as before.
Every call gets:
- a default timeout (no more hung scheduled runs on network blips)
- automatic retries with exponential backoff + jitter on connection
  errors, timeouts, and transient HTTP statuses (429/409/5xx)
- Retry-After header support for Notion/OpenAI rate limits (3 req/s on Notion)

`Session` is passed through unchanged so scraping code that manages its own
session/adapters keeps its existing behavior.
"""
from __future__ import annotations

import random
import time

import requests as _requests
from rich.console import Console

# Passthroughs so this module stays a drop-in for `requests`
Session = _requests.Session
exceptions = _requests.exceptions
Response = _requests.Response

console = Console()

DEFAULT_TIMEOUT = 30  # seconds, applied when the caller passes none
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0

# 409 is Notion's "conflict_error: transaction in progress, retry" status.
RETRYABLE_STATUSES = {409, 429, 500, 502, 503, 504}

_RETRYABLE_EXCEPTIONS = (
    _requests.exceptions.ConnectionError,
    _requests.exceptions.Timeout,
    _requests.exceptions.ChunkedEncodingError,
)


def _retry_delay(response: _requests.Response | None, attempt: int) -> float:
    """Honor Retry-After when present, else exponential backoff with jitter."""
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        try:
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            pass
    return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)


def request(method: str, url: str, **kwargs) -> _requests.Response:
    """`requests.request` with default timeout and transient-failure retries."""
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _requests.request(method, url, **kwargs)
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = _retry_delay(None, attempt)
            console.print(
                f"[yellow]HTTP {method} {url.split('?')[0]} failed ({type(exc).__name__}); "
                f"retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s[/yellow]"
            )
        else:
            if response.status_code not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS:
                return response
            delay = _retry_delay(response, attempt)
            console.print(
                f"[yellow]HTTP {response.status_code} from {method} {url.split('?')[0]}; "
                f"retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s[/yellow]"
            )
        time.sleep(delay)

    raise RuntimeError(f"Unreachable retry state for {method} {url}")


def get(url: str, **kwargs) -> _requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> _requests.Response:
    return request("POST", url, **kwargs)


def patch(url: str, **kwargs) -> _requests.Response:
    return request("PATCH", url, **kwargs)


def put(url: str, **kwargs) -> _requests.Response:
    return request("PUT", url, **kwargs)


def delete(url: str, **kwargs) -> _requests.Response:
    return request("DELETE", url, **kwargs)


def head(url: str, **kwargs) -> _requests.Response:
    return request("HEAD", url, **kwargs)
