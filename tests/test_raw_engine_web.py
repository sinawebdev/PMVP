"""GATE 7 — Raw Hours Engine web integration.

Route-level tests through the Flask test client: seed-vs-thin routing, mismatch
refusal, explicit pay_type, durable Postgres-blob preservation (with rollback),
template download round-trip, exports, and admin-only auth.
"""
import hashlib
import io
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import openpyxl
import sqlalchemy as sa

from app import create_app, db
from app.models import (
    ClientCompany,
    Employee,
    PayrollItem,
    PayrollRun,
    RawUploadArchive,
    StatutoryRate,
    WageRateProfile,
)
from app.raw_engine.exports.bank_routing import route_payments
from app.raw_engine.run import compute_seed_month
from app.raw_engine.seed import parse_rich_workbook
from app.raw_engine.store import persist_seed, write_payroll_items
from app.raw_engine.thin import (
    ThinEmployeeInput,
    join_and_compute,
    parse_thin_workbook,
    write_thin_workbook,
)

DZ_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "DZ-PAYROLL JAN 2026.xlsx"
)


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


@unittest.skipUnless(
    os.path.exists(DZ_FIXTURE),
    "Decrypted DZ specimen missing — see tests/test_raw_engine_seed.py.",
)
class RawEngineWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.tmp = tempfile.mkdtemp()
        self.app.config["EXPORT_FOLDER"] = self.tmp
        self.client = self.app.test_client()
        with self.app.app_context():
            company = ClientCompany(name="DZVANS COMPANY LIMITED", status="Active")
            db.session.add(company)
            db.session.commit()
            self.cid = company.id

    def _login(self, email="admin@chrisnat.local"):
        return self.client.post(
            "/login", data={"email": email, "password": "password123"},
            follow_redirects=True,
        )

    def _upload(self, path_or_bytes, filename="upload.xlsx"):
        data = path_or_bytes if isinstance(path_or_bytes, bytes) else _read(path_or_bytes)
        return self.client.post("/raw/upload", data={
            "client_company_id": str(self.cid),
            "month": "January", "year": "2026",
            "file": (io.BytesIO(data), filename),
        }, content_type="multipart/form-data")

    def _seed_via_lib(self):
        """Seed + compute the DZ month directly (library) for tests that start
        from an already-seeded company."""
        with self.app.app_context():
            context = parse_rich_workbook(DZ_FIXTURE, self.cid)
            rate = StatutoryRate.active_for(date(2026, 1, 1))
            run = PayrollRun(month="January", year=2026, status="Draft",
                             upload_type="raw", client_company_id=self.cid)
            db.session.add(run)
            db.session.flush()
            persist_seed(run=run, context=context, source_bytes=_read(DZ_FIXTURE),
                         source_filename="DZ.xlsx")
            write_payroll_items(run, compute_seed_month(context, rate))
            return run.id

    def _thin_file(self, records):
        path = os.path.join(self.tmp, "thin.xlsx")
        return write_thin_workbook(path, records)

    # ── routing ───────────────────────────────────────────────────────────

    def test_unseeded_raw_upload_routes_to_seed(self):
        self._login()
        resp = self._upload(DZ_FIXTURE, "DZ.xlsx")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["mode"], "seed")
        self.assertEqual(resp.get_json()["preview"]["employees"], 181)

        token = resp.get_json()["token"]
        confirm = self.client.post("/raw/confirm", data={"token": token})
        self.assertEqual(confirm.status_code, 200)
        with self.app.app_context():
            self.assertEqual(
                Employee.query.filter_by(client_company_id=self.cid).count(), 181
            )
            self.assertTrue(WageRateProfile.query.filter_by(client_company_id=self.cid).first())

    def test_seeded_raw_upload_routes_to_thin(self):
        self._seed_via_lib()
        self._login()
        with self.app.app_context():
            emp = Employee.query.filter_by(
                client_company_id=self.cid, full_name="GEORGE AKOTO"
            ).one()
            staff = emp.staff_id
        self._thin_file([ThinEmployeeInput(staff_id=staff, name="GEORGE AKOTO")])
        resp = self._upload(os.path.join(self.tmp, "thin.xlsx"), "thin.xlsx")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["mode"], "thin")

    # ── mismatch ──────────────────────────────────────────────────────────

    def test_rich_file_on_thin_path_is_blocked(self):
        self._seed_via_lib()
        self._login()
        before = self._run_count()
        resp = self._upload(DZ_FIXTURE, "DZ.xlsx")  # rich file, but seeded → thin path
        self.assertEqual(resp.status_code, 422)
        self.assertIn("rich", resp.get_json()["error"].lower())
        self.assertEqual(self._run_count(), before)  # nothing written

    def test_thin_file_on_seed_path_is_blocked(self):
        self._login()
        self._thin_file([ThinEmployeeInput(staff_id="X1", name="X")])
        resp = self._upload(os.path.join(self.tmp, "thin.xlsx"), "thin.xlsx")
        self.assertEqual(resp.status_code, 422)
        with self.app.app_context():
            self.assertEqual(
                Employee.query.filter_by(client_company_id=self.cid).count(), 0
            )

    def _run_count(self):
        with self.app.app_context():
            return PayrollRun.query.filter_by(client_company_id=self.cid).count()

    # ── pay_type ──────────────────────────────────────────────────────────

    def test_pay_type_seeded_and_edit_changes_basic_derivation(self):
        self._seed_via_lib()
        with self.app.app_context():
            george = Employee.query.filter_by(
                client_company_id=self.cid, full_name="GEORGE AKOTO"
            ).one()
            woode = Employee.query.filter_by(
                client_company_id=self.cid, full_name="RICHARD WOODE"
            ).one()
            self.assertEqual(george.pay_type, "salaried")   # default from heuristic
            self.assertEqual(woode.pay_type, "hourly")
            george_id, staff = george.id, george.staff_id

        rate_and_run = self._thin_run_basic(staff)
        self.assertAlmostEqual(rate_and_run, 1800.0, delta=0.01)  # salaried → flat

        # Correct the classification in the roster UI (no re-seed).
        self._login()
        self.client.post(
            f"/employees/clients/{self.cid}/edit/{george_id}",
            data={"full_name": "GEORGE AKOTO", "status": "Active", "pay_type": "hourly"},
            follow_redirects=True,
        )
        with self.app.app_context():
            self.assertEqual(
                db.session.get(Employee, george_id).pay_type, "hourly"
            )
        # Recompute a blank month: now hourly with no normal hours → basic 0.
        self.assertEqual(self._thin_run_basic(staff), 0.0)

    def _thin_run_basic(self, staff_id):
        """Compute George on a blank thin month and return his basic wage."""
        with self.app.app_context():
            rate = StatutoryRate.active_for(date(2026, 1, 1))
            run = PayrollRun(month="January", year=2026, status="Draft",
                             upload_type="raw", client_company_id=self.cid)
            db.session.add(run)
            db.session.flush()
            inputs = [ThinEmployeeInput(staff_id=staff_id)]
            result = join_and_compute(inputs, run, rate)
            db.session.rollback()
            return result.payslips[staff_id].basic_wage

    # ── preservation ──────────────────────────────────────────────────────

    def test_seed_archives_bytes_with_hash_and_is_downloadable(self):
        self._login()
        resp = self._upload(DZ_FIXTURE, "DZ.xlsx")
        token = resp.get_json()["token"]
        self.client.post("/raw/confirm", data={"token": token})

        with self.app.app_context():
            archive = RawUploadArchive.query.filter_by().first()
            self.assertIsNotNone(archive)
            self.assertEqual(archive.upload_kind, "seed")
            self.assertEqual(archive.sha256, hashlib.sha256(_read(DZ_FIXTURE)).hexdigest())
            aid = archive.id

        dl = self.client.get(f"/raw/archive/{aid}")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(hashlib.sha256(dl.get_data()).hexdigest(),
                         hashlib.sha256(_read(DZ_FIXTURE)).hexdigest())

    def test_preservation_failure_rolls_back_the_seed(self):
        with self.app.app_context():
            context = parse_rich_workbook(DZ_FIXTURE, self.cid)
            run = PayrollRun(month="January", year=2026, status="Draft",
                             upload_type="raw", client_company_id=self.cid)
            db.session.add(run)
            db.session.flush()
            with mock.patch(
                "app.raw_engine.store.archive_upload",
                side_effect=RuntimeError("simulated preservation failure"),
            ):
                with self.assertRaises(RuntimeError):
                    persist_seed(run=run, context=context, source_bytes=b"x",
                                 source_filename="x.xlsx")
            # Zero orphaned rows — no seeded context without its workbook.
            self.assertEqual(
                Employee.query.filter_by(client_company_id=self.cid).count(), 0
            )
            self.assertEqual(
                WageRateProfile.query.filter_by(client_company_id=self.cid).count(), 0
            )
            self.assertEqual(RawUploadArchive.query.count(), 0)

    # ── template download ─────────────────────────────────────────────────

    def test_template_download_round_trips_through_thin_parser(self):
        self._seed_via_lib()
        self._login()
        resp = self.client.get(
            f"/raw/clients/{self.cid}/template?month=February&year=2026"
        )
        self.assertEqual(resp.status_code, 200)
        path = os.path.join(self.tmp, "downloaded_template.xlsx")
        with open(path, "wb") as handle:
            handle.write(resp.get_data())
        inputs, _warn = parse_thin_workbook(path)
        self.assertEqual(len(inputs), 181)

    # ── exports ───────────────────────────────────────────────────────────

    def test_exports_zip_and_routing_totals(self):
        run_id = self._seed_via_lib()
        self._login()
        resp = self.client.get(f"/raw/runs/{run_id}/exports")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")

        with self.app.app_context():
            run = db.session.get(PayrollRun, run_id)
            items = list(run.items)
            routing = route_payments(items)
            self.assertTrue(routing.is_complete(items))          # nobody unrouted
            run_net = round(sum((i.net_pay or 0) for i in items), 2)
            self.assertAlmostEqual(routing.routed_total, run_net, delta=0.02)

    def test_raw_exports_rejects_standard_run(self):
        self._login()
        with self.app.app_context():
            run = PayrollRun(month="January", year=2026, status="Draft",
                             upload_type="standard", client_company_id=self.cid)
            db.session.add(run)
            db.session.commit()
            run_id = run.id
        resp = self.client.get(f"/raw/runs/{run_id}/exports")
        self.assertEqual(resp.status_code, 404)

    # ── auth ──────────────────────────────────────────────────────────────

    def test_non_admin_is_blocked_from_all_raw_routes(self):
        run_id = self._seed_via_lib()
        with self.app.app_context():
            aid = RawUploadArchive.query.first().id
        self._login("operations@chrisnat.local")  # operations_supervisor, not admin
        for method, url in [
            ("post", "/raw/upload"),
            ("post", "/raw/confirm"),
            ("get", f"/raw/clients/{self.cid}/template"),
            ("get", f"/raw/runs/{run_id}/exports"),
            ("get", f"/raw/archive/{aid}"),
        ]:
            resp = getattr(self.client, method)(url)
            self.assertEqual(resp.status_code, 302, f"{method} {url} not blocked")


