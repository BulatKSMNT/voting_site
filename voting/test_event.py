from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from .audit import results
from .models import Campaign, Round, Participant, Vote, AuditEvent, RoundResultSnapshot
from .serializers import VoteCreateSerializer
from . import tournament


@override_settings(DJANGO_API_TOKEN='a' * 40, FULL_ADMIN_IDS={101}, LIMITED_ADMIN_IDS={202},
                   REST_FRAMEWORK={
                       'DEFAULT_AUTHENTICATION_CLASSES': ['rest_framework.authentication.TokenAuthentication'],
                       'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
                       'EXCEPTION_HANDLER': 'voting.audit.api_exception_handler',
                   })
class EventTests(TestCase):
    def setUp(self):
        cache.clear()
        service = get_user_model().objects.create_user('bot')
        Token.objects.create(user=service, key='a' * 40)
        self.api = APIClient()
        self.campaign = Campaign.objects.create(name='Event', admin_telegram_id=101)

    def actor(self, uid=101):
        self.api.credentials(HTTP_AUTHORIZATION='Token ' + 'a' * 40, HTTP_X_TELEGRAM_USER_ID=str(uid))

    def action(self, rd, name, data=None, uid=101, expected=200):
        self.actor(uid)
        response = self.api.post(f'/voting/api/rounds/{rd.pk}/{name}/', data or {}, format='json')
        self.assertEqual(response.status_code, expected, response.data)
        return response.data

    def vote(self, p, uid, choice=None, expected=201):
        self.actor(uid)
        payload = {'round': p.round_id, 'participant': p.pk, 'user_telegram_id': uid}
        if choice:
            payload['choice'] = choice
        response = self.api.post('/voting/api/votes/', payload, format='json')
        self.assertEqual(response.status_code, expected, response.data)
        return response.data

    def board(self, rd=None):
        url = '/voting/results/?ajax=1' + (f'&round_id={rd.pk}' if rd else '')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        return data, data['left_column'] + data['right_column']

    def semifinal(self, scores=(8, 6, 5, 4, 3, 2, 1, 0)):
        rd = Round.objects.create(campaign=self.campaign, type='standard', event_stage='semifinal', status='active', winners_count=2)
        for i, count in enumerate(scores):
            p = Participant.objects.create(round=rd, full_name=f'Фамилия{i} Имя')
            Vote.objects.bulk_create([Vote(round=rd, participant=p, user_telegram_id=10000 + i * 100 + j) for j in range(count)])
        return rd

    def test_full_25_performers_to_eight_to_two_plus_one_to_winner(self):
        self.campaign.access_mode = 'private'
        self.campaign.allowed_telegram_ids = list(range(1000, 1026)) + [2000]
        self.campaign.save()
        names = [f'Фамилия{i:02d} Имя' for i in range(25)]
        self.actor()
        prepared = self.api.post(f'/voting/api/campaigns/{self.campaign.pk}/prepare_event/', {'names': names}, format='json')
        self.assertEqual(prepared.status_code, 200, prepared.data)
        qualification = Round.objects.get(pk=prepared.data['qualification_id'])
        performances = list(qualification.previous_rounds.order_by('number'))
        self.assertEqual(len(performances), 25)
        self.action(qualification, 'event_finish', expected=400)
        for i, rd in enumerate(performances):
            self.action(rd, 'event_start')
            p = rd.participants.get()
            for uid in range(1000, 1025 - i):
                self.vote(p, uid, choice='yes')
            self.vote(p, 2000, choice='no')
            self.vote(p, 999, choice='yes', expected=403)
            # Site shows the aggregate top eight while the bot offers the current performance.
            display, rows = self.board()
            self.assertEqual(display['round_id'], qualification.pk)
            self.assertEqual(len(rows), 8)
            self.actor(1000)
            self.assertEqual(self.api.get('/voting/api/rounds/active_info/').data['round_id'], rd.pk)
            finished = self.action(rd, 'event_finish')
            self.assertEqual(self.action(rd, 'event_finish'), finished)
            self.vote(p, 1025, choice='yes', expected=400)
        summary = results(qualification)
        self.assertEqual(summary['total_votes'], sum(range(1, 26)) + 25)
        self.assertEqual([p['yes'] for p in summary['participants']], list(range(25, 0, -1)))
        self.assertEqual([p['no'] for p in summary['participants']], [1] * 25)
        response = self.action(qualification, 'event_finish')
        semi = Round.objects.get(pk=response['next_round_id'])
        self.assertEqual((semi.event_stage, semi.status, semi.participants.count(), semi.votes.count()), ('semifinal', 'pending', 8, 0))
        self.assertEqual(list(semi.participants.values_list('full_name', flat=True)), names[:8])
        self.assertEqual(self.action(qualification, 'event_finish'), response)
        self.action(semi, 'event_start')
        people = list(semi.participants.all())
        for p in people[:3]:
            self.vote(p, 1000)
        self.assertEqual(self.vote(people[3], 1000, expected=400)['code'], 'vote_limit_reached')
        self.vote(people[0], 1001)
        self.vote(people[1], 1001)
        self.vote(people[0], 1002)
        self.assertEqual(len(self.board()[1]), 8)
        self.action(semi, 'jury', {'participant_id': people[7].pk})
        response = self.action(semi, 'event_finish')
        final = Round.objects.get(pk=response['next_round_id'])
        self.assertEqual((final.participants.count(), final.votes.count(), final.max_choices), (3, 0, 1))
        self.assertEqual(list(final.participants.values_list('full_name', flat=True)), [names[0], names[1], names[7]])
        # The site retains all eight in the closed second tour, while the final waits to start.
        self.assertEqual(len(self.board(semi)[1]), 8)
        original_snapshot = RoundResultSnapshot.objects.filter(round_id=semi.pk).first()
        self.action(semi, 'jury', {'full_name': 'Новый Финалист'})
        self.assertEqual(final.participants.count(), 3)
        self.assertTrue(final.participants.filter(full_name='Новый Финалист', is_admin_choice=True).exists())
        self.assertEqual(semi.votes.count(), 6)
        original_snapshot.refresh_from_db()
        self.assertEqual(original_snapshot.data['jury_choice_id'], people[7].pk)
        self.action(final, 'event_start')
        finalists = list(final.participants.all())
        self.vote(finalists[2], 1000)
        self.assertEqual(self.vote(finalists[0], 1000, expected=400)['code'], 'vote_limit_reached')
        self.vote(finalists[2], 1001)
        self.vote(finalists[0], 1002)
        self.action(semi, 'jury', {'participant_id': people[6].pk}, expected=400)
        outcome = self.action(final, 'event_finish')
        self.assertIsNone(outcome['next_round_id'])
        self.assertEqual([w['name'] for w in outcome['winners']], ['Новый Финалист'])
        self.assertEqual(len([r for r in self.board(final)[1] if r['winner']]), 1)
        self.action(final, 'event_start', expected=400)
        self.assertTrue(AuditEvent.objects.filter(event='round.jury_selected', actor_id=101).exists())
        self.assertTrue(AuditEvent.objects.filter(event='vote.rejected', reason_code='vote_limit_reached').exists())

    def test_general_round_limit_three_duplicate_and_independent_round(self):
        rd = Round.objects.create(campaign=self.campaign, status='active')
        people = [Participant.objects.create(round=rd, full_name=str(i)) for i in range(4)]
        for p in people[:3]:
            self.vote(p, 303)
        self.assertEqual(self.vote(people[3], 303, expected=400)['code'], 'vote_limit_reached')
        self.assertEqual(self.vote(people[0], 303, expected=400)['code'], 'duplicate')
        self.vote(people[3], 404)
        other = Round.objects.create(campaign=self.campaign, status='active')
        self.vote(Participant.objects.create(round=other, full_name='Other'), 303)

    def test_quota_is_rechecked_after_validation(self):
        rd = Round.objects.create(campaign=self.campaign, status='active')
        people = [Participant.objects.create(round=rd, full_name=str(i)) for i in range(4)]
        delayed = VoteCreateSerializer(data={'round': rd.pk, 'participant': people[3].pk, 'user_telegram_id': 303})
        delayed.is_valid(raise_exception=True)
        for p in people[:3]:
            self.vote(p, 303)
        with self.assertRaises(ValidationError):
            delayed.save()
        self.assertEqual(rd.votes.count(), 3)

    def test_tied_second_place_closes_voting_and_only_admin_can_resolve(self):
        rd = self.semifinal((9, 5, 5, 1, 0, 0, 0, 0))
        people = list(rd.participants.all())
        response = self.action(rd, 'event_finish', uid=202)
        self.assertTrue(response['needs_tiebreak'])
        self.assertEqual(response['tie_slots'], 1)
        self.assertEqual({p['id'] for p in response['tie_candidates']}, {people[1].pk, people[2].pk})
        self.vote(people[2], 303, expected=400)
        self.action(rd, 'resolve_tie', {'participant_ids': [people[2].pk]}, uid=202, expected=403)
        self.action(rd, 'resolve_tie', {'participant_ids': [people[3].pk]}, expected=400)
        self.action(rd, 'resolve_tie', {'participant_ids': [people[1].pk, people[2].pk]}, expected=400)
        result = self.action(rd, 'resolve_tie', {'participant_ids': [people[2].pk]})
        final = Round.objects.get(pk=result['next_round_id'])
        self.assertEqual(set(final.participants.values_list('source_participant_id', flat=True)), {people[0].pk, people[2].pk})
        self.assertEqual(final.votes.count(), 0)
        self.assertTrue(AuditEvent.objects.filter(event='round.tie_resolved', actor_id=101).exists())

    def test_all_zero_first_tour_requires_exact_eight_from_twenty_five(self):
        prepared = tournament.prepare_event(self.campaign.pk, [f'Name {i}' for i in range(25)])
        rd = Round.objects.get(pk=prepared['qualification_id'])
        for performance in rd.previous_rounds.all():
            tournament.start_round(performance.pk)
            tournament.finish_round(performance.pk)
        response = self.action(rd, 'event_finish')
        self.assertEqual((response['tie_slots'], len(response['tie_candidates'])), (8, 25))
        chosen = list(rd.participants.order_by('-pk').values_list('pk', flat=True)[:8])
        result = self.action(rd, 'resolve_tie', {'participant_ids': chosen})
        semi = Round.objects.get(pk=result['next_round_id'])
        self.assertEqual(set(semi.participants.values_list('source_participant_id', flat=True)), set(chosen))
        self.assertEqual({p['participant_full_name'] for p in self.board(rd)[1]}, set(semi.participants.values_list('full_name', flat=True)))

    def test_final_tie_selects_one_of_equal_leaders(self):
        rd = Round.objects.create(campaign=self.campaign, type='standard', event_stage='final', status='active', max_choices=1, winners_count=1)
        people = [Participant.objects.create(round=rd, full_name=str(i)) for i in range(3)]
        self.vote(people[0], 303)
        self.vote(people[1], 404)
        response = self.action(rd, 'event_finish')
        self.assertEqual(response['tie_slots'], 1)
        result = self.action(rd, 'resolve_tie', {'participant_ids': [people[1].pk]})
        self.assertEqual([w['id'] for w in result['winners']], [people[1].pk])
        self.assertIsNone(result['next_round_id'])

    def test_only_one_extra_finalist_and_selection_does_not_add_votes(self):
        rd = self.semifinal()
        people = list(rd.participants.all())
        before = list(rd.votes.values())
        response = self.action(rd, 'event_finish')
        final = Round.objects.get(pk=response['next_round_id'])
        self.action(rd, 'jury', {'participant_id': people[6].pk})
        self.action(rd, 'jury', {'participant_id': people[7].pk})
        self.assertEqual(final.participants.count(), 3)
        self.assertFalse(final.participants.filter(source_participant_id=people[6].pk).exists())
        self.action(rd, 'jury', {'participant_id': people[0].pk}, expected=400)
        self.action(rd, 'jury', {'full_name': people[0].full_name}, expected=400)
        self.action(rd, 'jury', {'full_name': 'JustOneName'}, expected=400)
        self.action(rd, 'jury', {'participant_id': None})
        self.assertEqual(final.participants.count(), 2)
        self.assertEqual(list(rd.votes.values()), before)

    def test_general_round_supports_independent_jury_winner(self):
        rd = Round.objects.create(campaign=self.campaign, status='active', winners_count=1)
        winner = Participant.objects.create(round=rd, full_name='Top')
        extra = Participant.objects.create(round=rd, full_name='Jury')
        self.vote(winner, 303)
        self.action(rd, 'jury', {'participant_id': extra.pk})
        response = self.action(rd, 'end_and_transfer', {'action_type': 'end_standard'})
        self.assertEqual({w['id'] for w in response['winners']}, {winner.pk, extra.pk})
        self.assertEqual(rd.votes.count(), 1)

    def test_preparation_idempotency_and_invalid_roster(self):
        names = [f'Name {i}' for i in range(25)]
        a = tournament.prepare_event(self.campaign.pk, names)
        b = tournament.prepare_event(self.campaign.pk, names)
        self.assertEqual(a, b)
        self.assertEqual(self.campaign.rounds.count(), 26)
        for bad in (['same'] * 25, ['short'], names[:-1], [None] * 25):
            with self.assertRaises(ValidationError):
                tournament.prepare_event(self.campaign.pk, bad)

    def test_nonadmins_and_moderators_cannot_choose_extra_or_prepare(self):
        rd = self.semifinal()
        for uid in (202, 303):
            self.action(rd, 'jury', {'participant_id': rd.participants.first().pk}, uid=uid, expected=403)
            self.actor(uid)
            self.assertEqual(self.api.post(f'/voting/api/campaigns/{self.campaign.pk}/prepare_event/', {'names': ['n'] * 25}, format='json').status_code, 403)
        self.action(rd, 'event_finish', uid=303, expected=403)

    def test_generic_end_routes_to_automatic_transfer_and_manual_mutations_rejected(self):
        rd = self.semifinal()
        result = self.action(rd, 'end_and_transfer', {'action_type': 'end_standard'})
        self.assertIsNotNone(result['next_round_id'])
        self.action(rd, 'end_and_transfer', {'action_type': 'transfer_standard'}, expected=400)
        self.actor()
        for data in ({'status': 'active'}, {'winners_count': 8}, {'max_choices': 99}):
            self.assertEqual(self.api.patch(f'/voting/api/rounds/{rd.pk}/', data).status_code, 400)
        self.assertEqual(self.api.delete(f'/voting/api/rounds/{rd.pk}/').status_code, 400)
        self.assertEqual(self.api.post('/voting/api/participants/', {'round': result['next_round_id'], 'full_name': 'Bypass'}).status_code, 400)

    def test_admin_event_pages_and_permission_checks(self):
        user = get_user_model().objects.create_superuser('admin', password='test')
        self.client.force_login(user)
        url = reverse('admin:voting_campaign_event', args=[self.campaign.pk])
        names = '\n'.join(f'Name {i}' for i in range(25))
        self.assertEqual(self.client.post(url, {'names': names}).status_code, 302)
        self.assertContains(self.client.get(url), 'Name 0')
        rd = self.campaign.rounds.get(event_stage='performance', number=2)
        url = reverse('admin:voting_round_event', args=[rd.pk])
        self.assertEqual(self.client.post(url, {'action': 'start'}).status_code, 302)
        rd.refresh_from_db()
        self.assertEqual(rd.status, 'active')
        self.assertContains(self.client.get(url), 'Name 0')
        staff = get_user_model().objects.create_user('restricted', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_qualification_reports_use_original_votes_and_roster_cannot_be_moved_out(self):
        import csv
        import io
        prepared = tournament.prepare_event(self.campaign.pk, [f'Name {i}' for i in range(8)])
        board = Round.objects.get(pk=prepared['qualification_id'])
        performance = board.previous_rounds.order_by('number').first()
        tournament.start_round(performance.pk)
        person = performance.participants.get()
        self.vote(person, 303, choice='yes')
        self.vote(person, 404, choice='no')
        tournament.finish_round(performance.pk)
        user = get_user_model().objects.create_superuser('admin', password='test')
        self.client.force_login(user)
        response = self.client.get(reverse('admin:voting_round_report', args=[board.pk]))
        self.assertEqual(response.context['summary']['total_votes'], 2)
        self.assertEqual(response.context['votes'].paginator.count, 2)
        self.actor()
        exported = self.api.get('/voting/api/rounds/export_csv/')
        rows = list(csv.reader(io.StringIO(exported.data['csv_content']), delimiter=';'))
        row = next(r for r in rows[1:] if r[1] == f'№{board.number}' and r[5] == person.full_name)
        self.assertEqual(row[6], '1')
        target = Round.objects.create(campaign=self.campaign, status='published')
        unvoted = board.participants.last()
        response = self.api.patch(f'/voting/api/participants/{unvoted.pk}/', {'round': target.pk})
        self.assertEqual(response.status_code, 400)

    def test_tour_can_be_built_incrementally_and_rounds_selected_by_name(self):
        self.actor()
        response = self.api.post(f'/voting/api/campaigns/{self.campaign.pk}/create_tour/',
                                 {'mode': 'individual', 'winners_count': 2}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        tour = Round.objects.get(pk=response.data['tour_id'])
        self.action(tour, 'event_finish', expected=400)
        self.action(tour, 'add_entries', {'names': ['Петров Пётр']})
        self.action(tour, 'add_entries', {'names': ['Иванов Иван', 'Сидоров Семён']})
        self.action(tour, 'add_entries', {'names': ['Петров Пётр']}, expected=400)
        rounds = list(tour.performance_rounds)
        self.assertEqual([r.participants.get().full_name for r in rounds], ['Петров Пётр', 'Иванов Иван', 'Сидоров Семён'])
        # The administrator chooses the last entry first, independently of creation order.
        for index, rd in enumerate(reversed(rounds)):
            self.action(rd, 'event_start')
            self.actor(303)
            active = self.api.get('/voting/api/rounds/active_info/').data
            self.assertEqual(active['round_id'], rd.pk)
            self.assertIn(rd.participants.get().full_name, active['round_name'])
            for uid in range(1000, 1003-index):
                self.vote(rd.participants.get(), uid, choice='yes')
            self.vote(rd.participants.get(), 2000, choice='no')
            self.action(rd, 'event_finish')
            # Closing one performance does not yet create the next tour.
            self.assertFalse(self.campaign.rounds.filter(event_stage='semifinal').exists())
        self.assertEqual(len(self.board(tour)[1]), 2)
        result = self.action(tour, 'event_finish')
        target = Round.objects.get(pk=result['next_round_id'])
        self.assertEqual(list(target.participants.values_list('full_name', flat=True)), ['Сидоров Семён', 'Иванов Иван'])
        self.assertEqual(target.votes.count(), 0)
        self.assertEqual(target.max_choices, 3)
        self.assertEqual(self.action(tour, 'event_finish'), result)
        self.action(tour, 'add_entries', {'names': ['Поздний Участник']}, expected=400)

    def test_top_n_changes_before_closing_and_managed_mutations_remain_guarded(self):
        prepared = tournament.create_tour(self.campaign.pk, 'individual', 8, ['Фамилия Имя'])
        tour = Round.objects.get(pk=prepared['tour_id'])
        self.actor()
        url = f'/voting/api/rounds/{tour.pk}/update_winners_count/'
        self.assertEqual(self.api.patch(url, {'winners_count': 1}, format='json').status_code, 200)
        tour.refresh_from_db()
        self.assertEqual(tour.winners_count, 1)
        self.assertIn('топ-1', self.board(tour)[0]['stage_label'])
        for bad in (0, -1, 101, True, 1.5, 'one'):
            self.assertEqual(self.api.patch(url, {'winners_count': bad}, format='json').status_code, 400)
        for uid in (202, 303):
            self.actor(uid)
            self.assertEqual(self.api.patch(url, {'winners_count': 2}, format='json').status_code, 403)
            self.assertEqual(self.api.post(f'/voting/api/rounds/{tour.pk}/add_entries/', {'names': ['Другой Участник']}, format='json').status_code, 403)
            self.assertEqual(self.api.post(f'/voting/api/campaigns/{self.campaign.pk}/create_tour/', {'mode': 'standard'}, format='json').status_code, 403)
        performance = tour.performance_rounds.get()
        tournament.start_round(performance.pk)
        tournament.finish_round(performance.pk)
        tournament.finish_round(tour.pk)
        self.actor()
        self.assertEqual(self.api.patch(url, {'winners_count': 2}, format='json').status_code, 400)

    def test_event_menu_includes_existing_legacy_rounds_and_preserves_votes(self):
        rd = Round.objects.create(campaign=self.campaign, type='individual', status='active')
        person = Participant.objects.create(round=rd, full_name='Существующий Участник')
        self.vote(person, 303, choice='yes')
        self.actor()
        data = self.api.get(f'/voting/api/campaigns/{self.campaign.pk}/event/').data
        self.assertEqual(data['rounds'][0]['id'], rd.pk)
        self.assertIn(person.full_name, data['rounds'][0]['name'])
        self.assertIsNone(data['rounds'][0]['parent_tour'])
        self.assertEqual(rd.votes.count(), 1)
        self.action(rd, 'event_finish')
        self.assertEqual(rd.votes.count(), 1)
        self.assertEqual(self.campaign.rounds.filter(type='standard').get().votes.count(), 1)

    def test_admin_creates_empty_tour_adds_names_and_configures_top_n(self):
        user = get_user_model().objects.create_superuser('tour-admin', password='test')
        self.client.force_login(user)
        url = reverse('admin:voting_campaign_event', args=[self.campaign.pk])
        self.assertEqual(self.client.post(url, {'mode': 'individual', 'winners_count': 2}).status_code, 302)
        tour = self.campaign.rounds.get(event_stage='qualification')
        url = reverse('admin:voting_round_event', args=[tour.pk])
        self.assertEqual(self.client.post(url, {'action': 'add', 'names': 'Первый Участник\nВторой Участник\nТретий Участник'}).status_code, 302)
        self.assertEqual(tour.performance_rounds.count(), 3)
        self.assertContains(self.client.get(url), 'Первый Участник')
        self.assertEqual(self.client.post(url, {'action': 'configure', 'winners_count': 1}).status_code, 302)
        tour.refresh_from_db()
        self.assertEqual(tour.winners_count, 1)
