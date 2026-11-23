"""The demo workspace's data side (app.demo_workspace).

What has to hold, because it runs against the live database:

* Set up builds the fixed shape: a demo bureau, its own company and two
  clients, three logins with no usable password yet, six months of payroll,
  and only made-up contacts.
* Refresh rebuilds the data the same way every time and keeps the logins.
* Remove deletes every demo row and nothing else.
* The guard refuses, changing nothing, when the rows don't look like the demo.
* Nothing is left pointing at a deleted row. SQLite doesn't enforce foreign
  keys and Postgres does, so every foreign key is checked by hand.
"""

import os
import unittest
from datetime import date, timedelta

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from sqlalchemy import exists, func, select, text  # noqa: E402

from app import create_app, db  # noqa: E402
from app.demo_workspace import (  # noqa: E402
    DemoGuardError,
    DemoStateError,
    refresh,
    remove,
    set_up,
    status,
)
from app.models import (  # noqa: E402
    DELIVERY_SENT,
    Bureau,
    ClientCompany,
    Employee,
    EmployeeTaxReliefApproval,
    Expense,
    ExpenseReceipt,
    PayrollItem,
    PayrollRun,
    PayslipDelivery,
    StatutoryRate,
    User,
)
from app.payroll_status import APPROVED, PROCESSED  # noqa: E402
from app.roles import BUREAU_ADMIN, CLIENT_ADMIN, CLIENT_PREPARER  # noqa: E402

LOGINS = {
    "bureau_admin": ("Bea Admin", "bureau@demo-test.example.com"),
    "client_admin": ("Cal Admin", "client@demo-test.example.com"),
    "client_preparer": ("Pat Preparer", "preparer@demo-test.example.com"),
}


def orphan_report():
    """[(table.column, n)] for every foreign key value with no row behind it."""
    found = []
    for table in db.metadata.sorted_tables:
        for fk in table.foreign_keys:
            column, target = fk.parent, fk.column
            if target.table is table:
                continue
            n = db.session.execute(
                select(func.count()).select_from(table)
                .where(column.isnot(None))
                .where(~exists().where(target == column))
            ).scalar()
            if n:
                found.append((f"{table.name}.{column.name}", n))
    return found


class DemoWorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.staff = User.query.filter_by(email="admin@payrolla.com").one()
        self._real_bureau()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _real_bureau(self):
        """A Chrisnat-shaped real bureau with a client, a run and a delivery:
        what the workspace must never touch."""
        bureau = Bureau(name="Real Bureau Ltd", code="REAL")
        db.session.add(bureau)
        db.session.flush()
        home = ClientCompany(name="Real Bureau Ltd", company_code="REAL", status="Active",
                             bureau_id=bureau.id, bureau_home=True)
        client = ClientCompany(name="Real Client Ltd", company_code="REALC", status="Active",
                               bureau_id=bureau.id)
        db.session.add_all([home, client])
        db.session.flush()
        admin = User(name="Real Admin", email="admin@real-bureau.example.com", role=BUREAU_ADMIN,
                     bureau_id=bureau.id)
        admin.set_password("password123")
        employee = Employee(staff_id="REAL1", full_name="Real Worker", client_company_id=client.id,
                            basic_salary=2000, status="Active")
        run = PayrollRun(month="October", year=2026, status=APPROVED, client_company_id=client.id,
                         total_net_pay=1800)
        db.session.add_all([admin, employee, run])
        db.session.flush()
        item = PayrollItem(payroll_run_id=run.id, employee_id=employee.id, staff_id="REAL1",
                           full_name="Real Worker", net_pay=1800)
        db.session.add(item)
        db.session.flush()
        db.session.add(PayslipDelivery(payroll_item_id=item.id, payroll_run_id=run.id, channel="email",
                                       recipient="worker@real-client.example.com", status=DELIVERY_SENT))
        db.session.commit()

    def _real_snapshot(self):
        """Row counts and a password hash for everything that is not demo."""
        real = db.session.query(ClientCompany.id).filter(ClientCompany.is_demo.is_(False))
        runs = db.session.query(PayrollRun.id).filter(PayrollRun.client_company_id.in_(real))
        return {
            "bureaus": Bureau.query.filter(Bureau.is_demo.is_(False)).count(),
            "companies": real.count(),
            "users": User.query.filter(User.email.notlike("%@demo-test.example.com")).count(),
            "employees": Employee.query.filter(Employee.client_company_id.in_(real)).count(),
            "runs": runs.count(),
            "items": PayrollItem.query.filter(PayrollItem.payroll_run_id.in_(runs)).count(),
            "deliveries": PayslipDelivery.query.filter(PayslipDelivery.payroll_run_id.in_(runs)).count(),
            "expenses": Expense.query.filter(db.or_(Expense.client_company_id.in_(real),
                                                    Expense.client_company_id.is_(None))).count(),
            "rates": StatutoryRate.query.filter(StatutoryRate.bureau_id.is_(None)).count(),
            "real_admin_hash": User.query.filter_by(email="admin@real-bureau.example.com").one().password_hash,
        }

    def _demo_totals(self):
        demo = db.session.query(ClientCompany.id).filter(ClientCompany.is_demo.is_(True))
        runs = PayrollRun.query.filter(PayrollRun.client_company_id.in_(demo))
        return {
            "companies": demo.count(),
            "employees": Employee.query.filter(Employee.client_company_id.in_(demo)).count(),
            "runs": runs.count(),
            "net": round(sum(r.total_net_pay for r in runs), 2),
            "statuses": sorted(r.status for r in runs),
        }

    # --- set up ------------------------------------------------------------------
    def test_set_up_builds_the_shape(self):
        bureau, users = set_up(LOGINS)
        self.assertTrue(bureau.is_demo)
        state = status()
        companies = state["companies"]
        self.assertEqual([c.name for c in companies],
                         ["Demo Payroll Bureau", "Volta Harbour Logistics (Demo)", "Asante Textiles (Demo)"])
        self.assertTrue(all(c.is_demo and c.bureau_id == bureau.id for c in companies))
        self.assertEqual([c.bureau_home for c in companies], [True, False, False])
        self.assertEqual(state["portal_company_id"], companies[1].id)

        roles = {(u.email, u.role, u.bureau_id, u.client_company_id) for u in users.values()}
        self.assertEqual(roles, {
            ("bureau@demo-test.example.com", BUREAU_ADMIN, bureau.id, None),
            ("client@demo-test.example.com", CLIENT_ADMIN, None, companies[1].id),
            ("preparer@demo-test.example.com", CLIENT_PREPARER, None, companies[1].id),
        })
        self.assertTrue(all(u.password_hash == "!" for u in users.values()))

        for company in companies:
            with self.subTest(company=company.name):
                self.assertTrue(company.email.endswith(".example.com"))
                for employee in company.employees:
                    self.assertTrue(employee.email is None or employee.email.endswith(".example.com"))
                    self.assertTrue(employee.phone.startswith("0200"))
                runs = company.payroll_runs
                self.assertEqual(len(runs), 6)
                approved = [r for r in runs if r.status == APPROVED]
                self.assertEqual(len(approved), 1)
                self.assertEqual(PayslipDelivery.query.filter_by(payroll_run_id=approved[0].id).count(), 0)
                processed = [r for r in runs if r.status == PROCESSED]
                self.assertTrue(processed)
                providers = {d.provider for d in PayslipDelivery.query.filter_by(payroll_run_id=processed[0].id)}
                self.assertEqual(providers, {"demo-seed"})
                self.assertTrue(all(r.statutory_rate_id for r in runs))
        self.assertEqual(orphan_report(), [])

    def test_set_up_twice_is_refused(self):
        set_up(LOGINS)
        before = self._demo_totals()
        with self.assertRaises(DemoStateError):
            set_up(LOGINS)
        db.session.rollback()
        self.assertEqual(self._demo_totals(), before)

    def test_a_real_company_with_a_demo_name_stops_set_up(self):
        db.session.add(ClientCompany(name="Asante Textiles (Demo)", company_code="ASX", status="Active"))
        db.session.commit()
        with self.assertRaises(DemoStateError):
            set_up(LOGINS)
        db.session.rollback()
        self.assertEqual(Bureau.query.filter(Bureau.is_demo.is_(True)).count(), 0)

    # --- refresh -----------------------------------------------------------------
    def test_refresh_rebuilds_the_same_data_and_keeps_the_logins(self):
        _bureau, users = set_up(LOGINS)
        for user in users.values():
            user.set_password("a-long-password-1")
        db.session.commit()
        logins = {u.id: u.password_hash for u in User.query.filter(User.email.like("%@demo-test.example.com"))}
        company_ids = [c.id for c in status()["companies"]]
        first = self._demo_totals()

        refresh(self.staff)
        refresh(self.staff)
        self.assertEqual(self._demo_totals(), first)
        after = {u.id: u.password_hash for u in User.query.filter(User.email.like("%@demo-test.example.com"))}
        self.assertEqual(after, logins)
        self.assertEqual([c.id for c in status()["companies"]], company_ids)
        self.assertEqual(orphan_report(), [])

    def test_refresh_clears_what_the_demo_users_did(self):
        bureau, users = set_up(LOGINS)
        client_a = db.session.get(ClientCompany, status()["portal_company_id"])
        employee = client_a.employees[0]
        db.session.add(EmployeeTaxReliefApproval(
            employee_id=employee.id, monthly_amount=50, reference="GRA/1", category="Marriage",
            valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31),
        ))
        expense = Expense.query.filter_by(client_company_id=client_a.id).first()
        db.session.add(ExpenseReceipt(expense_id=expense.id, client_company_id=client_a.id,
                                      original_filename="r.pdf", storage_key="receipts/demo/r.pdf",
                                      content_type="application/pdf", uploaded_by=users["client_admin"].id))
        default = StatutoryRate.query.filter(StatutoryRate.bureau_id.is_(None)).first()
        db.session.add(StatutoryRate(
            effective_from=date.today() - timedelta(days=1), bureau_id=bureau.id,
            paye_bands_json=default.paye_bands_json, ssf_employee_rate=default.ssf_employee_rate,
            ssf_employer_rate=default.ssf_employer_rate, created_by=users["bureau_admin"].id,
        ))
        db.session.add(Employee(staff_id="EXTRA1", full_name="Extra Worker", client_company_id=client_a.id,
                                status="Active"))
        client_a.name = "Renamed by the demo user"
        db.session.commit()

        stored_keys, _summary = refresh(self.staff)
        self.assertEqual(stored_keys, ["receipts/demo/r.pdf"])
        self.assertEqual(db.session.get(ClientCompany, client_a.id).name, "Volta Harbour Logistics (Demo)")
        self.assertEqual(Employee.query.filter_by(staff_id="EXTRA1").count(), 0)
        self.assertEqual(ExpenseReceipt.query.count(), 0)
        self.assertEqual(StatutoryRate.query.filter(StatutoryRate.bureau_id == bureau.id).count(), 0)
        self.assertEqual(orphan_report(), [])

    # --- remove ------------------------------------------------------------------
    def test_real_rows_are_untouched_by_set_up_refresh_and_remove(self):
        before = self._real_snapshot()
        set_up(LOGINS)
        self.assertEqual(self._real_snapshot(), before)
        refresh(self.staff)
        self.assertEqual(self._real_snapshot(), before)
        remove(self.staff)
        self.assertEqual(self._real_snapshot(), before)

    def test_remove_leaves_no_demo_rows(self):
        set_up(LOGINS)
        remove(self.staff)
        self.assertEqual(Bureau.query.filter(Bureau.is_demo.is_(True)).count(), 0)
        self.assertEqual(ClientCompany.query.filter(ClientCompany.is_demo.is_(True)).count(), 0)
        self.assertEqual(User.query.filter(User.email.like("%@demo-test.example.com")).count(), 0)
        self.assertEqual(status(), {"exists": False})
        self.assertEqual(orphan_report(), [])

    def test_refresh_and_remove_need_a_workspace(self):
        with self.assertRaises(DemoStateError):
            refresh(self.staff)
        with self.assertRaises(DemoStateError):
            remove(self.staff)

    # --- the guard ---------------------------------------------------------------
    def _refused_unchanged(self):
        before = (self._demo_totals(), self._real_snapshot())
        for action in (refresh, remove):
            with self.subTest(action=action.__name__):
                with self.assertRaises(DemoGuardError):
                    action(self.staff)
                db.session.rollback()
                self.assertEqual((self._demo_totals(), self._real_snapshot()), before)

    def test_an_unflagged_company_in_the_demo_bureau_stops_everything(self):
        bureau, _users = set_up(LOGINS)
        # Raw SQL, past the listener that would otherwise flag it.
        db.session.execute(text(
            "INSERT INTO client_company (name, status, bureau_home, is_demo, bureau_id) "
            "VALUES ('Sneaked In Ltd', 'Active', 0, 0, :bureau)"), {"bureau": bureau.id})
        db.session.commit()
        self._refused_unchanged()
        self.assertEqual(status()["exists"], True)
        self.assertIn("don't match", status()["problem"])

    def test_a_flagged_company_outside_the_demo_bureau_stops_everything(self):
        set_up(LOGINS)
        db.session.execute(text("UPDATE client_company SET is_demo = 1 WHERE name = 'Real Client Ltd'"))
        db.session.commit()
        self._refused_unchanged()

    def test_a_second_demo_bureau_stops_everything(self):
        set_up(LOGINS)
        db.session.execute(text("UPDATE bureau SET is_demo = 1 WHERE code = 'REAL'"))
        db.session.commit()
        with self.assertRaises(DemoGuardError):
            remove(self.staff)

    # --- the flag follows the demo bureau ------------------------------------------
    def test_a_company_added_to_the_demo_bureau_is_a_demo_company(self):
        bureau, _users = set_up(LOGINS)
        added = ClientCompany(name="Added By Demo Ltd", company_code="ADDED", status="Active",
                              bureau_id=bureau.id)
        moved = ClientCompany(name="Moved Later Ltd", company_code="MOVED", status="Active")
        db.session.add_all([added, moved])
        db.session.commit()
        self.assertTrue(added.is_demo)
        self.assertFalse(moved.is_demo)
        moved.bureau_id = bureau.id
        db.session.commit()
        self.assertTrue(moved.is_demo)
        added_id = added.id
        # Refresh keeps a company the demo bureau added, emptied.
        refresh(self.staff)
        self.assertIsNotNone(db.session.get(ClientCompany, added_id))
        remove(self.staff)
        self.assertIsNone(db.session.get(ClientCompany, added_id))
        self.assertEqual(orphan_report(), [])

    # --- rate locks --------------------------------------------------------------
    def test_demo_runs_do_not_lock_the_default_rates(self):
        from app.statutory import rate_locks

        before = rate_locks()
        set_up(LOGINS)
        self.assertEqual(rate_locks(), before)


if __name__ == "__main__":
    unittest.main()
