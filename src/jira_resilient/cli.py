"""A read-only diagnostic CLI. Nothing here writes to JIRA.

Two commands, deliberately. `probe` answers the only question that gates adoption of this
library — *do I actually have the hub problem?* — by running the three-tier ladder against
one of your own issues and printing what each attempt cost. A 3-minute links fetch is the
whole thesis; a `full` tier in 0.4s is an honest "you don't need this". `scan` is the same
question over a project rather than one issue.

Both commands split the two streams the same way: **stdout is the report** (the thing you
redirect into a file or paste into a ticket) and **stderr is progress** (the thing that tells
you it is still alive). Nothing on stderr is needed to read the result.

Everything else a user might want (fetching, changelogs, key listing) is three lines of
Python against `JiraClient`, and a wrapper for it would be surface to maintain without
teaching anything. See the README.

The PAT comes from `JIRA_PAT` only — never an argument, which would put it in shell history
and in the process table.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Self, TextIO

from jira_resilient import JiraClient, JiraResilientError, __version__

_ENV_URL = "JIRA_URL"
_ENV_PAT = "JIRA_PAT"

# Heartbeat redraw interval. Measured worst case is a single attempt that runs 504s, so the
# tick is there to be glanced at, not read; 2s is slow enough to be quiet and fast enough
# that someone who has just pressed Enter sees the clock move before they doubt it.
_TICK_SECONDS = 2.0

# A plain alias rather than a PEP 695 `type` statement: the package floor is Python 3.11.
_Sink = Callable[[str, float, str | None], None]


class _Progress:
    """The heartbeat `probe` ticks on stderr while a request is in flight.

    Measured against a production instance: one hub issue's tier-1 attempt did not fail until
    504s, and the whole probe took 11 minutes, during which the terminal showed nothing at
    all. Every structured record the ladder emits fires at attempt COMPLETION, so the slowest
    thing this command does is exactly the thing it could not report on — working and hung
    looked identical.

    Elapsed time is the only honest content. `requests` hands back no body until the response
    is complete, and these minutes are the server's serializer rather than transfer, so there
    is no byte count or percentage to show that would not be invented. A predicted deadline is
    equally unavailable: the tier budget is chosen inside `get_issue_resilient`, not taken
    from `--timeout`, so a countdown rendered here would be a guess wearing a fact's clothes.
    Likewise the rung currently in flight — the ladder announces a rung only once it is over,
    and naming the next one from the count of finished ones is inference, which is how a
    progress display starts lying.

    It owns the terminal for the duration, which is why report lines go out through `report`
    rather than straight to `print`: stdout landing on top of a half-drawn tick is the one way
    this could make output worse rather than better. WARNING records (the HTTP layer's retry
    notices) still belong to whatever handler `main` configured; those arrive complete with a
    newline, so the worst they do is push the tick down a line, and the next tick redraws.

    Disabled when stderr is not a terminal. A carriage-returned line is noise in a log file,
    and the report lines — the durable record — stream to stdout either way.
    """

    def __init__(self, label: str, stream: TextIO, *, enabled: bool, interval: float) -> None:
        self.label = label
        self.stream = stream
        self.enabled = enabled
        self.interval = interval
        # Re-entrant: `completed` clears the line and prints while already holding it.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = self._since = time.perf_counter()
        self._last: str | None = None
        self._drawn = False

    def __enter__(self) -> Self:
        if self.enabled:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        self._clear()

    def report(self, line: str) -> None:
        """Print one line of the report to stdout, without the heartbeat smearing it."""
        with self._lock:
            self._clear()
            print(line, flush=True)

    def completed(self, label: str, line: str) -> None:
        """A rung finished: print its line and restart the in-flight clock under it."""
        with self._lock:
            self.report(line)
            self._since, self._last = time.perf_counter(), label

    def _tick(self) -> None:
        # Draw first, then wait, so the first frame lands immediately rather than one
        # interval into a request that may run for eight minutes.
        while not self._stop.is_set():
            with self._lock:
                self.stream.write(f"\r  {self._status()}\x1b[K")
                self.stream.flush()
                self._drawn = True
            self._stop.wait(self.interval)

    def _status(self) -> str:
        now = time.perf_counter()
        rung = f"last rung: {self._last}" if self._last else "nothing has finished yet"
        return (
            f"{self.label}  {now - self._start:.0f}s elapsed | "
            f"{now - self._since:.0f}s in flight | {rung}"
        )

    def _clear(self) -> None:
        with self._lock:
            if not self._drawn:
                return
            self.stream.write("\r\x1b[K")
            self.stream.flush()
            self._drawn = False


class _AttemptLog(logging.Handler):
    """Collects the structured attempt records `get_issue_resilient` emits, and forwards
    each one to `sink` as it lands — which is what makes the report stream instead of
    appearing all at once when the ladder returns.

    Reads `record.jr_*` attributes rather than parsing the formatted message, so changing
    the human-readable wording cannot break this.
    """

    def __init__(self, sink: _Sink) -> None:
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.attempts: list[tuple[str, float, str | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # getattr, not attribute access: these are `extra=` fields, so they are absent from
        # LogRecord's declared type and a checker rejects `record.jr_step`. The repo carries
        # zero suppressions, so read them the way the stdlib actually supports.
        if getattr(record, "jr_event", None) != "attempt":
            return
        step: str = getattr(record, "jr_step", "?")
        elapsed: float = getattr(record, "jr_elapsed", 0.0)
        error: str | None = getattr(record, "jr_error", None)
        self.attempts.append((step, elapsed, error))
        self.sink(step, elapsed, error)


@contextmanager
def _collect_attempts(sink: _Sink) -> Iterator[_AttemptLog]:
    handler = _AttemptLog(sink)
    log = logging.getLogger("jira_resilient")
    previous, log.level = log.level, min(log.level or logging.INFO, logging.INFO)
    log.addHandler(handler)
    try:
        yield handler
    finally:
        log.removeHandler(handler)
        log.level = previous


_STEP_LABEL = {
    "full": "Full request",
    "hub-base": "Hub base request",
    "hub-links": "Links-only request",
    "hub": "Hub request",
    "minimal": "Minimal request",
}


def _client(args: argparse.Namespace) -> JiraClient:
    if not (pat := os.environ.get(_ENV_PAT)):
        raise SystemExit(f"{_ENV_PAT} is not set. Export your personal access token into it.")
    verify: str | bool = args.ca_bundle or not args.insecure
    return JiraClient(args.url, pat, verify=verify, timeout=args.timeout)


def probe(args: argparse.Namespace) -> int:
    """Run the resilient ladder against one issue and report what each attempt cost.

    The rungs print as they land rather than after the ladder returns. Same lines in the same
    order — but a tier-1 timeout that takes 504s is now visible when it happens, and one
    renderer serves both the success and the all-tiers-failed path, so a rung that SUCCEEDED
    before a later one failed can no longer be reported as "failed" on the way out.
    """
    client = _client(args)
    progress = _Progress(args.key, sys.stderr, enabled=sys.stderr.isatty(), interval=_TICK_SECONDS)

    def rung(step: str, elapsed: float, error: str | None) -> None:
        label = _STEP_LABEL.get(step, step)
        outcome = f"{error} after {elapsed:.1f}s" if error else f"success in {elapsed:.1f}s"
        progress.completed(label, f"{label + ':':<20} {outcome}")

    with progress, _collect_attempts(rung) as collected:
        started = time.perf_counter()
        try:
            result = client.get_issue_resilient(args.key)
        except JiraResilientError as exc:
            progress.report(f"\nAll tiers failed: {exc}")
            return 1
        wall = time.perf_counter() - started

    links = (result.issue.get("fields") or {}).get("issuelinks")
    print(f"\nIssue:  {args.key}")
    print(f"Tier:   {result.tier}")
    print(f"Links:  {len(links):,}" if isinstance(links, list) else "Links:  unknown (not fetched)")
    print(f"Fields: {len(result.issue.get('fields') or {}):,}")
    print(f"Wall:   {wall:.1f}s over {len(collected.attempts)} attempt(s)")
    if result.tier != "full":
        print(
            f"\nThis issue degraded to the {result.tier!r} tier. A client without that fallback "
            "would have failed or hung here."
        )
    return 0


def scan(args: argparse.Namespace) -> int:
    """Page a project and report the tier distribution over the issues actually seen."""
    client = _client(args)
    tiers: Counter[str] = Counter()
    fallback_pages = seen = 0
    started = time.perf_counter()
    try:
        for page in client.search_seek(args.project, page_size=args.page_size):
            tiers[page.tier] += len(page.issues)
            fallback_pages += page.fallback
            seen += len(page.issues)
            print(f"  {seen:>7,} issues  tier={page.tier}", file=sys.stderr)
            if args.limit and seen >= args.limit:
                break
    except JiraResilientError as exc:
        print(f"\nScan failed after {seen:,} issues: {exc}")
        return 1
    elapsed = time.perf_counter() - started

    print(f"\nProject: {args.project}")
    print(f"Issues:  {seen:,} in {elapsed:.1f}s")
    for tier in ("full", "hub", "minimal"):
        if tiers[tier]:
            print(f"  {tier:<8} {tiers[tier]:>7,}  ({tiers[tier] / seen:.1%})")
    if fallback_pages:
        print(f"\n{fallback_pages} page(s) came from post-reindex id-scan recovery.")
    if tiers["minimal"]:
        print(f"\n{tiers['minimal']:,} issue(s) landed on the minimal tier — those are LOSSY.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jira-resilient",
        description="Read-only diagnostics for JIRA Server / Data Center. Writes nothing.",
        epilog=f"Auth: export {_ENV_PAT}. Base URL: --url or {_ENV_URL}.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--url", default=os.environ.get(_ENV_URL), help=f"JIRA base URL (or ${_ENV_URL})"
    )
    parser.add_argument("--timeout", type=int, default=120, help="per-request seconds (120)")
    parser.add_argument("--ca-bundle", help="path to a custom CA bundle")
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (self-signed hosts)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="time the three-tier fetch of one issue")
    p.add_argument("key", help="issue key, e.g. PROJ-123")
    p.set_defaults(func=probe)

    s = sub.add_parser("scan", help="page a project and report its tier distribution")
    s.add_argument("project", help="project key, e.g. PROJ")
    s.add_argument("--limit", type=int, default=0, help="stop after N issues (0 = no limit)")
    s.add_argument("--page-size", type=int, default=20, help="issues per request (20)")
    s.set_defaults(func=scan)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url:
        raise SystemExit(f"No JIRA URL. Pass --url or export {_ENV_URL}.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # basicConfig sets the ROOT LOGGER to WARNING but leaves its handler at NOTSET, and
    # propagation is gated by handler levels rather than ancestor logger levels. So once
    # `_collect_attempts` lowers the library logger to INFO, every attempt record would also
    # reach the console — printing the raw instrumentation above the report rendered from it.
    # Pinning at WARNING rather than silencing the console is deliberate: the HTTP layer's
    # retry notices are the only thing besides the heartbeat that anyone can see during a
    # long attempt, and they say WHY it is slow, which the heartbeat cannot.
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)
    result: int = args.func(args)
    return result
