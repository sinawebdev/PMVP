"""What the product says has to match what it does.

Four defects found while running the first live SMS and email through production:
exports naming the wrong employer outside the bureau's own site, an import preview
saying rows failed and then importing them, a channel with no provider being offered
beside live ones, and the payslip page showing a letter instead of the logo.
"""
import os
import tempfile
import unittest
from io import BytesIO
from unittest import mock

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from openpyxl import load_workbook  # noqa: E402

from app import create_app, db  # noqa: E402
from app.distribution.channels import (  # noqa: E402
    channel_refused,
    refusal_message,
    sendable_channels,
    simulated_channels,
)
from app.distribution.service import resolve_channel  # noqa: E402
from app.distribution.tokens import issue_payslip_token  # noqa: E402
from app.models import ClientCompany, Employee, PayrollItem, PayrollRun, User  # noqa: E402
from app.payroll_status import APPROVED  # noqa: E402

OPERATOR_EXPORTS = ("/export/gra-paye", "/export/bank-listing", "/export/wages-sheet")
# SMS and email connected, WhatsApp not: production since 2026-11-09.
PARTLY_LIVE = dict(SMS_BACKEND="sasusync", EMAIL_BACKEND="apps_script", WHATSAPP_BACKEND="console")


class _SeededCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, EXPORT_FOLDER=self.temp_dir.name)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        tenant = User.query.filter_by(email="admin@msc.com").first()
        self.run = PayrollRun.query.filter_by(client_company_id=tenant.client_company_id).first()
        self.run.status = APPROVED
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        self.temp_dir.cleanup()

    def _login(self, email="admin@payrolla.com"):
        response = self.client.post("/login", data={"email": email, "password": "password123"})
        self.assertEqual(response.status_code, 302)

    def _status_page(self):
        return self.client.get(f"/distribution/run/{self.run.id}").get_data(as_text=True)


class EmployerOnOperatorExportsTests(_SeededCase):
    def _workbook(self, path):
        response = self.client.get(f"/payroll/runs/{self.run.id}{path}")
        self.assertEqual(response.status_code, 200, path)
        return load_workbook(BytesIO(response.data))

    def _text(self, path):
        cells = (cell.value for sheet in self._workbook(path).worksheets
                 for row in sheet.iter_rows() for cell in row)
        return "\n".join(str(value) for value in cells if value is not None)

    def test_the_bureau_stays_employer_of_record_on_the_live_site(self):
        self._login()
        self.assertEqual(self.app.config["OPERATOR_EMPLOYER_NAME"], "Chrisnat Limited")
        self.assertEqual(self._workbook("/export/gra-paye").active["D9"].value, "CHRISNAT LIMITED")
        self.assertEqual(self._workbook("/export/bank-listing").active["A1"].value, "Chrisnat Limited")

    def test_without_a_bureau_every_export_names_the_runs_own_company(self):
        self.app.config["OPERATOR_EMPLOYER_NAME"] = ""
        self._login()
        company = self.run.client_company.name
        self.assertEqual(self._workbook("/export/gra-paye").active["D9"].value, company)
        for path in OPERATOR_EXPORTS:
            with self.subTest(path=path):
                text = self._text(path)
                self.assertIn(company, text)
                self.assertNotIn("chrisnat", text.lower())

    def test_without_a_bureau_its_tin_is_not_stamped_on_someone_elses_form(self):
        self.app.config["OPERATOR_EMPLOYER_NAME"] = ""
        self._login()
        with mock.patch.dict(os.environ, {"CHRISNAT_EMPLOYER_TIN": "C0000000000"}):
            self.assertNotIn("C0000000000", self._text("/export/gra-paye"))

    def test_the_reports_page_names_who_the_download_will_name(self):
        self._login()
        html = self.client.get(f"/payroll/runs/{self.run.id}/reports").get_data(as_text=True)
        self.assertIn("Chrisnat Limited", html)


