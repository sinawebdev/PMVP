"""Expense receipt attachments at the route level.

Covers the HTTP surface: uploading through the expense form, the inline preview
and the download, replacing and removing a receipt (including that the stored
object really goes away), and the two gates around all of it —

  * **Authorization** — a tenant role outside ``EXPENSE_ROLES`` may read a
    receipt but not change one; anonymous and platform users never reach the
    tenant plane at all.
  * **Isolation** — another tenant's receipt is a 404, never a 403 that would
    confirm it exists.

The layers underneath — the storage backend contract and the file-format rules —
are tested directly in ``test_receipt_storage.py``, which is also where the byte
fixtures used here are defined.

Each test app gets its own temp STORAGE_ROOT, so nothing is written into
``instance/`` and parallel apps never share a backend.
"""

import os
import shutil
import tempfile
import unittest
from datetime import date

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.models import ClientCompany, Expense, ExpenseReceipt, User  # noqa: E402
from app.roles import CLIENT_ADMIN, CLIENT_PREPARER  # noqa: E402
from app.storage import get_storage  # noqa: E402
from tests.test_receipt_storage import (  # noqa: E402
    JPEG_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    upload,
)


class ReceiptTestBase(unittest.TestCase):
    def setUp(self):
        self.storage_root = tempfile.mkdtemp(prefix="receipts_test_")
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["STORAGE_ROOT"] = self.storage_root
        # Drop the cached backend so it is rebuilt against the temp root.
        self.app.extensions.pop("payrolla_storage", None)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        companies = ClientCompany.query.order_by(ClientCompany.id).all()
        self.company = companies[0]
        self.other_company = companies[1]
        self.admin = self._tenant_user("receipt.admin@test.local", CLIENT_ADMIN, self.company)
        self.preparer = self._tenant_user(
            "receipt.preparer@test.local", CLIENT_PREPARER, self.company
        )
        # A tenant user whose role is NOT in EXPENSE_ROLES: may view the plane,
        # may not mutate it.
        self.readonly = self._tenant_user(
            "receipt.readonly@test.local", "client_viewer", self.company
        )
        self.outsider = self._tenant_user(
            "receipt.outsider@test.local", CLIENT_ADMIN, self.other_company
        )

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()
        shutil.rmtree(self.storage_root, ignore_errors=True)

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
        elif user.role != role or user.client_company_id != company.id:
            user.role = role
            user.client_company_id = company.id
            db.session.commit()
        return user

    def _login(self, user):
        self.client.get("/logout")
        resp = self.client.post(
            "/login", data={"email": user.email, "password": "password123"}
        )
        self.assertEqual(resp.status_code, 302)

    def _payload(self, **overrides):
        data = {
            "expense_date": date.today().isoformat(),
            "category": "Fuel",
            "description": "Generator diesel",
            "amount": "300",
            "notes": "",
        }
        data.update(overrides)
        return data

    def _add_with_receipt(self, content=PDF_BYTES, filename="invoice.pdf",
                          content_type="application/pdf", **overrides):
        data = self._payload(**overrides)
        data["receipt"] = upload(content, filename, content_type)
        return self.client.post(
            "/company/expenses/add", data=data, content_type="multipart/form-data"
        )

    def _latest_expense(self):
        return (
            Expense.query.filter_by(client_company_id=self.company.id)
            .order_by(Expense.id.desc())
            .first()
        )

    def _make_expense_with_receipt(self, user=None, **kwargs):
        self._login(user or self.admin)
        resp = self._add_with_receipt(**kwargs)
        self.assertEqual(resp.status_code, 302)
        expense = self._latest_expense()
        self.assertIsNotNone(expense.receipt)
        return expense


