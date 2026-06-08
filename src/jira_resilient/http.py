"""HTTP foundation: TLS-1.2-min session, bearer-PAT auth, retry-with-backoff.

Private module. Public callers should construct a JiraClient, which wraps this.

The retry policy distinguishes failure modes explicitly:
    - 4xx (except 429) — client error, retrying won't help, raise immediately.
    - 429              — rate limited, exponential backoff respecting any Retry-After.
    - 5xx (502/503/504)— transient server, exponential backoff.
    - Network errors   — exponential backoff (different schedule than 5xx).
    - Everything else  — propagate.
"""

from __future__ import annotations

import logging
import ssl
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

logger = logging.getLogger(__name__)


class _TLSAdapter(HTTPAdapter):
    """Forces TLS 1.2+ minimum at the connection layer."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


class _Retry(Retry):
    """Connection-level retry for transient 5xx/413.

    Drops 429 from urllib3's Retry-After set so a rate-limit response is NOT silently
    retried at the connection layer — `request_with_retry` is the single authority for
    429, honoring Retry-After explicitly. 413/503 stay urllib3-handled.
    """

    RETRY_AFTER_STATUS_CODES = frozenset((413, 503))


# Connection-level retry for transient 5xx. Application-level retry layers on top.
_RETRY = _Retry(
    total=3,
    status_forcelist=[500, 502, 503, 504],
    backoff_factor=1,
    respect_retry_after_header=True,
)

# Ceiling on a single backoff sleep, in seconds. Honors a server's Retry-After up to
# this cap (a buggy/hostile header can't park a worker indefinitely); the retry loop
# re-polls and reads the next Retry-After if the server still wants more. Equals the
# blind schedule's own max (60 * 2**4), so behavior never sleeps longer than before.
_MAX_WAIT_SECONDS = 960


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


def make_session(pat: str | None, verify: str | bool = True) -> requests.Session:
    """Build a `requests.Session` configured for JIRA Server PAT auth + TLS 1.2+."""
    session = requests.Session()
    if pat:
        session.headers["Authorization"] = f"Bearer {pat}"
    session.verify = verify
    adapter = _TLSAdapter(max_retries=_RETRY)
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
    """HTTP request with exponential back-off on 429/5xx; 4xx fail-fast.

    Library callers should not invoke this directly — go through `JiraClient`.
    Exposed at the module level for the small handful of unit tests that need it.
    """
    for attempt in range(max_attempts):
        try:
            resp = session.request(method, url, json=json, params=params, timeout=timeout)
            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp)
                wait = (
                    min(retry_after, _MAX_WAIT_SECONDS)
                    if retry_after is not None
                    else 60 * (2**attempt)
                )
                logger.warning(
                    "Rate limited; sleeping %.0fs (attempt %d/%d; source=%s)",
                    wait,
                    attempt + 1,
                    max_attempts,
                    "retry-after" if retry_after is not None else "backoff",
                )
                time.sleep(wait)
                continue
            if resp.status_code in (502, 503, 504):
                wait = 30 * (2**attempt)
                logger.warning(
                    "Server error %d; sleeping %ds (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError:
            # 4xx (other than 429, handled above) — retrying won't help. Propagate.
            raise
        except requests.RequestException as exc:
            if attempt < max_attempts - 1:
                wait = 10 * (attempt + 1)
                logger.warning("%s %s failed (%s); retrying in %ds", method, url, exc, wait)
                time.sleep(wait)
            else:
                raise
    raise requests.RequestException(f"{method} {url}: exhausted {max_attempts} retries")
