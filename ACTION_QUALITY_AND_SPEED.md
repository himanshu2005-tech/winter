# Action quality and response timing

The existing models, integrations, planner and approval rules remain in place.

## Shared execution improvements

Older handlers that write to the legacy output bridge now pass their receipts
through the same registration verifiers as handlers using `ctx.report()`.
The bridge is cleared before a handler runs, so a missing result cannot inherit
an earlier success. If a receipt verifier itself fails, the original evidence
is retained and marked unverified. Verification failure does not establish that
an external operation was rolled back.

All declared actions have generated tests that send malformed inputs through
the real action parser and assert rejection before dispatch, speech or approval
queue writes. Existing provider-specific tests cover successful and unsuccessful
API, file, calendar, mail, browser, build and other workflows.

## Speed changes

The former shared 500 ms settling pause ran after nearly every action. It now
applies only to raw desktop interactions that need it. API calls, file reads,
computation and managed-browser operations return without this extra pause;
the managed browser retains its own readiness and verification waits. Desktop
settling can be interrupted by the task's abort event.

This removes 500 ms of imposed delay per affected action. It does not make the
remote model itself faster or establish an end-to-end percentage improvement.

Model-call timings include total attempt duration and time to first answer text.
Action timings record dispatcher duration. Samples are bounded to 512 records
in memory and contain names, durations and status metadata, not user messages,
credentials, action arguments or returned content. `SYSTEM_STATUS` includes a
compact summary of recent timings. Cold model startup, provider latency, voice
generation and complex tool workflows can still add time.

Empty provider responses are treated as failures rather than silent successes;
timeout diagnostics preserve their reason. No additional model calls are made
for timing collection.

ChatGPT account-model discovery is deferred until that provider is actually
attempted. An unavailable fallback catalog cannot delay or abort a healthy
AIRouter primary request. Model validation still happens before using ChatGPT;
this does not change the configured model or provider order.

Public-data GET requests fail promptly on permanent HTTP errors such as 401 and
404, retain bounded backoff for transient errors, and honour long Retry-After
instructions by deferring rather than retrying too early. Diagnostic messages
exclude URLs and query-string tokens.

## Repeatable checks

```powershell
$env:PYTHONPATH='D:\winter\.venv\Lib\site-packages;D:\winter'
$env:WINTER_BROWSER_IMPORT_COOKIES='0'
python -m pytest tests -q -p scripts.action_test_audit --action-audit-output=reports/action-tests.json --isolated-action-audit
python -m scripts.audit_actions --output reports/action-audit.json
```

The opt-in test audit blocks Python socket connections to external services
and the local production Neo4j ports. Browser fixtures use isolated profiles
and synthetic pages; browser subprocess networking is not controlled by this
Python socket guard. Provider tests must still mock consequential operations.

The JSON coverage report distinguishes schema checks and observed dispatcher
calls. A dispatcher call may stop at a precondition or use a mocked provider:
it is not proof that an authenticated real-world operation succeeded. No live
operation is marked verified merely because its registration or tests exist.

Neo4j integration round trips are opt-in against a separate disposable instance
selected by `VANTA_TEST_NEO4J_URI`; they never run in the isolated audit. Ordinary
graph tests use mocks and fixtures. A skipped live integration is reported as
skipped, not passed.
