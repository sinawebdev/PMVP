"""GATE 8 — Phase 8 cleanup: retire the old qtarpay raw path + pre-merge guards.

Asserts the old qtarpay uploader (routes + UI) is fully gone and the `/raw`
blueprint is the sole raw entry point, that the survivors (`normalise_emp_id`,
the `RawPayEntry` model) are intact, and that the `pay_type` change guard warns,
previews the resulting basic, and never silently zeroes pay.

These tests deliberately need no decrypted DZ fixture — they build their own
minimal roster — so they run in every environment.
"""
import os
import re
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app, db
from app.models import ClientCompany, Employee, RawPayEntry, WageRateProfile

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "templates")

# The retired qtarpay endpoints — none of these may exist anywhere anymore.
RETIRED_ENDPOINTS = {
    "payroll.raw_upload",
    "payroll.raw_confirm",
    "payroll.raw_upload_new",
    "payroll.raw_confirm_new",
}


class OldRawPathRetiredTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )

    def test_retired_endpoints_absent_from_url_map(self):
        endpoints = {r.endpoint for r in self.app.url_map.iter_rules()}
        for ep in RETIRED_ENDPOINTS:
            self.assertNotIn(ep, endpoints, f"{ep} should have been removed")

    def test_raw_engine_blueprint_is_the_sole_raw_entry_point(self):
        # Every route whose path lives under /raw belongs to the new blueprint,
        # and the seed/thin upload + confirm are present and intact.
        raw_rules = [r for r in self.app.url_map.iter_rules() if str(r).startswith("/raw")]
        self.assertTrue(raw_rules, "the /raw blueprint should still be mounted")
        for rule in raw_rules:
            self.assertTrue(
                rule.endpoint.startswith("raw_engine."),
                f"{rule.endpoint} serves a /raw path but is not on the raw_engine blueprint",
            )
        endpoints = {r.endpoint for r in self.app.url_map.iter_rules()}
        for ep in ("raw_engine.upload", "raw_engine.confirm", "raw_engine.download_template",
                   "raw_engine.run_exports", "raw_engine.download_archive"):
            self.assertIn(ep, endpoints)

    def test_runs_page_renders_without_the_raw_upload_form(self):
        resp = self.http.get("/payroll/runs")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("raw-upload-form", body)
        self.assertNotIn("Raw Data Upload", body)
        self.assertNotIn("Parse &amp; Preview", body)

    def test_no_template_references_a_retired_endpoint(self):
        # A dead url_for('payroll.raw_*') would raise at render time — scan every
        # template so no page can break, not just the ones we happened to open.
        dead = re.compile(r"url_for\(\s*['\"]payroll\.raw_")
        offenders = []
        for root, _dirs, files in os.walk(TEMPLATES_DIR):
            for name in files:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    if dead.search(handle.read()):
                        offenders.append(path)
        self.assertEqual(offenders, [], f"templates still reference retired endpoints: {offenders}")


class SurvivorsIntactTests(unittest.TestCase):
    """The must-not-break pieces: normalise_emp_id and the RawPayEntry model."""

    def test_normalise_emp_id_still_importable_and_correct(self):
        from app.raw_import import normalise_emp_id

        self.assertEqual(normalise_emp_id("DZ 048"), "DZ048")
        self.assertEqual(normalise_emp_id("dcl 9"), "DCL9")

    def test_normalise_emp_id_dependents_all_import(self):
        # Every module the prompt lists as a dependent must still load and reach
        # normalise_emp_id (raw_engine.cleaning re-exports it).
        import importlib

        for modname in (
            "app.distribution.service",
            "app.employees",
            "app.payroll",
            "app.payroll_calculations.hourly",
            "app.raw_engine.cleaning",
        ):
            importlib.import_module(modname)
        from app.raw_engine.cleaning import normalise_emp_id as cleaning_norm

        self.assertEqual(cleaning_norm("DZ 048"), "DZ048")

    def test_raw_pay_entry_model_still_present(self):
        # Model import + a real table so existing raw data is never orphaned.
        self.assertEqual(RawPayEntry.__tablename__, "raw_pay_entries")
        app = create_app()
        with app.app_context():
            self.assertIn("raw_pay_entries", db.inspect(db.engine).get_table_names())


