"""Order link validation and checkout regression tests (offline)."""
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User  # noqa: E402
from services import link_validator as links  # noqa: E402

INVITE = 'https://t.me/+AbCdEf123456'


class LinkValidatorTests(unittest.TestCase):
    def test_private_invites_and_public_rejection(self):
        with patch.object(links, 'link_mode', return_value='private'):
            for raw in (INVITE, 't.me/+AbCdEf123456', '+AbCdEf123456',
                        'tg://join?invite=AbCdEf123456', f'لینک گروه: {INVITE}.'):
                self.assertEqual(links.validate_order_link(raw)[:2], (True, INVITE))
            for raw in ('@public', 'https://t.me/public', 'https://t.me/public/23',
                        'https://example.com/x', 'https://t.me/+abc', '', None):
                ok, _, error = links.validate_order_link(raw)
                self.assertFalse(ok, raw)
                self.assertIn('لینک درست را بفرستید', error)
            self.assertEqual(links.validate_order_link('گروه', [INVITE])[:2], (True, INVITE))

    def test_any_preserves_legacy_input_but_uses_clickable_entity_url(self):
        with patch.object(links, 'link_mode', return_value='any'):
            self.assertEqual(links.validate_order_link('  public name here  ')[:2],
                             (True, 'public name here'))
            self.assertEqual(links.validate_order_link('گروه مقصد', iter([INVITE]))[:2],
                             (True, INVITE))
            self.assertEqual(links.validate_order_link('گروه مقصد',
                                                       ['https://t.me/public'])[:2],
                             (True, 'https://t.me/public'))
            self.assertEqual(links.validate_order_link(INVITE,
                                                       ['https://example.org/not-a-group'])[:2],
                             (True, INVITE))
            self.assertFalse(links.validate_order_link('   ')[0])

    def test_regex_uses_fullmatch_and_invalid_regex_never_opens_validation(self):
        with patch.dict(os.environ, {'ORDER_LINK_REGEX': r'https://t\.me/\+[A-Za-z0-9_-]{12,}'}, clear=False):
            self.assertTrue(links.validate_order_link(INVITE)[0])
            self.assertFalse(links.validate_order_link(INVITE + '/malicious')[0])
        with patch.dict(os.environ, {'ORDER_LINK_REGEX': r'.+'}, clear=False):
            self.assertEqual(links.validate_order_link('label', [INVITE])[:2],
                             (True, INVITE))
        with patch.dict(os.environ, {'ORDER_LINK_REGEX': '('}, clear=False):
            with self.assertRaises(ValueError):
                links.validate_order_link(INVITE)

    def test_normalized_key_preserves_invite_hash_case(self):
        self.assertEqual(links.target_key('t.me/+AbCdEf123456'), links.target_key(INVITE))
        self.assertNotEqual(links.target_key('t.me/+AbCdEf123456'),
                            links.target_key('t.me/+abCdEf123456'))
        self.assertNotEqual(links.target_key('@AbCdEf123456'), links.target_key(INVITE))

    def test_invalid_mode_fails_closed(self):
        with patch.dict(os.environ, {'ORDER_LINK_MODE': 'typo', 'ORDER_LINK_REGEX': ''}):
            with self.assertRaises(ValueError):
                links.validate_order_link(INVITE)


class CheckoutHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def _receive(self, text, *, entities=None, mode='private'):
        import handlers.order_handlers as h
        msg = Message(7, datetime.now(), Chat(567, 'private'), text=text, entities=entities)
        update = Update(1, message=msg)
        data = {}
        context = SimpleNamespace(user_data=data, bot=SimpleNamespace())
        with patch.object(links, 'link_mode', return_value=mode), \
                patch.object(h, 'send_safe', new_callable=AsyncMock) as send, \
                patch.object(Message, 'reply_text', new_callable=AsyncMock):
            state = await h.receive_order_link(update, context)
        return state, data, send

    async def test_invalid_link_does_not_advance_or_save_target(self):
        import handlers.order_handlers as h
        state, data, send = await self._receive('@public')
        self.assertEqual(state, h.AWAITING_ORDER_LINK)
        self.assertNotIn('target_link', data)
        self.assertIn('لینک درست را بفرستید', send.await_args.args[2])

    async def test_private_invite_and_hidden_link(self):
        import handlers.order_handlers as h
        state, data, _ = await self._receive('t.me/+AbCdEf123456')
        self.assertEqual(state, h.AWAITING_ORDER_TIMING_TYPE)
        self.assertEqual(data['target_link'], INVITE)
        state, data, _ = await self._receive('گروه مقصد', entities=[
            MessageEntity(MessageEntity.TEXT_LINK, 0, len('گروه مقصد'), url=INVITE)])
        self.assertEqual(state, h.AWAITING_ORDER_TIMING_TYPE)
        self.assertEqual(data['target_link'], INVITE)

    async def test_default_any_stores_hidden_click_target_not_display_label(self):
        import handlers.order_handlers as h
        state, data, _ = await self._receive('گروه مقصد', mode='any', entities=[
            MessageEntity(MessageEntity.TEXT_LINK, 0, len('گروه مقصد'), url=INVITE)])
        self.assertEqual(state, h.AWAITING_ORDER_TIMING_TYPE)
        self.assertEqual(data['target_link'], INVITE)

    async def test_checkout_revalidates_before_wallet_debit(self):
        import handlers.order_handlers as h
        user = User(567, 'Test', False)
        msg = Message(7, datetime.now(), Chat(567, 'private'), text='order')
        query = CallbackQuery('cq', user, 'instance', data='confirm_order_pay', message=msg)
        update = Update(1, callback_query=query)
        context = SimpleNamespace(user_data={'selected_plan': {'id': 1}, 'target_link': '@public'},
                                  bot_data={'bot_id': 1})
        with patch.object(links, 'link_mode', return_value='private'), \
                patch.object(h, 'safe_answer', new_callable=AsyncMock), \
                patch.object(CallbackQuery, 'edit_message_text', new_callable=AsyncMock), \
                patch.object(h.DatabaseManager, 'create_paid_order', new_callable=AsyncMock) as buy:
            state = await h.handle_order_confirmation(update, context)
        self.assertEqual(state, h.AWAITING_ORDER_LINK)
        buy.assert_not_awaited()
        self.assertNotIn('target_link', context.user_data)


if __name__ == '__main__':
    unittest.main()
