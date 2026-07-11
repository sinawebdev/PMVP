"""GATE 5 — Raw Hours Engine exports & bank grouping.

Exports regenerate from a computed run; bank + PV routing covers every worker
exactly once and totals to the run net; the ICU distribution ties to the ICU
total.
"""
import os
import tempfile
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import openpyxl

from app import create_app, db
from app.models import ClientCompany, PayrollRun, StatutoryRate
from app.raw_engine.exports.bank_routing import route_payments
from app.raw_engine.exports.service import generate_run_exports
from app.raw_engine.exports.writers import export_icu_distribution
from app.raw_engine.icu_distribution import distribute_union_dues
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import persist_seed, write_payroll_items

DZ_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "DZ-PAYROLL JAN 2026.xlsx"
)
WHITELIST = ["ADB", "SG-GH", "GCB", "ECOBANK", "GT BANK"]


class _Item:
    def __init__(self, staff_id, bank_name="", bank_account_number="", net_pay=0.0):
        self.staff_id = staff_id
        self.full_name = staff_id
        self.bank_name = bank_name
        self.bank_branch = ""
        self.bank_account_number = bank_account_number
        self.net_pay = net_pay
        self.icu_dues = 0.0


class BankRoutingUnitTests(unittest.TestCase):
    def test_routes_bank_vs_pv_and_covers_everyone(self):
        items = [
            _Item("A", "ADB", "111", 100.0),        # whitelisted + account -> bank
            _Item("B", "Mobile Money", "", 50.0),   # unknown bank -> PV
            _Item("C", "GCB", "", 40.0),            # whitelisted but no account -> PV
            _Item("D", "", "", 30.0),               # no bank -> PV
        ]
        routing = route_payments(items, whitelist=WHITELIST)
        self.assertTrue(routing.is_complete(items))
        self.assertEqual([i.staff_id for i in routing.banked], ["A"])
        self.assertEqual(sorted(i.staff_id for i in routing.pv), ["B", "C", "D"])
        self.assertAlmostEqual(routing.banked_total, 100.0, delta=0.01)
        self.assertAlmostEqual(routing.pv_total, 120.0, delta=0.01)
        self.assertAlmostEqual(routing.routed_total, 220.0, delta=0.01)


@unittest.skipUnless(
    os.path.exists(DZ_FIXTURE),
    "Decrypted DZ specimen missing — see tests/test_raw_engine_seed.py.",
)
class ExportsFullRunTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = ClientCompany(name="DZVANS COMPANY LIMITED", status="Active")
        db.session.add(self.client)
        db.session.commit()
        self.context = parse_rich_workbook(DZ_FIXTURE, self.client.id)
        persist_seed(self.context, preserve=False)
        self.rate = StatutoryRate.active_for(date(2026, 1, 1))
        self.run = PayrollRun(
            month="January", year=2026, status="Draft",
            upload_type="raw", client_company_id=self.client.id,
        )
        db.session.add(self.run)
        db.session.commit()
        write_payroll_items(self.run, compute_seed_month(self.context, self.rate))
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def test_no_worker_is_unrouted_and_routing_totals_to_run_net(self):
        items = list(self.run.items)
        routing = route_payments(items)  # uses configured whitelist
        self.assertTrue(routing.is_complete(items))
        self.assertEqual(len(routing.banked) + len(routing.pv), 181)
        run_net = round(sum((i.net_pay or 0) for i in items), 2)
        self.assertAlmostEqual(routing.routed_total, run_net, delta=0.01)
        self.assertAlmostEqual(
            routing.banked_total + routing.pv_total, run_net, delta=0.01
        )

    def test_icu_distribution_ties_to_icu_total(self):
        icu_total = round(sum((i.icu_dues or 0) for i in self.run.items), 2)
        self.assertGreater(icu_total, 0)
        self.assertAlmostEqual(
            distribute_union_dues(icu_total).total_payout, icu_total, delta=0.01
        )
        path = export_icu_distribution(self.run, self.tmp)
        self.assertTrue(os.path.exists(path))
        openpyxl.load_workbook(path)  # opens without error

    def test_full_family_regenerates_and_opens(self):
        result = generate_run_exports(self.run, self.tmp)
        self.assertTrue(result["routing"]["complete"])
        for key in ("wages_sheet", "gra_paye", "bank_grouping", "pv", "icu_distribution"):
            path = result["files"][key]
            self.assertTrue(os.path.exists(path), msg=f"{key} not written")
            openpyxl.load_workbook(path)  # every workbook opens

        # Bank + PV worker counts cover the whole run.
        self.assertEqual(
            result["routing"]["banked_workers"] + result["routing"]["pv_workers"], 181
        )
        run_net = round(sum((i.net_pay or 0) for i in self.run.items), 2)
        self.assertAlmostEqual(result["routing"]["routed_total"], run_net, delta=0.01)

    def test_bank_grouping_grand_total_equals_banked_net(self):
        routing = route_payments(list(self.run.items))
        banked_net = round(sum((i.net_pay or 0) for i in routing.banked), 2)
        self.assertAlmostEqual(routing.banked_total, banked_net, delta=0.01)


if __name__ == "__main__":
    unittest.main()
