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


# Connection-level retry for transient 5xx. Application-level retry layers on top.
_RETRY = Retry(
    total=3,
    status_forcelist=[500, 502, 503, 504],
    backoff_factor=1,
    respect_retry_after_header=True,
)


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
                wait = 60 * (2**attempt)
                logger.warning(
                    "Rate limited; sleeping %ds (attempt %d/%d)", wait, attempt + 1, max_attempts
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
