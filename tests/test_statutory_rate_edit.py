"""Statutory rate versions are editable until an approved run depends on them.

The bureau's MD wants to vary rates himself. Editing in place is safe only
while no approved or processed payroll uses the version: after that, changing
it would silently rewrite pay someone signed off. So a used version is locked
and offers "Copy as a new version" instead, an approved run always keeps the
version it was stamped with, and the start-up re-seed never undoes an edit.
"""

import json
import os
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import AuditTrail, ClientCompany, PayrollRun, StatutoryRate  # noqa: E402
from app.payroll_calculations import statutory_rate_for_run  # noqa: E402
from app.payroll_status import APPROVED, DRAFT, PROCESSED  # noqa: E402
from app.seed import seed_statutory_rates  # noqa: E402
from app.statutory import rate_locks  # noqa: E402

BANDS = [(588, 5), (668, 10), (768, 17.5), (3668, 25), (19668, 30), (50000, 35)]


class RateEditTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.company = ClientCompany.query.first()
        self.version = self._version(date(2031, 1, 1))
        self._login("admin@payrolla.com")

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # --- helpers ------------------------------------------------------------
    def _login(self, email):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(resp.status_code, 302)

    def _version(self, effective_from, ssf_employee=0.055):
        rate = StatutoryRate(
            effective_from=effective_from, ssf_employee_rate=ssf_employee, ssf_employer_rate=0.13,
            paye_bands_json=json.dumps([{"over": 0, "rate": 0.05, "base": 0}]),
            overtime_rate_low=0.05, overtime_rate_high=0.10, overtime_basic_threshold=0.5,
            bonus_rate=0.05, bonus_annual_basic_threshold=0.15, notes="test version",
        )
        db.session.add(rate)
        db.session.commit()
        return rate

    def _run(self, month, year, status, stamped=None):
        run = PayrollRun(client_company_id=self.company.id, month=month, year=year,
                         status=status, statutory_rate_id=stamped)
        db.session.add(run)
        db.session.commit()
        return run

    def _form(self, **overrides):
        data = {
            "effective_from": "2031-01-01",
            "ssf_employee_rate": "0.06", "ssf_employer_rate": "0.13",
            "overtime_rate_low": "0.05", "overtime_rate_high": "0.10",
            "overtime_basic_threshold": "0.5", "bonus_rate": "0.05",
            "bonus_annual_basic_threshold": "0.15", "overtime_junior_monthly_threshold": "1500",
            "icu_member_rate": "0.03", "ssnit_limits_year": "2026",
            "ssnit_min_insurable": "587.80", "ssnit_max_insurable": "69000",
            "ssnit_min_monthly_remittance": "79.40", "provident_fund_tax_limit": "0.165",
            "overtime_qualifying_annual_limit": "18000", "notes": "edited",
            "band_over": [str(o) for o, _ in BANDS], "band_rate": [str(r) for _, r in BANDS],
        }
        data.update(overrides)
        return data

    def _reload(self):
        db.session.expire_all()
        return db.session.get(StatutoryRate, self.version.id)

    # --- editing an unused version -------------------------------------------
    def test_unused_version_can_be_edited_and_the_change_is_audited(self):
        self.assertEqual(self.client.get(f"/statutory-rates/{self.version.id}/edit").status_code, 200)
        resp = self.client.post(f"/statutory-rates/{self.version.id}/edit", data=self._form())
        self.assertEqual(resp.status_code, 302)
        rate = self._reload()
        self.assertEqual(rate.ssf_employee_rate, 0.06)
        self.assertEqual(len(rate.paye_bands), 6)
        self.assertIsNotNone(rate.updated_at)
        self.assertIsNotNone(rate.updated_by)
        audit = AuditTrail.query.filter_by(action="Statutory rate version edited").one()
        self.assertIn("SSF employee rate: 0.055 -> 0.06", audit.notes)

    def test_effective_date_cannot_be_moved(self):
        self.client.post(f"/statutory-rates/{self.version.id}/edit",
                         data=self._form(effective_from="2030-06-01"))
        self.assertEqual(self._reload().effective_from, date(2031, 1, 1))

    def test_a_bad_value_rerenders_and_saves_nothing(self):
        resp = self.client.post(f"/statutory-rates/{self.version.id}/edit",
                                data=self._form(ssf_employee_rate="5"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("SSF rates must be fractions", resp.get_data(as_text=True))
        self.assertEqual(self._reload().ssf_employee_rate, 0.055)

    def test_saving_lists_open_runs_that_need_recalculating(self):
        self._run("February", 2031, DRAFT, stamped=self.version.id)
        resp = self.client.post(f"/statutory-rates/{self.version.id}/edit", data=self._form(),
                                follow_redirects=True)
        self.assertIn("February 2031", resp.get_data(as_text=True))
        self.assertIn("Recalculate", resp.get_data(as_text=True))

    # --- locking --------------------------------------------------------------
    def test_a_version_an_approved_run_uses_is_locked(self):
        self._run("March", 2031, APPROVED, stamped=self.version.id)
        self.assertIn(self.version.id, rate_locks())
        resp = self.client.get(f"/statutory-rates/{self.version.id}/edit")
        self.assertEqual(resp.status_code, 302)
        self.client.post(f"/statutory-rates/{self.version.id}/edit", data=self._form())
        self.assertEqual(self._reload().ssf_employee_rate, 0.055)
        listing = self.client.get("/statutory-rates/").get_data(as_text=True)
        self.assertIn(f"/statutory-rates/{self.version.id}/copy", listing)

    def test_an_unstamped_processed_run_in_the_window_also_locks_it(self):
        # Runs calculated before the stamp existed have a NULL statutory_rate_id.
        self._run("April", 2031, PROCESSED, stamped=None)
        self.assertIn(self.version.id, rate_locks())

    def test_a_draft_run_does_not_lock_it(self):
        self._run("May", 2031, DRAFT, stamped=self.version.id)
        self.assertNotIn(self.version.id, rate_locks())

    def test_copy_prefills_the_add_form(self):
        resp = self.client.get(f"/statutory-rates/{self.version.id}/copy")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('action="/statutory-rates/new"', html)
        self.assertIn('value="0.055"', html)
        self.assertNotIn('value="2031-01-01"', html, "the copy needs its own start date")

    # --- an approved run keeps the version it was approved with ----------------
    def test_closed_run_keeps_its_stamped_version_after_a_backdated_one(self):
        run = self._run("June", 2031, APPROVED, stamped=self.version.id)
        backdated = self._version(date(2031, 6, 1), ssf_employee=0.07)
        self.assertEqual(statutory_rate_for_run(run).id, self.version.id)
        draft = self._run("June", 2031, DRAFT)
        self.assertEqual(statutory_rate_for_run(draft).id, backdated.id)

    # --- who can edit -------------------------------------------------------------
    def test_roles_outside_statutory_roles_cannot_edit(self):
        # role_required answers a denied role with a redirect to the dashboard.
        self._login("payroll@payrolla.com")
        resp = self.client.get(f"/statutory-rates/{self.version.id}/edit")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/statutory-rates", resp.headers["Location"])
        self.client.post(f"/statutory-rates/{self.version.id}/edit", data=self._form())
        self._login("admin@msc.com")
        self.assertEqual(self.client.get(f"/statutory-rates/{self.version.id}/edit").status_code, 302)
        self.client.post(f"/statutory-rates/{self.version.id}/edit", data=self._form())
        self.assertEqual(self._reload().ssf_employee_rate, 0.055)

    # --- the start-up re-seed never undoes an edit ---------------------------------
    def test_reseeding_keeps_edited_versions(self):
        september = StatutoryRate.query.filter_by(effective_from=date(2026, 9, 1)).one()
        september.ssf_employee_rate = 0.0575
        db.session.commit()
        count = StatutoryRate.query.count()
        seed_statutory_rates()
        db.session.commit()
        seed_statutory_rates()
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(StatutoryRate.query.count(), count)
        self.assertEqual(
            StatutoryRate.query.filter_by(effective_from=date(2026, 9, 1)).one().ssf_employee_rate,
            0.0575,
        )


if __name__ == "__main__":
    unittest.main()
