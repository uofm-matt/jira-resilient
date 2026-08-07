"""A precedence-correct JQL evaluator, used as an independent oracle for scan scoping.

The point of this module is to be derived from JIRA's documented grammar rather than from
`jira_resilient`'s code, so a test can assert what a query MEANS instead of what string it
happens to be. A string assertion cannot catch an operator-precedence defect; this can.

Precedence, tightest first: NOT, AND, OR — so `a AND b OR c` parses as `(a AND b) OR c`.
Ref: https://confluence.atlassian.com/jiracorecloud/advanced-searching-operators-reference-765593677.html

Only the operators this library actually emits, plus those used in test filters, are
supported. An unrecognized field or operator raises rather than silently evaluating false —
a permissive oracle would hide the very rows it exists to detect.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<op>>=|<=|!=|=|>|<|~)
      | (?P<str>"[^"]*")
      | (?P<word>[A-Za-z0-9_.\-]+)
    )""",
    re.VERBOSE,
)


def _tokenize(jql: str) -> list[str]:
    tokens, pos = [], 0
    while pos < len(jql):
        if not (m := _TOKEN.match(jql, pos)):
            if jql[pos:].strip():
                raise ValueError(f"unlexable JQL at {jql[pos:]!r}")
            break
        tokens.append(m.group(0).strip())
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> str:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise ValueError(f"trailing tokens at {self.toks[self.i :]!r}")
        return node

    def parse_or(self) -> Any:
        node = self.parse_and()
        while (t := self.peek()) and t.lower() == "or":
            self.next()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_not()
        while (t := self.peek()) and t.lower() == "and":
            self.next()
            node = ("and", node, self.parse_not())
        return node

    def parse_not(self) -> Any:
        if (t := self.peek()) and t.lower() == "not":
            self.next()
            return ("not", self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> Any:
        if self.peek() == "(":
            self.next()
            node = self.parse_or()
            if self.next() != ")":
                raise ValueError("unbalanced parenthesis")
            return node
        field = self.next()
        op = self.next()
        if op.lower() == "in":
            if self.next() != "(":
                raise ValueError("`in` must be followed by a list")
            values = []
            while (t := self.next()) != ")":
                if t != ",":
                    values.append(_literal(t))
            return ("cmp", field, "in", values)
        return ("cmp", field, op.lower(), _literal(self.next()))


def _literal(tok: str) -> Any:
    if tok.startswith('"'):
        return tok[1:-1]
    if tok.isdigit():
        return int(tok)
    return tok


def _as_instant(value: str) -> datetime:
    """A bare minute literal is the INSTANT MM:00 to JIRA, not the whole minute."""
    return datetime.fromisoformat(f"{value}:00").replace(tzinfo=UTC)


def _compare(row: dict, field: str, op: str, value: Any) -> bool:
    match field:
        case "project":
            actual = row["project"]
        case "id":
            actual = row["id"]
        case "status":
            actual = row["status"]
        case "labels":
            # A set-valued field: `labels = x` means "x is among them".
            return value in row["labels"] if op in {"=", "in"} else value not in row["labels"]
        case "updated":
            actual, value = row["updated"], _as_instant(value)
        case _:
            raise ValueError(f"oracle does not model field {field!r}")
    match op:
        case "=":
            return actual == value
        case "!=":
            return actual != value
        case ">":
            return actual > value
        case "<":
            return actual < value
        case ">=":
            return actual >= value
        case "<=":
            return actual <= value
        case "in":
            return actual in value
        case _:
            raise ValueError(f"oracle does not model operator {op!r}")


def _evaluate(node: Any, row: dict) -> bool:
    match node:
        case ("and", left, right):
            return _evaluate(left, row) and _evaluate(right, row)
        case ("or", left, right):
            return _evaluate(left, row) or _evaluate(right, row)
        case ("not", inner):
            return not _evaluate(inner, row)
        case ("cmp", field, op, value):
            return _compare(row, field, op, value)
    raise ValueError(f"unevaluable node {node!r}")


def split_order_by(jql: str) -> tuple[str, str]:
    """Separate the WHERE part from the trailing ORDER BY, which is not a predicate."""
    if (m := re.search(r"\s+ORDER\s+BY\s+", jql, re.IGNORECASE)) is None:
        return jql, ""
    return jql[: m.start()], jql[m.end() :]


def matches(jql: str, row: dict) -> bool:
    where, _ = split_order_by(jql)
    return _evaluate(_Parser(_tokenize(where)).parse(), row)


def select(jql: str, rows: list[dict]) -> list[dict]:
    """Rows satisfying the query, ordered as the trailing ORDER BY asks."""
    where, order = split_order_by(jql)
    hits = [r for r in rows if _evaluate(_Parser(_tokenize(where)).parse(), r)]
    hits.sort(
        key=(lambda r: r["id"]) if "id" in order.lower() else (lambda r: (r["updated"], r["id"]))
    )
    return hits
