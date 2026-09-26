"""Tests for services/env_sanity.py.

باگِ واقعی که این ماژول برایش ساخته شد: روی سرور، `.env` هم
`MAX_CONCURRENT_ORDERS=10` (خط ۴۹) را داشت و هم `MAX_CONCURRENT_ORDERS=5`
(خط ۱۸۰)، چون یک اسکریپت `sed` مقدار تازه را به انتهای فایل «اضافه» کرد به‌جای
اینکه خط قبلی را جایگزین کند. dotenv آخرین مقدار را نگه می‌دارد، پس ربات ۵ را
خواند و همه‌چیز درست به نظر رسید — ولی مین سر جایش ماند.

Run:
    python -m pytest tests/test_env_sanity.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.env_sanity import (  # noqa: E402
    check_and_warn,
    describe_duplicates,
    find_duplicate_keys,
)


def _write(text):
    fd, path = tempfile.mkstemp(prefix="env-", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class FindDuplicateKeysTests(unittest.TestCase):
    def test_the_exact_server_scenario(self):
        p = _write("BOT_TOKEN=abc\nMAX_CONCURRENT_ORDERS=10\nFOO=1\n"
                   "MAX_CONCURRENT_ORDERS=5\n")
        d = find_duplicate_keys(p)
        self.assertEqual(list(d), ["MAX_CONCURRENT_ORDERS"])
        self.assertEqual(d["MAX_CONCURRENT_ORDERS"], [(2, "10"), (4, "5")])

    def test_clean_file_reports_nothing(self):
        p = _write("A=1\nB=2\nC=3\n")
        self.assertEqual(find_duplicate_keys(p), {})

    def test_comments_and_blank_lines_are_not_keys(self):
        p = _write("# A=1\n\n;A=2\nA=3\n[section]\nA=4\n")
        # only the two real definitions count, so this IS a duplicate pair
        d = find_duplicate_keys(p)
        self.assertEqual(list(d), ["A"])
        self.assertEqual(d["A"], [(4, "3"), (6, "4")])

    def test_export_prefix_counts_as_a_definition(self):
        p = _write("A=1\nexport A=2\n")
        self.assertEqual(find_duplicate_keys(p)["A"], [(1, "1"), (2, "2")])

    def test_empty_value_is_still_a_definition(self):
        p = _write("A=\nA=1\n")
        self.assertEqual(find_duplicate_keys(p)["A"], [(1, ""), (2, "1")])

    def test_value_containing_equals_keeps_only_the_first_split(self):
        p = _write("DATABASE_URL=postgres://u:p=x@h/db\nDATABASE_URL=y\n")
        d = find_duplicate_keys(p)
        self.assertEqual(d["DATABASE_URL"][0], (1, "postgres://u:p=x@h/db"))

    def test_whitespace_around_key_and_value_is_stripped(self):
        p = _write("  A  =  1  \nA=2\n")
        self.assertEqual(find_duplicate_keys(p)["A"], [(1, "1"), (2, "2")])

    def test_missing_file_returns_empty_and_does_not_raise(self):
        self.assertEqual(find_duplicate_keys("/nonexistent/.env"), {})

    def test_line_with_no_equals_is_ignored(self):
        p = _write("JUST_A_WORD\nA=1\n")
        self.assertEqual(find_duplicate_keys(p), {})

    def test_three_occurrences_are_all_reported_in_order(self):
        p = _write("A=1\nA=2\nA=3\n")
        self.assertEqual(find_duplicate_keys(p)["A"], [(1, "1"), (2, "2"), (3, "3")])


class DescribeDuplicatesTests(unittest.TestCase):
    def test_names_the_winning_line_because_last_wins(self):
        lines = describe_duplicates({"A": [(2, "10"), (4, "5")]})
        self.assertEqual(len(lines), 1)
        self.assertIn("خط 4", lines[0])
        self.assertIn("5", lines[0])

    def test_output_is_sorted_by_key(self):
        lines = describe_duplicates({"Z": [(1, "1"), (2, "2")],
                                     "A": [(1, "1"), (2, "2")]})
        self.assertTrue(lines[0].startswith("A"))
        self.assertTrue(lines[1].startswith("Z"))

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(describe_duplicates({}), [])


class CheckAndWarnTests(unittest.TestCase):
    def test_quiet_mode_still_returns_the_findings(self):
        p = _write("A=1\nA=2\n")
        self.assertEqual(list(check_and_warn(p, quiet=True)), ["A"])

    def test_clean_file_returns_empty(self):
        self.assertEqual(check_and_warn(_write("A=1\n")), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(check_and_warn("/nonexistent/.env"), {})

    def test_writes_the_banner_to_stderr_when_not_quiet(self):
        import io
        import contextlib
        p = _write("MAX_CONCURRENT_ORDERS=10\nMAX_CONCURRENT_ORDERS=5\n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            check_and_warn(p)
        out = buf.getvalue()
        self.assertIn("MAX_CONCURRENT_ORDERS", out)
        self.assertIn("کلید تکراری", out)

    def test_never_raises_on_binary_garbage(self):
        fd, p = tempfile.mkstemp(prefix="env-bin-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\x00\x01\xff\xfeA=1\nA=2\n")
        try:
            check_and_warn(p, quiet=True)      # must not raise
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
