"""SMS Phase 1: Ghana mobile normalisation and the contact predicate.

One rule decides whether a number is reachable (``normalise_gh_mobile``), and
every place that counts contacts goes through it: ``Employee.has_contact``, the
roster badge and the import-time ``no_contact`` warning. The 9-digit case is the
one production depends on: every live MoMo number lost its leading 0 to Excel.
"""
import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app, db  # noqa: E402
from app.distribution.phones import (  # noqa: E402
    GH_MOBILE_PREFIXES,
    mask_msisdn,
    normalise_gh_mobile,
)
from app.models import Employee, User  # noqa: E402
from app.payroll import crossref_employee_records  # noqa: E402


class NormaliseGhMobileTests(unittest.TestCase):
    def test_every_accepted_form_gives_the_same_number(self):
        for raw in (
            "0241234567",
            "+233241234567",
            "233241234567",
            "241234567",  # Excel dropped the 0
            "024 123 4567",
            "024-123-4567",
            "(024) 123.4567",
            "+233 24 123 4567",
            " 0241234567 ",
            241234567,  # a number cell read as an int
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalise_gh_mobile(raw), "233241234567")

    def test_every_listed_prefix_is_accepted_in_every_form(self):
        for prefix in sorted(GH_MOBILE_PREFIXES):
            for raw in (f"0{prefix}1234567", f"233{prefix}1234567",
                        f"+233{prefix}1234567", f"{prefix}1234567"):
                with self.subTest(raw=raw):
                    self.assertEqual(normalise_gh_mobile(raw), f"233{prefix}1234567")

    def test_the_prefix_list_is_exactly_the_decided_networks(self):
        self.assertEqual(
            GH_MOBILE_PREFIXES,
            {"20", "50", "24", "25", "53", "54", "55", "59", "26", "27", "56", "57"},
        )

    def test_rejects(self):
        for raw in (
            None, "", "   ",
            "0302123456",        # Accra landline
            "302123456",         # the same landline, 0 stripped
            "0231234567",        # Glo, excluded (Q1)
            "0281234567",        # Expresso, excluded (Q1)
            "231234567",         # Glo, 0 stripped
            "281234567",         # Expresso, 0 stripped
            "233231234567",      # Glo, international form
            "024123456",         # 9 digits starting with 0
            "02412345678",       # 11 digits
            "24123456",          # 8 digits
            "2332412345678",     # 13 digits
            "+0241234567",       # + without 233
            "+447700900123",     # another country
            "00233241234567",    # international dialling prefix
            "024123456a",        # a letter
            "MoMo 0241234567",
            "０２４１２３４５６７",  # full-width digits
            "024١٢٣٤٥٦٧",        # Arabic-Indic digits
            "241234567.0",       # a float cell as text: refused, not guessed at
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(normalise_gh_mobile(raw))


class MaskMsisdnTests(unittest.TestCase):
    def test_masks_to_the_planned_shape(self):
        self.assertEqual(mask_msisdn("233244123347"), "23324***347")
        self.assertEqual(mask_msisdn("0244123347"), "23324***347")
        self.assertEqual(mask_msisdn("244123347"), "23324***347")

    def test_empty_and_short_values(self):
        self.assertEqual(mask_msisdn(None), "")
        self.assertEqual(mask_msisdn(""), "")
        self.assertEqual(mask_msisdn("12345"), "***")

    def test_never_shows_the_middle_digits(self):
        masked = mask_msisdn("0244987347")
        self.assertNotIn("987", masked)


class HasContactTests(unittest.TestCase):
    def _employee(self, **contact):
        values = {"email": None, "phone": None, "momo_number": None}
        values.update(contact)
        return Employee(staff_id="X1", full_name="Test Worker", **values)

    def test_a_valid_momo_number_counts(self):
        self.assertTrue(self._employee(momo_number="0241234567").has_contact)

    def test_a_nine_digit_momo_number_counts_once_the_zero_is_restored(self):
        self.assertTrue(self._employee(momo_number="241234567").has_contact)

    def test_invalid_numbers_do_not_count(self):
        for number in ("12345", "0302123456", "0231234567", "0281234567", "n/a"):
            with self.subTest(number=number):
                self.assertFalse(self._employee(momo_number=number).has_contact)
                self.assertFalse(self._employee(phone=number).has_contact)

    def test_email_still_counts(self):
        self.assertTrue(self._employee(email="worker@example.test").has_contact)

    def test_a_bad_phone_does_not_hide_a_good_momo_number(self):
        self.assertTrue(
            self._employee(phone="0302123456", momo_number="241234567").has_contact
        )

    def test_nothing_at_all(self):
        self.assertFalse(self._employee().has_contact)


class ImportNoContactWarningTests(unittest.TestCase):
    """The import warning reads ``has_contact`` now, not its own copy."""

    CASES = {
        "SMS-VALIDMOMO": {"momo_number": "0241234567"},
        "SMS-9DIGIT": {"momo_number": "241234567"},
        "SMS-EMAIL": {"email": "worker@example.test"},
        "SMS-LANDLINE": {"phone": "0302123456"},
        "SMS-GLO": {"momo_number": "0231234567"},
        "SMS-EXPRESSO": {"phone": "0281234567"},
        "SMS-GARBAGE": {"momo_number": "12345"},
        "SMS-NOTHING": {},
    }

    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.company_id = (
            User.query.filter_by(email="admin@msc.com").first().client_company_id
        )
        for staff_id, contact in self.CASES.items():
            values = {"email": None, "phone": None, "momo_number": None}
            values.update(contact)
            db.session.add(Employee(
                client_company_id=self.company_id, staff_id=staff_id,
                full_name=staff_id.title(), status="Active", **values,
            ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_only_unreachable_workers_are_warned_about(self):
        rows = [{"staff_id": staff_id} for staff_id in self.CASES]
        _, no_contact = crossref_employee_records(self.company_id, rows)
        self.assertEqual(
            sorted(r["staff_id"] for r in no_contact),
            ["SMS-EXPRESSO", "SMS-GARBAGE", "SMS-GLO", "SMS-LANDLINE", "SMS-NOTHING"],
        )

    def test_the_warning_and_the_property_agree_on_every_worker(self):
        rows = [{"staff_id": staff_id} for staff_id in self.CASES]
        _, no_contact = crossref_employee_records(self.company_id, rows)
        warned = {r["staff_id"] for r in no_contact}
        for employee in Employee.query.filter(Employee.staff_id.in_(self.CASES)).all():
            with self.subTest(staff_id=employee.staff_id):
                self.assertEqual(employee.staff_id in warned, not employee.has_contact)


if __name__ == "__main__":
    unittest.main()
