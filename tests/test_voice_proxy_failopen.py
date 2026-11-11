"""A dead voice proxy must never silently zero out a paid order.

Incident 2026-11-11 (orders 1349/1352): the server had ``USE_PROXY=true`` with
``SOCKS5_HOST=payproxy`` / ``SOCKS5_PORT=8888`` — the tinyproxy HTTP container
that exists for Iranian payment gateways.  Pyrogram sat in

    Connecting to 91.108.56.130:443 via proxy SOCKS5Proxy(hostname='payproxy',
    port=8888, ...)
    WARNING unable to connect due to network issues: Connection timed out

until the 20s client timeout, so ``_create_pyrogram_client_locked`` raised a
bare ``TimeoutError`` for every account, no account ever reached ``play()``,
and the orders looped ``wave → 20s pause → wave`` with ``live=0`` for hours.
The bare-engine probe joined the same chat in 1.9s because it does not use the
proxy at all.

These tests pin the recovery contract: preflight the endpoint once, and unless
``VOICE_PROXY_REQUIRED`` is set, fall back to a DIRECT connection loudly
instead of burning the order.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="voice-proxy-tests-")
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


class VoiceProxyFailOpenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vcm._VOICE_PROXY_STATE.update(
            {"disabled": False, "reason": "", "ok": None,
             "probed_at": 0.0, "target": ""})
        self._saved = (Config.USE_PROXY, Config.SOCKS5_HOST, Config.SOCKS5_PORT,
                       getattr(Config, "VOICE_PROXY_REQUIRED", False))
        Config.USE_PROXY = True
        Config.SOCKS5_HOST = "payproxy"
        Config.SOCKS5_PORT = 8888
        Config.VOICE_PROXY_REQUIRED = False

    def tearDown(self):
        (Config.USE_PROXY, Config.SOCKS5_HOST, Config.SOCKS5_PORT,
         Config.VOICE_PROXY_REQUIRED) = self._saved
        vcm._VOICE_PROXY_STATE.update(
            {"disabled": False, "reason": "", "ok": None,
             "probed_at": 0.0, "target": ""})

    def test_no_proxy_when_use_proxy_is_off(self):
        Config.USE_PROXY = False
        self.assertIsNone(vcm._voice_proxy_config())

    async def test_reachable_proxy_is_used(self):
        with mock.patch.object(vcm, "_proxy_reachable", mock.AsyncMock(return_value=True)):
            proxy = await vcm._effective_voice_proxy(134)
        self.assertEqual(proxy["hostname"], "payproxy")
        self.assertEqual(proxy["port"], 8888)
        self.assertFalse(vcm._voice_proxy_circuit_open())

    async def test_dead_proxy_falls_back_to_direct(self):
        with mock.patch.object(vcm, "_proxy_reachable", mock.AsyncMock(return_value=False)):
            proxy = await vcm._effective_voice_proxy(134)
        self.assertIsNone(proxy, "a dead proxy must not be handed to Pyrogram")
        self.assertTrue(vcm._voice_proxy_circuit_open())
        self.assertIn("preflight failed", vcm._VOICE_PROXY_STATE["reason"])

    async def test_circuit_open_skips_further_probing(self):
        vcm._disable_voice_proxy("already dead")
        probe = mock.AsyncMock(return_value=True)
        with mock.patch.object(vcm, "_proxy_reachable", probe):
            self.assertIsNone(await vcm._effective_voice_proxy(134))
        probe.assert_not_awaited()

    async def test_required_proxy_stays_fail_closed(self):
        Config.VOICE_PROXY_REQUIRED = True
        with mock.patch.object(vcm, "_proxy_reachable", mock.AsyncMock(return_value=False)):
            with self.assertRaises(RuntimeError):
                await vcm._effective_voice_proxy(134)
        self.assertFalse(vcm._voice_proxy_circuit_open(),
                         "fail-closed mode must not silently switch to direct")

    async def test_preflight_result_is_cached(self):
        probe = mock.AsyncMock(return_value=True)
        with mock.patch.object(vcm, "_proxy_reachable", probe):
            await vcm._effective_voice_proxy(134)
            await vcm._effective_voice_proxy(135)
        self.assertEqual(probe.await_count, 1)

    def test_connect_failure_classification(self):
        self.assertTrue(vcm._looks_like_connect_failure(TimeoutError()))
        self.assertTrue(vcm._looks_like_connect_failure(asyncio.TimeoutError()))
        self.assertTrue(vcm._looks_like_connect_failure(ConnectionResetError("reset")))
        self.assertTrue(vcm._looks_like_connect_failure(OSError("network unreachable")))
        self.assertFalse(vcm._looks_like_connect_failure(asyncio.CancelledError()))
        self.assertFalse(vcm._looks_like_connect_failure(ValueError("bad session")))

    def test_preflight_uses_the_socks5_greeting_not_a_tcp_connect(self):
        # Regression: payproxy (HTTP) accepted TCP, so a TCP-only preflight said
        # "ok" and the real client still hung. The preflight must ask SOCKS5.
        self.assertTrue(asyncio.iscoroutinefunction(vcm._proxy_reachable))

    async def test_tcp_reachable_reports_false_on_refused_port(self):
        # A closed port on localhost: no listener -> False, never a raise.
        ok = await vcm._tcp_reachable("127.0.0.1", 1, timeout=0.5)
        self.assertIs(ok, False)


class Socks5GreetingTests(unittest.IsolatedAsyncioTestCase):
    """A SOCKS5 endpoint must be told apart from an HTTP proxy on the same port."""

    async def _serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.addCleanup(server.close)
        return port

    async def test_real_socks5_server_is_detected(self):
        async def handler(reader, writer):
            greeting = await reader.readexactly(3)
            self.assertEqual(greeting, b"\x05\x01\x00")
            writer.write(b"\x05\x00")
            await writer.drain()
            writer.close()

        port = await self._serve(handler)
        self.assertIs(await diag.probe_socks5("127.0.0.1", port, timeout=2), True)

    async def test_http_proxy_is_rejected(self):
        async def handler(reader, writer):
            await reader.read(1024)
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()

        port = await self._serve(handler)
        self.assertIs(await diag.probe_socks5("127.0.0.1", port, timeout=2), False)

    async def test_silent_black_hole_is_rejected(self):
        async def handler(reader, writer):
            await asyncio.sleep(5)

        port = await self._serve(handler)
        self.assertIs(await diag.probe_socks5("127.0.0.1", port, timeout=0.5), False)

    async def test_closed_port_is_rejected(self):
        self.assertIs(await diag.probe_socks5("127.0.0.1", 1, timeout=0.5), False)

    async def test_auth_negotiation(self):
        async def handler(reader, writer):
            await reader.readexactly(4)          # 05 02 00 02 (user/pass offered)
            writer.write(b"\x05\x02")
            await writer.drain()
            await reader.readexactly(2 + 1 + 1 + 3)   # version, ulen, user, plen, pwd
            writer.write(b"\x01\x00")
            await writer.drain()
            writer.close()

        port = await self._serve(handler)
        self.assertIs(
            await diag.probe_socks5("127.0.0.1", port, timeout=2,
                                    username="u", password="pwd"), True)


class MediaDiagnosticsProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_proxy_dominates_the_verdict(self):
        with mock.patch.object(diag, "probe_socks5", mock.AsyncMock(return_value=False)), \
             mock.patch.object(diag, "probe_udp_dns", mock.AsyncMock(return_value=True)):
            report = await diag.collect(proxy=("payproxy", 8888))
        self.assertIs(report["proxy_ok"], False)
        self.assertEqual(report["proxy_target"], "payproxy:8888")
        self.assertIn("SOCKS5 proxy", diag.verdict(report))
        self.assertIn("proxy=DEAD", diag.format_report(report))

    async def test_no_proxy_reports_off(self):
        with mock.patch.object(diag, "probe_udp_dns", mock.AsyncMock(return_value=True)):
            report = await diag.collect()
        self.assertIsNone(report["proxy_ok"])
        self.assertIn("proxy=off", diag.format_report(report))


if __name__ == "__main__":
    unittest.main()
