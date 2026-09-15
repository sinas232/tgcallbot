"""
tests/_order_failure_harness.py — offline simulation of order failure paths.

Runs ``services.order_executor`` against stubbed DB / Telegram / voice-engine
modules so the billing + failure behaviour can be asserted WITHOUT a database,
a bot token or pyrogram installed.  Intended to be executed in a SUBPROCESS by
``tests/test_order_refund_simulation.py`` (the stubs are installed in
``sys.modules``, which would otherwise leak into sibling tests).

Scenarios
---------
  A. The stored target link is the bot's own "order registered" receipt
     (production incident, order 692) → no account is spent, full refund.
  B. A syntactically valid link that Telegram rejects (USERNAME_INVALID)
     → the fill aborts after 2 distinct accounts (pool preserved), full refund.
  C. Every account fails for a NON-link reason ("voice call not active")
     → the order never delivers anything → full refund + user notified.
  D. Healthy order that fills to target → no refund, no abort.
  E. A failure AFTER the billable timer started → NO automatic full refund
     (prorated settlement is owned by the cancellation path).

Exit code 0 = all scenarios passed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ── stubs ──────────────────────────────────────────────────────────────
try:
    import dotenv  # noqa: F401
except ImportError:
    stub("dotenv", load_dotenv=lambda *a, **k: None)

CALLS: list = []


class FakeDB:
    """Records every side effect so the scenarios can assert on them."""

    pool_size = 10
    price = 12000

    @staticmethod
    async def count_active_accounts(bot_id=1):
        return FakeDB.pool_size

    @staticmethod
    async def get_active_accounts_batch(bot_id=1, offset=0, limit=100):
        if offset:
            return []
        return [
            {"id": i, "session_string": "s", "phone_number": "p"}
            for i in range(1, FakeDB.pool_size + 1)
        ]

    @staticmethod
    async def get_user_by_id(uid):
        return {"id": uid, "telegram_id": 555, "first_name": "Test"}

    @staticmethod
    async def update_user_credit(uid, amount, typ, desc, bot_id=1):
        CALLS.append(("refund", uid, float(amount)))
        return True, 1000

    @staticmethod
    async def update_order_status(order_id, status):
        CALLS.append(("status", order_id, status))

    @staticmethod
    async def mark_order_as_running(oid):
        return True

    @staticmethod
    async def complete_order(oid):
        CALLS.append(("completed", oid))
        return True

    @staticmethod
    async def start_order_duration(oid):
        return None

    @staticmethod
    async def get_setting(key, bot_id=1):
        return None


class FakeVCM:
    """Voice engine stub: counts join attempts and tracks who is 'inside'."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.attempts: list = []
        self.joined: dict = {}

    async def start_call(self, order_id, acc_id, session, target, dur=0):
        self.attempts.append(acc_id)
        ok, msg, cid = self.outcome(order_id, acc_id)
        if ok:
            self.joined.setdefault(order_id, set()).add(acc_id)
        return ok, msg, cid

    def get_active_count(self, order_id):
        return len(self.joined.get(order_id, ()))

    def get_active_account_ids(self, order_id):
        return set(self.joined.get(order_id, ()))

    def flood_wait_remaining(self, acc_id):
        return 0

    async def warmup_clients(self, accounts):
        return None

    async def stop_all_for_order(self, order_id, leave_group=True):
        return 0


class FakeBrain:
    """Deterministic Join Brain: one account per wave, never pauses."""

    async def wait_if_paused(self, order_id):
        return None

    def register_order(self, order_id, initial=None, min_window=None, max_window=None):
        return None

    def get_window(self, order_id):
        return 1

    def start_wave(self, order_id, n):
        return None

    def finish_wave(self, order_id, joined=0, failed=0, ok_rate=0.0, duration_s=0.0):
        return None

    def report_result(self, order_id, outcome, msg=None):
        return None

    def classify_message(self, msg):
        return "failed"

    def forget_order(self, order_id):
        return None

    def format_progress(self, order_id, live, target):
        return f"live={live}/{target}"


class FakeSelfHealing:
    @staticmethod
    def rank(candidates):
        return list(candidates)

    @staticmethod
    def report(msg, ok, key=None):
        return None

    @staticmethod
    def pick(msg, key=None):
        return (1.0, 1.0)


