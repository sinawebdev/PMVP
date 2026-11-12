"""Payroll bureaus, part B: a bureau works the operator pages over its own clients only.

Bureau A, bureau B and Payrolla's own managed companies (the seeded MSC and
Acme) share one database. Signed in as bureau A, every list page must show A's
clients and nothing of B's or Payrolla's; every object of theirs must be a 404,
by URL and by form; and a POST aimed at one must change nothing. Payrolla's own
staff keep seeing everything. A coverage check makes every new operator route
choose: open to bureaus (and scoped) or Payrolla's alone.
"""

import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.bureau_scope import BUREAU_ENDPOINTS, OWNERSHIP_RESOLVERS, SHARED_ENDPOINTS  # noqa: E402
from app.events import platform_admins, record_event  # noqa: E402
from app.models import (  # noqa: E402
    AccessRequest,
    Bureau,
    ClientCompany,
    Employee,
    Notification,
    PayrollItem,
    PayrollRun,
    User,
)
from app.payroll_status import DRAFT  # noqa: E402
from app.roles import BUREAU_ADMIN  # noqa: E402

PASSWORD = "bureau-pass-1"

# Endpoints that are not on the operator plane at all.
NOT_OPERATOR_PREFIXES = ("client.", "legal.", "payslip_link.", "distribution_webhooks.",
                         "desktop_release.", "auth.")
NOT_OPERATOR = {"static", "main.health", "main.robots_txt", "main.sitemap_xml", "main.favicon_ico",
                "main.company_dashboard", "invites.accept", "access.submit"}
# Operator endpoints deliberately kept from bureaus. Adding a route means
# putting it in BUREAU_ENDPOINTS (and scoping it) or here, with a reason.
PAYROLLA_ONLY = {
    "access.detail", "access.contacted", "access.decline",           # applications to use Payrolla
    "licences.ledger", "licences.issue_for_request", "licences.resend",  # Payrolla's desktop licences
    "main.db_health", "main.db_health_json",                         # infrastructure
    "distribution.dashboard", "distribution.dashboard_fragment",     # worker health across every tenant
    "statutory.index", "statutory.new", "statutory.edit", "statutory.copy",  # default rates (bureau overrides: part C)
}


class BureauIsolationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.msc = ClientCompany.query.filter_by(company_code="MSC").one()
        self.msc_run = PayrollRun.query.filter_by(client_company_id=self.msc.id).first()
        self.msc_item = PayrollItem.query.filter_by(payroll_run_id=self.msc_run.id).first()
        self.msc_emp = Employee.query.filter_by(client_company_id=self.msc.id).first()

        self.bureau_a = Bureau(name="Alpha Payroll Services", code="ALPHA")
        self.bureau_b = Bureau(name="Beta Payroll Services", code="BETA")
        db.session.add_all([self.bureau_a, self.bureau_b])
        db.session.flush()
        self.a_co = ClientCompany(name="Alpha Client Widgets", company_code="ACW", bureau_id=self.bureau_a.id)
        self.b_co = ClientCompany(name="Beta Client Gadgets", company_code="BCG", bureau_id=self.bureau_b.id)
        db.session.add_all([self.a_co, self.b_co])
        db.session.flush()
        self.a_emp = Employee(staff_id="AW1", full_name="Alpha Worker Zebulon", client_company_id=self.a_co.id)
        self.b_emp = Employee(staff_id="BW1", full_name="Beta Worker Quentin", client_company_id=self.b_co.id)
        self.a_run = PayrollRun(client_company_id=self.a_co.id, month="March", year=2031, status=DRAFT)
        self.b_run = PayrollRun(client_company_id=self.b_co.id, month="March", year=2031, status=DRAFT)
        db.session.add_all([self.a_emp, self.b_emp, self.a_run, self.b_run])
        db.session.flush()
        self.b_item = PayrollItem(payroll_run_id=self.b_run.id, employee_id=self.b_emp.id,
                                  staff_id="BW1", full_name="Beta Worker Quentin")
        db.session.add(self.b_item)
        self.request = AccessRequest(company_name="Gamma Applicant Ltd", contact_name="G",
                                     email="g@example.com", interest="web")
        db.session.add(self.request)
        self.a_admin = User(name="Alpha Admin", email="admin@alpha.example", role=BUREAU_ADMIN,
                            bureau_id=self.bureau_a.id)
        self.a_admin.set_password(PASSWORD)
        db.session.add(self.a_admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _login(self, email, password="password123"):
        self.client.get("/logout")
        resp = self.client.post("/login", data={"email": email, "password": password})
        self.assertEqual(resp.status_code, 302)

    def _as_alpha(self):
        self._login("admin@alpha.example", PASSWORD)

    # --- every operator route has made its choice -----------------------------------
    def test_every_operator_endpoint_is_classified(self):
        unclassified = []
        for endpoint in self.app.view_functions:
            if endpoint.startswith(NOT_OPERATOR_PREFIXES) or endpoint in NOT_OPERATOR:
                continue
            if endpoint in BUREAU_ENDPOINTS or endpoint in PAYROLLA_ONLY or endpoint in SHARED_ENDPOINTS:
                continue
            unclassified.append(endpoint)
        self.assertEqual(unclassified, [], "open it to bureaus (and scope it) or add it to PAYROLLA_ONLY")
        self.assertFalse(BUREAU_ENDPOINTS & PAYROLLA_ONLY)

    def test_every_id_a_bureau_route_takes_has_an_owner_check(self):
        missing = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in BUREAU_ENDPOINTS:
                missing |= set(rule.arguments) - set(OWNERSHIP_RESOLVERS)
        self.assertEqual(missing, set())

    # --- list pages show only the bureau's own clients --------------------------------
    def test_list_pages_show_nothing_of_anyone_else(self):
        self._as_alpha()
        pages = ["/dashboard", "/clients", "/payroll/runs", "/payroll/runs/new", "/payslip",
                 "/distribution/history", "/distribution/analytics", "/oversight/risk", "/audit",
                 "/search?q=Worker", "/search?q=Client", "/notifications"]
        for path in pages:
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                for foreign in ("MSC Limited", "Acme Manufacturing", "Beta Client Gadgets",
                                "Beta Worker Quentin", "Gamma Applicant"):
                    self.assertNotIn(foreign, html)
        self.assertIn("Alpha Client Widgets", self.client.get("/clients").get_data(as_text=True))
        self.assertIn("Alpha Worker Zebulon", self.client.get("/search?q=Worker").get_data(as_text=True)
                      + self.client.get(f"/employees/clients/{self.a_co.id}/roster").get_data(as_text=True))

    def test_own_objects_open(self):
        self._as_alpha()
        for path in (f"/clients/{self.a_co.id}", f"/employees/clients/{self.a_co.id}/roster",
                     f"/payroll/runs/{self.a_run.id}", f"/payroll/runs?client_id={self.a_co.id}"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_anyone_elses_objects_are_404(self):
        self._as_alpha()
        for path in (
            f"/clients/{self.b_co.id}", f"/clients/{self.msc.id}", f"/clients/{self.msc.id}/edit",
            f"/employees/clients/{self.msc.id}/roster", f"/employees/clients/{self.b_co.id}/edit/{self.b_emp.id}",
            f"/employees/clients/{self.a_co.id}/edit/{self.msc_emp.id}",
            f"/payroll/runs/{self.b_run.id}", f"/payroll/runs/{self.msc_run.id}/export",
            f"/payroll/items/{self.msc_item.id}/payslip", f"/distribution/run/{self.msc_run.id}",
            f"/payroll/runs?client_id={self.msc.id}", f"/payslip?client_id={self.msc.id}",
            f"/distribution/history?company_id={self.msc.id}",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_posts_at_anyone_elses_objects_change_nothing(self):
        self._as_alpha()
        self.assertEqual(self.client.post(f"/payroll/runs/{self.b_run.id}/delete").status_code, 404)
        self.assertEqual(self.client.post(f"/employees/clients/{self.msc.id}/deactivate/{self.msc_emp.id}").status_code, 404)
        resp = self.client.post("/payroll/runs/bulk/reject",
                                data={"run_ids": [str(self.a_run.id), str(self.b_run.id)]})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.post("/audit/expenses", data={
            "title": "x", "amount": "10", "client_company_id": str(self.msc.id)}).status_code, 404)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(PayrollRun, self.b_run.id))
        self.assertEqual(db.session.get(PayrollRun, self.b_run.id).status, DRAFT)
        self.assertEqual(db.session.get(Employee, self.msc_emp.id).status, "Active")

    def test_payrolla_only_pages_are_404(self):
        self._as_alpha()
        for path in ("/clients/licences", "/admin/db-health", "/db-health", "/distribution/dashboard",
                     "/statutory-rates/", f"/clients/requests/{self.request.id}"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_a_bureaus_new_client_is_its_own(self):
        self._as_alpha()
        resp = self.client.post("/clients/add", data={
            "name": "Alpha Second Client", "company_code": "ASC", "status": "Active",
            "request": str(self.request.id),
        })
        self.assertEqual(resp.status_code, 302)
        created = ClientCompany.query.filter_by(company_code="ASC").one()
        self.assertEqual(created.bureau_id, self.bureau_a.id)
        self.assertTrue(db.session.get(AccessRequest, self.request.id).is_open,
                        "a bureau can't approve Payrolla's access requests")

    def test_name_clash_does_not_reveal_other_firms(self):
        self._as_alpha()
        resp = self.client.post("/clients/add", data={"name": "MSC Limited", "company_code": "MSCX",
                                                      "status": "Active"})
        self.assertNotIn("registered as MSC Limited", resp.get_data(as_text=True))

    def test_bureau_hears_about_its_clients(self):
        record_event("run.risk_held", summary="Held for review", subject=self.a_run,
                     recipients=platform_admins())
        record_event("run.risk_held", summary="Held for review", subject=self.b_run,
                     recipients=platform_admins())
        db.session.commit()
        notes = Notification.query.filter_by(user_id=self.a_admin.id).all()
        self.assertEqual({n.client_company_id for n in notes}, {self.a_co.id})

    # --- Payrolla staff are unaffected -------------------------------------------------
    def test_payrolla_staff_still_see_everything(self):
        self._login("admin@payrolla.com")
        html = self.client.get("/clients").get_data(as_text=True)
        for name in ("MSC Limited", "Alpha Client Widgets", "Beta Client Gadgets"):
            self.assertIn(name, html)
        self.assertEqual(self.client.get(f"/payroll/runs/{self.b_run.id}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
