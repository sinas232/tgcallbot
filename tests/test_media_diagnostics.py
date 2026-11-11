"""Offline tests for the moment-of-failure media diagnostics.

The 2026-11-11 incident produced hundreds of identical
``join_task=failed detail='TimeoutError: TimeoutError'`` lines with no way to
tell WHICH layer was broken. The diagnostics module answers that in one line:
UDP egress, WARP MTU, zombie/ffmpeg count. Everything here is exercised without
Telegram, without network access to Telegram and without touching the real
WARP interface.
"""

import asyncio
import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import media_diagnostics as diag  # noqa: E402


class _ReplyServer:
    """UDP server that answers a DNS query the way a resolver would."""

    def __init__(self, reply: bool = True):
        self.reply = reply
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3.0)
        self.port = self.sock.getsockname()[1]
        self._thread = None

    def __enter__(self):
        import threading
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        try:
            packet, sender = self.sock.recvfrom(4096)
        except OSError:
            return
        if not self.reply:
            return
        transaction_id = struct.unpack(">H", packet[:2])[0]
        self.sock.sendto(
            struct.pack(">HHHHHH", transaction_id, 0x8180, 1, 1, 0, 0),
            sender,
        )

    def __exit__(self, *exc):
        self.sock.close()
        return False


class UdpProbeTests(unittest.TestCase):
    def test_a_real_dns_shaped_reply_is_reported_as_ok(self):
        with _ReplyServer(reply=True) as server:
            ok = asyncio.run(diag.probe_udp_dns("127.0.0.1", server.port, timeout=1.0))
        self.assertIs(ok, True)

    def test_silence_is_reported_as_blocked_not_unknown(self):
        with _ReplyServer(reply=False) as server:
            ok = asyncio.run(diag.probe_udp_dns("127.0.0.1", server.port, timeout=0.3))
        self.assertIs(ok, False,
                      "a timeout means 'no reply', which is the signal we want")

    def test_an_unusable_socket_is_unknown(self):
        # Port 0 / closed socket path must not raise.
        self.assertIn(asyncio.run(diag.probe_udp_dns("127.0.0.1", 1, timeout=0.2)),
                      (False, None))


class WarpMtuTests(unittest.TestCase):
    def test_reads_the_first_readable_mtu_file(self):
        root = Path(tempfile.mkdtemp(prefix="warp-mtu-"))
        good = root / "mtu"
        good.write_text("1280\n", encoding="utf-8")
        self.assertEqual(diag.read_warp_mtu((str(root / "missing"), str(good))), 1280)

    def test_absent_interface_is_none_not_zero(self):
        self.assertIsNone(diag.read_warp_mtu(("/nonexistent/mtu",)))

    def test_garbage_content_is_ignored(self):
        root = Path(tempfile.mkdtemp(prefix="warp-mtu-bad-"))
        bad = root / "mtu"
        bad.write_text("not-a-number", encoding="utf-8")
        self.assertIsNone(diag.read_warp_mtu((str(bad),)))


class StaticReportTests(unittest.TestCase):
    def _fake_proc(self, rows):
        root = tempfile.mkdtemp(prefix="diag-proc-")
        for pid, comm, state, ppid in rows:
            directory = Path(root) / str(pid)
            directory.mkdir()
            (directory / "stat").write_text(
                f"{pid} ({comm}) {state} {ppid} 0 0 0\n", encoding="utf-8")
        return root

    def test_static_report_collects_zombies_and_ffmpeg_children(self):
        proc = self._fake_proc([
            (10, "python", "S", "1"),
            (11, "ffmpeg", "Z", str(os.getpid())),
            (12, "ffmpeg", "S", str(os.getpid())),
        ])
        report = diag.collect_static(proc_root=proc, warp_mtu_paths=("/nonexistent",))
        self.assertEqual(report["zombies"], 1)
        self.assertEqual(report["ffmpeg_children"], 2)
        self.assertIsNone(report["warp_mtu"])

    def test_static_report_never_raises_on_a_broken_proc_root(self):
        report = diag.collect_static(proc_root="/nonexistent-proc",
                                     warp_mtu_paths=("/nonexistent",))
        self.assertIsNone(report["zombies"])
        self.assertIsNone(report["ffmpeg_children"])


class VerdictTests(unittest.TestCase):
    def test_blocked_udp_wins_over_everything(self):
        text = diag.verdict({"udp_dns_ok": False, "warp_mtu": 1500, "zombies": 900})
        self.assertIn("BLOCKED", text)

    def test_large_warp_mtu_is_called_out(self):
        text = diag.verdict({"udp_dns_ok": True, "warp_mtu": 1420, "zombies": 5})
        self.assertIn("MTU", text)
        self.assertIn("1280", text)

    def test_zombies_are_called_out_when_udp_and_mtu_look_fine(self):
        text = diag.verdict({"udp_dns_ok": True, "warp_mtu": 1280, "zombies": 883})
        self.assertIn("883", text)
        self.assertIn("zombie", text)

    def test_healthy_path_points_at_warp_or_telegram(self):
        text = diag.verdict({"udp_dns_ok": True, "warp_mtu": 1280, "zombies": 0})
        self.assertIn("UDP egress works", text)

    def test_report_line_is_greppable_and_has_no_secrets(self):
        line = diag.format_report({"udp_dns_ok": True, "warp_mtu": 1280,
                                   "zombies": 2, "ffmpeg_children": 1})
        for key in ("udp_dns_ok=True", "warp_mtu=1280", "zombies=2",
                    "ffmpeg_children=1"):
            self.assertIn(key, line)


class CollectTests(unittest.TestCase):
    def test_collect_returns_the_full_contract(self):
        report = asyncio.run(diag.collect(timeout=0.2,
                                          proc_root="/nonexistent-proc",
                                          warp_mtu_paths=("/nonexistent",)))
        for key in ("udp_dns_ok", "warp_mtu", "zombies", "ffmpeg_children"):
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
