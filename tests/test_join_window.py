"""Regression tests for the voice-join pacing window.

Incident (2026-10-01, order 928): a 42-account order took ~9.5 minutes to
build because the Join Brain window was pinned at 1 for its whole life::

    [JoinBrain] order=928 registered window=1 (min=1 max=1)

The anti-spam panel stored ``antispam_max_join_concurrency = 1`` and
``_voice_batched_fill`` fed that straight into ``register_order`` as
``max_window``.  ``JoinBrain.finish_wave`` only widens when
``window < max_window``, so ``1 < 1`` was false forever and the window could
never grow.

That protection was also redundant: the RPC *rate* is paced by the start-gap
(``ANTISPAM_JOIN_GAP_MIN``/``MAX`` + jitter), not by the window.  A join takes
30-45s, so a window of 1 serialises joins without lowering the RPC rate any
further than the gap already does.

These tests call the real ``resolve_join_window`` that
``_voice_batched_fill`` now uses.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="join-window-tests-")
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
from services.join_brain import AdaptiveJoinBrain  # noqa: E402
from services.order_executor import resolve_join_window  # noqa: E402


class _Profile:
    """Stand-in for anti_spam.AntiSpamProfile (same attributes)."""

    def __init__(self, *, enabled=True, max_join_concurrency=1):
        self.enabled = enabled
        self.max_join_concurrency = max_join_concurrency


class ResolveJoinWindowTests(unittest.TestCase):
    def _resolve(self, *, hw, initial, minw, floor, profile,
                 sequential=False, adaptive=True):
        vals = {
            "VOICE_JOIN_MAX_CONCURRENCY": hw,
            "VOICE_JOIN_INITIAL_CONCURRENCY": initial,
            "VOICE_JOIN_MIN_CONCURRENCY": minw,
            "ANTISPAM_JOIN_WINDOW_FLOOR": floor,
        }
        with mock.patch.multiple(Config, **vals):
            return resolve_join_window(
                profile, sequential=sequential, adaptive=adaptive)

    # ── the actual production regression ───────────────────────────────
    def test_antispam_cap_of_one_no_longer_pins_the_window(self):
        """order 928: anti-spam said 1, hardware said 16, window froze at 1."""
        initial, min_window, max_window, reason = self._resolve(
            hw=16, initial=1, minw=1, floor=4,
            profile=_Profile(enabled=True, max_join_concurrency=1),
        )
        self.assertGreater(max_window, 1,
                           'the window must be able to widen or a large order '
                           'builds one account per wave for minutes')
        self.assertEqual(max_window, 4)
        self.assertLess(min_window, max_window,
                        'min_window must be strictly below max_window or '
                        'JoinBrain.finish_wave can never widen')
        self.assertIn('anti-spam cap=1', reason)

    def test_floor_never_exceeds_the_hardware_ceiling(self):
        _, _, max_window, _ = self._resolve(
            hw=2, initial=1, minw=1, floor=4,
            profile=_Profile(enabled=True, max_join_concurrency=1),
        )
        self.assertEqual(max_window, 2)

    def test_floor_of_one_restores_the_old_serial_behaviour(self):
        """The escape hatch must actually reproduce the pre-fix behaviour."""
        initial, min_window, max_window, _ = self._resolve(
            hw=16, initial=1, minw=1, floor=1,
            profile=_Profile(enabled=True, max_join_concurrency=1),
        )
        self.assertEqual((initial, min_window, max_window), (1, 1, 1))

    def test_a_higher_antispam_cap_still_wins_over_the_floor(self):
        _, _, max_window, _ = self._resolve(
            hw=16, initial=1, minw=1, floor=4,
            profile=_Profile(enabled=True, max_join_concurrency=8),
        )
        self.assertEqual(max_window, 8)

    def test_min_window_is_clamped_to_the_cap(self):
        """Second bug: min_window used to be passed through unclamped.

        With cap=4 and VOICE_JOIN_MIN_CONCURRENCY=8 the old code registered
        min=8, max=4, so ``window < max_window`` was false and
        ``max(min_window, window/2)`` pushed the window back above the cap.
        """
        _, min_window, max_window, _ = self._resolve(
            hw=16, initial=1, minw=8, floor=4,
            profile=_Profile(enabled=True, max_join_concurrency=4),
        )
        self.assertEqual((min_window, max_window), (4, 4))
        self.assertLessEqual(min_window, max_window)

    def test_disabled_profile_falls_back_to_hardware(self):
        _, _, max_window, reason = self._resolve(
            hw=16, initial=1, minw=1, floor=4,
            profile=_Profile(enabled=False, max_join_concurrency=1),
        )
        self.assertEqual(max_window, 16)
        self.assertIn('adaptive', reason)

    def test_sequential_mode_still_pins_everything_to_one(self):
        """VOICE_JOIN_SEQUENTIAL is an explicit operator choice - untouched."""
        result = self._resolve(
            hw=16, initial=8, minw=4, floor=4,
            profile=_Profile(enabled=True, max_join_concurrency=8),
            sequential=True,
        )
        self.assertEqual(result[:3], (1, 1, 1))


class JoinBrainWidensTests(unittest.TestCase):
    """End-to-end: feed the resolved values into the real JoinBrain."""

    def test_window_actually_widens_after_clean_waves(self):
        brain = AdaptiveJoinBrain()
        profile = _Profile(enabled=True, max_join_concurrency=1)
        with mock.patch.multiple(
            Config,
            VOICE_JOIN_MAX_CONCURRENCY=16,
            VOICE_JOIN_INITIAL_CONCURRENCY=1,
            VOICE_JOIN_MIN_CONCURRENCY=1,
            ANTISPAM_JOIN_WINDOW_FLOOR=4,
            VOICE_JOIN_GROWTH_AFTER_WAVES=2,
        ):
            initial, min_window, max_window, _ = resolve_join_window(profile)
            brain.register_order(
                928, initial=initial, min_window=min_window,
                max_window=max_window,
            )
            self.assertEqual(brain.get_window(928), 1)

            seen = [brain.get_window(928)]
            for _ in range(12):
                brain.start_wave(928, brain.get_window(928))
                brain.finish_wave(928, joined=seen[-1], failed=0,
                                  ok_rate=1.0, duration_s=5.0)
                seen.append(brain.get_window(928))

        self.assertGreater(
            seen[-1], 1,
            'after 12 clean waves the window still had not widened - this is '
            'the order-928 symptom: %s' % seen)
        self.assertEqual(seen[-1], 4, 'it should have reached the cap: %s' % seen)


if __name__ == '__main__':
    unittest.main()
