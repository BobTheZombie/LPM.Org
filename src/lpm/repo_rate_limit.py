"""Shared GitLab HTTP cooldown, honoring 429 Retry-After without sleeping.

Transport errors from fs.urlread preserve urllib HTTPError in __cause__.
Only rate-limit errors are special: normal missing files still follow the
existing recipe discovery/fallback rules.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit

_LOCK = threading.Lock()
_BLOCKED_UNTIL: dict[str, float] = {}


class RecipeRateLimited(RuntimeError):
    """GitLab requested that LPM stop fetching recipes for a while."""


def _http_error(exc: BaseException) -> HTTPError | None:
    visited: set[int] = set()
    while exc is not None and id(exc) not in visited:
        visited.add(id(exc))
        if isinstance(exc, HTTPError):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def is_http_not_found(exc: BaseException) -> bool:
    error = _http_error(exc)
    return isinstance(exc, FileNotFoundError) or (error is not None and error.code == 404)


def _retry_seconds(value: str | None) -> int:
    if value:
        try:
            return max(1, min(int(value), 86400))
        except (TypeError, ValueError):
            try:
                dt = parsedate_to_datetime(value)
                return max(1, min(int((dt - datetime.now(timezone.utc)).total_seconds()), 86400))
            except (TypeError, ValueError, OverflowError):
                pass
    return 60


def rate_limited_read(reader, url: str):
    host = (urlsplit(url).hostname or "").lower()
    if host != "gitlab.com":
        return reader(url)
    with _LOCK:
        remaining = _BLOCKED_UNTIL.get(host, 0) - time.monotonic()
    if remaining > 0:
        raise RecipeRateLimited(
            f"GitLab HTTP 429: unauthenticated IP rate limit; retry in {int(remaining) + 1}s"
        )
    try:
        return reader(url)
    except Exception as exc:
        error = _http_error(exc)
        if error is None or error.code != 429:
            raise
        retry = _retry_seconds(error.headers.get("Retry-After") if error.headers else None)
        with _LOCK:
            _BLOCKED_UNTIL[host] = max(_BLOCKED_UNTIL.get(host, 0), time.monotonic() + retry)
        raise RecipeRateLimited(
            f"GitLab HTTP 429: unauthenticated IP rate limit; Retry-After={retry}s. "
            "Use the cached Git recipe snapshot or retry after the limit resets"
        ) from exc