CURRENT_VCM = {"vcm": None}

stub("database", DatabaseManager=FakeDB)
stub("telegram_client", TelegramAccountClient=object)
stub("utils.helpers", format_jalali_datetime=lambda *a, **k: "-",
     format_price=lambda v: f"{int(v):,}")
stub("services.bot_manager", bot_manager=types.SimpleNamespace(active_bots={}))
stub("services.self_healing", **{n: getattr(FakeSelfHealing, n)
                                 for n in ("rank", "report", "pick")})
_vcm_mod = stub("services.voice_call_manager", voice_call_manager=None)
stub("services.join_brain", join_brain=FakeBrain(), OUTCOME_OK="ok",
     OUTCOME_DEAD="dead", OUTCOME_FLOOD="flood")

from services.order_executor import OrderExecutor  # noqa: E402
import services.order_executor as oe  # noqa: E402

# Keep the simulation fast and deterministic: no stagger, 1s backoff,
# one attempt per account.
for _name, _value in (
    ("VOICE_JOIN_START_STAGGER_MIN", 0.0),
    ("VOICE_JOIN_START_STAGGER_MAX", 0.0),
    ("VOICE_JOIN_START_JITTER_MIN", 0.0),
    ("VOICE_JOIN_START_JITTER_MAX", 0.0),
    ("VOICE_RETRY_BACKOFF_BASE", 1.0),
    ("VOICE_ACCOUNT_ATTEMPT_LIMIT", 1),
    ("VOICE_WAVE_TIMEOUT", 20),
):
    setattr(oe.Config, _name, _value)

RECEIPT = (
    "✅ **سفارش با موفقیت ثبت و آغاز شد.**\n"
    "🆔 کد پیگیری: 671\n"
    "در صورت نیاز می‌توانید سفارش را با دکمه زیر لغو کنید."
)


def make_order(link, order_id=692, duration=0, accounts=5):
    return {
        "id": order_id, "bot_id": 1, "user_id": 7, "order_type": "voice_chat",
        "target_link": link, "accounts_count": accounts,
        "duration_minutes": duration, "price_paid": FakeDB.price,
        "started_at": None,
    }


async def run(order, outcome, timeout=60):
    vcm = FakeVCM(outcome)
    _vcm_mod.voice_call_manager = vcm
    CALLS.clear()
    ex = OrderExecutor()
    ex.active_orders[order["id"]] = {
        "status": "running", "data": order, "joined_accounts": [],
        "task": None, "dead_accounts_count": 0, "cancel_requested": False,
        "target_count": order["accounts_count"], "live_count": 0,
        "pool_ids": set(), "swapped_accounts": 0,
    }
    await asyncio.wait_for(ex._execute_order_logic(order["id"], order), timeout=timeout)
    return ex, vcm


def refunds():
    return [c for c in CALLS if c[0] == "refund"]


def statuses():
    return [c for c in CALLS if c[0] == "status"]


async def scenario_a_receipt_stored_as_link():
    ex, vcm = await run(make_order(RECEIPT), lambda o, a: (True, "ok", 1))
    assert vcm.attempts == [], f"no account may be spent on a broken link: {vcm.attempts}"
    assert [(r[1], r[2]) for r in refunds()] == [(7, float(FakeDB.price))], refunds()
    assert statuses() == [("status", 692, "failed")], statuses()
    assert ex.active_orders == {}, ex.active_orders
    return f"0 accounts spent · refund {FakeDB.price:,} · status=failed"


async def scenario_b_link_rejected_by_telegram():
    ex, vcm = await run(
        make_order("@nosuchgroup"),
        lambda o, a: (False, "Invalid Link (USERNAME_INVALID): @nosuchgroup", 0),
    )
    assert len(set(vcm.attempts)) == 2, f"must abort after 2 accounts, got {vcm.attempts}"
    assert len(vcm.attempts) < FakeDB.pool_size, "the whole pool must not be drained"
    assert not ex._voice_banned.get(692), "accounts must not be banned for a link fault"
    assert [(r[1], r[2]) for r in refunds()] == [(7, float(FakeDB.price))], refunds()
    return f"aborted after 2/{FakeDB.pool_size} accounts · refund {FakeDB.price:,}"


