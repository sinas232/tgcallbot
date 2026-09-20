"""The welcome screen must not mix a reseller's branding with the main bot."""
import os
import unittest
from sqlalchemy import event
from database import DatabaseManager as DB
from services.start_message import START_DEFAULTS
from tests import test_settlement_postgres as fixtures


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class StartSettingsPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown

    async def test_batch_is_one_query_tenant_scoped_and_preserves_defaults(self):
        await DB.set_setting('start_brand', 'Main', bot_id=1)
        await DB.set_setting('start_brand', 'Reseller', bot_id=7)
        await DB.set_setting('start_text', '**سلام {name}** {credit}', bot_id=7)
        await DB.set_setting('service_voice_chat', 'false', bot_id=7)
        await DB.set_setting('unrequested', 'secret', bot_id=7)
        calls = []
        def track(conn, cursor, statement, parameters, context, executemany):
            calls.append(statement)
        event.listen(self.engine.sync_engine, 'before_cursor_execute', track)
        try:
            values = await DB.get_settings(START_DEFAULTS, bot_id=7)
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', track)
        self.assertEqual(len(calls), 1)
        self.assertEqual(values['start_brand'], 'Reseller')
        self.assertEqual(values['start_text'], '**سلام {name}** {credit}')
        self.assertEqual(values['service_voice_chat'], 'false')
        self.assertEqual(values['start_support'], START_DEFAULTS['start_support'])
        self.assertNotIn('unrequested', values)
        self.assertEqual(START_DEFAULTS['start_brand'], '')
        self.assertEqual((await DB.get_settings(START_DEFAULTS, bot_id=1))['start_brand'], 'Main')
        self.assertEqual(await DB.get_settings({}, bot_id=7), {})


    async def test_new_sections_and_reset_persist_only_for_selected_tenant(self):
        from types import SimpleNamespace
        from services.start_message import START_FIELDS, render_start_message
        for key, _ in START_FIELDS.values():
            await DB.set_setting(key, 'Custom ' + key, bot_id=7)
        await DB.set_setting('service_voice_chat', 'false', bot_id=7)
        settings = await DB.get_settings(START_DEFAULTS, bot_id=7)
        user = SimpleNamespace(id=567, first_name='Sina', last_name=None, username=None)
        bot = SimpleNamespace(first_name='Demo', username='demo_bot')
        text = render_start_message(user, {'credit': 100.25}, bot, settings)
        for key in ('start_intro', 'start_benefits', 'start_guide', 'start_cta', 'start_label_group_join'):
            self.assertIn('Custom ' + key, text)
        self.assertNotIn('Custom start_label_voice_chat', text)
        self.assertIn('100.25', text)
        self.assertEqual(await DB.get_settings(START_DEFAULTS, bot_id=1), START_DEFAULTS)
        await DB.set_setting('start_brand', '', bot_id=7)
        settings = await DB.get_settings(START_DEFAULTS, bot_id=7)
        self.assertIn('<b>Demo</b>', render_start_message(user, {'credit': 100.25}, bot, settings))
