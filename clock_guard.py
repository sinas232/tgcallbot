#!/usr/bin/env python3
"""
clock_guard.py
--------------
این اسکریپت قبل از اجرای ربات (طبق CMD داخل Dockerfile) اجرا می‌شود.

Run once before the bot starts (see the Dockerfile CMD). Telegram rejects
requests when the server clock drifts too far from real time (the classic
"auth_key" / "time is out of sync" errors). This script:

  1. Reads the current time from several HTTPS endpoints using the HTTP
     `Date` response header. This works even when UDP NTP (port 123) is
     blocked by the datacenter, which is common on Iranian VPS providers.
  2. Compares it with the local clock.
  3. If the drift exceeds CLOCK_MAX_SKEW_SECONDS it tries to correct the
     clock; when that is not possible from inside a container it prints the
     exact commands to run on the HOST.
  4. Always exits 0 (unless CLOCK_GUARD_STRICT=1) so a clock problem never
     crash-loops the bot.

Environment variables
---------------------
CLOCK_MAX_SKEW_SECONDS    drift threshold in seconds (default: 300)
CLOCK_GUARD_STRICT        set to "1" to exit non-zero on unresolvable drift
CLOCK_GUARD_TIME_SOURCES  comma separated hosts (defaults below)
CLOCK_GUARD_TIMEOUT       per-source timeout in seconds (default: 6)
"""

import calendar
import datetime
import http.client
import os
import subprocess
import sys
import time
from email.utils import parsedate

# ---------------------------------------------------------------------------
# Timezone helpers (Tehran = UTC+03:30)
# ---------------------------------------------------------------------------

def _tehran_tz():
    """Return a Tehran tzinfo, falling back to a fixed UTC+03:30 offset."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Tehran")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=3, minutes=30))


def _now_utc_naive():
    return datetime.datetime.utcnow()


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Network time via the HTTP "Date" header
# ---------------------------------------------------------------------------

DEFAULT_SOURCES = [
    "google.com",
    "cloudflare.com",
    "github.com",
    "telegram.org",
    "apple.com",
]


def _fetch_epoch(host: str, timeout: int) -> float | None:
    """Return epoch seconds from an HTTPS Date header, or None on failure."""
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        conn.request("HEAD", "/", headers={"User-Agent": "clock-guard/1.0"})
        resp = conn.getresponse()
        # Reading the body is not needed; we only want the header.
        header = resp.getheader("Date")
        if header:
            parsed = parsedate(header)
            if parsed:
                return float(calendar.timegm(parsed))
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def _network_epoch(sources: list[str], timeout: int) -> float | None:
    """Best-effort network time: median of successful sources."""
    results = []
    for host in sources:
        epoch = _fetch_epoch(host, timeout)
        if epoch is not None:
            results.append(epoch)
        else:
            print(f"  ⚠️  time source unreachable: {host}")
    if not results:
        return None
    results.sort()
    return results[len(results) // 2]


# ---------------------------------------------------------------------------
# Clock correction
# ---------------------------------------------------------------------------

def _set_system_clock(epoch: float) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["date", "-s", "@%d" % int(epoch)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return True, ""
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err
    except FileNotFoundError:
        return False, "`date` command not found"
    except Exception as exc:  # noqa: BLE001 - report any failure
        return False, str(exc)


def _print_host_fix(skew: float) -> None:
    direction = "behind" if skew < 0 else "ahead"
    print()
    print("=" * 68)
    print("⚠️  CLOCK SKEW DETECTED — Telegram may reject requests.")
    print(f"   Local clock is {abs(skew):.0f}s {direction} of network time.")
    print()
    print("   The container usually cannot change the host clock")
    print("   (CAP_SYS_TIME is not granted). Fix it on the HOST as root:")
    print()
    print("     # 1) Preferred: enable NTP")
    print("     timedatectl set-ntp true")
    print()
    print("     # 2) If NTP (UDP 123) is blocked, sync via HTTPS Date header:")
    print("     date -s \"$(curl -sI https://google.com | grep -i '^date:' | cut -d' ' -f2-)\"")
    print()
    print("     # 3) Or use ntpdate against an allowed server")
    print("     apt-get install -y ntpdate && ntpdate -s time.google.com")
    print("=" * 68)
    print()


def _strict_mode() -> bool:
    return os.getenv("CLOCK_GUARD_STRICT", "0").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    max_skew = int(os.getenv("CLOCK_MAX_SKEW_SECONDS", "300") or 300)
    timeout = int(os.getenv("CLOCK_GUARD_TIMEOUT", "6") or 6)
    sources = [
        s.strip()
        for s in os.getenv("CLOCK_GUARD_TIME_SOURCES", ",".join(DEFAULT_SOURCES)).split(",")
        if s.strip()
    ] or DEFAULT_SOURCES

    # Apply Tehran timezone for the local clock if tzdata is available.
    os.environ.setdefault("TZ", "Asia/Tehran")
    try:
        time.tzset()
    except Exception:
        pass

    print("=" * 68)
    print("🕐 CLOCK GUARD — checking time before starting the bot…")
    print("=" * 68)

    local_now = datetime.datetime.now()
    tehran_tz = _tehran_tz()
    utc_now = datetime.datetime.utcnow()
    tehran_now = utc_now.replace(tzinfo=datetime.timezone.utc).astimezone(tehran_tz)

    print(f"   Container local time : {_fmt(local_now)}")
    print(f"   UTC time             : {_fmt(utc_now)}")
    print(f"   Tehran time          : {_fmt(tehran_now)}")
    print()

    net_epoch = _network_epoch(sources, timeout)

    if net_epoch is None:
        print("⚠️  Could not reach any network time source.")
        print("   Skipping drift check (the bot will still start).")
        if _strict_mode():
            print("🚨 CLOCK_GUARD_STRICT=1 → failing hard.")
            return 1
        print("✅ Clock guard finished (unverified).")
        print("=" * 68)
        return 0

    local_epoch = time.time()
    skew = net_epoch - local_epoch
    net_dt = datetime.datetime.utcfromtimestamp(net_epoch)

    print(f"   Network time         : {_fmt(net_dt)} UTC")
    print(f"   Local clock drift    : {skew:+.1f} seconds")
    print()

    if abs(skew) <= max_skew:
        print(f"✅ Clock drift is within tolerance (±{max_skew}s).")
        print("   Bot can start safely.")
        print("=" * 68)
        return 0

    print(f"⚠️  Drift ({abs(skew):.0f}s) exceeds tolerance (±{max_skew}s).")
    print("   Attempting to correct the clock inside the container…")
    ok, err = _set_system_clock(net_epoch)
    if ok:
        print("✅ Clock corrected from inside the container.")
        print("=" * 68)
        return 0

    print(f"   ❌ Could not set the clock: {err or 'unknown error'}")
    _print_host_fix(skew)

    if _strict_mode():
        print("🚨 CLOCK_GUARD_STRICT=1 → failing hard.")
        return 1

    print("   Continuing anyway so the bot does not crash-loop.")
    print("✅ Clock guard finished.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - never let the guard crash the bot
        print(f"⚠️  clock_guard.py failed unexpectedly ({exc}); continuing.")
        sys.exit(0)