async def scenario_c_all_accounts_fail_generic():
    ex, vcm = await run(
        make_order("@mygroup", order_id=701),
        lambda o, a: (False, "Voice call not active", -1001234567890),
    )
    assert vcm.attempts, "the engine must actually try to join"
    assert [(r[1], r[2]) for r in refunds()] == [(7, float(FakeDB.price))], refunds()
    assert statuses() == [("status", 701, "failed")], statuses()
    return f"{len(vcm.attempts)} attempts, 0 delivered · refund {FakeDB.price:,}"


async def scenario_d_healthy_order_no_refund():
    ex, vcm = await run(
        make_order("@mygroup", order_id=702),
        lambda o, a: (True, "ok", -1001234567890),
    )
    assert len(vcm.joined.get(702, ())) == 5, vcm.joined
    assert refunds() == [], f"a delivered order must not be refunded: {refunds()}"
    return f"filled 5/5 · refunds={refunds()}"


async def scenario_e_failure_after_timer_started():
    """A mid-duration failure must NOT trigger the automatic full refund."""
    ex = OrderExecutor()
    data = make_order("@mygroup", order_id=703, duration=60)
    data["started_at"] = "2026-09-15 07:00:00"  # billable window already open
    ex.active_orders[703] = {"data": data, "joined_accounts": []}
    await ex._fail_order(703, "monitor crashed", refund_if_unstarted=True)
    assert refunds() == [], f"no full refund after start: {refunds()}"
    assert statuses() == [("status", 703, "failed")], statuses()
    return "no auto-refund after the billable timer started"


async def scenario_f_link_error_after_partial_joins():
    """Permanent link error on subsequent accounts must NOT abort order if accounts joined."""
    def outcome(order_id, acc_id):
        if acc_id in (4, 5):
            return False, "Invalid Link (INVITE_HASH_EXPIRED): https://t.me/+fakeinvite", -1001234567890
        return True, "ok", -1001234567890

    ex, vcm = await run(
        make_order("https://t.me/+fakeinvite", order_id=704, accounts=5),
        outcome,
    )
    joined = vcm.joined.get(704, set())
    assert len(joined) >= 3, f"at least 3 accounts must have joined, got {joined}"
    assert refunds() == [], f"an order with live accounts must NEVER be refunded/aborted: {refunds()}"
    assert statuses() != [("status", 704, "failed")], "order with live accounts must not fail"
    return f"filled {len(joined)} accounts despite invite expired on 2 accounts · refunds={refunds()}"


async def scenario_g_invite_link_dead_from_start():
    """An invite link expired from the start (0 joins) must abort early and refund."""
    ex, vcm = await run(
        make_order("https://t.me/+deadinvite", order_id=705, accounts=5),
        lambda o, a: (False, "Invalid Link (INVITE_HASH_EXPIRED): https://t.me/+deadinvite", 0),
    )
    assert len(set(vcm.attempts)) <= 5, f"must abort after bounded attempts, got {vcm.attempts}"
    assert len(vcm.attempts) < FakeDB.pool_size or len(vcm.attempts) <= 5
    assert [(r[1], r[2]) for r in refunds()] == [(7, float(FakeDB.price))], refunds()
    assert statuses() == [("status", 705, "failed")], statuses()
    return f"aborted after {len(set(vcm.attempts))}/{FakeDB.pool_size} accounts · refund {FakeDB.price:,}"


SCENARIOS = (
    ("A receipt-as-link", scenario_a_receipt_stored_as_link),
    ("B link rejected by Telegram", scenario_b_link_rejected_by_telegram),
    ("C all accounts fail (non-link)", scenario_c_all_accounts_fail_generic),
    ("D healthy order", scenario_d_healthy_order_no_refund),
    ("E failure after timer start", scenario_e_failure_after_timer_started),
    ("F link error after partial joins", scenario_f_link_error_after_partial_joins),
    ("G invite link dead from start", scenario_g_invite_link_dead_from_start),
)


async def main():
    failed = 0
    for name, func in SCENARIOS:
        try:
            detail = await func()
            print(f"PASS {name}: {detail}")
        except Exception as exc:  # noqa: BLE001 - report, don't crash the runner
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print("ALL SCENARIOS PASSED" if not failed else f"{failed} SCENARIO(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