class PayTypeChangeGuardTests(unittest.TestCase):
    """GOAL 4 — a pay_type change that zeroes/materially changes basic must warn,
    confirm, and preview the resulting basic. No silent zeroing."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.http = self.app.test_client()
        self.http.post(
            "/login", data={"email": "admin@chrisnat.local", "password": "password123"}
        )
        with self.app.app_context():
            client = ClientCompany(name="DZ GUARD CO", status="Active")
            db.session.add(client)
            db.session.commit()
            self.cid = client.id
            # George: salaried, flat basic 1800, NO hourly basic rate on file.
            george = Employee(
                staff_id="DZ048", full_name="GEORGE AKOTO", client_company_id=self.cid,
                basic_salary=1800, pay_type="salaried", status="Active",
            )
            # Richard: genuinely hourly, carries a basic hourly rate profile.
            richard = Employee(
                staff_id="DCL9", full_name="RICHARD WOODE", client_company_id=self.cid,
                basic_salary=0, pay_type="salaried", status="Active",
            )
            db.session.add_all([george, richard])
            db.session.commit()
            self.george_id = george.id
            self.richard_id = richard.id
            db.session.add(
                WageRateProfile(
                    client_company_id=self.cid, employee_id=richard.id,
                    pay_code="ABNH01", hourly_rate=12.5,
                    category=WageRateProfile.CATEGORY_BASIC,
                )
            )
            db.session.commit()

    def _edit_url(self, emp_id):
        return f"/employees/clients/{self.cid}/edit/{emp_id}"

    def test_salaried_to_hourly_no_rate_warns_and_does_not_zero(self):
        # Unconfirmed salaried->hourly for a worker with no hourly basic rate.
        resp = self.http.post(
            self._edit_url(self.george_id),
            data={"full_name": "GEORGE AKOTO", "pay_type": "hourly"},
        )
        self.assertEqual(resp.status_code, 200)  # re-render, NOT a redirect/commit
        body = resp.get_data(as_text=True)
        self.assertIn("Confirm pay type change", body)
        self.assertIn("GH₵0.00", body)  # the resulting basic is previewed
        self.assertIn('name="confirm_pay_type" value="1"', body)
        with self.app.app_context():  # nothing was committed
            self.assertEqual(db.session.get(Employee, self.george_id).pay_type, "salaried")

    def test_confirmed_change_is_applied(self):
        resp = self.http.post(
            self._edit_url(self.george_id),
            data={"full_name": "GEORGE AKOTO", "pay_type": "hourly", "confirm_pay_type": "1"},
        )
        self.assertEqual(resp.status_code, 302)  # committed -> redirect to roster
        with self.app.app_context():
            self.assertEqual(db.session.get(Employee, self.george_id).pay_type, "hourly")

    def test_hourly_worker_correction_previews_derived_basic(self):
        # Richard genuinely hourly: salaried->hourly still confirms (material) but
        # is NOT a zeroing — the preview says basic is derived from hours × rate.
        resp = self.http.post(
            self._edit_url(self.richard_id),
            data={"full_name": "RICHARD WOODE", "pay_type": "hourly"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Confirm pay type change", body)
        self.assertIn("hours × rate", body)
        with self.app.app_context():
            self.assertEqual(db.session.get(Employee, self.richard_id).pay_type, "salaried")

    def test_non_paytype_edit_does_not_trigger_the_guard(self):
        # Changing an unrelated field (same pay_type) must not pop the confirm.
        resp = self.http.post(
            self._edit_url(self.george_id),
            data={"full_name": "GEORGE A", "pay_type": "salaried"},
        )
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            emp = db.session.get(Employee, self.george_id)
            self.assertEqual(emp.pay_type, "salaried")
            self.assertEqual(emp.full_name, "GEORGE A")


if __name__ == "__main__":
    unittest.main()
