"""Review real pages, keyboard controls and mobile overflow on disposable data.

Install Playwright per CONTRIBUTING.md, fetch fonts, then run this script.
No real payroll files, production database or sending provider is used.
Screenshots and the JSON report go to the gitignored .screenshots directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from capture_ui import DEMO_EMAIL, OPERATOR_EMAIL, OUTPUT_DIR, _login, _serve

PAGES = {
    "public": ("landing", "/", "login", "/login", "recovery", "/forgot-password"),
    "company": (
        "company-dashboard", "/company", "employees", "/company/employees",
        "employee-form", "/company/employees/add", "payroll", "/company/runs",
        "run", "/company/runs/1", "upload", "/company/runs/upload",
        "statutory", "/company/statutory", "expenses", "/company/expenses",
        "expense-form", "/company/expenses/add", "audit", "/company/audit",
        "branding", "/company/branding", "delivery", "/company/runs/1/distribute",
    ),
    "operator": (
        "operator-dashboard", "/dashboard", "clients", "/clients",
        "client-form", "/clients/add", "operator-runs", "/payroll/runs",
        "operator-run", "/payroll/runs/1", "operator-upload", "/payroll/runs/new",
        "reports", "/payroll/runs/1/reports", "rates", "/statutory-rates/",
        "rate-form", "/statutory-rates/new", "operator-audit", "/audit",
        "operator-delivery", "/distribution/run/1",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", help="optional page names for visual rechecks")
    args = parser.parse_args()
    output = Path(OUTPUT_DIR)
    output.mkdir(exist_ok=True)
    report = {"pages": [], "checks": [], "failures": []}

    def check(condition, description):
        report["checks"].append({"check": description, "passed": bool(condition)})
        if not condition:
            report["failures"].append(description)

    from playwright.sync_api import sync_playwright

    with _serve(with_app=True) as (base, app), sync_playwright() as pw:
        # Exercise real CSRF-protected login and form fields, not cookie injection.
        app.config["WTF_CSRF_ENABLED"] = True
        from app import db
        from app.models import PayrollItem
        from app.distribution.links import mint_payslip_link
        with app.app_context():
            item = PayrollItem.query.first()
            code = mint_payslip_link(item)
            db.session.commit()
        public_pages = PAGES["public"] + ("payslip", f"/s/{code}", "expired", "/s/invalid")
        browser = pw.chromium.launch()
        try:
            for width in (1280, 390):
                for actor, pairs in dict(PAGES, public=public_pages).items():
                    context = browser.new_context(viewport={"width": width, "height": 900})
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    if actor != "public":
                        _login(page, base, OPERATOR_EMAIL if actor == "operator" else DEMO_EMAIL)
                    for name, route in zip(pairs[::2], pairs[1::2]):
                        if args.only and name not in args.only:
                            continue
                        response = page.goto(base + route, wait_until="networkidle")
                        page.evaluate("document.fonts.ready")
                        if name == "landing":
                            page.wait_for_timeout(4500)
                        expected = 404 if name == "expired" else 200
                        check(response.status == expected, f"{name}-{width}: HTTP {expected}")
                        overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                        check(not overflow, f"{name}-{width}: no horizontal page overflow")
                        check(page.evaluate("document.fonts.check('14px Satoshi')"),
                              f"{name}-{width}: local Satoshi loaded")
                        missing_csrf = page.locator('form[method="post"]').evaluate_all(
                            "forms => forms.filter(f => !f.querySelector('input[name=csrf_token]')?.value).length")
                        check(missing_csrf == 0, f"{name}-{width}: POST forms have CSRF tokens")
                        if name == "landing":
                            check(page.locator('.marketing-close a[href$="Payrolla-Desktop-Setup.exe"]').count() == 1,
                                  f"landing-{width}: installer beside login")
                        page.screenshot(path=str(output / f"review-{name}-{width}.png"), full_page=True)
                        report["pages"].append({"name": name, "width": width, "status": response.status})
                        print(f"Reviewed {name}-{width}: {response.status}", flush=True)
                    check(not errors, f"{actor}-{width}: no uncaught JavaScript errors {errors}")
                    context.close()

            context = browser.new_context(viewport={"width": 390, "height": 900}, reduced_motion="reduce")
            page = context.new_page()
            page.goto(base, wait_until="networkidle")
            check(page.locator('.hero-mark video').count() == 0, "Reduced motion: static P, no video")
            check(page.locator('.hero-mark-still').evaluate("e => getComputedStyle(e).visibility") == "visible",
                  "Reduced motion: P remains visible")
            page.goto(base + "/login", wait_until="networkidle")
            page.locator("#password").fill("password123")
            page.get_by_role("button", name="Show", exact=True).click()
            check(page.locator("#password").get_attribute("type") == "text", "Password visibility: show")
            page.get_by_role("button", name="Hide", exact=True).click()
            check(page.locator("#password").get_attribute("type") == "password", "Password visibility: hide")
            _login(page, base)
            page.goto(base + "/company", wait_until="networkidle")
            toggle = page.locator(".nav-toggle")
            toggle.click()
            check(toggle.get_attribute("aria-expanded") == "true", "Mobile drawer opens")
            page.keyboard.press("Escape")
            check(toggle.get_attribute("aria-expanded") == "false", "Mobile drawer closes on Escape")
            check(toggle.evaluate("e => e === document.activeElement"), "Drawer returns focus to toggle")

            page.locator(".portal-logo").focus()
            page.evaluate("() => { window.payrollaConfirm('Review this demonstration', {requireTyped:'DEMO', danger:true}); }")
            typed = page.locator(".cn-modal-input")
            cancel = page.locator(".cn-modal-actions button").first
            confirm = page.locator(".cn-modal-actions button").last
            check(typed.evaluate("e => e === document.activeElement"), "Typed confirmation focuses input")
            check(confirm.is_disabled(), "Typed confirmation starts disabled")
            page.keyboard.press("Tab")
            check(cancel.evaluate("e => e === document.activeElement"), "Tab moves from typed input to Cancel")
            page.keyboard.press("Tab")
            check(typed.evaluate("e => e === document.activeElement"), "Tab wraps past disabled confirm")
            typed.fill("DEMO")
            check(confirm.is_enabled(), "Exact typed value enables confirmation")
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            check(confirm.evaluate("e => e === document.activeElement"), "Enabled confirm reachable by keyboard")
            page.keyboard.press("Tab")
            check(typed.evaluate("e => e === document.activeElement"), "Tab wraps from Confirm to input")
            page.keyboard.press("Escape")
            page.wait_for_selector(".cn-modal", state="detached")
            check(page.locator(".portal-logo").evaluate("e => e === document.activeElement"),
                  "Cancel returns focus without submitting an action")
            context.close()
        finally:
            browser.close()
    (output / "interface-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{len(report['pages'])} page views; {len(report['checks'])} checks; {len(report['failures'])} failures")
    for failure in report["failures"]:
        print("FAIL:", failure)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
