"""`tools/prod_join_probe.py` must stay import-safe and its matching logic exact.

The tool exists because the bare-engine probe joined in 1.9s on 2026-11-11 while
the orders reported live=0 — so the ground truth it prints ("how many of OUR
accounts does Telegram list inside the call?") decides between an accounting bug
and a media bug.  A wrong join of DB rows to Telegram user ids would answer that
question wrongly, so the comparison is pinned here.
"""

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "prod_join_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prod_join_probe_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProdJoinProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_import_is_side_effect_free(self):
        # Importing must not need DATABASE_URL, a network or the bot's config,
        # so a plain interpreter with no environment at all can load it.
        code = (
            "import importlib.util as u;"
            f"s=u.spec_from_file_location('p', {str(MODULE_PATH)!r});"
            "m=u.module_from_spec(s);s.loader.exec_module(m);"
            "assert hasattr(m,'main')"
        )
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])

    def test_chat_target_helpers_are_self_contained(self):
        # The deployed tools/join_probe.py may be an older copy, so this tool
        # must not import it (2026-11-11: AttributeError: module 'join_probe'
        # has no attribute '_resolve_chat' broke the probe mid-run).
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import join_probe", source)
        self.assertEqual(self.mod._normalise_chat_target("-1004473844619"),
                         ("id", -1004473844619))
        self.assertEqual(self.mod._normalise_chat_target("https://t.me/+AbC"),
                         ("invite", "AbC"))
        self.assertEqual(self.mod._normalise_chat_target("@grp"), ("username", "grp"))

    def test_arguments(self):
        args = self.mod._parse_args(["--account", "134", "--chat", "-1004473844619"])
        self.assertEqual(args.account, 134)
        self.assertEqual(args.chat, "-1004473844619")
        self.assertFalse(args.presence_only)
        self.assertEqual(args.order, 0)

        args = self.mod._parse_args(
            ["--account", "134", "--chat", "t.me/x", "--presence-only", "--order", "1352"])
        self.assertTrue(args.presence_only)
        self.assertEqual(args.order, 1352)

    def test_missing_chat_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                self.mod._parse_args(["--account", "134"])
        self.assertEqual(ctx.exception.code, 2)

    def test_match_accounts_dict_rows(self):
        accounts = [
            {"id": 134, "user_id": 6566128890},
            {"id": 135, "user_id": 111},
            {"id": 136, "user_id": 222},
            {"id": 137, "user_id": None},      # unusable row: ignored, not a crash
        ]
        present, missing = self.mod.match_accounts([6566128890, 222, 999], accounts)
        self.assertEqual(present, [134, 136])
        self.assertEqual(missing, [135])

    def test_match_accounts_object_rows(self):
        accounts = [SimpleNamespace(id=5, user_id=42), SimpleNamespace(id=6, user_id=7)]
        present, missing = self.mod.match_accounts([42], accounts)
        self.assertEqual((present, missing), ([5], [6]))

    def test_match_accounts_empty_input(self):
        self.assertEqual(self.mod.match_accounts([], None), ([], []))
        self.assertEqual(self.mod.match_accounts(None, []), ([], []))

    def test_proxy_line_reports_the_circuit_state(self):
        class FakeVcm:
            def __init__(self, open_):
                self._open = open_

            def _voice_proxy_circuit_open(self):
                return self._open

        proxy = {"hostname": "payproxy", "port": 8888}
        self.assertEqual(self.mod._proxy_line(FakeVcm(False), None), "off")
        self.assertIn("preflight decides", self.mod._proxy_line(FakeVcm(False), proxy))
        self.assertIn("circuit=OPEN", self.mod._proxy_line(FakeVcm(True), proxy))

    def test_tool_is_executable_from_the_repo_root(self):
        self.assertTrue(os.access(MODULE_PATH, os.R_OK))
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("if __name__ == \"__main__\":", text)


if __name__ == "__main__":
    unittest.main()
