# Shell tests assert CSS class names, not behaviour — latent brittleness

- **Status**: NOT URGENT — both tests pass today and guard something real
- **Where**: `tests/test_client_reports.py:80`, `tests/test_client_run_upload.py:264-265`
- **Trigger to act**: the next structural rename of the tenant shell, or the
  next time one of these fails for a reason that is not a real regression
- **Logged during**: the client dashboard redesign (Step 1, horizontal nav),
  which is what surfaced it

## What the tests do

Both assert a CSS class name as a stand-in for "this page rendered inside the
tenant chrome":

```python
# tests/test_client_reports.py:80
# Shared tenant chrome (the top-nav shell).
self.assertIn("portal-topbar", html)

# tests/test_client_run_upload.py:264-265
# Shared design language: the tenant shell's own chrome, not a bare page.
self.assertIn("portal-topbar", html)
self.assertIn("portal-shell", html)
```

## Why this is on a list at all

The intent is sound. Both pages had previously rendered outside the shell, and
`test_client_run_upload` is an explicit regression guard for CSRF wiring that
only exists because the shell provides it. The pages must stay in the chrome.

But a class name is not the behaviour. These assertions already had to be
edited once, during Step 1, purely because the sidebar became a top bar — the
pages never stopped rendering inside the shell, and no user-visible contract
changed. They previously read `portal-sidebar`. That is a test failing for a
reason unrelated to what it protects, which is the failure mode that teaches a
reviewer to edit the assertion rather than read it.

It will happen again on the next rename, and the edit is invisible in review:
changing one string to another string looks identical whether the shell is
intact or gutted.

## What to assert instead

The behaviour the shell actually guarantees is that a client page can reach
every primary destination. That is stable across any amount of restyling and
would have needed no edit in Step 1:

```python
NAV_DESTINATIONS = [
    "/company", "/company/employees", "/company/runs",
    "/company/statutory", "/company/expenses", "/company/audit",
]

def assertRendersInTenantShell(self, html):
    for href in NAV_DESTINATIONS:
        self.assertIn(f'href="{href}"', html)
```

`test_client_run_upload`'s CSRF half is already behavioural (`name="csrf-token"`
and `app.js`) and needs no change.

Note the one thing a href-based check does NOT catch: a page that renders the
nav markup while the shell's stylesheet is missing. If that matters, assert the
stylesheet link rather than a class inside it — `portal.css` is a filename, and
a filename is a contract in a way an internal class name is not.

## Do not fix by deleting

Dropping the assertion entirely would be worse than the brittleness. Both pages
regressed out of the shell before; that is why the guards exist.
