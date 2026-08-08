"""A read-only diagnostic CLI. Nothing here writes to JIRA.

Two commands, deliberately. `probe` answers the only question that gates adoption of this
library — *do I actually have the hub problem?* — by running the three-tier ladder against
one of your own issues and printing what each attempt cost. A 3-minute links fetch is the
whole thesis; a `full` tier in 0.4s is an honest "you don't need this". `scan` is the same
question over a project rather than one issue.

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
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from jira_resilient import JiraClient, JiraResilientError, __version__

_ENV_URL = "JIRA_URL"
_ENV_PAT = "JIRA_PAT"


class _AttemptLog(logging.Handler):
    """Collects the structured attempt records `get_issue_resilient` emits.

    Reads `record.jr_*` attributes rather than parsing the formatted message, so changing
    the human-readable wording cannot break this.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
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


@contextmanager
def _collect_attempts() -> Iterator[_AttemptLog]:
    handler = _AttemptLog()
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
    """Run the resilient ladder against one issue and report what each attempt cost."""
    client = _client(args)
    with _collect_attempts() as collected:
        started = time.perf_counter()
        try:
            result = client.get_issue_resilient(args.key)
        except JiraResilientError as exc:
            for step, elapsed, error in collected.attempts:
                print(
                    f"{_STEP_LABEL.get(step, step) + ':':<20} {error or 'failed'} after {elapsed:.1f}s"
                )
            print(f"\nAll tiers failed: {exc}")
            return 1
        wall = time.perf_counter() - started

    for step, elapsed, error in collected.attempts:
        outcome = f"{error} after {elapsed:.1f}s" if error else f"success in {elapsed:.1f}s"
        print(f"{_STEP_LABEL.get(step, step) + ':':<20} {outcome}")

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
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)
    result: int = args.func(args)
    return result
