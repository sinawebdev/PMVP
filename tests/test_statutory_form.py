"""The Add Statutory Rate Version form: PAYE bands as rows, not JSON.

The operator types each band's starting income and rate; the tax on income
below each band is calculated, and must reproduce the published GRA table.
A mistake re-renders the form with what was typed instead of throwing it away.
"""

import json
import os
import unittest

os.environ["SKIP_DOTENV"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PERSISTENCE_REQUIRED"] = "false"

from app import create_app  # noqa: E402
from app.models import StatutoryRate, User  # noqa: E402
from app.statutory import bands_from_rows  # noqa: E402

# GRA monthly bands from September 2026 (migration 8b62026a0901), as typed.
SEPT_2026_ROWS = [(588, 5), (668, 10), (768, 17.5), (3668, 25), (19668, 30), (50000, 35)]
SEPT_2026_BASES = {588: 0, 668: 4.00, 768: 14.00, 3668: 521.50, 19668: 4521.50, 50000: 13621.10}


class BandMathTests(unittest.TestCase):
    def test_bases_reproduce_the_published_table(self):
        bands = bands_from_rows(SEPT_2026_ROWS)
        self.assertEqual([b["over"] for b in bands], sorted(SEPT_2026_BASES, reverse=True))
        for band in bands:
            self.assertAlmostEqual(band["base"], SEPT_2026_BASES[band["over"]], places=2)
        self.assertAlmostEqual(next(b for b in bands if b["over"] == 768)["rate"], 0.175)

    def test_order_typed_does_not_matter(self):
        self.assertEqual(bands_from_rows(list(reversed(SEPT_2026_ROWS))), bands_from_rows(SEPT_2026_ROWS))

    def test_rejects_bad_bands(self):
        for rows in ([], [(100, 5), (100, 10)], [(100, 150)], [(-5, 5)], [(float("nan"), 5)]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    bands_from_rows(rows)


class StatutoryFormTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        admin = User.query.filter(User.role == "admin", User.client_company_id.is_(None)).first()
        self.client.post("/login", data={"email": admin.email, "password": "password123"})

    def tearDown(self):
        self.ctx.pop()

    def _form(self, rows=SEPT_2026_ROWS, **overrides):
        data = {
            "effective_from": "2031-01-01",
            "ssf_employee_rate": "0.055", "ssf_employer_rate": "0.13",
            "overtime_rate_low": "0.05", "overtime_rate_high": "0.10",
            "overtime_basic_threshold": "0.5", "bonus_rate": "0.05",
            "bonus_annual_basic_threshold": "0.15", "overtime_junior_monthly_threshold": "1500",
            "icu_member_rate": "0.03", "ssnit_limits_year": "2026",
            "ssnit_min_insurable": "587.80", "ssnit_max_insurable": "69000",
            "ssnit_min_monthly_remittance": "79.40", "provident_fund_tax_limit": "0.165",
            "overtime_qualifying_annual_limit": "18000", "notes": "GRA notice test",
            "band_over": [str(o) for o, _ in rows], "band_rate": [str(r) for _, r in rows],
        }
        data.update(overrides)
        return data

    def test_form_has_band_rows_and_no_json_box(self):
        html = self.client.get("/statutory-rates/new").get_data(as_text=True)
        self.assertIn('name="band_over"', html)
        self.assertIn('name="band_rate"', html)
        self.assertNotIn("paye_bands_json", html)
        self.assertNotIn("JSON", html)

    def test_saving_rows_stores_calculated_bands(self):
        resp = self.client.post("/statutory-rates/new", data=self._form())
        self.assertEqual(resp.status_code, 302)
        rate = StatutoryRate.query.filter_by(notes="GRA notice test").one()
        stored = {b["over"]: b["base"] for b in json.loads(rate.paye_bands_json)}
        for over, base in SEPT_2026_BASES.items():
            self.assertAlmostEqual(stored[over], base, places=2)

    def test_a_bad_band_rerenders_with_what_was_typed(self):
        resp = self.client.post("/statutory-rates/new", data=self._form(rows=[(588, 5), (588, 10)]))
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("Two bands start at the same income", html)
        self.assertIn('value="2031-01-01"', html)
        self.assertIn('value="GRA notice test"', html)
        self.assertEqual(StatutoryRate.query.filter_by(notes="GRA notice test").count(), 0)

    def test_legacy_json_with_a_null_band_is_a_400_not_a_500(self):
        data = self._form()
        del data["band_over"], data["band_rate"]
        data["paye_bands_json"] = json.dumps([None])
        resp = self.client.post("/statutory-rates/new", data=data)
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