class ImportPreviewTests(_SeededCase):
    def _preview(self, csv_text):
        company = ClientCompany.query.filter(ClientCompany.name == "Acme Manufacturing Ltd").first()
        self._login()
        response = self.client.post(
            "/payroll/runs/new",
            data={
                "import_mode": "single_client", "client_company_id": company.id,
                "month": "March", "year": "2031",
                "payroll_file": (BytesIO(csv_text.encode()), "upload.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:400])
        return response.headers["Location"], self.client.get(response.headers["Location"]).get_data(as_text=True)

    def test_warned_rows_are_not_reported_as_failed_and_they_do_import(self):
        location, html = self._preview("Staff ID,Name,Basic Salary\nQ1,Ama Mensah,1000\n")
        self.assertIn("1 row has warnings.", html)
        self.assertIn("will still import", html)
        self.assertIn("Rows with warnings", html)
        self.assertNotIn("failed validation", html)
        self.assertNotIn("Rows failing", html)

        import_id = location.rstrip("/").rsplit("/", 1)[-1]
        self.client.post(f"/payroll/confirm/{import_id}")
        run = PayrollRun.query.filter_by(month="March", year=2031).first()
        self.assertIsNotNone(run)
        self.assertEqual(PayrollItem.query.filter_by(payroll_run_id=run.id).count(), 1)

    def test_an_upload_confirm_will_refuse_does_not_promise_to_import(self):
        # A negative employer provident fund is one of confirm's hard-stops.
        _, html = self._preview(
            "Staff ID,Name,Basic Salary,Employer Provident Fund\nQ1,Ama Mensah,1000,-5\n"
        )
        self.assertNotIn("will still import", html)
        self.assertIn("employer provident fund", html)


class ChannelsOnOfferTests(_SeededCase):
    def test_a_channel_with_no_provider_is_not_offered_beside_live_ones(self):
        self.app.config.update(PARTLY_LIVE)
        self.assertEqual(sendable_channels(), ("sms", "email"))
        self.assertEqual(simulated_channels(), [])
        self.assertTrue(channel_refused("whatsapp"))
        self.assertFalse(channel_refused("auto"))
        self.assertIn("WhatsApp", refusal_message("whatsapp"))

    def test_with_nothing_connected_every_channel_stays_under_the_notice(self):
        self.app.config.update(SMS_BACKEND="console", EMAIL_BACKEND="console", WHATSAPP_BACKEND="console")
        self.assertEqual(sendable_channels(), ("sms", "whatsapp", "email"))
        self.assertEqual(simulated_channels(), ["sms", "whatsapp", "email"])

    def test_the_operator_page_drops_the_notice_and_the_whatsapp_option(self):
        self.app.config.update(PARTLY_LIVE, SASUSYNC_API_KEY="k", SMS_SENDER_ID="Payrollagh",
                               SASUSYNC_BASE_URL="https://sms.example.com",
                               PUBLIC_BASE_URL="https://payrollagh.com")
        self._login()
        html = self._status_page()
        self.assertNotIn("Delivery channels are not connected yet", html)
        self.assertNotIn('value="whatsapp"', html)
        self.assertIn('value="sms"', html)
        self.assertIn('value="email"', html)

    def test_a_direct_post_cannot_send_on_a_channel_that_is_not_offered(self):
        self.app.config.update(PARTLY_LIVE)
        self._login()
        html = self.client.post(
            f"/distribution/run/{self.run.id}/send",
            data={"channel": "whatsapp", "nonce": "n1"},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("WhatsApp isn&#39;t connected yet", html)

    def test_auto_never_routes_a_worker_to_a_channel_that_goes_nowhere(self):
        # An unusable number with no email used to land on simulated WhatsApp and
        # be recorded as sent. It now fails on SMS, where someone will see it.
        self.app.config.update(PARTLY_LIVE)
        employee = Employee.query.filter_by(client_company_id=self.run.client_company_id).first()
        employee.phone, employee.momo_number, employee.email = "12345", None, None
        employee.preferred_channel = "whatsapp"
        item = PayrollItem(payroll_run_id=self.run.id, staff_id=employee.staff_id,
                           full_name=employee.full_name, employee_id=employee.id, momo_number=None)
        db.session.add(item)
        db.session.flush()
        self.assertEqual(resolve_channel(item), "sms")
        db.session.rollback()

    def test_a_worker_cannot_be_given_a_preferred_channel_that_is_not_offered(self):
        self.app.config.update(PARTLY_LIVE)
        self._login()
        item = PayrollItem.query.filter(PayrollItem.payroll_run_id == self.run.id,
                                        PayrollItem.employee_id.isnot(None)).first()
        self.assertIsNotNone(item)
        self.client.post(f"/distribution/item/{item.id}/preferred-channel",
                         data={"preferred_channel": "whatsapp"})
        self.assertNotEqual(db.session.get(Employee, item.employee_id).preferred_channel, "whatsapp")


class CompanyEditFormTests(_SeededCase):
    def test_a_company_with_blank_fields_can_be_edited_and_they_stay_blank(self):
        company = ClientCompany(name="Blank Fields Ltd", company_code="BLANKF", status="Active")
        db.session.add(company)
        db.session.commit()
        self._login()
        html = self.client.get(f"/clients/{company.id}/edit").get_data(as_text=True)
        self.assertNotIn('value="None"', html)
        self.assertNotIn(">None</textarea>", html)
        self.assertNotIn("<td>None</td>", self.client.get("/clients").get_data(as_text=True))

        response = self.client.post(f"/clients/{company.id}/edit", data={
            "name": company.name, "company_code": company.company_code, "status": "Inactive",
            "contact_person": "", "email": "", "phone": "", "address": "",
            "location": "", "service_type": "", "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        db.session.refresh(company)
        self.assertEqual(company.status, "Inactive")
        self.assertIsNone(company.contact_person)


class PayslipPageLogoTests(_SeededCase):
    def test_the_payslip_page_shows_the_logo_not_a_letter(self):
        item = PayrollItem.query.filter_by(payroll_run_id=self.run.id).first()
        html = self.client.get(f"/p/{issue_payslip_token(item)}").get_data(as_text=True)
        self.assertIn("img/payrolla-logo.svg", html)
        self.assertNotIn('class="brand-mark"', html)

    def test_the_expired_link_page_shows_the_logo_too(self):
        html = self.client.get("/p/garbage-token").get_data(as_text=True)
        self.assertIn("img/payrolla-logo.svg", html)
        self.assertNotIn('class="brand-mark"', html)


if __name__ == "__main__":
    unittest.main()
