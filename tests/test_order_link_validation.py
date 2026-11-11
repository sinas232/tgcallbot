"""نسخهٔ ۲.۲.۱۸/۲.۲.۲۰ — فقط «لینک خصوصی» پذیرفته می‌شود.

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

# لینک واقعی ادمین (نمونهٔ درخواست‌شده) و شکل‌های هم‌ارزش
REAL_LINK = 'https://t.me/+8hR1-wquL2liMTVk'
REAL_LINK_VARIANTS = [
    REAL_LINK,
    REAL_LINK + '/',
    'http://t.me/+8hR1-wquL2liMTVk',
    't.me/+8hR1-wquL2liMTVk',
    'www.t.me/+8hR1-wquL2liMTVk',
    'https://telegram.me/+8hR1-wquL2liMTVk',
    'https://telegram.dog/+8hR1-wquL2liMTVk',
    'tg://join?invite=8hR1-wquL2liMTVk',
    'https://t.me/joinchat/8hR1-wquL2liMTVk',
    '+8hR1-wquL2liMTVk',
    'joinchat/8hR1-wquL2liMTVk',
    REAL_LINK + '?single',
    REAL_LINK + '\u200c',                      # نیم‌فاصله/کاراکتر نامرئی
    f'«{REAL_LINK}»',                          # گیومهٔ فارسی
    f'({REAL_LINK})',                          # پرانتز
    REAL_LINK + '.',                           # نقطهٔ انتهایی
    f'لینک گروه: {REAL_LINK}',                 # متن اضافه قبل از لینک
    f'لینک رو بفرست {REAL_LINK} ممنون',        # متن اضافه دو طرف
    f'[گروه]({REAL_LINK})',                    # مارک‌داون
    f'{REAL_LINK}\nhttps://t.me/+OtherHash123',  # دو لینک در یک پیام
    'https://t.me/+8hR1_wqu-L2liMTVk',         # آندرلاین و خط تیره در هش
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

    def test_the_admin_sample_link_is_accepted_in_every_real_form(self):
        for link in REAL_LINK_VARIANTS:
            ok, normalized, error = validate_order_link(link)
            self.assertTrue(ok, f'should accept {link!r} ({error})')
            self.assertFalse(error)
            self.assertTrue(normalized.startswith(('https://t.me/', 'https://telegram.dog/')),
                            normalized)
            self.assertTrue(normalized.rstrip('/').split('/')[-1].lstrip('+'), normalized)

    def test_sample_link_normalizes_to_one_canonical_value(self):
        expected = REAL_LINK
        for link in ('t.me/+8hR1-wquL2liMTVk', '+8hR1-wquL2liMTVk',
                     'tg://join?invite=8hR1-wquL2liMTVk', f'«{REAL_LINK}»',
                     f'لینک: {REAL_LINK}.'):
            self.assertEqual(normalize_invite_link(link), expected, link)

    def test_decorated_text_is_still_rejected_when_the_link_itself_is_wrong(self):
        for bad in (f'@mygroup و {REAL_LINK.replace("+", "")}',   # یوزرنیم + متن بی‌ربط
                    'گروه من: @mygroup',
                    'لینک: https://t.me/mygroup/12',
                    'https://t.me/+short'):                       # هش کوتاه
            ok, _n, error = validate_order_link(bad)
            self.assertFalse(ok, bad)
            self.assertIn('لینک درست را بفرستید', error)

    def test_wrong_format_message_always_contains_the_required_sentence(self):
        for bad in INVALID:
            ok, _n, error = validate_order_link(bad)
            self.assertFalse(ok, bad)
            self.assertIn('👉 لینک درست را بفرستید.', error)
            self.assertIn('❌', error)

    def test_only_private_links_survive_a_full_message_with_words(self):
        ok, normalized, _e = validate_order_link(
            f'سلام، این لینک گروهه: {REAL_LINK} لطفاً سفارش رو ثبت کن')
        self.assertTrue(ok)
        self.assertEqual(normalized, REAL_LINK)
        # پیام دیگری که فقط یوزرنیم دارد، با هیچ متنی پذیرفته نمی‌شود
        self.assertFalse(validate_order_link('سلام، این لینک گروهه: @mygroup لطفاً')[0])

    def test_hidden_hyperlink_url_is_checked(self):
        """متن ≠ آدرس: آدرس واقعیِ هایپرلینک بررسی می‌شود."""
        self.assertTrue(validate_order_link('گروه مقصد', [REAL_LINK])[0])
        ok, normalized, _e = validate_order_link('گروه مقصد', [f'  {REAL_LINK}/  '])
        self.assertTrue(ok)
        self.assertEqual(normalized, REAL_LINK)
        self.assertFalse(validate_order_link('گروه مقصد', ['https://t.me/mygroup'])[0])
        self.assertFalse(validate_order_link('گروه مقصد', ['@mygroup'])[0])

    def test_legacy_joinchat_hash_of_the_sample_link_is_kept(self):
        ok, normalized, _e = validate_order_link('https://t.me/joinchat/8hR1-wquL2liMTVk')
        self.assertTrue(ok)
        self.assertEqual(normalized, 'https://t.me/joinchat/8hR1-wquL2liMTVk')

    def test_custom_display_example_is_respected(self):
        with patch.dict(os.environ, {'ORDER_LINK_EXAMPLE': REAL_LINK}):
            self.assertIn(REAL_LINK, link_validator.message('public'))
            self.assertIn(REAL_LINK, link_validator.help_text())
            self.assertIn('joinchat/8hR1-wquL2liMTVk', link_validator.message('invalid'))
        # مقدار نامعتبر نادیده گرفته می‌شود و قالب پیش‌فرض می‌ماند
        with patch.dict(os.environ, {'ORDER_LINK_EXAMPLE': '@mygroup'}):
            self.assertIn(PRIVATE_LINK_EXAMPLE, link_validator.message('public'))

    def test_same_group_forms_share_one_key(self):
        from services.deferred_leave import normalize_target
        keys = {normalize_target(v) for v in ('https://t.me/+AbCdEf123456',
                                              't.me/+AbCdEf123456',
                                              '+AbCdEf123456',
                                              'https://t.me/AbCdEf123456/'.replace('/AbCd', '/+AbCd'),
                                              '«https://t.me/+AbCdEf123456»',
                                              f'لینک گروه: https://t.me/+AbCdEf123456 ممنون',
                                              'https://t.me/+AbCdEf123456?single',
                                              'https://telegram.dog/+AbCdEf123456',
                                              'tg://join?invite=AbCdEf123456')}
        self.assertEqual(keys, {'invite:abcdef123456'})

    def test_group_key_helper_shares_the_validator_rule(self):
        """قاعدهٔ کلید گروه نباید از اعتبارسنج جدا شود."""
        import inspect
        from services import deferred_leave
        source = inspect.getsource(deferred_leave.normalize_target)
        self.assertIn('link_validator', source)
        self.assertIn('invite_hash', source)

    def test_different_groups_never_share_a_key(self):
        from services.deferred_leave import normalize_target
        self.assertNotEqual(normalize_target('https://t.me/+AbCdEf123456'),
                            normalize_target('https://t.me/+ZzYyXx654321'))
        # لینک عمومی هرگز با لینک خصوصی یکی نمی‌شود
        self.assertNotEqual(normalize_target('@mygroup'), normalize_target('+mygroup'))


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

    async def test_sample_link_with_surrounding_text_is_accepted(self):
        state, data, reply = await self.call(f'لینک گروه: {REAL_LINK} — ممنون')
        self.assertEqual(state, 13)                     # AWAITING_ORDER_TIMING_TYPE
        self.assertEqual(data['target_link'], REAL_LINK)

    async def test_hidden_hyperlink_message_is_accepted(self):
        """پیامی که فقط آدرسش (entity) لینک خصوصی است."""
        import handlers.order_handlers as OH
        from telegram import MessageEntity
        user = User(567, 'Test', False)
        msg = Message(7, datetime.now(), Chat(567, 'private'), text='گروه مقصد',
                      entities=[MessageEntity(MessageEntity.TEXT_LINK, 0, len('گروه مقصد'),
                                              url=REAL_LINK)])
        update = Update(1, message=msg)
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(), bot_data={})
        with patch.object(OH, 'send_safe', AsyncMock()), \
                patch.object(Message, 'reply_text', AsyncMock()):
            state = await OH.receive_order_link(update, context)
        self.assertEqual(state, 13)
        self.assertEqual(context.user_data['target_link'], REAL_LINK)

    async def test_hidden_public_hyperlink_is_rejected(self):
        import handlers.order_handlers as OH
        from telegram import MessageEntity
        user = User(567, 'Test', False)
        msg = Message(7, datetime.now(), Chat(567, 'private'), text='گروه مقصد',
                      entities=[MessageEntity(MessageEntity.TEXT_LINK, 0, len('گروه مقصد'),
                                              url='https://t.me/mygroup')])
        update = Update(1, message=msg)
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(), bot_data={})
        with patch.object(OH, 'send_safe', AsyncMock()) as send, \
                patch.object(Message, 'reply_text', AsyncMock()):
            state = await OH.receive_order_link(update, context)
        self.assertEqual(state, 11)
        self.assertNotIn('target_link', context.user_data)
        self.assertIn('لینک درست را بفرستید', send.await_args.args[2])

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
        self.assertIn('ORDER_LINK_EXAMPLE=', env)
        self.assertEqual(config.Config.ORDER_LINK_EXAMPLE, '')

    def test_version_and_docs(self):
        root = os.path.join(os.path.dirname(__file__), '..')
        constants = open(os.path.join(root, 'constants.py'), encoding='utf-8').read()
        changelog = open(os.path.join(root, 'CHANGELOG.md'), encoding='utf-8').read()
        docs = os.path.join(root, 'docs', 'LINK_FORMAT_2.2.19_FA.md')
        self.assertTrue('BOT_VERSION = "2.2.23"' in constants)   # نسخهٔ جاری
        self.assertTrue('نسخهٔ ۲.۲.۱۹' in changelog)
        self.assertTrue(os.path.exists(docs))
        self.assertTrue(os.path.exists(os.path.join(root, 'docs', 'LINK_FORMAT_2.2.18_FA.md')))


if __name__ == '__main__':
    unittest.main()
