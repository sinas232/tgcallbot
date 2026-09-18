"""
Offline tests for the admin balance-change report builder
(services/payment_reporter.py — گزارش افزایش/کاهش موجودی ادمین در کانال
گزارشات پرداختی).

Run:
    python -m unittest tests.test_payment_reporter -v
"""

from __future__ import annotations

import os
import unittest

from services.payment_reporter import build_balance_change_report  # noqa: E402


class BalanceChangeReportTests(unittest.TestCase):
    def setUp(self):
        self.kwargs = dict(
            admin_name="Sina",
            admin_tg_id=111,
            target_name="Ali",
            target_tg_id=222,
            amount=+500_000,
            new_balance=1_200_000,
            note="تغییر موجودی افزایش از پنل ادمین",
            when_str="۲۷ شهریور ۱۴۰۵، ساعت ۱۲:۰۰",
            bot_id=1,
        )

    def test_increase_report_contains_all_fields(self):
        txt = build_balance_change_report(**self.kwargs)
        self.assertIn("افزایش (شارژ)", txt)
        self.assertIn("500,000", txt)
        self.assertIn("1,200,000", txt)
        self.assertIn("Ali", txt)
        self.assertIn("222", txt)
        self.assertIn("Sina", txt)
        self.assertIn("111", txt)
        self.assertIn("📈", txt)

    def test_decrease_report_wording(self):
        txt = build_balance_change_report(**{**self.kwargs, "amount": -300_000})
        self.assertIn("کاهش", txt)
        self.assertIn("300,000", txt)   # قدرمطلق مبلغ
        self.assertNotIn("-300,000", txt)
        self.assertIn("📉", txt)

    def test_zero_amount_counts_as_increase(self):
        txt = build_balance_change_report(**{**self.kwargs, "amount": 0})
        self.assertIn("افزایش (شارژ)", txt)

    def test_empty_note_fallback(self):
        txt = build_balance_change_report(**{**self.kwargs, "note": "   "})
        self.assertIn("---", txt)

    def test_never_raises_on_bad_numbers(self):
        txt = build_balance_change_report(**{**self.kwargs, "amount": "abc", "new_balance": None})
        self.assertIsInstance(txt, str)
        self.assertIn("0", txt)


if __name__ == "__main__":
    unittest.main()
