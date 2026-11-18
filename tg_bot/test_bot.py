import asyncio
import importlib
import logging.config
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from django.conf import settings
from aiogram.types import Update, CallbackQuery, Message, User, Chat
from core.observability import context


class BotTests(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = patch.dict(os.environ, {'TELEGRAM_TOKEN': '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',
                                         'FULL_ADMIN_IDS': '101', 'LIMITED_ADMIN_IDS': '202'})
        cls.env.start()
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        cls.bot = importlib.import_module('bot')
        logging.config.dictConfig(settings.LOGGING)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        logging.config.dictConfig(settings.LOGGING)
        super().tearDownClass()

    async def asyncTearDown(self):
        await self.bot.bot.session.close()

    async def dispatch(self, data, actor=303, state=None, message_id=1, private=False, api_result=None):
        chat_id = actor if private else -1001234567890
        storage = self.bot.dp.fsm.get_context(bot=self.bot.bot, chat_id=chat_id, user_id=actor)
        await storage.clear()
        if state:
            await storage.set_state(state)
        # These tests concern permissions, not the independent one-second flood limit.
        for middleware in self.bot.dp.callback_query.middleware:
            if hasattr(middleware, 'user_timestamps'):
                middleware.user_timestamps.clear()
        event = Update(update_id=message_id, callback_query=CallbackQuery(
            id=str(message_id), from_user=User(id=actor, is_bot=False, first_name='Test'), chat_instance='test', data=data,
            message=Message(message_id=message_id, date=datetime.now(timezone.utc),
                            chat=Chat(id=chat_id, type='private' if private else 'supergroup'), text='Administrative menu')))
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value=api_result or {}) as api:
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                await self.bot.dp.feed_update(self.bot.bot, event)
            return api

    async def send_text(self, text, actor=101, state=None, state_data=None, preserve_state=False):
        storage = self.bot.dp.fsm.get_context(bot=self.bot.bot, chat_id=actor, user_id=actor)
        if not preserve_state:
            await storage.clear()
        if state:
            await storage.set_state(state)
            await storage.update_data(**(state_data or {}))
        for middleware in self.bot.dp.message.middleware:
            if hasattr(middleware, 'user_timestamps'):
                middleware.user_timestamps.clear()
        event = Update(update_id=123, message=Message(
            message_id=123, date=datetime.now(timezone.utc), text=text,
            from_user=User(id=actor, is_bot=False, first_name='Test'), chat=Chat(id=actor, type='private')))
        await self.bot.dp.feed_update(self.bot.bot, event)
        return storage

    async def test_nonadmins_cannot_invoke_admin_buttons_or_fsm(self):
        cases = [('hide_2', None), ('resu_1', None), ('setc_2_active', None),
                 ('rmp_1', self.bot.DeleteParticipantStates.choose_round),
                 ('smart_open', self.bot.SmartSetCurrentStates.choose_action),
                 ('keep_yes', self.bot.TransferStandardStates.choose_keep_votes),
                 ('trans_new', self.bot.TransferStandardStates.choose_target),
                 ('sr_auto', self.bot.StartRoundStates.enter_number)]
        for i, (data, state) in enumerate(cases):
            with self.subTest(data=data):
                api = await self.dispatch(data, state=state, message_id=i+1)
                api.assert_not_awaited()

    async def test_moderator_can_show_but_not_resurrect(self):
        api = await self.dispatch('setc_2_active', actor=202)
        api.assert_awaited_once()
        api = await self.dispatch('resu_1', actor=202, message_id=2)
        api.assert_not_awaited()

    async def test_full_admin_can_resurrect(self):
        api = await self.dispatch('resu_1', actor=101)
        api.assert_awaited_once()

    async def test_no_active_round_and_backend_failure_differ(self):
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock,
                          side_effect=self.bot.api.APIError('none', status=404, code='no_active_round')):
            text, kb = await self.bot.get_vote_menu(303)
            self.assertEqual(text, self.bot.lexicon.MSG_NO_ROUND)
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock,
                          side_effect=self.bot.api.APIError('backend', status=500)):
            text, kb = await self.bot.get_vote_menu(303)
            self.assertIn('временно недоступен', text)
            self.assertNotEqual(text, self.bot.lexicon.MSG_NO_ROUND)

    async def test_delivery_failure_does_not_resubmit_vote(self):
        callback = AsyncMock()
        callback.data = 'vote_1_2'
        callback.from_user.id = 303
        callback.answer.side_effect = RuntimeError('delivery failed')
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={'vote_id': 4}) as api:
            with self.assertLogs('bot', level='ERROR') as logs:
                await self.bot.process_vote_callback(callback)
            api.assert_awaited_once()
            self.assertIn('after_vote_commit', logs.output[0])

    async def test_api_passes_trusted_actor_and_correlation(self):
        response = AsyncMock(status=200, headers={})
        response.json.return_value = {'ok': True}
        session = AsyncMock()
        # aiohttp session.request returns an async context manager, not a coroutine.
        from unittest.mock import MagicMock
        session.request = MagicMock()
        session.request.return_value.__aenter__ = AsyncMock(return_value=response)
        session.request.return_value.__aexit__ = AsyncMock(return_value=False)
        token = context.set({'actor_id': 303, 'request_id': 'test-request'})
        try:
            with patch.object(self.bot.api, 'session', session):
                await self.bot.api.request('GET', 'rounds/active_info/?user_id=303')
            headers = session.request.call_args.kwargs['headers']
            self.assertEqual(headers['X-Telegram-User-ID'], '303')
            self.assertEqual(headers['X-Request-ID'], 'test-request')
        finally:
            context.reset(token)

    async def test_private_denial_has_no_vote_buttons_but_published_results_remain_visible(self):
        data = {'status': 'active', 'round_type': 'standard', 'can_vote': False, 'participants': [], 'round_name': 'Event'}
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value=data):
            text, keyboard = await self.bot.get_vote_menu(303)
            self.assertIn('приватное', text)
            self.assertIsNone(keyboard)
            data['status'] = 'published'
            text, keyboard = await self.bot.get_vote_menu(303)
            self.assertIn('Результаты', text)

    async def test_old_vote_callback_shows_access_denied(self):
        callback = AsyncMock()
        callback.data = 'vote_1_2'
        callback.from_user.id = 303
        error = self.bot.api.APIError('denied', status=403, code='voter_not_allowed')
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, side_effect=error) as api:
            await self.bot.process_vote_callback(callback)
            api.assert_awaited_once()
            callback.answer.assert_awaited_once_with(self.bot.VOTER_NOT_ALLOWED, show_alert=True)
            callback.message.edit_text.assert_not_awaited()

    async def test_access_callbacks_and_input_require_full_admin(self):
        for actor in (202, 303):
            for data in ('ca_open_1', 'ca_mode_1_private', 'ca_add_1', 'ca_remove_1', 'ca_list_1'):
                with self.subTest(actor=actor, data=data):
                    api = await self.dispatch(data, actor=actor, private=True)
                    api.assert_not_awaited()
            with patch.object(self.bot.api, 'request', new_callable=AsyncMock) as api:
                with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                    await self.send_text('/campaign_access', actor=actor)
                    await self.send_text('303 404', actor=actor, state=self.bot.CampaignAccessStates.enter_ids,
                                         state_data={'campaign_id': 1, 'operation': 'add'})
                api.assert_not_awaited()

    async def test_full_admin_mode_and_add_ids_flow(self):
        campaign = {'id': 1, 'name': 'Event', 'order_number': 1, 'access_mode': 'private', 'allowed_telegram_ids': [303, 404]}
        api = await self.dispatch('ca_mode_1_private', actor=101, private=True, api_result=campaign)
        api.assert_awaited_once_with('PATCH', 'campaigns/1/access', {'access_mode': 'private'})
        # The button must really set FSM state; subsequent input uses that state.
        await self.dispatch('ca_add_1', actor=101, private=True)
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value=campaign) as api:
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                storage = await self.send_text('404\n303, 404', preserve_state=True)
            api.assert_awaited_once_with('PATCH', 'campaigns/1/access', {'operation': 'add', 'telegram_ids': [303, 404]})
            self.assertIsNone(await storage.get_state())

    async def test_bad_input_keeps_state_and_does_not_submit_partial_list(self):
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock) as api:
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                storage = await self.send_text('303\n@name', state=self.bot.CampaignAccessStates.enter_ids,
                                               state_data={'campaign_id': 1, 'operation': 'add'})
            api.assert_not_awaited()
            self.assertEqual(await storage.get_state(), self.bot.CampaignAccessStates.enter_ids.state)

    async def test_full_admin_command_and_list_download(self):
        campaign = {'id': 1, 'name': '<Event>', 'order_number': 1, 'access_mode': 'private', 'allowed_telegram_ids': [303]}
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value=[campaign]) as api:
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True) as telegram:
                await self.send_text('/campaign_access')
            api.assert_awaited_once_with('GET', 'campaigns')
            keyboard = telegram.call_args.args[1].reply_markup
            self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'ca_open_1')
        api = await self.dispatch('ca_list_1', actor=101, private=True, api_result=campaign)
        api.assert_awaited_once_with('GET', 'campaigns/1')

    async def test_standard_and_final_menus_show_remaining_votes(self):
        people = [{'id': i, 'full_name': f'Person {i}', 'order_number': i, 'votes': 0} for i in range(1, 9)]
        data = {'round_id': 1, 'status': 'active', 'round_type': 'standard', 'max_choices': 3,
                'participants': people, 'user_votes': [{'participant_id': 1}, {'participant_id': 2}]}
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value=data):
            text, kb = await self.bot.get_vote_menu(303)
            self.assertIn('Осталось голосов: 1', text)
            self.assertEqual(len(kb.inline_keyboard), 6)
            data['user_votes'].append({'participant_id': 3})
            text, kb = await self.bot.get_vote_menu(303)
            self.assertIsNone(kb)
            self.assertIn('Все ваши голоса учтены', text)
            data.update(max_choices=1, user_votes=[])
            text, kb = await self.bot.get_vote_menu(303)
            self.assertIn('не более 1', text)

    async def test_old_callback_reports_limit(self):
        callback = AsyncMock()
        callback.data = 'vote_1_2'
        callback.from_user.id = 303
        exc = self.bot.api.APIError('Можно выбрать не более 3 участников.', status=400, code='vote_limit_reached')
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, side_effect=exc):
            await self.bot.process_vote_callback(callback)
        callback.answer.assert_awaited_once_with(str(exc), show_alert=True)

    async def test_event_roles_guard_every_callback_and_input(self):
        for actor in (202, 303):
            for value in ('ev_prepare_1', 'ev_jury_1', 'ev_pick_1_2', 'ev_clearjury_1', 'ev_name_1', 'ev_tie_1', 'evt_2', 'ev_tie_save'):
                api = await self.dispatch(value, actor=actor, state=self.bot.EventStates.tie)
                api.assert_not_awaited()
            for state in (self.bot.EventStates.names, self.bot.EventStates.jury_name):
                with patch.object(self.bot.api, 'request', new_callable=AsyncMock) as api:
                    with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                        await self.send_text('Фамилия Имя', actor=actor, state=state,
                                             state_data={'campaign_id': 1, 'round_id': 1})
                api.assert_not_awaited()
        for value in ('ev_camp_1', 'ev_round_1', 'ev_start_1', 'ev_end_1'):
            api = await self.dispatch(value, actor=303)
            api.assert_not_awaited()

    async def test_event_preparation_fsm_sends_whole_roster(self):
        await self.dispatch('ev_prepare_1', actor=101, private=True)
        roster = '\n'.join(f'Фамилия{i} Имя' for i in range(25))
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock,
                          side_effect=[{}, {'name': 'Event', 'rounds': []}]) as api:
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                storage = await self.send_text(roster, preserve_state=True)
        api.assert_any_await('POST', 'campaigns/1/prepare_event', {'names': roster.splitlines()})
        self.assertIsNone(await storage.get_state())

    async def test_event_moderator_start_and_finish_and_full_jury(self):
        module = importlib.import_module('event_menu')
        with patch.object(module, 'show_round', new_callable=AsyncMock):
            for action, endpoint in (('start', 'event_start'), ('end', 'event_finish')):
                api = await self.dispatch(f'ev_{action}_1', actor=202, api_result={'message': 'OK'})
                api.assert_awaited_once_with('POST', f'rounds/1/{endpoint}', {})
            api = await self.dispatch('ev_pick_1_7', actor=101, api_result={'message': 'OK'})
            api.assert_awaited_once_with('POST', 'rounds/1/jury', {'participant_id': 7})

    async def test_event_new_finalist_name_fsm(self):
        await self.dispatch('ev_name_1', actor=101, private=True)
        module = importlib.import_module('event_menu')
        with patch.object(module, 'show_round', new_callable=AsyncMock):
            with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={'message': 'OK'}) as api:
                with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                    storage = await self.send_text('Фамилия Имя', preserve_state=True)
            api.assert_awaited_once_with('POST', 'rounds/1/jury', {'full_name': 'Фамилия Имя'})
            self.assertIsNone(await storage.get_state())

    async def test_empty_campaign_offers_tour_types_and_legacy_rounds_are_visible(self):
        module = importlib.import_module('event_menu')
        message = AsyncMock()
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock,
                          return_value={'name': 'Event', 'rounds': []}):
            await module.show_campaign(message, 1, True)
        buttons = message.answer.call_args.kwargs['reply_markup'].inline_keyboard
        callbacks = {button.callback_data for row in buttons for button in row}
        self.assertIn('ev_new_1', callbacks)
        self.assertNotIn('ev_create_individual_1', callbacks)
        legacy = {'id': 5, 'name': 'Раунд: Иванов Иван', 'stage': '', 'completed': False, 'status': 'active'}
        message.reset_mock()
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock,
                          return_value={'name': 'Event', 'rounds': [legacy]}):
            await module.show_campaign(message, 1, True)
        buttons = message.answer.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, 'ev_new_1')
        self.assertEqual(buttons[1][0].callback_data, 'ev_round_5')
        self.assertIn('Иванов Иван', buttons[1][0].text)

    async def test_create_tour_and_add_one_person_fsm(self):
        module = importlib.import_module('event_menu')
        await self.dispatch('ev_create_individual_1', actor=101, private=True)
        with patch.object(module, 'show_round', new_callable=AsyncMock):
            with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={'tour_id': 8}) as api:
                with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                    storage = await self.send_text('5', preserve_state=True)
            api.assert_awaited_once_with('POST', 'campaigns/1/create_tour', {'mode': 'individual', 'winners_count': 5})
            self.assertIsNone(await storage.get_state())
        await self.dispatch('ev_add_8', actor=101, private=True)
        with patch.object(module, 'show_round', new_callable=AsyncMock):
            with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={'message': 'OK'}) as api:
                with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                    await self.send_text('Иванов Иван', preserve_state=True)
            api.assert_awaited_once_with('POST', 'rounds/8/add_entries', {'names': ['Иванов Иван']})

    async def test_new_tour_controls_require_full_administrator(self):
        for actor in (202, 303):
            for value in ('ev_new', 'ev_new_1', 'ev_create_individual_1', 'ev_create_standard_1', 'ev_add_1', 'ev_count_1'):
                api = await self.dispatch(value, actor=actor, private=True)
                api.assert_not_awaited()
            for state in (self.bot.EventStates.create_count, self.bot.EventStates.add_names, self.bot.EventStates.winners_count):
                with patch.object(self.bot.api, 'request', new_callable=AsyncMock) as api:
                    with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                        await self.send_text('3', actor=actor, state=state,
                                             state_data={'campaign_id': 1, 'mode': 'individual', 'round_id': 1})
                api.assert_not_awaited()

    async def test_create_button_visible_during_unfinished_tour_and_type_selection(self):
        module = importlib.import_module('event_menu')
        message = AsyncMock()
        rd = {'id': 5, 'name': 'Тур 1', 'stage': 'qualification', 'completed': False, 'status': 'published'}
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={'name': 'Event', 'rounds': [rd]}):
            await module.show_campaign(message, 1, True)
        self.assertEqual(message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data, 'ev_new_1')
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={'campaigns': [{'id': 1, 'name': 'Event'}]}):
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True) as telegram:
                await self.send_text('/event')
            buttons = telegram.call_args.args[1].reply_markup.inline_keyboard
            self.assertEqual([b.callback_data for row in buttons for b in row], ['ev_camp_1'])
        handler = next(h.callback for h in self.bot.dp.callback_query.handlers if h.callback.__name__ == 'choose_tour_type')
        call, state = AsyncMock(), AsyncMock()
        call.data = 'ev_new_1'
        await handler(call, state)
        buttons = call.message.answer.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([row[0].callback_data for row in buttons[:2]], ['ev_create_standard_1', 'ev_create_individual_1'])

    async def test_export_chooses_one_campaign_and_sends_archive(self):
        import base64
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={
                'campaigns': [{'id': 7, 'name': 'Мероприятие', 'is_active': True}]} ) as api:
            with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True) as telegram:
                await self.send_text('/export')
            api.assert_awaited_once_with('GET', 'campaigns/report_choices', is_admin=True)
            self.assertEqual(telegram.call_args.args[1].reply_markup.inline_keyboard[0][0].callback_data, 'report_campaign_7')
        call = AsyncMock()
        call.data = 'report_campaign_7'
        call.message.chat.type = 'private'
        with patch.object(self.bot.api, 'request', new_callable=AsyncMock, return_value={
                'filename': 'report.zip', 'content_base64': base64.b64encode(b'archive bytes').decode()}) as api:
            await self.bot.export_campaign(call)
        api.assert_awaited_once_with('GET', 'campaigns/7/report', is_admin=True)
        self.assertEqual(call.message.answer_document.call_args.args[0].data, b'archive bytes')
        api = await self.dispatch('report_campaign_7', actor=101, private=False)
        api.assert_not_awaited()
        for actor in (202, 303):
            api = await self.dispatch('report_campaign_7', actor=actor, private=True)
            api.assert_not_awaited()
            with patch.object(self.bot.api, 'request', new_callable=AsyncMock) as api:
                with patch.object(self.bot.bot.session, 'make_request', new_callable=AsyncMock, return_value=True):
                    await self.send_text('/export', actor=actor)
            api.assert_not_awaited()
