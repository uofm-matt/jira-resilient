# Refutations

Claims that cost a measurement to kill. Read this before proposing a finding; add to it
after refuting one. A refutation leaves no trace in the diff, so this file is the only
place it exists.

Written by `refute.py`; the format is parsed, so keep the `- key: value` shape.

## client.py coverage is capped at 99% because the _jql_error_from None-guard (client.py:79->82) is permanently unreachable and cannot be removed without a type: ignore

- scope: `src/jira_resilient/client.py`
- verdict: REFUTED
- measured: 2026-08-06
- commit: ef2a4b55649bc0b174fa7340a4ce7163d5a76b06
- oracle: Restructured _is_http_400 (TypeGuard[HTTPError]) into _http_400_response(exc) -> Response | None and walrus-narrowed at both call sites (client.py:892, :951). Measured on a git-archive copy: mypy --strict clean over 6 files, suite green, client.py 333 stmts / 0 missed / 74 branches / 0 partial = 100%. The guard is removable; only its shape forced the branch.
- cost: one scratch copy, one restructure, three commands
- unmeasured: Whether the restructure is worth doing on its own merits (it trades a TypeGuard for an Optional-returning accessor). This refutes the coverage-ceiling claim, not the design question.
