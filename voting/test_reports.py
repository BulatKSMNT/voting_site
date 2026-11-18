import base64
import csv
import io
import json
import tempfile
from pathlib import Path
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from . import tournament
from .audit import snapshot, results
from .models import Campaign, Round, Participant, Vote, RoundResultSnapshot
from .reports import campaign_archive, recount, consistent_read


@override_settings(DJANGO_API_TOKEN='a'*40, FULL_ADMIN_IDS={101}, LIMITED_ADMIN_IDS={202})
class ReportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.campaign = Campaign.objects.create(name='Мероприятие', admin_telegram_id=101)
        self.rd = Round.objects.create(campaign=self.campaign, status='active')
        self.p = Participant.objects.create(round=self.rd, full_name='Иванов Иван')
        Vote.objects.create(round=self.rd, participant=self.p, user_telegram_id=303)
        self.user = get_user_model().objects.create_superuser('report_admin', password='test')
        self.client.force_login(self.user)
        self.api = APIClient()
        service = get_user_model().objects.create_user('report_bot')
        Token.objects.create(user=service, key='a'*40)
        self.actor(101)

    def actor(self, uid):
        self.api.credentials(HTTP_AUTHORIZATION='Token '+'a'*40, HTTP_X_TELEGRAM_USER_ID=str(uid))

    def unpack(self, content):
        archive = ZipFile(io.BytesIO(content))
        data = json.loads(archive.read('Данные.json'))
        votes = list(csv.DictReader(io.StringIO(archive.read('Голоса.csv').decode('utf-8-sig')), delimiter=';'))
        totals = list(csv.DictReader(io.StringIO(archive.read('Результаты.csv').decode('utf-8-sig')), delimiter=';'))
        return data, votes, totals

    def test_campaign_report_is_scoped_and_can_recount_without_database(self):
        other = Campaign.objects.create(name='Не включать', admin_telegram_id=101)
        rd = Round.objects.create(campaign=other)
        person = Participant.objects.create(round=rd, full_name='Другой участник')
        Vote.objects.create(round=rd, participant=person, user_telegram_id=999)
        response = self.api.get(f'/voting/api/campaigns/{self.campaign.pk}/report/')
        self.assertEqual(response.status_code, 200)
        data, votes, totals = self.unpack(base64.b64decode(response.data['content_base64']))
        self.assertEqual([v['Telegram ID'] for v in votes], ['303'])
        self.assertEqual(votes[0]['Выбор'], 'За участника')
        self.assertEqual(len(totals), 1)
        self.assertEqual(len(data['rounds']), 1)
        with self.assertNumQueries(0):
            self.assertEqual(recount(data)[0][-2:], [1, 0])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            output = io.StringIO()
            with self.assertNumQueries(0):
                call_command('recount_report', str(path), stdout=output)
            self.assertIn('Иванов Иван', output.getvalue())
        data['rounds'][0]['votes'] = []
        with self.assertRaises(ValueError):
            recount(data)

    def test_qualification_votes_and_totals_not_duplicated_in_csv(self):
        prepared = tournament.create_tour(self.campaign.pk, 'individual', 2, ['Петров Пётр', 'Сидоров Семён'])
        board = Round.objects.get(pk=prepared['tour_id'])
        performance = board.performance_rounds.first()
        person = performance.participants.get()
        Vote.objects.create(round=performance, participant=person, user_telegram_id=404, choice='yes')
        Vote.objects.create(round=performance, participant=person, user_telegram_id=505, choice='no')
        frozen = snapshot(board)
        data, votes, totals = self.unpack(campaign_archive(self.campaign))
        self.assertEqual(len(votes), 3)
        self.assertEqual(len({v['Голос ID'] for v in votes}), 3)
        self.assertEqual(len([r for r in totals if r['Участник'] == person.full_name]), 1)
        self.assertEqual({v['Выбор'] for v in votes}, {'Да', 'Нет', 'За участника'})
        self.assertEqual(frozen.data['total_votes'], len(frozen.data['votes']))
        self.assertTrue(recount(data))
        row = next(r for r in data['rounds'] if r['round_id'] == board.pk)
        self.assertNotEqual(row['votes'][0]['participant_id'], row['votes'][0]['result_participant_id'])

    def test_snapshot_votes_are_immutable_and_export_never_uses_live_votes(self):
        frozen = snapshot(self.rd)
        self.p.full_name = 'Новое имя'
        self.p.save()
        Vote.objects.create(round=self.rd, participant=self.p, user_telegram_id=404)
        url = reverse('admin:voting_round_report', args=[self.rd.pk])
        content = self.client.get(url, {'export': 'votes', 'snapshot': frozen.pk}).content.decode('utf-8-sig')
        self.assertIn('303', content)
        self.assertIn('Иванов Иван', content)
        self.assertNotIn('Новое имя', content)
        self.assertNotIn('404', content)
        response = self.client.get(url, {'snapshot': frozen.pk})
        self.assertEqual(response.context['votes'].paginator.count, 1)
        self.assertContains(response, f'export=votes&amp;snapshot={frozen.pk}')
        content = self.client.get(url, {'export': 'backup', 'snapshot': frozen.pk}).content
        data, votes, totals = self.unpack(content)
        self.assertEqual(len(votes), 1)
        self.assertTrue(recount(data))
        self.rd.delete()
        frozen.refresh_from_db()
        self.assertEqual(frozen.data['votes'][0]['user_telegram_id'], 303)
        self.assertEqual(frozen.data['votes'][0]['participant_name'], 'Иванов Иван')
        data, _, _ = self.unpack(campaign_archive(self.campaign))
        self.assertEqual(len(data['snapshots']), 1)

    def test_old_snapshot_has_explicit_warning_and_no_fabricated_votes(self):
        old = RoundResultSnapshot.objects.create(campaign_id=self.campaign.pk, round_id=self.rd.pk,
                                                version=1, data=results(self.rd))
        url = reverse('admin:voting_round_report', args=[self.rd.pk])
        response = self.client.get(url, {'snapshot': old.pk})
        self.assertContains(response, 'без отдельных голосов')
        self.assertFalse(response.context['votes_available'])
        self.assertEqual(self.client.get(url, {'snapshot': old.pk, 'export': 'votes'}).status_code, 400)
        self.assertEqual(self.client.get(url, {'snapshot': 999999}).status_code, 404)
        self.assertEqual(self.client.get(url, {'snapshot': 'oops'}).status_code, 404)

    def test_exports_require_full_admin_and_archived_campaigns_remain_available(self):
        for actor in (202, 303):
            self.actor(actor)
            for endpoint in ('report_choices', f'{self.campaign.pk}/report'):
                self.assertEqual(self.api.get(f'/voting/api/campaigns/{endpoint}/').status_code, 403)
        self.actor(101)
        self.campaign.is_active = False
        self.campaign.save()
        choices = self.api.get('/voting/api/campaigns/report_choices/').data['campaigns']
        self.assertEqual(choices[0]['id'], self.campaign.pk)
        self.assertFalse(choices[0]['is_active'])
        self.assertEqual(self.api.get(f'/voting/api/campaigns/{self.campaign.pk}/report/').status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:voting_campaign_report', args=[self.campaign.pk])).status_code, 200)
        staff = get_user_model().objects.create_user('staff', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse('admin:voting_campaign_report', args=[self.campaign.pk])).status_code, 403)
        self.api.credentials()
        self.assertEqual(self.api.get(f'/voting/api/campaigns/{self.campaign.pk}/report/').status_code, 401)

    def test_csv_sanitizes_formulas_json_keeps_exact_names_and_zero_scores(self):
        self.p.full_name = '  =1+1'
        self.p.save()
        zero = Participant.objects.create(round=self.rd, full_name='Без голосов')
        data, votes, totals = self.unpack(campaign_archive(self.campaign))
        self.assertEqual(votes[0]['Участник'], "'  =1+1")
        self.assertEqual(data['rounds'][0]['votes'][0]['participant_name'], '  =1+1')
        self.assertEqual(next(r for r in recount(data) if r[2] == zero.pk)[-2:], [0, 0])
