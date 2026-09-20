"""نسخهٔ ۲.۲.۱۸ — فقط «لینک خصوصی» پذیرفته می‌شود.

قاعده:
    ✔ https://t.me/+HASH  ·  https://t.me/joinchat/HASH  (و شکل‌های هم‌ارز)
    ✘ لینک عمومی (`@username` / `t.me/username`)، لینک پیام، دامنهٔ دیگر،
      متن نامربوط ⇒ سفارش ساخته نمی‌شود و پیام «لینک درست را بفرستید» می‌آید.
"""
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from telegram import Chat, Message, Update, User  # noqa: E402
from services import link_validator  # noqa: E402
from services.link_validator import (  # noqa: E402
    PRIVATE_LINK_EXAMPLE, help_text, is_private_invite_link, normalize_invite_link,
    validate_order_link,
)

VALID = [
    'https://t.me/+AbCdEf123456',
    'http://t.me/+AbCdEf123456',
    't.me/+AbCdEf123456',
    'telegram.me/+AbCdEf123456',
    'www.t.me/+AbCdEf123456',
    'https://t.me/+AbCdEf123456/',
    'https://t.me/+AbCdEf123456?join=1',
    '+AbCdEf123456',
    'https://t.me/joinchat/AbCdEf123456',
    't.me/joinchat/AbCdEf123456',
    'joinchat/AbCdEf123456',
    'https://telegram.dog/+AbCdEf123456',
    'tg://join?invite=AbCdEf123456',
    '  https://t.me/+AbCdEf123456  ',
    'https://t.me/+2bX9kLmN0pQrStUv',
]

INVALID = [
    '@mygroup',
    't.me/mygroup',
    'https://t.me/mygroup',
    'mygroup',
    'https://t.me/mygroup/123',
    'https://t.me/c/1234567890/12',
    'https://example.com/+abcdef123456',
    'https://t.me/+abc',            # هش کوتاه
    'https://t.me/+',               # بدون هش
    'https://t.me/+AbCdEf123456/extra',
    'سلام',
    'لینک گروه',
    '',
    '   ',
    None,
]


class ValidatorTests(unittest.TestCase):
    def test_only_private_invite_links_are_accepted(self):
        for link in VALID:
            ok, normalized, error = validate_order_link(link)
            self.assertTrue(ok, f'should accept {link!r} ({error})')
            self.assertFalse(error)
            self.assertTrue(normalized.startswith(('https://t.me/', 'https://telegram.dog/')))
            self.assertTrue(is_private_invite_link(link))

    def test_everything_else_is_rejected_with_a_clear_message(self):
        for link in INVALID:
            ok, normalized, error = validate_order_link(link)
            self.assertFalse(ok, f'should reject {link!r}')
            self.assertTrue(error)
            self.assertIn('❌', error)

    def test_normalization_keeps_the_invite_kind(self):
        self.assertEqual(normalize_invite_link('t.me/+AbCdEf123456'), 'https://t.me/+AbCdEf123456')
        self.assertEqual(normalize_invite_link('t.me/joinchat/AbCdEf123456'),
                         'https://t.me/joinchat/AbCdEf123456')
        self.assertEqual(normalize_invite_link('+AbCdEf123456'), 'https://t.me/+AbCdEf123456')
        self.assertEqual(normalize_invite_link('@someone'), '@someone')  # چیزی خراب نمی‌شود

    def test_public_link_message_mentions_the_correct_template(self):
        ok, _n, error = validate_order_link('@mygroup')
        self.assertFalse(ok)
        self.assertIn(PRIVATE_LINK_EXAMPLE, error)

    def test_rejection_message_tells_how_to_cancel(self):
        from constants import BTN_CANCEL
        text = link_validator.rejection_message(link_validator.MESSAGES['public'])
        self.assertIn(BTN_CANCEL, text)

    def test_help_text_shows_both_examples(self):
        text = help_text()
        self.assertIn('https://t.me/+AbCdEf123456', text)
        self.assertIn('https://t.me/joinchat/AbCdEf123456', text)

    def test_escape_hatch_mode_accepts_legacy_input(self):
        with patch.object(link_validator, 'link_mode', return_value='any'):
            ok, normalized, error = validate_order_link('@mygroup')
        self.assertTrue(ok)
        self.assertEqual(normalized, '@mygroup')
        self.assertFalse(error)
        # حتی در حالت any، لینک خالی پذیرفته نمی‌شود
        with patch.object(link_validator, 'link_mode', return_value='any'):
            self.assertFalse(validate_order_link('   ')[0])

    def test_custom_regex_overrides_the_default(self):
        with patch.dict(os.environ, {'ORDER_LINK_REGEX': r'^https://t\.me/\+[A-Za-z0-9_-]{16,}$'}):
            self.assertTrue(validate_order_link('https://t.me/+AbCdEf123456789012')[0])
            self.assertFalse(validate_order_link('https://t.me/+AbCdEf123456')[0])  # ۱۲ کاراکتر
        with patch.dict(os.environ, {'ORDER_LINK_REGEX': '([unclosed'}):
            self.assertTrue(validate_order_link('https://t.me/+AbCdEf123456')[0])  # الگوی خراب ⇒ پیش‌فرض

    def test_same_group_forms_share_one_key(self):
        from services.deferred_leave import normalize_target
        keys = {normalize_target(v) for v in ('https://t.me/+AbCdEf123456',
                                              't.me/+AbCdEf123456',
                                              '+AbCdEf123456',
                                              'tg://join?invite=AbCdEf123456')}
        self.assertEqual(len(keys), 1)


