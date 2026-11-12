"""Payroll bureaus, part C: a bureau keeps its own statutory-rate versions.

Chrisnat's MD asked to vary rates for the companies he runs payroll for. A
bureau admin sees Payrolla's defaults read-only and adds or edits versions that
apply to the bureau's own clients and nobody else's. Payrolla staff keep the
defaults and never touch a bureau's versions; another bureau never sees them;
the bureau's clients see the rates that actually apply to them.
"""

import json
import os
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import Bureau, ClientCompany, PayrollRun, StatutoryRate, User  # noqa: E402
from app.payroll_calculations import statutory_rate_for_run  # noqa: E402
from app.payroll_status import APPROVED, DRAFT  # noqa: E402
from app.roles import BUREAU_ADMIN, BUREAU_OFFICER, CLIENT_ADMIN  # noqa: E402
from app.statutory import rate_locks  # noqa: E402

BANDS = [(588, 5), (668, 10), (768, 17.5), (3668, 25), (19668, 30), (50000, 35)]
PASSWORD = "bureau-pass-1"


def _form(**overrides):
    data = {
        "effective_from": "2031-01-01",
        "ssf_employee_rate": "0.06", "ssf_employer_rate": "0.13",
        "overtime_rate_low": "0.05", "overtime_rate_high": "0.10",
        "overtime_basic_threshold": "0.5", "bonus_rate": "0.05",
        "bonus_annual_basic_threshold": "0.15", "overtime_junior_monthly_threshold": "1500",
        "icu_member_rate": "0.03", "ssnit_limits_year": "2026",
        "ssnit_min_insurable": "587.80", "ssnit_max_insurable": "69000",
        "ssnit_min_monthly_remittance": "79.40", "provident_fund_tax_limit": "0.165",
        "overtime_qualifying_annual_limit": "18000", "notes": "Bureau version",
        "band_over": [str(o) for o, _ in BANDS], "band_rate": [str(r) for _, r in BANDS],
    }
    data.update(overrides)
    return data


class BureauRatesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.bureau = Bureau(name="Alpha Payroll Services", code="ALPHA")
        self.other = Bureau(name="Beta Payroll Services", code="BETA")
        db.session.add_all([self.bureau, self.other])
        db.session.flush()
        self.company = ClientCompany(name="Alpha Client Widgets", company_code="ACW", bureau_id=self.bureau.id)
        db.session.add(self.company)
        db.session.flush()
        self.users = {}
        for email, role, fields in (
            ("admin@alpha.example", BUREAU_ADMIN, {"bureau_id": self.bureau.id}),
            ("officer@alpha.example", BUREAU_OFFICER, {"bureau_id": self.bureau.id}),
            ("admin@beta.example", BUREAU_ADMIN, {"bureau_id": self.other.id}),
            ("owner@widgets.example", CLIENT_ADMIN, {"client_company_id": self.company.id}),
        ):
            user = User(name=email, email=email, role=role, **fields)
            user.set_password(PASSWORD)
            db.session.add(user)
            self.users[email] = user
        self.default = StatutoryRate.query.filter_by(bureau_id=None).order_by(
            StatutoryRate.effective_from.desc()).first()
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email, password=PASSWORD):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": password})
        self.assertEqual(resp.status_code, 302)

    def _override(self, bureau=None, effective=date(2031, 1, 1)):
        rate = StatutoryRate(
            effective_from=effective, bureau_id=(bureau or self.bureau).id,
            ssf_employee_rate=0.06, ssf_employer_rate=0.13,
            paye_bands_json=json.dumps([{"over": 0, "rate": 0.05, "base": 0}]),
            overtime_rate_low=0.05, overtime_rate_high=0.10, overtime_basic_threshold=0.5,
            bonus_rate=0.05, bonus_annual_basic_threshold=0.15, notes="override",
        )
        db.session.add(rate)
        db.session.commit()
        return rate

    # --- a bureau adds and edits its own versions ----------------------------------
    def test_bureau_admin_adds_a_version_for_its_own_clients(self):
        self._login("admin@alpha.example")
        self.assertEqual(self.client.get("/statutory-rates/").status_code, 200)
        resp = self.client.post("/statutory-rates/new", data=_form(bureau_id=str(self.other.id)))
        self.assertEqual(resp.status_code, 302)
        created = StatutoryRate.query.filter_by(notes="Bureau version").one()
        self.assertEqual(created.bureau_id, self.bureau.id, "stamped from the user, not the form")
        run = PayrollRun(client_company_id=self.company.id, month="March", year=2031, status=DRAFT)
        db.session.add(run)
        db.session.commit()
        self.assertEqual(statutory_rate_for_run(run).id, created.id)

    def test_bureau_edits_its_own_version_but_never_a_default(self):
        mine = self._override()
        self._login("admin@alpha.example")
        resp = self.client.post(f"/statutory-rates/{mine.id}/edit", data=_form(ssf_employee_rate="0.065"))
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(StatutoryRate, mine.id).ssf_employee_rate, 0.065)
        self.assertEqual(self.client.get(f"/statutory-rates/{self.default.id}/edit").status_code, 404)
        self.client.post(f"/statutory-rates/{self.default.id}/edit", data=_form(ssf_employee_rate="0.09"))
        db.session.expire_all()
        self.assertNotEqual(db.session.get(StatutoryRate, self.default.id).ssf_employee_rate, 0.09)
        self.assertEqual(self.client.get(f"/statutory-rates/{self.default.id}/copy").status_code, 200,
                         "a default can be copied as the bureau's own version")

    def test_another_bureaus_versions_are_invisible(self):
        theirs = self._override(bureau=self.other)
        self._login("admin@alpha.example")
        self.assertNotIn(f"/statutory-rates/{theirs.id}/", self.client.get("/statutory-rates/").get_data(as_text=True))
        for path in (f"/statutory-rates/{theirs.id}/edit", f"/statutory-rates/{theirs.id}/copy"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_payrolla_staff_never_edit_a_bureaus_version(self):
        mine = self._override()
        self._login("admin@payrolla.com", "password123")
        self.assertNotIn(f"/statutory-rates/{mine.id}/", self.client.get("/statutory-rates/").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/statutory-rates/{mine.id}/edit").status_code, 404)

    def test_officers_cannot_manage_rates(self):
        self._login("officer@alpha.example")
        resp = self.client.get("/statutory-rates/")
        self.assertEqual(resp.status_code, 302)

    def test_lock_follows_the_bureaus_runs(self):
        mine = self._override()
        db.session.add(PayrollRun(client_company_id=self.company.id, month="April", year=2031, status=APPROVED))
        db.session.commit()
        locks = rate_locks()
        self.assertIn(mine.id, locks, "an unstamped approved run of the bureau's client used its version")
        self._login("admin@alpha.example")
        self.assertEqual(self.client.get(f"/statutory-rates/{mine.id}/edit").status_code, 302)

    def test_the_bureaus_clients_see_the_rates_that_apply_to_them(self):
        # A version of the bureau's that starts after the newest default is the
        # one in force for its clients; another bureau's newer one is not.
        self._override(effective=date(2026, 10, 1))
        self._override(bureau=self.other, effective=date(2026, 11, 1))
        self._login("owner@widgets.example")
        resp = self.client.get("/company/statutory")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("2026-10-01", html)
        self.assertNotIn("2026-11-01", html)
        self.assertIn("maintained by Alpha Payroll Services", html)

if __name__ == "__main__":
    unittest.main()