class ReceiptUploadTestCase(ReceiptTestBase):
    def test_upload_records_every_documented_metadata_field(self):
        expense = self._make_expense_with_receipt(
            content=PNG_BYTES, filename="fuel-receipt.png", content_type="image/png"
        )
        receipt = expense.receipt
        self.assertEqual(receipt.original_filename, "fuel-receipt.png")
        self.assertEqual(receipt.content_type, "image/png")
        self.assertEqual(receipt.byte_size, len(PNG_BYTES))
        self.assertIsNotNone(receipt.uploaded_at)
        self.assertEqual(receipt.uploaded_by, self.admin.id)
        self.assertEqual(receipt.client_company_id, self.company.id)
        self.assertTrue(receipt.storage_key.startswith(f"receipts/{self.company.id}/"))
        # And the bytes really landed in storage.
        self.assertTrue(get_storage().exists(receipt.storage_key))

    def test_every_documented_format_uploads_through_the_form(self):
        cases = [
            (PDF_BYTES, "r.pdf", "application/pdf"),
            (PNG_BYTES, "r.png", "image/png"),
            (JPEG_BYTES, "r.jpg", "image/jpeg"),
            (JPEG_BYTES, "r.jpeg", "image/jpeg"),
        ]
        self._login(self.admin)
        for content, filename, declared in cases:
            with self.subTest(filename=filename):
                resp = self._add_with_receipt(
                    content=content, filename=filename, content_type=declared,
                    description=f"Spend with {filename}",
                )
                self.assertEqual(resp.status_code, 302)
                expense = Expense.query.filter_by(
                    description=f"Spend with {filename}"
                ).first()
                self.assertIsNotNone(expense.receipt)

    def test_an_expense_saves_without_a_receipt(self):
        self._login(self.admin)
        resp = self.client.post("/company/expenses/add", data=self._payload())
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self._latest_expense().receipt)

    def test_a_rejected_receipt_saves_nothing_at_all(self):
        """The expense and its receipt are one submission: a bad file must not
        leave a receiptless expense behind."""
        self._login(self.admin)
        before = Expense.query.count()
        resp = self._add_with_receipt(
            content=b"MZ\x90\x00", filename="payload.exe",
            content_type="application/octet-stream",
            description="Should not be saved",
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered, not redirected
        self.assertEqual(Expense.query.count(), before)
        self.assertIsNone(Expense.query.filter_by(description="Should not be saved").first())

    def test_a_rejected_receipt_explains_why_on_the_form(self):
        self._login(self.admin)
        body = self._add_with_receipt(
            content=b"nope", filename="notes.txt", content_type="text/plain"
        ).get_data(as_text=True)
        self.assertIn("PDF, PNG, JPG or JPEG", body)

    def test_an_oversized_upload_is_refused_with_a_clear_message(self):
        self._login(self.admin)
        oversized = PDF_BYTES + b"0" * (10 * 1024 * 1024)
        resp = self._add_with_receipt(content=oversized, filename="huge.pdf")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("10 MB", body)
        self.assertEqual(ExpenseReceipt.query.count(), 0)

    def test_a_body_over_the_global_ceiling_is_explained_not_dumped(self):
        """Werkzeug aborts a body over MAX_CONTENT_LENGTH before any view runs,
        so the receipt validator never sees it — the 413 handler has to explain
        it instead of leaving the user on a bare error page."""
        self.app.config["MAX_CONTENT_LENGTH"] = 1024  # 1 KB, to keep this fast
        self._login(self.admin)
        resp = self._add_with_receipt(content=PDF_BYTES + b"0" * 4096, filename="big.pdf")
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(ExpenseReceipt.query.count(), 0)
        body = self.client.get("/company/expenses").get_data(as_text=True)
        self.assertIn("too large", body)

    def test_uploading_a_second_receipt_replaces_the_first(self):
        expense = self._make_expense_with_receipt(
            content=PDF_BYTES, filename="first.pdf"
        )
        first_key = expense.receipt.storage_key

        data = self._payload(description=expense.description)
        data["receipt"] = upload(PNG_BYTES, "second.png", "image/png")
        resp = self.client.post(
            f"/company/expenses/{expense.id}/edit",
            data=data, content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)

        db.session.refresh(expense)
        self.assertEqual(expense.receipt.original_filename, "second.png")
        self.assertEqual(expense.receipt.content_type, "image/png")
        # Still exactly one receipt for the expense, and the superseded object
        # is gone rather than orphaned in storage.
        self.assertEqual(
            ExpenseReceipt.query.filter_by(expense_id=expense.id).count(), 1
        )
        self.assertFalse(get_storage().exists(first_key))

    def test_editing_without_choosing_a_file_keeps_the_existing_receipt(self):
        expense = self._make_expense_with_receipt(filename="keep.pdf")
        key = expense.receipt.storage_key
        resp = self.client.post(
            f"/company/expenses/{expense.id}/edit",
            data=self._payload(description="Edited description"),
        )
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(expense)
        self.assertEqual(expense.description, "Edited description")
        self.assertIsNotNone(expense.receipt)
        self.assertEqual(expense.receipt.storage_key, key)


class ReceiptDownloadTestCase(ReceiptTestBase):
    def test_download_returns_the_bytes_as_an_attachment(self):
        expense = self._make_expense_with_receipt(
            content=PDF_BYTES, filename="invoice.pdf"
        )
        resp = self.client.get(f"/company/expenses/{expense.id}/receipt/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, PDF_BYTES)
        self.assertEqual(resp.mimetype, "application/pdf")
        disposition = resp.headers["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn("invoice.pdf", disposition)

    def test_inline_view_backs_the_image_preview(self):
        expense = self._make_expense_with_receipt(
            content=PNG_BYTES, filename="scan.png", content_type="image/png"
        )
        resp = self.client.get(f"/company/expenses/{expense.id}/receipt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, PNG_BYTES)
        self.assertEqual(resp.mimetype, "image/png")
        self.assertNotIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_served_receipts_forbid_mime_sniffing(self):
        expense = self._make_expense_with_receipt()
        for path in ("receipt", "receipt/download"):
            with self.subTest(path=path):
                resp = self.client.get(f"/company/expenses/{expense.id}/{path}")
                self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

    def test_an_expense_without_a_receipt_is_404(self):
        self._login(self.admin)
        self.client.post("/company/expenses/add", data=self._payload())
        expense = self._latest_expense()
        self.assertEqual(
            self.client.get(f"/company/expenses/{expense.id}/receipt").status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/company/expenses/{expense.id}/receipt/download"
            ).status_code,
            404,
        )

    def test_a_row_whose_object_vanished_is_404_not_a_crash(self):
        expense = self._make_expense_with_receipt()
        get_storage().delete(expense.receipt.storage_key)  # out-of-band removal
        self.assertEqual(
            self.client.get(f"/company/expenses/{expense.id}/receipt").status_code, 404
        )

    def test_the_detail_page_offers_download_and_preview(self):
        expense = self._make_expense_with_receipt(
            content=PNG_BYTES, filename="scan.png", content_type="image/png"
        )
        body = self.client.get(f"/company/expenses/{expense.id}").get_data(as_text=True)
        self.assertIn("Receipt", body)
        self.assertIn("scan.png", body)
        self.assertIn(f"/company/expenses/{expense.id}/receipt/download", body)
        self.assertIn("receipt-preview", body)  # <img> preview for an image

    def test_the_detail_page_shows_a_document_mark_for_a_pdf(self):
        expense = self._make_expense_with_receipt(filename="invoice.pdf")
        body = self.client.get(f"/company/expenses/{expense.id}").get_data(as_text=True)
        self.assertIn("receipt-doc-mark", body)
        self.assertNotIn("receipt-preview", body)

    def test_the_detail_page_says_so_when_nothing_is_attached(self):
        self._login(self.admin)
        self.client.post("/company/expenses/add", data=self._payload())
        expense = self._latest_expense()
        body = self.client.get(f"/company/expenses/{expense.id}").get_data(as_text=True)
        self.assertIn("No receipt attached", body)


class ReceiptDeleteTestCase(ReceiptTestBase):
    def test_deleting_a_receipt_removes_the_row_and_the_object(self):
        expense = self._make_expense_with_receipt()
        key = expense.receipt.storage_key
        resp = self.client.post(f"/company/expenses/{expense.id}/receipt/delete")
        self.assertEqual(resp.status_code, 302)

        db.session.refresh(expense)
        self.assertIsNone(expense.receipt)
        self.assertEqual(ExpenseReceipt.query.filter_by(expense_id=expense.id).count(), 0)
        self.assertFalse(get_storage().exists(key))

    def test_deleting_twice_is_harmless(self):
        expense = self._make_expense_with_receipt()
        self.client.post(f"/company/expenses/{expense.id}/receipt/delete")
        resp = self.client.post(f"/company/expenses/{expense.id}/receipt/delete")
        self.assertEqual(resp.status_code, 302)  # a no-op, not a 500

    def test_deleting_the_expense_takes_its_receipt_and_file_with_it(self):
        expense = self._make_expense_with_receipt()
        key = expense.receipt.storage_key
        receipt_id = expense.receipt.id

        resp = self.client.post(f"/company/expenses/{expense.id}/delete")
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.session.get(Expense, expense.id))
        self.assertIsNone(db.session.get(ExpenseReceipt, receipt_id))
        self.assertFalse(get_storage().exists(key))


class ReceiptAuthorizationTestCase(ReceiptTestBase):
    def test_a_tenant_role_outside_expense_roles_may_read_but_not_mutate(self):
        expense = self._make_expense_with_receipt()

        self._login(self.readonly)
        # Reading is allowed — same as the ledger itself.
        self.assertEqual(
            self.client.get(f"/company/expenses/{expense.id}").status_code, 200
        )
        self.assertEqual(
            self.client.get(
                f"/company/expenses/{expense.id}/receipt/download"
            ).status_code,
            200,
        )
        # Mutating is not: bounced to the dashboard, and the receipt survives.
        resp = self.client.post(f"/company/expenses/{expense.id}/receipt/delete")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/company"))
        db.session.refresh(expense)
        self.assertIsNotNone(expense.receipt)

    def test_a_preparer_may_upload_and_delete(self):
        expense = self._make_expense_with_receipt(user=self.preparer)
        self.assertEqual(expense.receipt.uploaded_by, self.preparer.id)
        resp = self.client.post(f"/company/expenses/{expense.id}/receipt/delete")
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(expense)
        self.assertIsNone(expense.receipt)

    def test_anonymous_requests_are_sent_to_login(self):
        expense = self._make_expense_with_receipt()
        self.client.get("/logout")
        for path in ("", "/receipt", "/receipt/download"):
            with self.subTest(path=path):
                resp = self.client.get(f"/company/expenses/{expense.id}{path}")
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login", resp.headers["Location"])

    def test_a_platform_operator_is_bounced_off_the_tenant_receipt_routes(self):
        expense = self._make_expense_with_receipt()
        operator = User.query.filter(User.client_company_id.is_(None)).first()
        self._login(operator)
        for path in ("", "/receipt", "/receipt/download"):
            with self.subTest(path=path):
                resp = self.client.get(f"/company/expenses/{expense.id}{path}")
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.headers["Location"].endswith("/dashboard"))


class ReceiptTenantIsolationTestCase(ReceiptTestBase):
    def test_another_tenant_gets_404_on_every_receipt_route(self):
        expense = self._make_expense_with_receipt()
        key = expense.receipt.storage_key

        self._login(self.outsider)
        paths = [
            ("get", f"/company/expenses/{expense.id}"),
            ("get", f"/company/expenses/{expense.id}/receipt"),
            ("get", f"/company/expenses/{expense.id}/receipt/download"),
            ("post", f"/company/expenses/{expense.id}/receipt/delete"),
        ]
        for method, path in paths:
            with self.subTest(path=path):
                resp = getattr(self.client, method)(path)
                # 404, never 403 — a 403 would confirm the row exists.
                self.assertEqual(resp.status_code, 404)

        # Nothing was touched.
        db.session.refresh(expense)
        self.assertIsNotNone(expense.receipt)
        self.assertTrue(get_storage().exists(key))

    def test_a_receipt_is_scoped_to_its_tenant_by_the_choke_point(self):
        from app.tenancy import tenant_query

        self._make_expense_with_receipt()
        self._login(self.outsider)
        with self.app.test_request_context():
            from flask_login import login_user

            login_user(self.outsider)
            visible = tenant_query(ExpenseReceipt).all()
        self.assertEqual(visible, [])

    def test_a_receipt_carries_the_owning_tenant_not_the_uploaders_input(self):
        expense = self._make_expense_with_receipt()
        self.assertEqual(expense.receipt.client_company_id, expense.client_company_id)
        self.assertEqual(expense.receipt.client_company_id, self.company.id)

    def test_storage_keys_are_partitioned_by_company(self):
        mine = self._make_expense_with_receipt()
        self.assertIn(f"receipts/{self.company.id}/", mine.receipt.storage_key)

        self._login(self.outsider)
        data = self._payload(description="Outsider spend")
        data["receipt"] = upload(PDF_BYTES, "theirs.pdf", "application/pdf")
        self.client.post(
            "/company/expenses/add", data=data, content_type="multipart/form-data"
        )
        theirs = (
            Expense.query.filter_by(client_company_id=self.other_company.id)
            .order_by(Expense.id.desc())
            .first()
        )
        self.assertIn(f"receipts/{self.other_company.id}/", theirs.receipt.storage_key)


if __name__ == "__main__":
    unittest.main()
