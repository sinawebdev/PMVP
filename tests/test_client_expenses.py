"""Operations & Onboarding, Phase 2 — client expense management.

Covers self-service expense CRUD on the tenant plane: validation, tenant
isolation (another tenant's expense is a 404 and never appears in a list), the
existing permission model (only the roles that may prepare a run may record
spend), and the analytics feed — a recorded expense must move the company
dashboard's own totals with no cache to clear.
"""

import os
import unittest
from datetime import date, timedelta

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.analytics import category_breakdown, expense_summary  # noqa: E402
from app.client.expenses import EXPENSE_CATEGORIES  # noqa: E402
from app.models import ClientCompany, Expense, User  # noqa: E402
from app.roles import CLIENT_ADMIN, CLIENT_PREPARER  # noqa: E402


class ClientExpensesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        companies = ClientCompany.query.order_by(ClientCompany.id).all()
        self.company = companies[0]
        self.other_company = companies[1]
        self.admin = self._tenant_user("expense.admin@test.local", CLIENT_ADMIN, self.company)
        self.preparer = self._tenant_user(
            "expense.preparer@test.local", CLIENT_PREPARER, self.company
        )
        self.outsider = self._tenant_user(
            "expense.outsider@test.local", CLIENT_ADMIN, self.other_company
        )

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def _tenant_user(self, email, role, company):
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(
                name=email.split("@")[0], email=email, role=role,
                client_company_id=company.id,
            )
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()
        return user

    def _login(self, user):
        resp = self.client.post(
            "/login", data={"email": user.email, "password": "password123"}
        )
        self.assertEqual(resp.status_code, 302)

    def _payload(self, **overrides):
        data = {
            "expense_date": date.today().isoformat(),
            "category": "Fuel",
            "description": "Generator diesel for the Tema site",
            "amount": "1,250.50",
            "notes": "Two 200L drums.",
        }
        data.update(overrides)
        return data

    def _add(self, **overrides):
        return self.client.post("/company/expenses/add", data=self._payload(**overrides))

    # --- create --------------------------------------------------------------
    def test_client_admin_can_record_an_expense(self):
        self._login(self.admin)
        resp = self._add()
        self.assertEqual(resp.status_code, 302)

        expense = Expense.query.filter_by(description="Generator diesel for the Tema site").first()
        self.assertIsNotNone(expense)
        self.assertEqual(expense.client_company_id, self.company.id)  # forced to tenant
        self.assertEqual(expense.category, "Fuel")
        self.assertEqual(expense.amount, 1250.50)  # thousands separator parsed
        self.assertEqual(expense.notes, "Two 200L drums.")
        self.assertEqual(expense.status, "Recorded")
        self.assertEqual(expense.recorded_by, self.admin.id)

    def test_client_preparer_can_record_an_expense(self):
        self._login(self.preparer)
        self.assertEqual(self._add(description="Office internet").status_code, 302)
        self.assertIsNotNone(Expense.query.filter_by(description="Office internet").first())

    def test_every_documented_category_is_accepted(self):
        self._login(self.admin)
        for category in EXPENSE_CATEGORIES:
            with self.subTest(category=category):
                resp = self._add(category=category, description=f"Spend on {category}")
                self.assertEqual(resp.status_code, 302)
                self.assertIsNotNone(Expense.query.filter_by(category=category).first())

    # --- validation ----------------------------------------------------------
    def test_invalid_submissions_are_rejected_without_saving(self):
        self._login(self.admin)
        cases = {
            "amount": {"amount": "0"},
            "negative amount": {"amount": "-40"},
            "non-numeric amount": {"amount": "lots"},
            "category": {"category": "Bribes"},
            "description": {"description": "   "},
            "date": {"expense_date": ""},
            "future date": {
                "expense_date": (date.today() + timedelta(days=1)).isoformat()
            },
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                before = Expense.query.count()
                resp = self._add(**override)
                self.assertEqual(resp.status_code, 200)  # re-rendered, not redirected
                self.assertEqual(Expense.query.count(), before)

    def test_rejected_submission_is_echoed_back(self):
        self._login(self.admin)
        resp = self._add(amount="")
        body = resp.get_data(as_text=True)
        self.assertIn("Generator diesel for the Tema site", body)
        self.assertIn("Two 200L drums.", body)

    # --- edit / delete -------------------------------------------------------
    def test_edit_updates_the_expense(self):
        self._login(self.admin)
        self._add()
        expense = Expense.query.filter_by(category="Fuel").first()
        resp = self.client.post(
            f"/company/expenses/{expense.id}/edit",
            data=self._payload(category="Travel", amount="90", description="Site visit taxi"),
        )
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(expense)
        self.assertEqual(expense.category, "Travel")
        self.assertEqual(expense.amount, 90.0)
        self.assertEqual(expense.description, "Site visit taxi")

    def test_delete_removes_the_expense(self):
        self._login(self.admin)
        self._add()
        expense = Expense.query.filter_by(category="Fuel").first()
        resp = self.client.post(f"/company/expenses/{expense.id}/delete")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.session.get(Expense, expense.id))

    # --- tenant isolation ----------------------------------------------------
    def test_another_tenants_expense_is_404_not_403(self):
        self._login(self.admin)
        self._add()
        expense = Expense.query.filter_by(category="Fuel").first()

        self.client.get("/logout")
        self._login(self.outsider)
        self.assertEqual(
            self.client.get(f"/company/expenses/{expense.id}/edit").status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/company/expenses/{expense.id}/delete").status_code, 404
        )
        self.assertIsNotNone(db.session.get(Expense, expense.id))  # untouched

    def test_expense_list_shows_only_the_active_tenants_rows(self):
        self._login(self.admin)
        self._add(description="Only mine")
        self.client.get("/logout")

        self._login(self.outsider)
        body = self.client.get("/company/expenses").get_data(as_text=True)
        self.assertNotIn("Only mine", body)

    def test_a_platform_operator_is_bounced_off_the_client_expense_plane(self):
        operator = User.query.filter(User.client_company_id.is_(None)).first()
        self._login(operator)
        for path in ["/company/expenses", "/company/expenses/add"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, path)
            self.assertTrue(resp.headers["Location"].endswith("/dashboard"), path)

    # --- analytics feed ------------------------------------------------------
    def test_recorded_expenses_feed_the_company_dashboard(self):
        self._login(self.admin)
        self._add(amount="500", description="Water bill", category="Utilities")
        self._add(amount="250", description="Fuel top-up", category="Fuel")

        page = self.client.get("/company")
        self.assertEqual(page.status_code, 200)
        # 750 across both entries, rendered by the cedis filter on the stat card.
        self.assertIn("750.00", page.get_data(as_text=True))

    def test_expenses_page_shows_totals_and_breakdown(self):
        self._login(self.admin)
        self._add(amount="500", description="Water bill", category="Utilities")
        body = self.client.get("/company/expenses").get_data(as_text=True)
        self.assertIn("Total expenses", body)
        self.assertIn("Category breakdown", body)
        self.assertIn("Utilities", body)
        self.assertIn("+ Add Expense", body)

    def test_expense_summary_splits_total_from_this_month(self):
        today = date.today()
        last_month = (today.replace(day=1) - timedelta(days=1))
        rows = [
            Expense(expense_date=today, category="Fuel", description="a", amount=100),
            Expense(expense_date=today, category="Travel", description="b", amount=50),
            Expense(expense_date=last_month, category="Fuel", description="c", amount=400),
        ]
        summary = expense_summary(rows, today=today)
        self.assertEqual(summary["total"], 550)
        self.assertEqual(summary["monthly_total"], 150)
        self.assertEqual(summary["count"], 3)

    def test_category_breakdown_orders_by_size_and_sums_to_100(self):
        rows = [
            Expense(expense_date=date.today(), category="Fuel", description="a", amount=300),
            Expense(expense_date=date.today(), category="Travel", description="b", amount=100),
            Expense(expense_date=date.today(), category="Fuel", description="c", amount=100),
        ]
        breakdown = category_breakdown(rows)
        self.assertEqual([s["label"] for s in breakdown["slices"]], ["Fuel", "Travel"])
        self.assertEqual(breakdown["slices"][0]["value"], 400)
        self.assertEqual(round(sum(s["pct"] for s in breakdown["slices"])), 100)
        self.assertEqual(category_breakdown([]), {"slices": [], "total": 0})


if __name__ == "__main__":
    unittest.main()
