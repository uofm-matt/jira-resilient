"""The adapter's retry policy, MEASURED against a real socket rather than asserted.

`responses` replaces the adapter's `send`, so every other test in this suite is blind to
what urllib3 does with a retry. These tests speak HTTP over a loopback socket and count the
request lines the server actually received, because the defect they guard against was
invisible to unit tests and cost 504 seconds against production: `Retry(total=3)` turned
each app-level attempt into 4 HTTP attempts, so a 60s/2-attempt budget became
2 x (4 x 60s + 6s urllib3 backoff) + 10s = 502s.

Read timeouts are driven with a sub-second budget; `no_sleep` collapses both backoff layers
(urllib3 calls `time.sleep` through the module, so patching it covers the adapter too).
"""

from __future__ import annotations

import socket
import threading
from collections import Counter

import pytest
import requests
import urllib3.util.connection

from jira_resilient.http import make_session, request_with_retry

_READ_TIMEOUT = 0.25  # long enough to be reached reliably, short enough to burn 8 of them


class _FaultServer:
    """Speaks just enough HTTP/1.1 by hand to fail on purpose, and records every request.

    A real `http.server` cannot express "read the request and then never answer" or "hang up
    mid-flight", which are exactly the two failure modes the retry policy is about.
    """

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._port = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._live: list[socket.socket] = []
        self.requests: list[str] = []
        self._seen: Counter[str] = Counter()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"

    def count(self, path: str) -> int:
        with self._lock:
            return sum(1 for line in self.requests if line.endswith(f" {path}"))

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return  # listener closed by teardown
            with self._lock:
                self._live.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                if not (chunk := conn.recv(65536)):
                    return
                buf += chunk
            request_line = buf.split(b"\r\n")[0].decode()
            method, path, _ = request_line.split(" ")
            with self._lock:
                self.requests.append(f"{method} {path}")
                self._seen[path] += 1
                nth = self._seen[path]
            if path == "/hang" or (path == "/hang-once" and nth == 1):
                conn.recv(65536)  # answer nothing; the client's read timeout ends this
                return
            if path == "/abort-once" and nth == 1:
                return  # hang up mid-flight -> ProtocolError, which urllib3 calls a READ error
            body = b'{"ok":true}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
        except OSError:
            return  # socket closed under us by teardown
        finally:
            conn.close()

    def close(self) -> None:
        self._listener.close()
        with self._lock:
            live, self._live = self._live, []
        for conn in live:
            conn.close()  # unblocks any handler parked in recv


@pytest.fixture
def server():
    srv = _FaultServer()
    yield srv
    srv.close()


@pytest.fixture
def session():
    """A real session on the real adapter — no `responses`, or the policy under test is
    replaced by the mock."""
    sess = make_session(pat="x")
    yield sess
    sess.close()


def test_read_timeout_costs_exactly_one_http_attempt(server, session, no_sleep):
    """THE regression test for the 504s. Under `Retry(total=3)` this hit the server 4 times
    for one `session.request`; a read timeout is deterministic here (JIRA is still
    serializing an oversized payload), so retrying it only re-spends the budget."""
    with pytest.raises(requests.exceptions.ReadTimeout):
        session.get(server.url("/hang"), timeout=_READ_TIMEOUT)
    assert server.count("/hang") == 1


def test_read_timeout_surfaces_as_read_timeout_not_connection_error(server, session, no_sleep):
    """`read=False` re-raises the original; `read=0` would exhaust the counter and hand the
    caller a `ConnectionError` wrapping MaxRetryError, losing the true cause. Both stop after
    one attempt, so only the exception TYPE distinguishes the two configurations."""
    with pytest.raises(requests.exceptions.ReadTimeout) as ei:
        session.get(server.url("/hang"), timeout=_READ_TIMEOUT)
    assert not isinstance(ei.value, requests.exceptions.ConnectionError)


def test_get_and_post_take_the_same_read_attempts(server, session, no_sleep):
    """urllib3 never retried read errors for POST (`DEFAULT_ALLOWED_METHODS` excludes it), so
    every `/search` in this library already took one read attempt per app-level attempt. The
    fix is GET catching up to POST, not a new regime — assert they now agree."""
    for method in ("GET", "POST"):
        with pytest.raises(requests.exceptions.ReadTimeout):
            session.request(method, server.url("/hang"), timeout=_READ_TIMEOUT)
    assert server.count("/hang") == 2  # one apiece, not 4 + 1


def test_tier1_budget_makes_exactly_max_attempts_requests(server, session, no_sleep):
    """The tier-1 shape (`timeout=T, max_attempts=2`) end to end. Before: 8 HTTP requests
    and 2 x (4T + 6) + 10 seconds. After: 2 requests and 2T + 10."""
    with pytest.raises(requests.exceptions.RequestException):
        request_with_retry(
            session, "GET", server.url("/hang"), timeout=_READ_TIMEOUT, max_attempts=2
        )
    assert server.count("/hang") == 2


def test_connect_failure_is_still_retried_by_the_adapter(session, monkeypatch, no_sleep):
    """The half that must SURVIVE. A refused connect means the request never reached the
    server, so retrying is free of side effects — `read=False` must not disarm it."""
    attempts: list[str] = []
    real = urllib3.util.connection.create_connection

    def counting(address, *args, **kwargs):
        attempts.append(f"{address[0]}:{address[1]}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(urllib3.util.connection, "create_connection", counting)
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    with pytest.raises(requests.exceptions.ConnectionError):
        session.get(f"http://127.0.0.1:{port}/ok", timeout=_READ_TIMEOUT)
    assert len(attempts) == 4  # the initial connect + Retry(total=3)


def test_transient_read_timeout_still_recovers_at_the_app_level(server, session, no_sleep):
    """Blast radius. Dropping adapter read retries does not drop read retries — it moves them
    to `request_with_retry`, which spaces attempts 10s/20s apart instead of urllib3's 0s/2s.
    A read timeout that really was transient still succeeds on the next attempt."""
    resp = request_with_retry(
        session, "GET", server.url("/hang-once"), timeout=_READ_TIMEOUT, max_attempts=2
    )
    assert resp.status_code == 200
    assert server.count("/hang-once") == 2


def test_mid_flight_abort_recovers_at_the_app_level(server, session, no_sleep):
    """The one behaviour that genuinely changes. urllib3 classes a mid-flight hang-up
    (ProtocolError) as a read error, so it used to be retried instantly by the adapter for
    GET. It now costs one app-level backoff instead — the same path POST has always taken —
    and still recovers."""
    resp = request_with_retry(session, "GET", server.url("/abort-once"), timeout=5)
    assert resp.status_code == 200
    assert server.count("/abort-once") == 2


def test_a_healthy_request_is_untouched(server, session):
    assert request_with_retry(session, "GET", server.url("/ok"), timeout=5).json() == {"ok": True}
    assert server.count("/ok") == 1
