"""SMS Phase 2: the short /s/<code> link and the one-part SMS that carries it.

A code is a bearer secret for one payslip, so the tests pin what makes it safe
to put in an SMS: only its hash is stored, it expires, it can be revoked two
ways, malformed guesses never reach the database, and guessing is rate limited.
The SMS body is pinned to one GSM-7 part whatever the company is called.
"""
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution import request_limit  # noqa: E402
from app.distribution.gsm7 import clean_for_sms, septets  # noqa: E402
from app.distribution.links import (  # noqa: E402
    PayslipLink,
    mint_payslip_link,
    resolve_payslip_link,
    revoke_code,
)
from app.distribution.render import render_payslip_sms  # noqa: E402
from app.distribution.service import distribute_run  # noqa: E402
from app.distribution.tokens import revoke_payslip_links  # noqa: E402
from app.models import PayrollRun, PayslipDelivery  # noqa: E402

BASE = "https://pmvp-v1.onrender.com"
OK_BODY = '{"success": true, "data": {"status": "queued", "task_id": "t"}}'


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _LinkCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, PUBLIC_BASE_URL=BASE)
        self.http = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        request_limit.reset()
        self.run = PayrollRun.query.filter_by(status="Approved").first()
        self.item = self.run.items[0]

    def tearDown(self):
        request_limit.reset()
        db.session.remove()
        self.ctx.pop()

    def mint(self, **kwargs):
        code = mint_payslip_link(self.item, **kwargs)
        db.session.commit()
        return code


class CodeStorageTests(_LinkCase):
    def test_a_code_is_twelve_base62_characters_and_only_its_hash_is_stored(self):
        code = self.mint()
        self.assertRegex(code, r"^[A-Za-z0-9]{12}$")
        link = PayslipLink.query.one()
        self.assertRegex(link.code_hash, r"^[0-9a-f]{64}$")
        stored = [str(getattr(link, c.name)) for c in PayslipLink.__table__.columns]
        self.assertFalse([value for value in stored if code in value])

    def test_codes_do_not_repeat(self):
        self.assertEqual(len({self.mint() for _ in range(20)}), 20)

    def test_it_expires_after_thirty_days(self):
        now = datetime.now(timezone.utc)
        code = self.mint(now=now)
        self.assertEqual(resolve_payslip_link(code, now=now + timedelta(days=29)), self.item)
        self.assertIsNone(resolve_payslip_link(code, now=now + timedelta(days=30, seconds=1)))

    def test_the_lifetime_follows_the_setting(self):
        self.app.config["PAYSLIP_SMS_LINK_DAYS"] = 7
        now = datetime.now(timezone.utc)
        code = self.mint(now=now)
        self.assertIsNone(resolve_payslip_link(code, now=now + timedelta(days=8)))


class RevocationTests(_LinkCase):
    def test_revoking_the_items_links_kills_its_short_codes(self):
        code = self.mint()
        revoke_payslip_links(self.item)
        db.session.commit()
        self.assertIsNone(resolve_payslip_link(code))

    def test_one_code_can_be_revoked_alone(self):
        first, second = self.mint(), self.mint()
        self.assertTrue(revoke_code(first))
        db.session.commit()
        self.assertIsNone(resolve_payslip_link(first))
        self.assertEqual(resolve_payslip_link(second), self.item)
        self.assertFalse(revoke_code(first))  # already revoked

    def test_resending_a_delivery_revokes_its_earlier_code(self):
        delivery = PayslipDelivery(payroll_item_id=self.item.id, payroll_run_id=self.run.id,
                                   channel="sms", status="failed")
        db.session.add(delivery)
        db.session.commit()
        old = self.mint(delivery=delivery)
        new = self.mint(delivery=delivery)
        self.assertIsNone(resolve_payslip_link(old))
        self.assertEqual(resolve_payslip_link(new), self.item)


