"""`tools/fix_env_proxy.py` must fix EVERY USE_PROXY line, not just the first.

The 2026-11-11 deployment had a `USE_PROXY=false` line the operator had just
edited plus a later `export USE_PROXY=true`; dotenv keeps the last one, so
`docker compose config` still rendered `USE_PROXY: "true"` and the voice clients
hung on the HTTP payproxy.  A single `sed 's/^USE_PROXY=/.../'` cannot fix that,
which is exactly why this tool exists.
"""

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "fix_env_proxy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fix_env_proxy_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FixEnvProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_every_duplicate_is_rewritten(self):
        lines = ["TZ=Asia/Tehran\n", "USE_PROXY=true\n", "SOCKS5_HOST=payproxy\n",
                 "SOCKS5_PORT=8888\n", "export USE_PROXY=true\n", "  USE_PROXY = true\n"]
        new, changes = self.mod.plan_changes(lines)
        text = "".join(new)
        self.assertEqual(text.count("USE_PROXY=true"), 3)
        self.assertNotIn("payproxy", text)
        self.assertNotIn("8888", text)
        self.assertIn("SOCKS5_HOST=127.0.0.1", text)
        self.assertIn("SOCKS5_PORT=1080", text)
        self.assertTrue(any("duplicate" in c for c in changes))

    def test_credentials_and_payment_keys_are_untouched(self):
        lines = ["SOCKS5_USERNAME=user\n", "SOCKS5_PASSWORD=pw\n",
                 "PAYMENT_HTTP_PROXY=http://payproxy:8888\n", "USE_PROXY=true\n"]
        new, _ = self.mod.plan_changes(lines)
        text = "".join(new)
        self.assertIn("SOCKS5_USERNAME=user", text)
        self.assertIn("SOCKS5_PASSWORD=pw", text)
        self.assertIn("PAYMENT_HTTP_PROXY=http://payproxy:8888", text)

    def test_keep_socks_leaves_the_endpoint_alone(self):
        lines = ["SOCKS5_HOST=payproxy\n", "SOCKS5_PORT=8888\n", "USE_PROXY=false\n"]
        new, _ = self.mod.plan_changes(lines, socks_host=None, socks_port=None)
        text = "".join(new)
        self.assertIn("SOCKS5_HOST=payproxy", text)
        self.assertIn("USE_PROXY=true", text)

    def test_missing_keys_are_appended(self):
        new, changes = self.mod.plan_changes(["TZ=UTC\n"])
        text = "".join(new)
        self.assertIn("USE_PROXY=true", text)
        self.assertIn("VOICE_PROXY_REQUIRED=false", text)
        self.assertTrue(all(any(key in c for c in changes)
                            for key in ("USE_PROXY", "VOICE_PROXY_REQUIRED")))

    def test_file_is_written_once_with_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("A=1\nUSE_PROXY=false\nexport USE_PROXY=false\n", encoding="utf-8")
            changes = self.mod.fix_env(str(env))
            self.assertTrue(any("backup" in c for c in changes))
            self.assertEqual(env.read_text(encoding="utf-8").count("USE_PROXY=true"), 2)
            backups = list(Path(tmp).glob(".env.bak-*"))
            self.assertEqual(len(backups), 1)
            # idempotent: a second run changes nothing
            self.assertEqual(self.mod.fix_env(str(env)), [])

    def test_dry_run_does_not_touch_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("USE_PROXY=false\n", encoding="utf-8")
            self.mod.fix_env(str(env), dry_run=True)
            self.assertIn("USE_PROXY=false", env.read_text(encoding="utf-8"))

    def test_cli_reports_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("USE_PROXY=false\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main(["--env", str(env)])
            self.assertEqual(code, 0)
            self.assertIn("USE_PROXY", buf.getvalue())
            self.assertIn("USE_PROXY=true", env.read_text(encoding="utf-8"))

    def test_missing_file_is_an_error(self):
        self.assertEqual(self.mod.main(["--env", "/nonexistent/.env"]), 2)

    def test_warp_socks5_is_the_default_target(self):
        # The deployment's own topology: voice clients ride the WARP container's
        # SOCKS5 at 127.0.0.1:1080 (not the HTTP payproxy).
        new, _ = self.mod.plan_changes(["USE_PROXY=false\n"])
        text = "".join(new)
        self.assertIn("USE_PROXY=true", text)
        self.assertIn("SOCKS5_HOST=127.0.0.1", text)
        self.assertIn("SOCKS5_PORT=1080", text)

    def test_use_proxy_false_is_available_for_deliberate_direct_runs(self):
        new, _ = self.mod.plan_changes(["USE_PROXY=true\n"], use_proxy="false")
        self.assertIn("USE_PROXY=false", "".join(new))


if __name__ == "__main__":
    unittest.main()
