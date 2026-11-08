# Browser reliability

The existing Selenium/Firefox and Playwright engines remain in place. No new
provider, model, website integration or package is required.

## Changes

- Snapshot references belong to a particular DOM element and document. Filtered
  snapshots retain those references; navigation, replacement elements and other
  frames cannot accidentally reuse them. A missing reference is not interpreted
  as a fuzzy label or a different upload slot.
- Untargeted operations stay on the last selected working tab. Listing tabs
  does not change it. Click and submit receipts report newly opened tabs; one
  new popup becomes the working tab. Multiple popups require explicit selection.
- CSS and XPath selectors work through the existing click/type actions. Use
  `selector="css=#observed-id"` or `selector="xpath=..."`, or the same prefixed
  string as `ref`. Ambiguous explicit selectors do not choose an arbitrary match.
- Submission checks the target form's native constraints before dispatch and
  surfaces application-marked invalid fields afterward. An unrelated empty form
  does not block the target form. A page transition remains evidence of browser
  activity, not a guarantee of server acceptance.
- Custom dropdowns select exact option labels and require a retained value or
  selected state. A closed menu alone is not success.
- Native click recovery observes whether a click event already dispatched.
  A lost acknowledgement must not cause a second click on the same control.
- Requests that expire in the worker queue are skipped. Long form fills stop
  starting additional fields after their request expires.
- Task cancellation is carried to browser calls through a context-local scope.
  The caller stops waiting promptly; the worker checks cancellation before
  mutations, between form fields and during timed waits. An already-running
  WebDriver call can finish before control returns. An external submission
  already accepted cannot be undone by cancelling locally.
- Browser errors cannot be counted as successful action receipts.

## Verification

```powershell
$env:PYTHONPATH='D:\winter\.venv\Lib\site-packages;D:\winter'
$env:WINTER_BROWSER_IMPORT_COOKIES='0'
python -m pytest -q tests/test_browser_durability.py
```

The durability suite starts real Chromium and installed Firefox against a local
synthetic application, using temporary profiles and no personal cookies. It
checks form filling, file selection, submission, popups, tab continuity, stale
references, exact selectors, dropdowns, expired requests, lost click
acknowledgements and cancellation. It does not submit applications to real
portals or modify accounts.

Canvas-only editors still need the existing screenshot/desktop-control path.
Closed shadow roots, authentication challenges and site-specific server rules
cannot be inferred from DOM controls alone. Browser crashes do not justify
automatically replaying a submission whose outcome is unknown.
