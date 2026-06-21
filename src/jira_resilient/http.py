"""HTTP foundation: TLS-1.2-min session, bearer-PAT auth, retry-with-backoff.

Private module. Public callers should construct a JiraClient, which wraps this.

The retry policy distinguishes failure modes explicitly, and `request_with_retry` is the
SINGLE authority for HTTP-status retries (429 + 5xx) and Retry-After (capped):
    - 3xx              — not followed; a redirect on a REST call means a proxy/SSO
                         interposed and would turn `POST /search` into a body-less GET.
    - 4xx (except 429) — client error, retrying won't help, raise immediately.
    - 429              — rate limited, backoff respecting Retry-After (capped).
    - 5xx (500/502/503/504) — transient server, backoff respecting Retry-After (capped).
    - Network errors   — connection-level retries (adapter) + app-level backoff.
The adapter retries connection/read errors only — it does NOT retry HTTP statuses, so
there is exactly one place that interprets Retry-After and one cap.
"""

from __future__ import annotations

import logging
import ssl
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import DefaultCookiePolicy

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

logger = logging.getLogger(__name__)


class _TLSAdapter(HTTPAdapter):
    """Forces a TLS 1.2+ floor on BOTH direct and proxied connections, and honors
    `verify=False` (the self-signed escape hatch) by relaxing the custom SSL context —
    otherwise urllib3 raises `ValueError: Cannot set verify_mode to CERT_NONE when
    check_hostname is enabled` against our context."""

    def __init__(self, *args, verify: str | bool = True, **kwargs):
        self._verify = verify
        super().__init__(*args, **kwargs)

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = create_urllib3_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self._verify is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context()
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


# Connection-level retry ONLY (connect/read errors). HTTP-status retries (429/5xx) and
# the single capped Retry-After authority live in `request_with_retry`, so the adapter
# never sleeps on a status or a Retry-After header.
_RETRY = Retry(
    total=3,
    status_forcelist=[],
    backoff_factor=1,
    respect_retry_after_header=False,
)

# Ceiling on a single backoff sleep, in seconds. Honors a server's Retry-After up to this
# cap (a buggy/hostile header can't park a worker indefinitely); the loop re-polls and
# reads the next Retry-After if the server still wants more.
_MAX_WAIT_SECONDS = 960

# PAT (bearer) auth needs no cookies; blocking Set-Cookie removes the one piece of shared
# MUTABLE state on a Session, making it safe to share read-only across a fan-out of threads.
_BLOCK_ALL_COOKIES = DefaultCookiePolicy(allowed_domains=[])


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a `Retry-After` header (delta-seconds or HTTP-date) to seconds; None if absent/unparseable."""
    if not (value := resp.headers.get("Retry-After", "").strip()):
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff_wait(resp: requests.Response, default: float) -> float:
    """Single retry-wait authority: honor Retry-After capped at `_MAX_WAIT_SECONDS`,
    else fall back to the caller's exponential default (also capped)."""
    retry_after = _retry_after_seconds(resp)
    wait = retry_after if retry_after is not None else default
    return min(wait, _MAX_WAIT_SECONDS)


def make_session(
    pat: str | None, verify: str | bool = True, pool_maxsize: int = 10
) -> requests.Session:
    """Build a `requests.Session` configured for JIRA Server PAT auth + TLS 1.2+.

    pool_maxsize sizes the per-host connection pool. The default (10, urllib3's default)
    is fine for serial use; raise it to the number of concurrent requests you fan out
    through this session (e.g. a thread pool issuing N parallel GETs) — otherwise urllib3
    caps live connections at 10 and discards/reopens the surplus ("Connection pool is full").

    The session blocks cookies (PAT auth needs none), so its only shared state is read-only
    and it is safe to share across threads.
    """
    session = requests.Session()
    if pat:
        session.headers["Authorization"] = f"Bearer {pat}"
    session.verify = verify
    session.cookies.set_policy(_BLOCK_ALL_COOKIES)
    adapter = _TLSAdapter(
        max_retries=_RETRY,
        pool_connections=pool_maxsize,
        pool_maxsize=pool_maxsize,
        verify=verify,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: int = 120,
    max_attempts: int = 5,
) -> requests.Response:
    """HTTP request with exponential back-off on 429/5xx; 4xx fail-fast; 3xx rejected.

    Library callers should not invoke this directly — go through `JiraClient`.
    Exposed at the module level for the small handful of unit tests that need it.
    """
    last_attempt = max_attempts - 1
    for attempt in range(max_attempts):
        try:
            resp = session.request(
                method, url, json=json, params=params, timeout=timeout, allow_redirects=False
            )
            if 300 <= resp.status_code < 400:
                # A REST endpoint should never redirect; a proxy/SSO did, and following it
                # would re-issue `POST /search` as a body-less GET. Surface, don't follow.
                raise requests.HTTPError(
                    f"{method} {url}: unexpected {resp.status_code} redirect to "
                    f"{resp.headers.get('Location')!r} (proxy/SSO?) — not following",
                    response=resp,
                )
            if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                if attempt == last_attempt:
                    resp.raise_for_status()  # exhausted — raise the REAL 429/5xx, not a bare error
                default = 60 * (2**attempt) if resp.status_code == 429 else 30 * (2**attempt)
                wait = _backoff_wait(resp, default)
                logger.warning(
                    "%s %s -> %d; sleeping %.0fs (attempt %d/%d; source=%s)",
                    method,
                    url,
                    resp.status_code,
                    wait,
                    attempt + 1,
                    max_attempts,
                    "retry-after" if _retry_after_seconds(resp) is not None else "backoff",
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError:
            # 4xx (other than 429), a 3xx redirect, or an exhausted 429/5xx — propagate
            # with the response attached.
            raise
        except requests.RequestException as exc:
            if attempt < last_attempt:
                wait = 10 * (attempt + 1)
                logger.warning("%s %s failed (%s); retrying in %ds", method, url, exc, wait)
                time.sleep(wait)
            else:
                raise
    raise requests.RequestException(f"{method} {url}: exhausted {max_attempts} retries")
