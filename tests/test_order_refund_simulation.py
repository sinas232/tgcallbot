"""
Offline regression test for order failure / refund behaviour.

The real ``services.order_executor`` is exercised against stubbed DB,
Telegram and voice-engine modules.  Because those stubs live in
``sys.modules``, the simulation runs in a SEPARATE PROCESS
(``tests/_order_failure_harness.py``) so it can never leak into sibling tests.

Locked-in behaviour (the "order paid for but never delivered" bug class):

  * a stored link that is not a link → abort immediately, refund in full;
  * a link Telegram rejects → abort after 2 accounts, refund in full;
  * every account failing for any reason before the timer starts →
    refund in full and notify the customer;
  * a healthy order → no refund;
  * a failure AFTER the billable timer started → no automatic refund
    (prorated settlement belongs to the cancellation path).

Run:
    python -m unittest tests.test_order_refund_simulation -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "tests", "_order_failure_harness.py")


class TestOrderRefundSimulation(unittest.TestCase):
    def test_failure_scenarios(self):
        if not os.path.exists(HARNESS):
            self.skipTest("harness missing")

        proc = subprocess.run(
            [sys.executable, HARNESS],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        print(output.strip())
        if proc.returncode != 0:
            self.fail(f"order-failure simulation failed:\n{output}")

        for expected in (
            "PASS A receipt-as-link",
            "PASS B link rejected by Telegram",
            "PASS C all accounts fail (non-link)",
            "PASS D healthy order",
            "PASS E failure after timer start",
            "PASS F link error after partial joins",
            "PASS G invite link dead from start",
        ):
            self.assertIn(expected, proc.stdout)


if __name__ == "__main__":
    unittest.main()