def message_update(text):
    user = User(567, 'Test', False)
    msg = Message(7, datetime.now(), Chat(567, 'private'), text=text)
    return Update(1, message=msg)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    """`receive_order_link` باید لینک بد را رد کند و منتظر لینک درست بماند."""

    async def call(self, text, user_data=None):
        import handlers.order_handlers as OH
        update = message_update(text)
        context = SimpleNamespace(user_data=user_data if user_data is not None else {},
                                  bot=SimpleNamespace(), bot_data={})
        with patch.object(OH, 'send_safe', AsyncMock()) as send, \
                patch.object(Message, 'reply_text', AsyncMock()) as reply:
            state = await OH.receive_order_link(update, context)
        if send.await_args:
            reply_text = send.await_args.args[2]
        else:
            reply_text = reply.await_args.args[0] if reply.await_args else ''
        return state, context.user_data, reply_text

    async def test_wrong_link_is_rejected_and_asked_again(self):
        state, data, reply = await self.call('@mygroup')
        self.assertEqual(state, 11)                     # AWAITING_ORDER_LINK
        self.assertNotIn('target_link', data)           # سفارش ثبت نمی‌شود
        self.assertIn(PRIVATE_LINK_EXAMPLE, reply)

    async def test_random_text_is_rejected(self):
        for bad in ('سلام', 'hello', 'https://example.com/x', 't.me/group/12'):
            state, data, reply = await self.call(bad)
            self.assertEqual(state, 11, bad)
            self.assertNotIn('target_link', data, bad)
            self.assertIn('❌', reply)

    async def test_private_link_is_accepted_and_normalized(self):
        state, data, reply = await self.call('t.me/+AbCdEf123456')
        self.assertEqual(state, 13)                     # AWAITING_ORDER_TIMING_TYPE
        self.assertEqual(data['target_link'], 'https://t.me/+AbCdEf123456')

    async def test_payment_path_refuses_a_bad_link_and_returns_to_link_step(self):
        """مسیر پرداخت: لینک بد ذخیره‌شده ⇒ هیچ پرداختی انجام نمی‌شود."""
        import handlers.order_handlers as OH
        from telegram import CallbackQuery
        user = User(567, 'Test', False)
        msg = Message(7, datetime.now(), Chat(567, 'private'), text='order')
        query = CallbackQuery('cq', user, 'instance', data='confirm_order_pay', message=msg)
        update = Update(1, callback_query=query)
        context = SimpleNamespace(user_data={'selected_plan': {'id': 1, 'price': 1000, 'duration_minutes': 10},
                                             'target_link': '@publicgroup',
                                             'checkout_message': (567, 7)},
                                  bot=SimpleNamespace(), bot_data={'bot_id': 1})
        with patch.object(OH, 'safe_answer', AsyncMock()), \
                patch.object(CallbackQuery, 'edit_message_text', AsyncMock()) as edit, \
                patch.object(OH.DatabaseManager, 'get_user', AsyncMock(return_value={'id': 1, 'credit': 999999})), \
                patch.object(OH.DatabaseManager, 'purchase_order_atomic', AsyncMock()) as purchase:
            state = await OH.handle_order_confirmation(update, context)
        self.assertEqual(state, 11)                      # برگشت به مرحلهٔ لینک
        purchase.assert_not_awaited()
        self.assertTrue(edit.await_args)
        self.assertIn(PRIVATE_LINK_EXAMPLE, edit.await_args.args[0])

    async def test_link_is_normalized_again_before_payment(self):
        """مسیر پرداخت: لینک ذخیره‌شده دوباره اعتبارسنجی و نرمال می‌شود."""
        import inspect
        src = inspect.getsource(__import__('handlers.order_handlers', fromlist=['x']))
        pay_block = src.split('if data == "confirm_order_pay":')[1].split('user = await DatabaseManager.get_user')[0]
        self.assertIn('validate_order_link(link)', pay_block)
        self.assertIn('return AWAITING_ORDER_LINK', pay_block)


class StorageTests(unittest.TestCase):
    def test_purchase_normalizes_the_link(self):
        import inspect
        import database
        src = inspect.getsource(database.DatabaseManager.purchase_order_atomic)
        self.assertIn('normalize_invite_link', src)

    def test_overlap_check_compares_normalized_group_keys(self):
        import inspect
        import database
        src = inspect.getsource(database.DatabaseManager.has_time_overlap_order)
        self.assertIn('normalize_target', src)
        self.assertNotIn('Order.target_link == link', src)

    def test_defaults_are_documented(self):
        import config
        self.assertEqual(config.Config.ORDER_LINK_MODE, 'private')
        env = open(os.path.join(os.path.dirname(__file__), '..', '.env.example'), encoding='utf-8').read()
        self.assertIn('ORDER_LINK_MODE=private', env)
        self.assertIn('ORDER_LINK_REGEX=', env)

    def test_version_and_docs(self):
        root = os.path.join(os.path.dirname(__file__), '..')
        constants = open(os.path.join(root, 'constants.py'), encoding='utf-8').read()
        changelog = open(os.path.join(root, 'CHANGELOG.md'), encoding='utf-8').read()
        docs = os.path.join(root, 'docs', 'LINK_FORMAT_2.2.18_FA.md')
        self.assertTrue('BOT_VERSION = "2.2.18"' in constants)
        self.assertTrue('نسخهٔ ۲.۲.۱۸' in changelog)
        self.assertTrue(os.path.exists(docs))


if __name__ == '__main__':
    unittest.main()
