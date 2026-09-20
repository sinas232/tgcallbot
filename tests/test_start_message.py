import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services.start_message import (
    DEFAULT_START_TEXT, START_DEFAULTS, START_FIELDS, PREVIEW_START,
    USE_DEFAULT_START, render_start_message, start_values, validate_start_template,
    PREVIEW_DEFAULT_START, SHOW_START_TEMPLATE, RESET_START_FIELD, BACK_START_EDITOR, START_FIELD_LIMITS,
)
from database import DatabaseManager as DB
from constants import AWAITING_SUPPORT_TEXT, BTN_CANCEL, USER_MAIN_MENU


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=123, first_name='🐢ѕιηα🐢', last_name='Test', username='sina_test')
        self.bot = SimpleNamespace(first_name='برند آزمایشی', username='demo_bot')
        self.wallet = {'credit': 90945050.53}

    def render(self, template=None, **settings):
        data = dict(START_DEFAULTS, **settings)
        if template is not None:
            data['start_text'] = template
        return render_start_message(self.user, self.wallet, self.bot, data)

    def test_default_layout_and_fractional_wallet(self):
        validate_start_template(DEFAULT_START_TEXT)
        text = self.render()
        self.assertIn('<b>🐢ѕιηα🐢 عزیز، خوش آمدید!</b>', text)
        self.assertIn('<b>برند آزمایشی</b>', text)
        self.assertIn('<code>90,945,050.53</code>', text)
        self.assertNotIn('۲۴ ساعته', text)
        self.assertNotIn('{', text)

    def test_markup_in_names_and_brand_is_only_text(self):
        self.user.first_name = '<b>A & B</b> **X** {credit}'
        text = self.render('<b>{name}</b> {brand}', start_brand='<a href="bad">evil</a>')
        self.assertIn('&lt;b&gt;A &amp; B&lt;/b&gt; **X** {credit}', text)
        self.assertIn('&lt;a href=&quot;bad&quot;&gt;', text)
        self.assertNotIn('<a href=', text)
        self.assertNotIn('90,945', text)  # no recursive substitution

    def test_legacy_markdown_is_converted_before_value_insertion(self):
        self.user.first_name = '*unsafe* <b>x</b>'
        text = self.render('**سلام {name}**\n`{credit}`\n{support_hours}')
        self.assertIn('<b>سلام *unsafe* &lt;b&gt;x&lt;/b&gt;</b>', text)
        self.assertIn('<code>90,945,050.53</code>', text)
        self.assertNotIn('{support_hours}', text)

    def test_literal_and_unknown_legacy_tokens_are_preserved(self):
        self.assertEqual(self.render('{{name}} {missing} {id}'), '{name} {missing} 123')

    def test_unsafe_format_expressions_rejected(self):
        for template in ['{name.__class__}', '{credit:.2f}', '{name!r}', '{unknown}', 'سلام {name']:
            with self.subTest(template=template), self.assertRaises(ValueError):
                validate_start_template(template)

    def test_invalid_html_rejected(self):
        for template in ['<b>open', '<b><i>x</b></i>', '<b>x<br/></b>', '<b><script>x</script></b>']:
            with self.subTest(template=template), self.assertRaises(ValueError):
                validate_start_template(template)

    def test_supported_premium_and_link_markup_is_kept(self):
        template = '<tg-emoji emoji-id="1234567890123456789">💎</tg-emoji> <a href="tg://user?id={id}">{name}</a>'
        validate_start_template(template)
        text = self.render(template)
        self.assertIn('tg://user?id=123', text)
        self.assertIn('<tg-emoji', text)

    def test_disabled_services_not_advertised(self):
        text = self.render('{services}', service_voice_chat='false', service_group_join='false',
                           service_channel_join='false', service_incall_chat='true')
        self.assertEqual(text, '💬 چت در ویس‌کال')

    def test_all_services_disabled(self):
        text = self.render('{services}', **{k: 'false' for k in START_DEFAULTS if k.startswith('service_')})
        self.assertIn('فعلاً سرویسی فعال نیست', text)

    def test_empty_username_and_brand_fallback(self):
        self.user.username = None
        text = self.render('{username} / {brand}')
        self.assertEqual(text, 'بدون نام کاربری / برند آزمایشی')

    def test_clock_uses_tehran_and_jalali(self):
        values = start_values(self.user, self.wallet, self.bot, START_DEFAULTS,
                              now=datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(values['time'], '03:30')
        self.assertTrue(values['date'].startswith('1405/'))

    def test_long_expanded_template_falls_back_without_losing_wallet(self):
        text = self.render('{brand}' * 40, start_brand='b' * 120)
        self.assertIn('خدمات در دسترس شما', text)
        self.assertIn('90,945,050.53', text)
        self.assertLess(len(text.encode('utf-16-le')) // 2, 3900)

    def test_validation_size_bounds(self):
        for text in ['', '  ', 'a' * 2501]:
            with self.assertRaises(ValueError):
                validate_start_template(text)

    def test_all_sections_customizable_without_html_injection(self):
        settings = {f'start_{key}': f'<b>{key}</b>' for key in ('intro', 'benefits', 'guide', 'cta')}
        text = self.render(**settings)
        for key in ('intro', 'benefits', 'guide', 'cta'):
            self.assertIn(f'&lt;b&gt;{key}&lt;/b&gt;', text)

    def test_custom_service_label_does_not_enable_disabled_service(self):
        text = self.render('{services}', start_label_voice_chat='<b>Custom voice</b>', service_voice_chat='false')
        self.assertNotIn('Custom voice', text)
        text = self.render('{services}', start_label_voice_chat='<b>Custom voice</b>')
        self.assertIn('&lt;b&gt;Custom voice&lt;/b&gt;', text)

    def test_extreme_parameter_expansion_keeps_wallet_and_valid_budget(self):
        settings = {key: '&' * limit for key, limit in START_FIELD_LIMITS.items()}
        text = self.render(**settings)
        self.assertIn('90,945,050.53', text)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 3900)
        self.assertNotIn('{credit}', text)

    def test_old_two_variable_template_still_works(self):
        self.assertEqual(self.render('سلام {name} | {credit} تومان'), 'سلام 🐢ѕιηα🐢 | 90,945,050.53 تومان')


class StartHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=123, first_name='Sina', last_name=None, username='sina')
        self.bot = SimpleNamespace(first_name='Demo', username='demo_bot', send_message=AsyncMock())
        self.update = SimpleNamespace(effective_user=self.user, effective_chat=SimpleNamespace(id=123),
                                      message=SimpleNamespace(text=''), callback_query=None)
        self.context = SimpleNamespace(bot=self.bot, bot_data={'bot_id': 7}, user_data={})

    async def edit(self, text, key='start_text', allowed=True):
        from handlers.admin_handlers import handle_setting_text_input
        self.update.message.text = text
        self.update.effective_message = SimpleNamespace(reply_text=AsyncMock())
        self.context.user_data['setting_type'] = key
        with patch('services.maintenance.is_super_admin', AsyncMock(return_value=allowed)), \
                patch.object(DB, 'set_setting', AsyncMock()) as save, \
                patch.object(DB, 'get_setting', AsyncMock(side_effect=lambda key, default='', **kw: START_DEFAULTS.get(key, default))), \
                patch.object(DB, 'get_settings', AsyncMock(return_value=dict(START_DEFAULTS))), \
                patch.object(DB, 'get_user', AsyncMock(return_value={'credit': 100.25})):
            state = await handle_setting_text_input(self.update, self.context)
        return save, state

    async def test_start_reads_one_tenant_batch_and_preserves_menu(self):
        from handlers.general_handlers import start_command
        settings = dict(START_DEFAULTS, start_brand='Tenant Seven', service_incall_chat='true')
        with patch.object(DB, 'get_settings', AsyncMock(return_value=settings)) as read, \
                patch.object(DB, 'create_or_update_user', AsyncMock(return_value={'credit': 100.25, 'is_admin': True})), \
                patch('handlers.general_handlers.check_security', AsyncMock(return_value=True)), \
                patch('handlers.conversation_registry.clear_conversations'):
            await start_command(self.update, self.context)
        read.assert_awaited_once_with(START_DEFAULTS, bot_id=7)
        message = self.bot.send_message.await_args.kwargs
        self.assertEqual(message['parse_mode'], 'HTML')
        self.assertIn('Tenant Seven', message['text'])
        labels = [[b.text for b in row] for row in message['reply_markup'].keyboard]
        self.assertEqual(labels[:len(USER_MAIN_MENU)], USER_MAIN_MENU)
        self.assertIn(['💬 چت در ویس‌کال'], labels)
        self.assertIn(['🔐 پنل مدیریت (ادمین)'], labels)

    async def test_security_denial_precedes_settings_and_welcome(self):
        from handlers.general_handlers import start_command
        with patch.object(DB, 'create_or_update_user', AsyncMock(return_value={})), \
                patch.object(DB, 'get_settings', AsyncMock()) as read, \
                patch('handlers.general_handlers.check_security', AsyncMock(return_value=False)), \
                patch('handlers.conversation_registry.clear_conversations'):
            await start_command(self.update, self.context)
        read.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_preview_never_saves(self):
        save, state = await self.edit(PREVIEW_START)
        save.assert_not_awaited()
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)

    async def test_preset_is_explicit_opt_in(self):
        save, state = await self.edit(USE_DEFAULT_START)
        save.assert_awaited_once_with('start_text', DEFAULT_START_TEXT, bot_id=7)
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)

    async def test_custom_template_saved_for_current_bot(self):
        save, _ = await self.edit('<b>{name}</b> {credit}')
        save.assert_awaited_once_with('start_text', '<b>{name}</b> {credit}', bot_id=7)

    async def test_invalid_template_not_saved(self):
        save, state = await self.edit('{not_known}')
        save.assert_not_awaited()
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)

    async def test_parameter_button_switches_input_without_saving(self):
        save, state = await self.edit('🏷 نام برند')
        save.assert_not_awaited()
        self.assertEqual(self.context.user_data['setting_type'], 'start_brand')
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)

    async def test_parameter_save_returns_to_editor(self):
        save, state = await self.edit('My brand', key='start_brand')
        save.assert_awaited_once_with('start_brand', 'My brand', bot_id=7)
        self.assertEqual(self.context.user_data['setting_type'], 'start_text')
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)

    async def test_parameter_size_limited(self):
        save, _ = await self.edit('a' * 121, key='start_brand')
        save.assert_not_awaited()

    async def test_unauthorized_text_input_cannot_write(self):
        save, _ = await self.edit('new brand', key='start_brand', allowed=False)
        save.assert_not_awaited()

    async def test_support_text_flow_kept(self):
        save, _ = await self.edit('support message', key='support_text')
        save.assert_awaited_once_with('support_text', 'support message', bot_id=7)

    async def test_cancel_does_not_save(self):
        with patch('handlers.admin_handlers.settings_menu_handler', AsyncMock(return_value=99)):
            save, state = await self.edit(BTN_CANCEL)
        save.assert_not_awaited()
        self.assertEqual(state, 99)


    async def test_proposed_preview_does_not_replace_saved_template(self):
        save, state = await self.edit(PREVIEW_DEFAULT_START)
        save.assert_not_awaited()
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)
        self.assertIn('خدمات در دسترس شما', self.bot.send_message.await_args.kwargs['text'])
        from handlers.admin_handlers import _preview_start_text
        saved = dict(START_DEFAULTS, start_text='قالب قدیمی {name}')
        with patch.object(DB, 'get_settings', AsyncMock(return_value=saved)), \
                patch.object(DB, 'get_user', AsyncMock(return_value={'credit': 25.5})), \
                patch.object(DB, 'set_setting', AsyncMock()) as write:
            await _preview_start_text(self.update, self.context, proposed=True)
        write.assert_not_awaited()
        self.assertEqual(saved['start_text'], 'قالب قدیمی {name}')
        self.assertIn('خدمات در دسترس شما', self.bot.send_message.await_args.kwargs['text'])

    async def test_copy_template_sends_raw_markup_without_save(self):
        save, state = await self.edit(SHOW_START_TEMPLATE)
        save.assert_not_awaited()
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)
        payload = self.bot.send_message.await_args.kwargs
        self.assertEqual(payload['text'], DEFAULT_START_TEXT)
        self.assertIsNone(payload['parse_mode'])

    async def test_multiline_guide_allowed_up_to_its_own_limit(self):
        text = 'line one\n' + 'a' * 300
        save, _ = await self.edit(text, key='start_guide')
        save.assert_awaited_once_with('start_guide', text, bot_id=7)
        save, _ = await self.edit('a' * 361, key='start_guide')
        save.assert_not_awaited()

    async def test_reset_brand_restores_auto_bot_name(self):
        save, _ = await self.edit(RESET_START_FIELD, key='start_brand')
        save.assert_awaited_once_with('start_brand', '', bot_id=7)

    async def test_reset_guide_uses_preset_not_an_arbitrary_key(self):
        save, _ = await self.edit(RESET_START_FIELD, key='start_guide')
        save.assert_awaited_once_with('start_guide', START_DEFAULTS['start_guide'], bot_id=7)

    async def test_back_to_editor_does_not_save_button_as_a_value(self):
        save, state = await self.edit(BACK_START_EDITOR, key='start_intro')
        save.assert_not_awaited()
        self.assertEqual(state, AWAITING_SUPPORT_TEXT)
        self.assertEqual(self.context.user_data['setting_type'], 'start_text')

    async def test_each_parameter_has_a_reachable_two_column_button(self):
        from handlers.admin_handlers import _start_editor_keyboard
        rows = _start_editor_keyboard().keyboard
        labels = [button.text for row in rows for button in row]
        self.assertTrue(all(len(row) <= 2 for row in rows))
        for label in START_FIELDS:
            self.assertEqual(labels.count(label), 1)
            save, state = await self.edit(label)
            save.assert_not_awaited()
            self.assertEqual(state, AWAITING_SUPPORT_TEXT)
            self.assertEqual(self.context.user_data['setting_type'], START_FIELDS[label][0])
