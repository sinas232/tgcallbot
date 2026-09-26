"""Tests for tools/dedup_env.py.

مهم‌ترین اینواریانت: بعد از حذف تکراری‌ها، **مقدار مؤثرِ هر کلید باید دقیقاً همان
باشد که dotenv قبلاً انتخاب می‌کرد** (آخرین مقدار). اگر این بشکند، ابزار به‌جای
تمیز کردن فایل، پیکربندی ربات را عوض کرده است.

Run:
    python -m pytest tests/test_dedup_env.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import dedup_env  # noqa: E402
from services.env_sanity import find_duplicate_keys  # noqa: E402


def _env(text):
    fd, path = tempfile.mkstemp(prefix="env-", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _effective(path):
    """Parse the file the way python-dotenv does (last occurrence wins)."""
    out = {}
    for line in _read(path).splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


class PlanTests(unittest.TestCase):
    def test_the_exact_server_scenario(self):
        p = _env("BOT_TOKEN=abc\nMAX_CONCURRENT_ORDERS=10\nFOO=1\n"
                 "MAX_CONCURRENT_ORDERS=5\n")
        drop, dups = dedup_env.plan(p)
        self.assertEqual(drop, [2])            # line 2 is the stale 10
        self.assertEqual(list(dups), ["MAX_CONCURRENT_ORDERS"])

    def test_keeps_the_last_occurrence(self):
        p = _env("A=1\nA=2\nA=3\n")
        drop, _ = dedup_env.plan(p)
        self.assertEqual(drop, [1, 2])         # line 3 survives

    def test_clean_file_plans_nothing(self):
        self.assertEqual(dedup_env.plan(_env("A=1\nB=2\n")), ([], {}))

    def test_comments_and_blanks_are_never_planned_for_removal(self):
        p = _env("# A=1\n\n;A=2\nA=3\nA=4\n")
        drop, _ = dedup_env.plan(p)
        self.assertEqual(drop, [4])

    def test_export_prefix_is_recognised(self):
        p = _env("A=1\nexport A=2\n")
        drop, dups = dedup_env.plan(p)
        self.assertEqual(drop, [1])
        self.assertEqual(list(dups), ["A"])

    def test_value_containing_equals_is_not_split_twice(self):
        p = _env("DATABASE_URL=postgres://u:p=x@h/db\nDATABASE_URL=y\n")
        drop, _ = dedup_env.plan(p)
        self.assertEqual(drop, [1])


class ApplyTests(unittest.TestCase):
    def test_effective_values_are_identical_before_and_after(self):
        """The invariant that matters."""
        text = ("BOT_TOKEN=abc\nMAX_CONCURRENT_ORDERS=10\n# c\nFOO=1\n"
                "MAX_CONCURRENT_ORDERS=5\nDATABASE_URL=postgres://u:p=x@h/db\n"
                "FOO=2\n")
        p = _env(text)
        before = _effective(p)
        dedup_env.apply(p, backup=False)
        self.assertEqual(_effective(p), before)
        self.assertEqual(find_duplicate_keys(p), {})

    def test_backup_is_created_by_default(self):
        p = _env("A=1\nA=2\n")
        dedup_env.apply(p)
        self.assertTrue(os.path.exists(p + ".bak"))
        self.assertEqual(_read(p + ".bak"), "A=1\nA=2\n")

    def test_comments_and_order_of_survivors_are_preserved(self):
        p = _env("A=1\n# keep me\nB=2\nA=3\n")
        dedup_env.apply(p, backup=False)
        self.assertEqual(_read(p), "# keep me\nB=2\nA=3\n")

    def test_apply_is_idempotent(self):
        p = _env("A=1\nA=2\nB=1\n")
        dedup_env.apply(p, backup=False)
        once = _read(p)
        self.assertEqual(dedup_env.apply(p, backup=False), ([], {}))
        self.assertEqual(_read(p), once)

    def test_apply_on_a_clean_file_is_a_no_op(self):
        p = _env("A=1\nB=2\n")
        self.assertEqual(dedup_env.apply(p, backup=False), ([], {}))
        self.assertEqual(_read(p), "A=1\nB=2\n")


class CliTests(unittest.TestCase):
    def test_dry_run_does_not_modify_the_file(self):
        p = _env("A=1\nA=2\n")
        self.assertEqual(dedup_env.main(["--path", p]), 0)
        self.assertEqual(_read(p), "A=1\nA=2\n")

    def test_write_modifies_the_file(self):
        p = _env("A=1\nA=2\n")
        self.assertEqual(dedup_env.main(["--path", p, "--write", "--no-backup"]), 0)
        self.assertEqual(_read(p), "A=2\n")

    def test_missing_file_returns_exit_code_2(self):
        self.assertEqual(dedup_env.main(["--path", "/nonexistent/.env"]), 2)

    def test_clean_file_reports_success(self):
        self.assertEqual(dedup_env.main(["--path", _env("A=1\n")]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
