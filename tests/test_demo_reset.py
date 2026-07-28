"""Operations & Onboarding, Phase 5 — demo data reset & professional tenants.

Two things must hold after ``flask demo-reset``:

  * the database contains ONLY the two professional tenants and the professional
    platform roster, with no orphaned child rows left behind by the deletions;
  * every one of those tenants is populated — employees, payroll runs across
    several months, expenses, deliveries — so no dashboard a demo reaches is
    empty.
"""

import os
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.demo_data import DEFAULT_MONTHS, reset_demo_data  # noqa: E402
from app.models import (  # noqa: E402
    DELIVERY_SENT,
    AuditTrail,
    ClientCompany,
    DomainEvent,
    Employee,
    Expense,
    Notification,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    User,
)
from app.payroll_status import PROCESSED  # noqa: E402

PROFESSIONAL_TENANTS = {"MSC Limited", "Acme Manufacturing Ltd"}

# Months of history the fixture builds (see setUp).
MONTHS = 4


class DemoResetTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._seed_junk()
        # Four months: long enough that the newest three in-flight states
        # leave exactly one CLOSED run behind, short enough to stay fast.
        self.summary = reset_demo_data(months=MONTHS)

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _seed_junk(self):
        """A throwaway tenant with a full set of child rows, plus an obsolete
        platform login that owns some history — exactly the shape the reset has
        to dismantle without leaving anything dangling."""
        junk = ClientCompany(name="Test Co 3 DELETEME", company_code="JUNK", status="Active")
        db.session.add(junk)
        db.session.flush()

        stale = User(name="Old Demo Admin", email="old.admin@legacy.invalid", role="admin")
        stale.set_password("password123")
        db.session.add(stale)
        db.session.flush()
        self.stale_user_id = stale.id

        employee = Employee(
            staff_id="JUNK1", full_name="Junk Worker",
            client_company_id=junk.id, basic_salary=1000, status="Active",
        )
        db.session.add(employee)
        run = PayrollRun(
            month="January", year=2026, status="Approved",
            client_company_id=junk.id, created_by=stale.id, approved_by=stale.id,
            total_net_pay=900,
        )
        db.session.add(run)
        db.session.flush()
        item = PayrollItem(
            payroll_run_id=run.id, employee_id=employee.id,
            staff_id="JUNK1", full_name="Junk Worker", net_pay=900,
        )
        db.session.add(item)
        db.session.flush()
        db.session.add(
            PayslipDelivery(
                payroll_item_id=item.id, payroll_run_id=run.id,
                channel="sms", recipient="0244000000", status=DELIVERY_SENT,
            )
        )
        db.session.add(
            Expense(
                title="Junk expense", expense_date=date(2026, 1, 5), category="Fuel",
                description="Junk expense", amount=100, client_company_id=junk.id,
                payroll_run_id=run.id, recorded_by=stale.id,
            )
        )
        event = DomainEvent(
            event_type="run.risk_accepted", client_company_id=junk.id,
            actor_user_id=stale.id, summary="junk",
        )
        db.session.add(event)
        db.session.flush()
        db.session.add(
            Notification(
                user_id=stale.id, client_company_id=junk.id, event_id=event.id,
                title="Junk", body="junk",
            )
        )
        # History owned by the stale user but attached to a tenant that SURVIVES —
        # this must be reassigned, not deleted.
        survivor = ClientCompany.query.filter_by(name="MSC Limited").first()
        db.session.add(
            AuditTrail(
                user_id=stale.id, user_role="admin", action="Payroll approval",
                related_record_type="ClientCompany", related_record_id=survivor.id,
                notes="approved long ago",
            )
        )
        db.session.commit()
        self.junk_company_id = junk.id

    # --- what survives -------------------------------------------------------
    def test_only_the_two_professional_tenants_remain(self):
        names = {company.name for company in ClientCompany.query.all()}
        self.assertEqual(names, PROFESSIONAL_TENANTS)
        self.assertIn("Test Co 3 DELETEME", self.summary["removed_companies"])

    def test_only_professional_platform_logins_remain(self):
        emails = {
            user.email
            for user in User.query.filter(User.client_company_id.is_(None)).all()
        }
        self.assertTrue(all(email.endswith("@payrolla.com") for email in emails), emails)
        self.assertIn("old.admin@legacy.invalid", self.summary["removed_users"])
        self.assertIsNone(User.query.filter_by(email="old.admin@legacy.invalid").first())

    def test_tenant_logins_survive_the_rebuild(self):
        for email in ("admin@msc.com", "payroll@msc.com", "finance@acme.com"):
            with self.subTest(email=email):
                user = User.query.filter_by(email=email).first()
                self.assertIsNotNone(user)
                self.assertIsNotNone(user.client_company_id)

    # --- referential integrity ----------------------------------------------
    def test_the_deleted_tenant_leaves_no_child_rows(self):
        cid = self.junk_company_id
        self.assertIsNone(db.session.get(ClientCompany, cid))
        self.assertEqual(Employee.query.filter_by(client_company_id=cid).count(), 0)
        self.assertEqual(PayrollRun.query.filter_by(client_company_id=cid).count(), 0)
        self.assertEqual(Expense.query.filter_by(client_company_id=cid).count(), 0)
        self.assertEqual(DomainEvent.query.filter_by(client_company_id=cid).count(), 0)
        self.assertEqual(Notification.query.filter_by(client_company_id=cid).count(), 0)

    def test_no_orphaned_records_anywhere(self):
        run_ids = {run.id for run in PayrollRun.query.all()}
        company_ids = {company.id for company in ClientCompany.query.all()}
        user_ids = {user.id for user in User.query.all()}
        item_ids = {item.id for item in PayrollItem.query.all()}

        for item in PayrollItem.query.all():
            self.assertIn(item.payroll_run_id, run_ids)
        for delivery in PayslipDelivery.query.all():
            self.assertIn(delivery.payroll_run_id, run_ids)
            self.assertIn(delivery.payroll_item_id, item_ids)
        for run in PayrollRun.query.all():
            self.assertIn(run.client_company_id, company_ids)
            for owner in (run.created_by, run.reviewed_by, run.approved_by):
                if owner is not None:
                    self.assertIn(owner, user_ids)
        for expense in Expense.query.all():
            if expense.client_company_id is not None:
                self.assertIn(expense.client_company_id, company_ids)
            if expense.recorded_by is not None:
                self.assertIn(expense.recorded_by, user_ids)
        for notification in Notification.query.all():
            self.assertIn(notification.user_id, user_ids)
        for entry in AuditTrail.query.all():
            if entry.user_id is not None:
                self.assertIn(entry.user_id, user_ids)

    def test_history_owned_by_a_removed_user_is_reassigned_not_deleted(self):
        entry = AuditTrail.query.filter_by(notes="approved long ago").first()
        self.assertIsNotNone(entry, "surviving history must not be deleted")
        self.assertNotEqual(entry.user_id, self.stale_user_id)
        admin = User.query.filter_by(email="admin@payrolla.com").first()
        self.assertEqual(entry.user_id, admin.id)

    # --- what gets built -----------------------------------------------------
    def test_both_tenants_are_populated(self):
        for name in PROFESSIONAL_TENANTS:
            with self.subTest(company=name):
                company = ClientCompany.query.filter_by(name=name).first()
                self.assertGreaterEqual(len(company.employees), 8)
                self.assertEqual(len(company.payroll_runs), MONTHS)
                self.assertGreater(
                    Expense.query.filter_by(client_company_id=company.id).count(), 0
                )
                for run in company.payroll_runs:
                    self.assertEqual(len(run.items), len(company.employees))
                    self.assertGreater(run.total_net_pay, 0)

    def test_run_totals_match_their_items(self):
        for run in PayrollRun.query.all():
            with self.subTest(run=f"{run.month} {run.year}"):
                self.assertAlmostEqual(
                    run.total_net_pay, sum(item.net_pay for item in run.items), places=2
                )
                self.assertAlmostEqual(
                    run.total_paye, sum(item.paye for item in run.items), places=2
                )

    def test_statutory_figures_are_calculated_not_invented(self):
        item = PayrollItem.query.filter(PayrollItem.basic_salary > 0).first()
        self.assertIsNotNone(item)
        # 5.5% employee SSF and 13% employer SSF on basic, from StatutoryRate.
        self.assertAlmostEqual(item.ssnit, round(item.basic_salary * 0.055, 2), places=2)
        self.assertAlmostEqual(
            item.ssf_employer, round(item.basic_salary * 0.13, 2), places=2
        )
        self.assertGreater(item.paye, 0)
        self.assertAlmostEqual(
            item.net_pay, round(item.gross_pay - item.total_deductions, 2), places=2
        )

    def test_closed_runs_have_delivered_payslips(self):
        processed = PayrollRun.query.filter_by(status=PROCESSED).all()
        self.assertTrue(processed, "the cycle should leave at least one closed run")
        for run in processed:
            delivered = PayslipDelivery.query.filter_by(
                payroll_run_id=run.id, status=DELIVERY_SENT
            ).count()
            self.assertEqual(delivered, len(run.items))

    def test_status_variety_makes_the_distribution_chart_meaningful(self):
        statuses = {run.status for run in PayrollRun.query.all()}
        self.assertGreaterEqual(len(statuses), 2)

    def test_activity_timeline_has_entries(self):
        self.assertGreater(
            AuditTrail.query.filter_by(action="Client company onboarded").count(), 0
        )

    # --- idempotence ---------------------------------------------------------
    def test_running_twice_is_stable(self):
        before = (
            ClientCompany.query.count(),
            Employee.query.count(),
            PayrollRun.query.count(),
            PayrollItem.query.count(),
            Expense.query.count(),
            User.query.count(),
        )
        reset_demo_data(months=MONTHS)
        after = (
            ClientCompany.query.count(),
            Employee.query.count(),
            PayrollRun.query.count(),
            PayrollItem.query.count(),
            Expense.query.count(),
            User.query.count(),
        )
        self.assertEqual(before, after)

    def test_default_history_length_fills_the_charts(self):
        self.assertEqual(DEFAULT_MONTHS, 6)

    # --- the dashboards it exists to fill ------------------------------------
    def test_every_demo_login_lands_on_a_populated_dashboard(self):
        # (email, path, a heading ONLY a populated dashboard renders, that same
        # page's empty-state string). The two planes have different templates and
        # therefore different empty-state copy, so the negative assertion is per
        # case — asserting one page's empty string against another's body passes
        # vacuously and tests nothing.
        cases = [
            (
                "admin@payrolla.com", "/dashboard",
                "Companies by payroll cost", "No payroll processed yet",
            ),
            (
                "admin@msc.com", "/company",
                "Payroll cost trend", "No payroll run yet",
            ),
            (
                "finance@acme.com", "/company",
                "Payroll cost trend", "No payroll run yet",
            ),
        ]
        for email, path, marker, empty_marker in cases:
            with self.subTest(email=email):
                self.client.post(
                    "/login", data={"email": email, "password": "password123"}
                )
                page = self.client.get(path)
                self.assertEqual(page.status_code, 200)
                body = page.get_data(as_text=True)
                self.assertIn(marker, body)
                self.assertNotIn(empty_marker, body)
                self.client.get("/logout")

    def test_client_expense_page_is_populated(self):
        self.client.post(
            "/login", data={"email": "admin@msc.com", "password": "password123"}
        )
        body = self.client.get("/company/expenses").get_data(as_text=True)
        self.assertIn("Category breakdown", body)
        self.assertNotIn("No expenses recorded", body)


if __name__ == "__main__":
    unittest.main()