class MigrationUpDownTests(unittest.TestCase):
    def test_single_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", "migrations")
        self.assertEqual(len(ScriptDirectory.from_config(cfg).get_heads()), 1)

    def test_pay_type_migration_upgrade_then_downgrade(self):
        from flask_migrate import downgrade as fm_downgrade
        from flask_migrate import stamp as fm_stamp
        from flask_migrate import upgrade as fm_upgrade

        tmp = tempfile.mkdtemp()
        dbfile = os.path.join(tmp, "mig.sqlite")
        prev = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{dbfile}"
        try:
            app = create_app()  # create_all builds every table incl the new ones
            with app.app_context():
                fm_stamp(revision="head")
                fm_downgrade(revision="c9e5b2d478a1")
                insp = sa.inspect(db.engine)
                self.assertNotIn("raw_upload_archives", insp.get_table_names())
                self.assertNotIn(
                    "pay_type", {c["name"] for c in insp.get_columns("employee")}
                )
                fm_upgrade(revision="head")
                insp = sa.inspect(db.engine)
                self.assertIn("raw_upload_archives", insp.get_table_names())
                self.assertIn(
                    "pay_type", {c["name"] for c in insp.get_columns("employee")}
                )
        finally:
            os.environ["DATABASE_URL"] = prev or "sqlite:///:memory:"


if __name__ == "__main__":
    unittest.main()
