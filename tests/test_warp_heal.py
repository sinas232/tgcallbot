"""`tools/warp_heal.sh` must heal the egress WITHOUT touching a live order.

2026-11-21 00:29: every new voice client died with
``ProxyError 503 Service Unavailable`` (warp SOCKS5) → DIRECT fallback →
``TimeoutError``, while order 1428 was still being served. A "just restart it"
script would have killed that order, so these tests pin the safety properties:

  * the probe is the SAME proof compose uses (TUN device + ``warp=on``),
  * the only healing action is ``warp-cli connect`` inside the warp container,
  * the bot container is never restarted/recreated by the script,
  * the fallback it prints is the idle-gated recreate,
  * the final verdict comes from ``tools/net_check.py`` inside the bot.
"""

import os
import subprocess
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT_PATH = os.path.join(REPO_ROOT, "tools", "warp_heal.sh")


class WarpHealScriptTests(unittest.TestCase):
    def setUp(self):
        with open(SCRIPT_PATH, encoding="utf-8") as handle:
            self.script = handle.read()

    def test_script_compiles(self):
        result = subprocess.run(["bash", "-n", SCRIPT_PATH], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tunnel_verdict_comes_from_dockers_own_healthcheck(self):
        """The healthcheck already proves TUN + `warp=on`; don't re-run curl.

        The first version re-ran the healthcheck with `docker compose exec` and
        hung at that step on the live server (the operator was left with a
        frozen shell), so the verdict is read from `docker inspect` instead.
        """
        self.assertIn("State.Health.Status", self.script)
        self.assertIn("docker inspect", self.script)

    def test_only_healing_action_is_warp_cli_connect(self):
        self.assertIn("warp-cli --accept-tos connect", self.script)
        self.assertIn("warp-cli connect", self.script)

    def test_never_restarts_or_recreates_containers(self):
        # Comments may NAME the forbidden commands (to explain them); executable
        # lines must never contain them.
        code = "\n".join(line for line in self.script.splitlines()
                         if not line.lstrip().startswith("#"))
        for forbidden in ("up -d", "--force-recreate", "compose restart", "docker restart"):
            self.assertNotIn(forbidden, code, f"{forbidden!r} must not be executed by warp_heal.sh")

    def test_fallback_is_the_idle_gated_recreate(self):
        self.assertIn("deploy_voice_fix.sh --apply --restart-warp --wait-idle", self.script)

    def test_quiet_mode_is_cron_safe(self):
        """The tunnel flapped three times; a watchdog must not spam the log.

        `--quiet` keeps only the lines that mean something is wrong and still
        performs the reconnect, so a 10-minute cron entry can guard the egress
        without touching the bot container.
        """
        self.assertIn("--quiet|-q) QUIET=1", self.script)
        self.assertIn("grep --line-buffered -E", self.script)
        self.assertIn("warp_heal check", self.script)
        self.assertIn("*/10 * * * *", self.script)

    def test_quiet_mode_still_reconnects(self):
        code = "\n".join(line for line in self.script.splitlines()
                          if not line.lstrip().startswith("#"))
        self.assertIn("warp-cli connect", code)
        # the quiet filter must be output-only: it may not skip the healing path
        self.assertLess(code.index("QUIET=1"), code.index("warp-cli connect"))

    def test_final_verdict_is_net_check_inside_the_bot(self):
        self.assertIn("tools/net_check.py", self.script)
        self.assertIn('docker exec "$BOT_C" python3 tools/net_check.py', self.script)

    def test_every_docker_call_is_wrapped_in_timeout(self):
        """A hung `docker exec` must never freeze the operator's shell again."""
        code = "\n".join(line for line in self.script.splitlines()
                         if not line.lstrip().startswith("#"))
        for line in code.splitlines():
            stripped = line.strip()
            if stripped.startswith("docker ") or stripped.startswith('docker exec'):
                self.fail(f"bare docker call without a timeout: {stripped}")
        self.assertIn("run() { timeout -k 5", self.script)


if __name__ == "__main__":
    unittest.main()
