"""Every admin_conv entry point must be authorization-gated.

Incident found in review (1405-07-04): ``admin_conv`` is registered with
``application.add_handler(...)`` and **no filter**, and PTB entry points are
reachable without ever entering the conversation.  Seventeen of its entry
points pointed at handlers with no authorization check in the body, and
``callback_data`` is client-controlled.  A non-admin user could therefore send

    callback_data = "admincancel_refund_859"

and reach ``admin_cancel_order_callback``, which calls
``order_executor.settle_and_refund_order(...)`` unconditionally - cancelling
and refunding any order in the system.  Reproduced: a mocked non-admin update
produced exactly one ``settle_and_refund_order`` call.

The fix wraps every unguarded entry point in ``require_admin(...)`` at the
registration site.  These tests pin it, so a future entry point added without
the wrapper fails the build instead of silently opening the panel.
"""
from __future__ import annotations

import asyncio
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import main  # noqa: E402
from handlers import admin_handlers as ah  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Entry points whose target is ALREADY decorated inside its own module, so
# wrapping again at the call site would be redundant. Verified by reading each
# definition's preceding lines.
ALREADY_DECORATED = {
    'admin_panel_start',               # @require_admin
    'handle_reseller_action',          # @require_god_admin
    'premium_emoji_callback',          # @require_super_admin
    'health_report_handler',           # @require_admin
    'deleted_account_cleanup_handler',  # @require_super_admin
}


def _entry_point_block():
    """Return the source lines of admin_conv's entry_points=[...] block."""
    lines = (ROOT / 'main.py').read_text(encoding='utf-8').split('\n')
    start = next(i for i, l in enumerate(lines)
                 if 'admin_conv = ConversationHandler(' in l)
    ep = next(i for i in range(start, start + 12) if 'entry_points=[' in lines[i])
    depth = 0
    for j in range(ep, start + 80):
        depth += lines[j].count('[') - lines[j].count(']')
        if depth == 0 and j > ep:
            return ep, j, lines
    raise AssertionError('could not find the end of entry_points')


def _entry_point_targets():
    """(handler_name, wrapped_in_require_admin) for every entry point."""
    ep, end, lines = _entry_point_block()
    out = []
    for line in lines[ep:end + 1]:
        m = re.search(r'(?:Callback|Message)QueryHandler\(\s*'
                      r'(?:require_admin\(\s*)?(\w+)', line)
        if m:
            out.append((m.group(1), 'require_admin(' in line))
    return out


class AdminEntryPointAuthTests(unittest.TestCase):
    def test_every_entry_point_is_gated(self):
        targets = _entry_point_targets()
        self.assertGreater(len(targets), 15,
                           'parser found too few entry points - fix the test')
        exposed = [n for n, wrapped in targets
                   if not wrapped and n not in ALREADY_DECORATED]
        self.assertEqual(
            exposed, [],
            'these admin_conv entry points have no authorization gate; a '
            'non-admin can reach them with a crafted callback_data: %s' % exposed)

    def test_the_allowlist_is_still_accurate(self):
        """Guard the guard: an entry in ALREADY_DECORATED must really be
        decorated, or the allowlist becomes a hole."""
        for name in sorted(ALREADY_DECORATED):
            found = False
            for f in (ROOT / 'handlers').glob('*.py'):
                src = f.read_text(encoding='utf-8').split('\n')
                for i, l in enumerate(src):
                    if re.match(r'^async def %s\b' % name, l):
                        preceding = '\n'.join(src[max(0, i - 5):i])
                        self.assertRegex(
                            preceding, r'@require_\w+',
                            '%s is allowlisted as decorated but is not '
                            '(%s:%d)' % (name, f.name, i + 1))
                        found = True
                        break
                if found:
                    break
            self.assertTrue(found, '%s no longer exists in handlers/' % name)

    def test_admin_conv_is_registered_without_a_filter(self):
        """Documents WHY the entry points must be gated individually."""
        src = (ROOT / 'main.py').read_text(encoding='utf-8')
        self.assertIn('add_handler(register_conversation(admin_conv))', src)
        reg = (ROOT / 'handlers' / 'conversation_registry.py').read_text(encoding='utf-8')
        # Only the function body matters - the module docstring legitimately
        # mentions filters.ALL / filters.TEXT while explaining the menu router.
        body = reg.split('def register_conversation', 1)[1]
        body = body.split('\ndef ', 1)[0]
        self.assertNotIn('filters.', body,
                         'register_conversation gained a filter - reconsider '
                         'whether per-entry-point gating is still needed')
        self.assertIn('return handler', body,
                      'register_conversation must stay a pass-through')


class RequireAdminBlocksTheExploitTests(unittest.IsolatedAsyncioTestCase):
    """The actual attack, replayed against the wrapped handler."""

    ATTACKER = 999_000_111
    ADMIN = 111

    async def _try(self, uid):
        query = SimpleNamespace(data="admincancel_refund_859",
                                answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(
            callback_query=query, message=None,
            effective_user=SimpleNamespace(id=uid, first_name="X"))
        settle = AsyncMock(return_value={
            'total_cost': 1, 'used_cost': 0, 'refund_amount': 1,
            'refund_tx_id': 'T', 'user_wallet_balance': 1})
        wrapped = ah.require_admin(ah.admin_cancel_order_callback)
        with patch.object(ah.Config, 'ADMIN_IDS', [self.ADMIN]), \
             patch.object(ah.DatabaseManager, 'get_order',
                          AsyncMock(return_value={'id': 859, 'status': 'running'})), \
             patch.object(ah.DatabaseManager, 'get_user',
                          AsyncMock(return_value={'is_admin': False})), \
             patch.object(ah.order_executor, 'settle_and_refund_order', settle):
            await wrapped(update, SimpleNamespace(bot_data={'bot_id': 1}))
        return settle.await_count

    async def test_a_non_admin_cannot_refund_an_order(self):
        self.assertEqual(await self._try(self.ATTACKER), 0,
                         'a non-admin reached settle_and_refund_order')

    async def test_a_real_admin_still_can(self):
        """The fix must not lock admins out of their own panel."""
        self.assertEqual(await self._try(self.ADMIN), 1)


if __name__ == '__main__':
    unittest.main()
