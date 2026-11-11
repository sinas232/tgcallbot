"""`[VoiceNet]` must distinguish "no egress path" from "bad account".

2026-11-11 21:33: the SOCKS5 preflight said OK (greeting accepted) and Pyrogram
then died with `ProxyError Host unreachable`, while DIRECT timed out too — the
WARP tunnel could not carry Telegram traffic at all. The bot turned that into
20s of silence per account and a `Client Init Error: TimeoutError`. These tests
pin the two behaviours that make such a state obvious instead:

  * a proxy counts as healthy only if it can CONNECT to a Telegram DC,
  * when neither path works, the client refuses with a message naming the
    egress fault instead of timing out.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="voice-net-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config  # noqa: E402
from services import media_diagnostics as diag  # noqa: E402
from services import voice_call_manager as vcm  # noqa: E402

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

NET_CHECK = Path(__file__).resolve().parents[1] / "tools" / "net_check.py"


def _load_net_check():
    spec = importlib.util.spec_from_file_location("net_check_under_test", NET_CHECK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Socks5ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def _serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.addCleanup(server.close)
        return port

    async def test_connect_refused_by_proxy_is_false(self):
        # greeting ok, CONNECT answered with rep=0x04 (host unreachable) — the
        # exact ProxyError of the 21:33 run.
        async def handler(reader, writer):
            await reader.readexactly(3)          # greeting
            writer.write(b"\x05\x00")
            await writer.drain()
            await reader.readexactly(4 + 4 + 2)  # CONNECT request
            writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
            await writer.drain()
            writer.close()

        port = await self._serve(handler)
        self.assertIs(
            await diag.probe_socks5("127.0.0.1", port, timeout=2,
                                    target=("91.108.56.130", 443)), False)

    async def test_connect_accepted_by_proxy_is_true(self):
        async def handler(reader, writer):
            await reader.readexactly(3)
            writer.write(b"\x05\x00")
            await writer.drain()
            await reader.readexactly(4 + 4 + 2)
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            writer.close()

        port = await self._serve(handler)
        self.assertIs(
            await diag.probe_socks5("127.0.0.1", port, timeout=2,
                                    target=("91.108.56.130", 443)), True)


class EffectiveProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vcm._VOICE_PROXY_STATE.update({"disabled": False, "reason": "", "ok": None,
                                       "probed_at": 0.0, "target": ""})
        self._saved = (Config.USE_PROXY, Config.SOCKS5_HOST, Config.SOCKS5_PORT,
                       getattr(Config, "VOICE_PROXY_REQUIRED", False))
        Config.USE_PROXY = True
        Config.SOCKS5_HOST = "127.0.0.1"
        Config.SOCKS5_PORT = 1080
        Config.VOICE_PROXY_REQUIRED = False

    def tearDown(self):
        (Config.USE_PROXY, Config.SOCKS5_HOST, Config.SOCKS5_PORT,
         Config.VOICE_PROXY_REQUIRED) = self._saved
        vcm._VOICE_PROXY_STATE.update({"disabled": False, "reason": "", "ok": None,
                                       "probed_at": 0.0, "target": ""})

    async def test_proxy_that_reaches_telegram_is_used(self):
        with mock.patch.object(vcm, "_proxy_reachable", mock.AsyncMock(return_value=True)):
            proxy = await vcm._effective_voice_proxy(134)
        self.assertEqual(proxy["port"], 1080)

    async def test_proxy_down_but_direct_alive_means_direct(self):
        with mock.patch.object(vcm, "_proxy_reachable", mock.AsyncMock(return_value=False)), \
             mock.patch.object(vcm, "_direct_reachable", mock.AsyncMock(return_value=True)):
            self.assertIsNone(await vcm._effective_voice_proxy(134))
        self.assertTrue(vcm._voice_proxy_circuit_open())

    async def test_no_path_at_all_fails_loudly(self):
        with mock.patch.object(vcm, "_proxy_reachable", mock.AsyncMock(return_value=False)), \
             mock.patch.object(vcm, "_direct_reachable", mock.AsyncMock(return_value=False)):
            with self.assertRaises(RuntimeError) as ctx:
                await vcm._effective_voice_proxy(134)
        message = str(ctx.exception)
        self.assertIn("[VoiceNet]", message)
        self.assertIn("warp bot", message)
        self.assertFalse(vcm._voice_proxy_circuit_open(),
                         "a dead tunnel must not be remembered as 'direct works'")

    async def test_result_is_cached(self):
        probe = mock.AsyncMock(return_value=True)
        with mock.patch.object(vcm, "_proxy_reachable", probe):
            await vcm._effective_voice_proxy(134)
            await vcm._effective_voice_proxy(135)
        self.assertEqual(probe.await_count, 1)


class NetCheckVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_net_check()

    def test_both_dead_says_tunnel(self):
        text = self.mod.verdict({"DC5": "TimeoutError"}, {"DC5": "unreachable"},
                                "127.0.0.1:1080")
        self.assertIn("NO path to Telegram", text)
        self.assertIn("warp bot", text)

    def test_direct_only(self):
        self.assertIn("USE_PROXY=false",
                      self.mod.verdict({"DC5": "ok"}, {"DC5": "unreachable"},
                                       "127.0.0.1:1080"))

    def test_proxy_only(self):
        self.assertIn("keep USE_PROXY=true",
                      self.mod.verdict({"DC5": "TimeoutError"}, {"DC5": "ok"},
                                       "127.0.0.1:1080"))

    def test_no_proxy_configured(self):
        self.assertIn("no proxy configured and DIRECT cannot reach Telegram",
                      self.mod.verdict({"DC5": "TimeoutError"}, None, None))

    def test_cli_parsing(self):
        args = self.mod._parse_args(["--timeout", "1", "--json"])
        self.assertEqual(args.timeout, 1.0)
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
