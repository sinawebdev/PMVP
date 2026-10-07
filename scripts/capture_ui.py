"""Capture named UI screenshots of the client portal, for design review.

Dev tooling. Playwright is deliberately NOT in ``requirements.txt`` — that file
is the exact pinned set Render installs into production, and a browser
automation library has no business on a payroll dyno. Install it locally per
CONTRIBUTING.md ("Screenshot tooling"), Chromium only.

Why this exists: headless Chrome's ``--screenshot`` flag renders one static
document and cannot click, so every interactive state (an open drawer, an open
menu, a focused control) was previously unreviewable — or, worse, reviewable
only by hand-editing the markup to fake the state, which proves nothing about
whether the control actually works. Here the drawer is opened by clicking the
toggle. If the toggle is broken the capture fails instead of lying.

Usage::

    .venv\\Scripts\\python.exe scripts/capture_ui.py                 # everything
    .venv\\Scripts\\python.exe scripts/capture_ui.py shell-drawer-open  # by name
    .venv\\Scripts\\python.exe scripts/capture_ui.py --list

Output goes to ``.screenshots/`` at the repo root, which is gitignored.

To add a capture, append a ``Capture`` to :data:`CAPTURES`. Do not add branching
to the runner — if a capture needs a new kind of step, add a verb to
:func:`_run_action` and keep the capture list declarative.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(REPO_ROOT, ".screenshots")

# The seeded tenant admin. Same identity the tests use.
DEMO_EMAIL = "admin@msc.com"
DEMO_PASSWORD = "password123"
# The seeded platform admin, for operator-console captures.
OPERATOR_EMAIL = "admin@payrolla.com"


# --- The capture list -------------------------------------------------------
# `actions` is a tuple of (verb, argument) pairs applied after the route loads
# and before the shot is taken. Verbs are implemented in _run_action.


@dataclass(frozen=True)
class Capture:
    name: str
    route: str
    viewport: tuple[int, int]
    actions: tuple[tuple[str, str], ...] = field(default=())
    full_page: bool = False
    # Who signs in first. Most captures are the client portal; the operator
    # console's own screens sign in as OPERATOR_EMAIL.
    email: str = DEMO_EMAIL


CAPTURES: tuple[Capture, ...] = (
    Capture(
        name="shell-1280",
        route="/company",
        viewport=(1280, 800),
    ),
    Capture(
        name="shell-800",
        route="/company",
        viewport=(800, 900),
    ),
    Capture(
        name="shell-drawer-open",
        route="/company",
        viewport=(800, 900),
        # Clicked, not class-injected: this is the assertion that the toggle
        # wires up. wait_for holds until the CSS transition has settled, so the
        # shot never catches the panel mid-slide.
        actions=(
            ("click", ".nav-toggle"),
            ("wait_for", ".portal-nav.open"),
            ("wait_for", ".nav-backdrop.show"),
            ("settle", "250"),
        ),
    ),
    Capture(
        name="shell-account-menu",
        route="/company",
        viewport=(1280, 800),
        actions=(
            ("click", ".utility-menu--account > summary"),
            ("wait_for", ".utility-menu--account[open] .utility-pop"),
            ("settle", "150"),
        ),
    ),
    # A tenant's 404 is a client page — you reach it by mistyping a URL inside
    # your own portal — and it used to render the OPERATOR shell. This capture
    # is the visual half of the regression test in tests/test_client_interface.
    Capture(
        name="shell-404-tenant",
        route="/company/no-such-page",
        viewport=(1280, 800),
    ),
    # SMS Phase 3: both send pages and the confirm step each one gains. The
    # seeded database holds one approved run, id 1.
    Capture(
        name="distribute-tenant",
        route="/company/runs/1/distribute",
        viewport=(1280, 900),
        full_page=True,
    ),
    Capture(
        name="distribute-confirm-tenant",
        route="/company/runs/1/distribute/confirm?channel=sms&action=send",
        viewport=(1280, 900),
        full_page=True,
    ),
    Capture(
        name="distribute-confirm-tenant-390",
        route="/company/runs/1/distribute/confirm?channel=sms&action=send",
        viewport=(390, 900),
        full_page=True,
    ),
    # Keyboard focus on the tenant page has to be visible (WCAG 1.4.11; see
    # plans/focus-indicator-contrast-defect.md). Tabbed to, not focused by
    # script, so the shot shows what a keyboard user sees.
    Capture(
        name="distribute-confirm-tenant-focus",
        route="/company/runs/1/distribute/confirm?channel=sms&action=send",
        viewport=(1280, 900),
        actions=(
            ("focus", ".ds-confirm-actions a.btn"),
            ("press", "Shift+Tab"),
            ("settle", "150"),
        ),
    ),
    Capture(
        name="run-delivery-operator",
        route="/distribution/run/1",
        viewport=(1280, 900),
        full_page=True,
        email=OPERATOR_EMAIL,
    ),
    Capture(
        name="run-delivery-confirm-operator",
        route="/distribution/run/1/confirm?channel=sms&action=send",
        viewport=(1280, 900),
        full_page=True,
        email=OPERATOR_EMAIL,
    ),
    # Bulk Distribute on the runs list stops at the same step, for its selection.
    Capture(
        name="bulk-confirm-operator",
        route="/distribution/runs/confirm?run_ids=1",
        viewport=(1280, 900),
        full_page=True,
        email=OPERATOR_EMAIL,
    ),
    Capture(
        name="bulk-confirm-operator-390",
        route="/distribution/runs/confirm?run_ids=1",
        viewport=(390, 900),
        full_page=True,
        email=OPERATOR_EMAIL,
    ),
)


def _run_action(page, verb: str, argument: str) -> None:
    """Apply one declarative step. Add verbs here, not branches in main()."""
    if verb == "click":
        page.click(argument)
    elif verb == "wait_for":
        page.wait_for_selector(argument, state="visible")
    elif verb == "hover":
        page.hover(argument)
    elif verb == "focus":
        page.focus(argument)
    elif verb == "press":
        page.keyboard.press(argument)
    elif verb == "settle":
        page.wait_for_timeout(int(argument))
    else:
        raise ValueError(f"unknown action verb: {verb!r}")


# --- App under test ---------------------------------------------------------


@contextmanager
def _serve(*, with_app=False):
    """Boot the app on an ephemeral port, seeded, in a background thread.

    Port 0 lets the OS pick a free one. That sidesteps the trap this project
    has hit before: on Windows two dev servers can both bind a fixed port, and
    requests then land on either process at random, which looks exactly like a
    code change that did not take effect.

    The database is a throwaway file in the system temp directory, so a capture
    run can never touch a developer's local database — and SKIP_DOTENV keeps it
    away from .env, which points at production.
    """
    workdir = tempfile.mkdtemp(prefix="payrolla-capture-")
    os.environ.update(
        SKIP_DOTENV="true",
        FLASK_SKIP_DOTENV="1",
        FLASK_ENV="development",
        SECRET_KEY="capture-ui-ephemeral-key",
        DATABASE_URL="sqlite:///" + os.path.join(workdir, "capture.db").replace("\\", "/"),
        SEED_DEMO_DATA="true",
        PERSISTENCE_REQUIRED="false",
        WTF_CSRF_ENABLED="false",
        SMS_BACKEND="console",
        DESKTOP_DOWNLOAD_URL="https://github.com/sinawebdev/PMVP/releases/download/desktop-v0.1.0-20261005/Payrolla-Desktop-Setup.exe",
    )
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import logging

    from werkzeug.serving import make_server

    from app import create_app

    # One page load pulls a dozen static files, so the request log buries the
    # only output that matters — which captures were written.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app = create_app()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        yield (base_url, app) if with_app else base_url
    finally:
        server.shutdown()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


def _login(page, base_url: str, email: str = DEMO_EMAIL) -> None:
    """Sign in through the real form, as the demo tenant unless told otherwise.

    Posting the session cookie directly would be faster and would skip the one
    thing worth confirming for free on every run: that the portal is reachable
    the way a user reaches it.
    """
    page.goto(f"{base_url}/login", wait_until="domcontentloaded")
    page.fill("#email", email)
    page.fill("#password", DEMO_PASSWORD)
    page.click("form button[type=submit], form input[type=submit], form .btn")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15000)


# --- Runner -----------------------------------------------------------------


def capture_all(selected: list[str] | None = None) -> int:
    wanted = [c for c in CAPTURES if not selected or c.name in selected]
    if selected:
        unknown = set(selected) - {c.name for c in CAPTURES}
        if unknown:
            print(f"unknown capture(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    from playwright.sync_api import sync_playwright

    written = 0
    with _serve() as base_url, sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for cap in wanted:
                width, height = cap.viewport
                # A fresh context per capture: one capture's open menu or
                # scroll position must never leak into the next one's shot.
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                )
                page = context.new_page()
                try:
                    _login(page, base_url, cap.email)
                    page.goto(f"{base_url}{cap.route}", wait_until="networkidle")
                    for verb, argument in cap.actions:
                        _run_action(page, verb, argument)
                    path = os.path.join(OUTPUT_DIR, f"{cap.name}.png")
                    page.screenshot(path=path, full_page=cap.full_page)
                    size = os.path.getsize(path)
                    print(f"  {cap.name:22} {width:>5}x{height:<5} {size:>8,} bytes  {cap.route}")
                    written += 1
                finally:
                    context.close()
        finally:
            browser.close()

    print(f"\n{written} screenshot(s) -> {OUTPUT_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="capture names; omit for all")
    parser.add_argument("--list", action="store_true", help="list capture names and exit")
    args = parser.parse_args()

    if args.list:
        for cap in CAPTURES:
            w, h = cap.viewport
            steps = " -> ".join(f"{v}({a})" for v, a in cap.actions) or "default"
            print(f"{cap.name:22} {w}x{h:<5} {cap.route:12} {steps}")
        return 0

    return capture_all(args.names or None)


if __name__ == "__main__":
    raise SystemExit(main())
