"""Payroll bureaus, part A: the model, rate overrides, and the scope gate.

A bureau runs payroll for its own client companies. Its staff use the operator
interface, but only over those companies, so the gate is deny-by-default: a
bureau user reaches nothing on the operator plane unless the route is marked as
open to bureaus, and even then every id in the request must belong to one of
the bureau's companies. A bureau may also keep its own statutory-rate versions,
used for its own clients only.
"""

import json
import os
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from flask_login import login_user  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import create_app, db  # noqa: E402
from app.bureau_scope import bureau_scoped, operator_query  # noqa: E402
from app.models import Bureau, ClientCompany, PayrollRun, StatutoryRate, User  # noqa: E402
from app.payroll_calculations import statutory_rate_for_run  # noqa: E402
from app.roles import BUREAU_ADMIN, BUREAU_OFFICER  # noqa: E402
from app.seed import seed_statutory_rates  # noqa: E402

BANDS = json.dumps([{"over": 0, "rate": 0.05, "base": 0}])


def _rate(effective_from, bureau=None, ssf=0.055):
    rate = StatutoryRate(
        effective_from=effective_from, bureau_id=bureau.id if bureau else None,
        ssf_employee_rate=ssf, ssf_employer_rate=0.13, paye_bands_json=BANDS,
        overtime_rate_low=0.05, overtime_rate_high=0.10, overtime_basic_threshold=0.5,
        bonus_rate=0.05, bonus_annual_basic_threshold=0.15,
    )
    db.session.add(rate)
    db.session.flush()
    return rate


class BureauTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        # A test-only operator route marked open to bureaus, so the central id
        # check can be exercised before any real route is opened.
        self.app.add_url_rule(
            "/_test/runs/<int:run_id>", "test_run_probe",
            bureau_scoped(lambda run_id: "probe ok"),
        )
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.bureau = Bureau(name="Test Bureau Ltd", code="TB")
        self.other_bureau = Bureau(name="Other Bureau Ltd", code="OB")
        db.session.add_all([self.bureau, self.other_bureau])
        db.session.flush()
        self.ours = ClientCompany(name="Bureau Client One", company_code="BC1", bureau_id=self.bureau.id)
        self.theirs = ClientCompany(name="Other Bureau Client", company_code="OBC", bureau_id=self.other_bureau.id)
        self.managed = ClientCompany.query.filter(ClientCompany.bureau_id.is_(None)).first()
        db.session.add_all([self.ours, self.theirs])
        db.session.flush()
        self.our_run = PayrollRun(client_company_id=self.ours.id, month="March", year=2031)
        self.their_run = PayrollRun(client_company_id=self.theirs.id, month="March", year=2031)
        db.session.add_all([self.our_run, self.their_run])
        self.staff = User(name="Bureau Admin", email="admin@testbureau.example",
                          role=BUREAU_ADMIN, bureau_id=self.bureau.id)
        self.staff.set_password("bureau-pass-1")
        db.session.add(self.staff)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email, password="password123"):
        self.client.get("/logout")
        self.assertEqual(
            self.client.post("/login", data={"email": email, "password": password}).status_code, 302
        )

    # --- rate versions ---------------------------------------------------------
    def test_default_versions_stay_unique_by_date(self):
        _rate(date(2031, 1, 1))
        with self.assertRaises(IntegrityError):
            _rate(date(2031, 1, 1))
        db.session.rollback()

    def test_a_bureau_may_override_a_default_date_but_only_once(self):
        _rate(date(2031, 1, 1))
        _rate(date(2031, 1, 1), self.bureau)
        _rate(date(2031, 1, 1), self.other_bureau)
        with self.assertRaises(IntegrityError):
            _rate(date(2031, 1, 1), self.bureau)
        db.session.rollback()

    def test_resolution_order(self):
        default_2031 = _rate(date(2031, 1, 1))
        override_2031 = _rate(date(2031, 1, 1), self.bureau, ssf=0.06)
        default_2032 = _rate(date(2032, 1, 1))
        db.session.commit()
        on = date(2031, 6, 1)
        self.assertEqual(StatutoryRate.active_for(on).id, default_2031.id, "no bureau, no overrides")
        self.assertEqual(StatutoryRate.active_for(on, self.bureau.id).id, override_2031.id,
                         "same date: the bureau's own version wins")
        self.assertEqual(StatutoryRate.active_for(on, self.other_bureau.id).id, default_2031.id)
        self.assertEqual(StatutoryRate.active_for(date(2032, 2, 1), self.bureau.id).id, default_2032.id,
                         "a newer default supersedes an older override")

    def test_a_run_uses_its_companys_bureau_rates(self):
        _rate(date(2031, 1, 1))
        override = _rate(date(2031, 1, 1), self.bureau, ssf=0.06)
        db.session.commit()
        self.assertEqual(statutory_rate_for_run(self.our_run).id, override.id)
        self.assertNotEqual(statutory_rate_for_run(self.their_run).id, override.id)

    def test_reseeding_ignores_and_keeps_overrides(self):
        override = _rate(date(2026, 9, 1), self.bureau, ssf=0.06)
        db.session.commit()
        defaults = StatutoryRate.query.filter(StatutoryRate.bureau_id.is_(None)).count()
        seed_statutory_rates()
        db.session.commit()
        self.assertEqual(StatutoryRate.query.filter(StatutoryRate.bureau_id.is_(None)).count(), defaults)
        self.assertEqual(db.session.get(StatutoryRate, override.id).ssf_employee_rate, 0.06)

    # --- a login is one scope ----------------------------------------------------
    def test_logins_cannot_mix_scopes(self):
        bad = [
            dict(role=BUREAU_OFFICER),                                         # no bureau
            dict(role="admin", bureau_id=self.bureau.id),                      # operator role
            dict(role=BUREAU_ADMIN, bureau_id=self.bureau.id,
                 client_company_id=self.ours.id),                              # both scopes
        ]
        for i, fields in enumerate(bad):
            with self.subTest(fields=fields):
                user = User(name="x", email=f"bad{i}@example.com", password_hash="x", **fields)
                db.session.add(user)
                with self.assertRaises((ValueError, IntegrityError)):
                    db.session.flush()
                db.session.rollback()

    # --- the gate -------------------------------------------------------------
    def test_bureau_user_reaches_no_unmarked_operator_page(self):
        self._login("admin@testbureau.example", "bureau-pass-1")
        for path in ("/dashboard", "/clients", f"/clients/{self.ours.id}", "/statutory-rates/",
                     f"/payroll/{self.our_run.id}", "/audit", "/notifications"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_marked_page_checks_every_id(self):
        self._login("admin@testbureau.example", "bureau-pass-1")
        self.assertEqual(self.client.get(f"/_test/runs/{self.our_run.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/_test/runs/{self.their_run.id}").status_code, 404)
        self.assertEqual(self.client.get("/_test/runs/999999").status_code, 404)
        self.assertEqual(
            self.client.get(f"/_test/runs/{self.our_run.id}?client_id={self.managed.id}").status_code, 404,
            "an id smuggled in the query string is checked too",
        )
        self.assertEqual(
            self.client.get(f"/_test/runs/{self.our_run.id}?run_ids={self.our_run.id}&run_ids={self.their_run.id}").status_code,
            404,
        )

    def test_payrolla_staff_are_not_affected(self):
        self._login("admin@payrolla.com")
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get(f"/_test/runs/{self.their_run.id}").status_code, 200)

    def test_operator_query_scopes_lists(self):
        with self.app.test_request_context():
            login_user(self.staff)
            self.assertEqual({c.id for c in operator_query(ClientCompany)}, {self.ours.id})
            self.assertEqual({r.id for r in operator_query(PayrollRun)}, {self.our_run.id})
        payrolla = User.query.filter_by(email="admin@payrolla.com").one()
        with self.app.test_request_context():
            login_user(payrolla)
            self.assertGreaterEqual(operator_query(ClientCompany).count(), 3)


if __name__ == "__main__":
    unittest.main()