class RouteTests(_LinkCase):
    def test_a_code_opens_the_payslip_and_its_pdf_uncached(self):
        code = self.mint()
        page = self.http.get(f"/s/{code}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.item.full_name, page.get_data(as_text=True))
        self.assertIn(f"/s/{code}/pdf", page.get_data(as_text=True))
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        pdf = self.http.get(f"/s/{code}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertEqual(pdf.headers["Cache-Control"], "no-store")

    def test_every_failure_is_the_same_404(self):
        live = self.mint()
        expired = self.mint(now=datetime.now(timezone.utc) - timedelta(days=31))
        revoked = self.mint()
        revoke_code(revoked)
        db.session.commit()
        expected = self.http.get(f"/s/{'A' * 12}").get_data(as_text=True)
        self.assertIn("no longer valid", expected)
        for code in (expired, revoked, "short", "x" * 13, "abc!defghijk", live.lower() + "Z"):
            with self.subTest(code=code):
                response = self.http.get(f"/s/{code}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.get_data(as_text=True), expected)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_a_malformed_code_never_reaches_the_database(self):
        with mock.patch("app.distribution.links.code_hash") as hashed:
            for code in ("short", "x" * 13, "abc!defghijk", "a b c d e f g", "ÀÀÀÀÀÀÀÀÀÀÀÀ"):
                self.assertEqual(self.http.get(f"/s/{code}").status_code, 404)
        hashed.assert_not_called()

    def test_too_many_requests_from_one_address_get_429(self):
        self.app.config["PAYSLIP_LINK_RATE_PER_MIN"] = 3
        code = self.mint()
        statuses = [self.http.get(f"/s/{code}").status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 429])
        limited = self.http.get(f"/s/{'B' * 12}")
        self.assertEqual(limited.status_code, 429)  # guesses count too
        self.assertEqual(limited.headers["Retry-After"], "60")
        other = self.http.get(f"/s/{code}", headers={"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual(other.status_code, 200)

    def test_the_signed_link_still_works_and_points_at_its_own_pdf(self):
        from app.distribution.tokens import issue_payslip_token

        token = issue_payslip_token(self.item)
        page = self.http.get(f"/p/{token}").get_data(as_text=True)
        self.assertIn(f"/p/{token}/pdf", page)


class RequestLimitTests(unittest.TestCase):
    def setUp(self):
        request_limit.reset()
        self.addCleanup(request_limit.reset)

    def test_a_sliding_minute_per_key(self):
        clock = [1000.0]
        allow = lambda key: request_limit.allow("b", key, 2, now=lambda: clock[0])  # noqa: E731
        self.assertEqual([allow("ip1"), allow("ip1"), allow("ip1")], [True, True, False])
        self.assertTrue(allow("ip2"))
        clock[0] += 61
        self.assertTrue(allow("ip1"))

    def test_zero_means_unlimited(self):
        self.assertTrue(all(request_limit.allow("b", "k", 0) for _ in range(100)))


class SmsBodyTests(unittest.TestCase):
    LINK = f"{BASE}/s/aB3dE5gH7jK9"  # 43 characters, as minted
    RUN = _Obj(month="September", year=2026)

    def body(self, name, link=LINK):
        return render_payslip_sms(self.RUN, _Obj(name=name), link)

    def test_the_wording(self):
        self.assertEqual(
            self.body("MSC Limited"),
            f"MSC Limited: your September 2026 payslip is ready. View (valid 30 days): {self.LINK}",
        )

    def test_always_one_gsm7_part(self):
        for name in (
            "MSC Limited",
            "Société Générale de Construction et de Travaux Publics du Ghana Limited",
            "“Kwame’s” Building – Supplies & Sons — Accra",
            "Ñandú Ölçer Ğıda Şirketi Ltd",
            "Acme {Holdings} [Ghana] ~ € Ltd " * 3,
            " Tema Port Services ",
            "🚀 中文公司",
        ):
            with self.subTest(name=name):
                body = self.body(name)
                self.assertIsNotNone(septets(body), "not GSM-7")
                self.assertLessEqual(septets(body), 160)
                self.assertTrue(body.endswith(self.LINK))
                self.assertNotIn("—", body)

    def test_the_name_gets_what_the_rest_leaves(self):
        body = self.body("X" * 200)
        self.assertEqual(septets(body), 160)

    def test_validity_follows_the_setting_and_reads_naturally(self):
        from flask import Flask

        app = Flask(__name__)
        with app.app_context():
            app.config["PAYSLIP_SMS_LINK_DAYS"] = 1
            self.assertIn("(valid 1 day):", self.body("MSC Limited"))
            app.config["PAYSLIP_SMS_LINK_DAYS"] = 7
            self.assertIn("(valid 7 days):", self.body("MSC Limited"))

    def test_cleanup_rules(self):
        self.assertEqual(clean_for_sms("“O’Neil”"), '"O\'Neil"')
        self.assertEqual(clean_for_sms("A – B — C"), "A - B - C")
        self.assertEqual(clean_for_sms("Société Générale"), "Societe Generale")
        self.assertEqual(clean_for_sms("Acme 🚀 Ltd\n"), "Acme Ltd")
        self.assertEqual(septets("[]"), 4)  # extension characters cost two


class SentSmsLinkTests(_LinkCase):
    def test_the_code_in_a_sent_sms_opens_that_payslip(self):
        self.app.config.update(
            SMS_BACKEND="sasusync", SMS_SENDER_ID="Payrolla", SASUSYNC_API_KEY="k",
            SASUSYNC_BASE_URL="https://sms.sasusync.com",
        )
        with mock.patch("app.distribution.sasusync._http_post",
                        return_value=(200, OK_BODY)) as post:
            distribute_run(self.run, channel="sms")
        bodies = [c.kwargs["json"]["message"] for c in post.call_args_list]
        self.assertTrue(bodies)
        for body in bodies:
            self.assertLessEqual(septets(body), 160)
            code = re.search(r"/s/([A-Za-z0-9]{12})$", body).group(1)
            self.assertIsNotNone(resolve_payslip_link(code))


if __name__ == "__main__":
    unittest.main()
