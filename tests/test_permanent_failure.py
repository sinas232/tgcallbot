"""A permanently failed account must not be handed a second chance.

Incident (2026-10-01, order 930). The build reached ``live=38/42`` and then
churned on the same two accounts forever::

    [VoiceFill] order=930 second chance 1/2 accounts=4
    Order 930: account 139 gave up after 1 attempt(s)
        (Group join failed: Telegram says: [420 FROZEN_METHOD_INVALID] ...)
    Order 930: account 151 gave up after 1 attempt(s) (same)

The accounts were frozen by Telegram, so every retry returned the identical
error.  They *were* being added to ``_voice_banned`` - but
``_voice_second_chance_retry`` deliberately **discards** that ban so that
transient failures get another go::

    for aid in retry_ids:
        self._voice_banned[order_id].discard(aid)

The method already had the right escape hatch: its ``eligible()`` excludes
``_voice_terminal``, and three specific cases (AUTH_KEY_DUPLICATED, dead
session, SESSION_IN_USE) already populated it.  The generic
``OUTCOME_PERMANENT`` path did not, so a frozen account was banned,
un-banned, retried, banned again - and the last slots of the order could
never be filled or released.

These tests drive the real ``_mark_voice_account_permanent`` and the real
``_voice_second_chance_retry``.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="permanent-fail-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH",
                      os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH",
                      os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402

ORDER = 930
FROZEN_139, FROZEN_151, TRANSIENT_116 = 139, 151, 116
FROZEN_MSG = (
    "membership failed: Group join failed: Telegram says: "
    "[420 FROZEN_METHOD_INVALID] - The method can't be used by frozen account."
)


class PermanentFailureTests(unittest.TestCase):
    def setUp(self):
        self.ex = OrderExecutor()
        self.ex._voice_pool[ORDER] = [
            {"id": FROZEN_139}, {"id": FROZEN_151}, {"id": TRANSIENT_116},
        ]
        self.ex._voice_attempts[ORDER] = {}
        self.ex._voice_banned[ORDER] = set()
        self.ex._voice_terminal[ORDER] = set()
        self.ex._voice_second_chance[ORDER] = 0

    def test_permanent_failure_lands_in_the_terminal_set(self):
        self.ex._mark_voice_account_permanent(ORDER, FROZEN_139, FROZEN_MSG, 1)
        self.assertIn(FROZEN_139, self.ex._voice_terminal[ORDER],
                      '_voice_terminal is the only set eligible() excludes')
        self.assertIn(FROZEN_139, self.ex._voice_banned[ORDER])
        self.assertEqual(self.ex._voice_attempts[ORDER][FROZEN_139], 1,
                         'the budget must be spent so the wave does not retry it')

    def _second_chance(self, joined):
        """Drive the real retry round with the pool/DB calls stubbed out."""
        with mock.patch.multiple(
            Config,
            VOICE_SECOND_CHANCE_ROUNDS=2,
            VOICE_SECOND_CHANCE_COOLDOWN_SECONDS=0,
        ), mock.patch.object(self.ex, "_voice_load_pool",
                             new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(self.ex, "_is_order_active", return_value=True), \
                mock.patch("services.order_executor.voice_cooldown") as vc:
            vc.remaining.return_value = 0.0
            return asyncio.run(
                self.ex._voice_second_chance_retry(ORDER, 1, joined))

    def test_frozen_accounts_are_never_second_chanced(self):
        """The order-930 symptom: these two were retried round after round."""
        for aid in (FROZEN_139, FROZEN_151):
            self.ex._mark_voice_account_permanent(ORDER, aid, FROZEN_MSG, 1)

        took = self._second_chance(joined=set())

        self.assertFalse(
            took, 'with only frozen accounts banned there is nothing worth '
                  'retrying - the round must not run at all')
        self.assertIn(FROZEN_139, self.ex._voice_banned[ORDER],
                      'the ban must survive, not be discarded')
        self.assertIn(FROZEN_151, self.ex._voice_banned[ORDER])

    def test_a_transient_failure_IS_still_second_chanced(self):
        """The whole point of the round must keep working."""
        self.ex._voice_banned[ORDER].add(TRANSIENT_116)
        self.ex._voice_attempts[ORDER][TRANSIENT_116] = 1

        took = self._second_chance(joined=set())

        self.assertTrue(took, 'a transient failure deserves another attempt')
        self.assertNotIn(TRANSIENT_116, self.ex._voice_banned[ORDER],
                         'the ban is supposed to be lifted for transient ones')

    def test_frozen_and_transient_are_separated_in_the_same_round(self):
        """Mixed pool: retry the transient one, leave the frozen ones out."""
        for aid in (FROZEN_139, FROZEN_151):
            self.ex._mark_voice_account_permanent(ORDER, aid, FROZEN_MSG, 1)
        self.ex._voice_banned[ORDER].add(TRANSIENT_116)

        took = self._second_chance(joined=set())

        self.assertTrue(took, 'the transient account should have driven a round')
        self.assertNotIn(TRANSIENT_116, self.ex._voice_banned[ORDER])
        self.assertIn(FROZEN_139, self.ex._voice_banned[ORDER],
                      'frozen accounts must stay banned even when a round runs')
        self.assertIn(FROZEN_151, self.ex._voice_banned[ORDER])

    def test_already_joined_accounts_are_not_retried(self):
        self.ex._voice_banned[ORDER].add(TRANSIENT_116)
        took = self._second_chance(joined={TRANSIENT_116})
        self.assertFalse(took, 'an account already in the call needs no retry')


class OutcomeNameImportTests(unittest.TestCase):
    """Every OUTCOME_* used in order_executor must actually be imported.

    The permanent branch was first shipped referencing ``OUTCOME_PERMANENT``
    without importing it.  The resulting NameError was swallowed by the
    ``except Exception as _bookkeep_err`` guard around the wave bookkeeping,
    so it surfaced only as a "wave bookkeeping error" log line and the new
    branch silently never ran.  Calling the helper directly does not catch
    that - the name has to resolve in the module's own namespace.
    """

    def test_every_outcome_name_used_is_defined(self):
        import ast
        import services.order_executor as oe

        src = open(oe.__file__, encoding='utf-8').read()
        tree = ast.parse(src)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id.startswith('OUTCOME_')}
        self.assertTrue(used, 'sanity: the scan found no OUTCOME_* names')
        missing = sorted(n for n in used if not hasattr(oe, n))
        self.assertEqual(
            missing, [],
            '%s is referenced in order_executor.py but not imported; the '
            'NameError would be swallowed by the wave-bookkeeping handler'
            % missing)

    def test_permanent_branch_exists_and_reports_to_the_brain(self):
        import inspect
        import services.order_executor as oe
        src = inspect.getsource(oe.OrderExecutor._voice_batched_fill)
        self.assertIn('if outcome == OUTCOME_PERMANENT:', src)
        self.assertIn('_mark_voice_account_permanent(', src)


if __name__ == '__main__':
    unittest.main()
