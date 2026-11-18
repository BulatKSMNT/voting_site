import csv
import io
import json
import logging
import logging.config
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from core.observability import logging_config, event
from .models import Campaign, Round, Participant, Vote, AuditEvent, RoundResultSnapshot, RoundOperation
from .serializers import VoteCreateSerializer
from .services import end_and_transfer


@override_settings(DJANGO_API_TOKEN='a'*40, FULL_ADMIN_IDS={101}, LIMITED_ADMIN_IDS={202})
class VotingTests(TestCase):
    def setUp(self):
        cache.clear()
        service = get_user_model().objects.create_user('bot')
        Token.objects.create(user=service, key='a'*40)
        self.api = APIClient()
        self.actor(101)
        self.campaign = Campaign.objects.create(name='Campaign', admin_telegram_id=101)
        self.rd = Round.objects.create(campaign=self.campaign, number=1, status='active', winners_count=2)
        self.p = Participant.objects.create(round=self.rd, full_name='First')
        self.target = Round.objects.create(campaign=self.campaign, number=2, status='published')

    def actor(self, uid):
        self.api.credentials(HTTP_AUTHORIZATION='Token '+'a'*40, HTTP_X_TELEGRAM_USER_ID=str(uid))

    def vote(self, user=303, participant=None, rd=None, choice=None):
        self.actor(user)
        payload = {'round': (rd or self.rd).pk, 'participant': (participant or self.p).pk, 'user_telegram_id': user}
        if choice:
            payload['choice'] = choice
        return self.api.post('/voting/api/votes/', payload, format='json')

    def close(self, rd=None, **payload):
        self.actor(101)
        return self.api.post(f'/voting/api/rounds/{(rd or self.rd).pk}/end_and_transfer/', payload or {'action_type': 'end_standard'}, format='json')

    def transfer(self, keep=True):
        return self.close(action_type='transfer_standard', target_round_id=self.target.pk, winners_ids=[self.p.pk], keep_votes=keep)

    def test_multiple_candidates_allowed_but_duplicate_rejected(self):
        second = Participant.objects.create(round=self.rd, full_name='Second')
        self.assertEqual(self.vote().status_code, 201)
        self.assertEqual(self.vote(participant=second).status_code, 201)
        self.assertEqual(self.vote().status_code, 400)
        self.assertEqual(self.rd.votes.count(), 2)

    def test_vote_attribution_and_request_id(self):
        response = self.vote()
        audit = AuditEvent.objects.get(event='vote.accepted')
        self.assertEqual((audit.actor_id, audit.source, audit.round_id), (303, 'bot', self.rd.pk))
        self.assertEqual(audit.request_id, response['X-Request-ID'])
        self.assertEqual(audit.vote_id, response.data['vote_id'])

    def test_spoofed_voter_rejected(self):
        self.actor(303)
        response = self.api.post('/voting/api/votes/', {'round': self.rd.pk, 'participant': self.p.pk, 'user_telegram_id': 404})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Vote.objects.exists())

    def test_participant_from_different_round_rejected(self):
        self.assertEqual(self.vote(rd=self.target).status_code, 400)

    def test_standard_no_vote_rejected(self):
        self.assertEqual(self.vote(choice='no').status_code, 400)

    def test_individual_counts_and_transfer_only_yes(self):
        self.rd.type = 'individual'
        self.rd.save()
        self.assertEqual(self.vote(choice='yes').status_code, 201)
        self.assertEqual(self.vote(user=304, choice='no').status_code, 201)
        response = self.close(action_type='auto_individual', target_round_id=self.target.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.target.votes.count(), 1)
        snap = RoundResultSnapshot.objects.get(round_id=self.rd.pk)
        self.assertEqual((snap.data['participants'][0]['yes'], snap.data['participants'][0]['no']), (1, 1))
        self.assertEqual(snap.data['unique_voters'], 2)

    def test_repeated_standard_transfer_returns_same_result(self):
        self.vote()
        first, second = self.transfer(), self.transfer()
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.data, second.data)
        self.assertEqual(self.target.participants.count(), 1)
        self.assertEqual(self.target.votes.count(), 1)
        self.assertEqual(RoundOperation.objects.count(), 1)

    def test_transfer_keeps_provenance(self):
        self.vote()
        original = Vote.objects.get(round=self.rd)
        self.transfer()
        copied = Vote.objects.get(round=self.target)
        self.assertEqual(copied.source_vote_id, original.pk)
        self.assertEqual(copied.source_created_at, original.created_at)
        self.assertEqual(copied.participant.source_participant_id, self.p.pk)
        self.assertTrue(copied.transfer_key)
        self.assertTrue(AuditEvent.objects.filter(event='vote.transferred', vote_id=copied.pk).exists())

    def test_transfer_without_votes(self):
        self.vote()
        self.assertEqual(self.transfer(keep=False).status_code, 200)
        self.assertEqual(self.target.participants.count(), 1)
        self.assertEqual(self.target.votes.count(), 0)

    def test_same_or_other_campaign_transfer_rejected_without_changes(self):
        camp = Campaign.objects.create(name='Other', admin_telegram_id=101)
        other = Round.objects.create(campaign=camp, number=1, status='active')
        for target in (self.rd, other):
            response = self.close(action_type='transfer_standard', target_round_id=target.pk, winners_ids=[self.p.pk])
            self.assertIn(response.status_code, (400, 404))
        self.assertEqual(Participant.objects.count(), 1)
        self.assertFalse(RoundOperation.objects.exists())

    def test_individual_auto_target_and_repeat(self):
        self.rd.type = 'individual'
        self.rd.save()
        self.target.delete()
        self.vote(choice='yes')
        first = self.close(action_type='auto_individual')
        second = self.close(action_type='auto_individual')
        self.assertEqual(first.data, second.data)
        self.assertEqual(Round.objects.count(), 2)
        self.assertEqual(Vote.objects.count(), 2)

    def test_closing_twice_makes_one_snapshot(self):
        self.vote()
        first, second = self.close(), self.close()
        self.assertEqual(first.data, second.data)
        self.assertEqual(RoundResultSnapshot.objects.count(), 1)

    def test_reopen_makes_new_snapshot_and_keeps_old(self):
        self.vote()
        self.close()
        old = RoundResultSnapshot.objects.get()
        self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'status': 'active'}, format='json')
        self.vote(user=304)
        self.close()
        self.assertEqual(RoundResultSnapshot.objects.count(), 2)
        old.refresh_from_db()
        self.assertEqual(old.data['total_votes'], 1)
        self.assertEqual(RoundResultSnapshot.objects.first().data['total_votes'], 2)

    def test_ties_preserve_existing_boundary_rule(self):
        p2 = Participant.objects.create(round=self.rd, full_name='Tie')
        p3 = Participant.objects.create(round=self.rd, full_name='Third')
        for p, count in ((self.p, 3), (p2, 3), (p3, 2)):
            for i in range(count):
                Vote.objects.create(round=self.rd, participant=p, user_telegram_id=1000+i)
        response = self.close()
        self.assertEqual({w['id'] for w in response.data['winners']}, {self.p.pk, p2.pk})

    def test_tie_at_last_place_includes_all_tied(self):
        p2 = Participant.objects.create(round=self.rd, full_name='Second')
        p3 = Participant.objects.create(round=self.rd, full_name='Third')
        for p, count in ((self.p, 3), (p2, 2), (p3, 2)):
            for i in range(count):
                Vote.objects.create(round=self.rd, participant=p, user_telegram_id=1000+i)
        self.assertEqual(len(self.close().data['winners']), 3)

    def test_late_vote_revalidated_under_lock(self):
        serializer = VoteCreateSerializer(data={'round': self.rd.pk, 'participant': self.p.pk, 'user_telegram_id': 303})
        serializer.is_valid(raise_exception=True)
        self.close()
        with self.assertRaises(ValidationError):
            serializer.save()
        self.assertEqual(Vote.objects.count(), 0)

    def test_snapshot_and_audit_survive_cascade(self):
        self.vote()
        self.close()
        rd_id = self.rd.pk
        self.campaign.delete()
        self.assertFalse(Vote.objects.exists())
        self.assertEqual(RoundResultSnapshot.objects.get(round_id=rd_id).data['total_votes'], 1)
        self.assertTrue(AuditEvent.objects.filter(round_id=rd_id, event='vote.deleted').exists())
        self.assertTrue(AuditEvent.objects.filter(round_id=rd_id, event='vote.accepted').exists())

    def test_audit_rolls_back_with_vote(self):
        before = AuditEvent.objects.count()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Vote.objects.create(round=self.rd, participant=self.p, user_telegram_id=303)
                raise RuntimeError('rollback')
        self.assertFalse(Vote.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_transfer_error_rolls_back_all_records(self):
        self.vote()
        before = AuditEvent.objects.count()
        with patch('voting.services.vote_event', side_effect=RuntimeError('audit storage failed')):
            with self.assertRaises(RuntimeError):
                end_and_transfer(self.rd.pk, {'action_type': 'transfer_standard', 'target_round_id': self.target.pk, 'winners_ids': [self.p.pk]})
        self.assertFalse(self.target.participants.exists())
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_viewer_and_untrusted_token_cannot_manage(self):
        self.actor(303)
        self.assertEqual(self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'status': 'ended'}).status_code, 403)
        user = get_user_model().objects.create_user('ordinary')
        token = Token.objects.create(user=user)
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {token.key}', HTTP_X_TELEGRAM_USER_ID='101')
        self.assertEqual(self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'status': 'ended'}).status_code, 403)

    def test_service_requires_actor_even_for_staff_token(self):
        Token.objects.get(key='a'*40).user.__class__.objects.filter(username='bot').update(is_staff=True)
        self.api.credentials(HTTP_AUTHORIZATION='Token '+'a'*40)
        self.assertEqual(self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'status': 'ended'}).status_code, 403)

    def test_limited_role_keeps_existing_permissions(self):
        self.actor(202)
        self.assertEqual(self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'is_current': True}).status_code, 200)
        self.assertEqual(self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'winners_count': 99}).status_code, 403)
        self.assertEqual(self.api.post('/voting/api/participants/', {'round': self.rd.pk, 'full_name': 'No'}).status_code, 403)
        self.assertEqual(self.api.get('/voting/api/rounds/export_csv/').status_code, 403)
        self.assertEqual(self.api.post(f'/voting/api/rounds/{self.rd.pk}/end_and_transfer/', {'action_type': 'end_standard'}).status_code, 200)

    def test_individual_participant_update_cannot_bypass_limit(self):
        self.rd.type = 'individual'
        self.rd.save()
        second = Participant.objects.create(round=self.target, full_name='Other')
        self.assertEqual(self.api.patch(f'/voting/api/participants/{second.pk}/', {'round': self.rd.pk}).status_code, 400)
        self.assertEqual(self.rd.participants.count(), 1)

    def test_public_campaign_does_not_expose_admin(self):
        data = APIClient().get('/voting/api/campaigns/active_list/').data
        self.assertNotIn('admin_telegram_id', data['campaigns'][0])

    def test_no_round_traffic_does_not_create_audit_rows(self):
        before = AuditEvent.objects.count()
        with self.assertLogs('activity', level='INFO') as captured:
            response = self.api.get('/voting/api/rounds/active_info/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['code'], 'no_active_round')
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertTrue(any(r.fields.get('reason_code') == 'no_active_round' for r in captured.records))

    def test_301_different_users_do_not_share_quota(self):
        for user in range(1000, 1301):
            self.actor(user)
            self.assertEqual(self.api.get('/voting/api/rounds/active_info/').status_code, 404)
        before = AuditEvent.objects.count()
        for _ in range(120):
            self.api.get('/voting/api/rounds/active_info/')
        self.assertEqual(self.api.get('/voting/api/rounds/active_info/').status_code, 429)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_background_read_quota_does_not_block_vote(self):
        self.actor(303)
        for _ in range(121):
            self.api.get('/voting/api/rounds/active_info/')
        self.assertEqual(self.vote().status_code, 201)

    def test_admin_report_export_and_readonly_history(self):
        self.vote(choice='yes')
        self.close()
        admin = get_user_model().objects.create_superuser('admin', password='test')
        self.client.force_login(admin)
        url = reverse('admin:voting_round_report', args=[self.rd.pk])
        response = self.client.get(url)
        self.assertContains(response, 'Снимки при завершении')
        self.assertContains(response, 'First')
        self.assertEqual(self.client.get(url+'?export=votes').status_code, 200)
        self.assertIn('Исходный голос ID', self.client.get(url+'?export=votes').content.decode())
        snap = RoundResultSnapshot.objects.get()
        report = self.client.get(url+f'?export=results&snapshot={snap.pk}')
        self.assertIn('Версия снимка', report.content.decode())
        event_row = AuditEvent.objects.first()
        self.assertEqual(self.client.post(reverse('admin:voting_auditevent_change', args=[event_row.pk]), {'event': 'tamper'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin:voting_auditevent_delete', args=[event_row.pk]), {'post': 'yes'}).status_code, 403)

    def test_admin_change_is_attributed(self):
        admin = get_user_model().objects.create_superuser('admin', password='test')
        self.client.force_login(admin)
        response = self.client.post(reverse('admin:voting_participant_change', args=[self.p.pk]),
                                    {'round': self.rd.pk, 'full_name': 'Changed', 'description': ''})
        self.assertEqual(response.status_code, 302)
        row = AuditEvent.objects.filter(event='participant.updated').first()
        self.assertEqual((row.source, row.actor_id), ('admin', admin.pk))

    def test_report_requires_permissions(self):
        user = get_user_model().objects.create_user('limited_staff', is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('admin:voting_round_report', args=[self.rd.pk])).status_code, 403)

    def test_safe_rendering_and_csv(self):
        self.p.full_name = '<img src=x onerror=alert(1)>'
        self.p.save()
        response = self.client.get(f'/voting/results/?round_id={self.rd.pk}')
        self.assertContains(response, '&lt;img')
        self.assertNotContains(response, '<img src=x')
        self.assertContains(response, 'cell.textContent = value')
        self.assertNotContains(response, 'innerHTML =')


    def test_reclosing_after_new_transferred_votes_refreshes_snapshot(self):
        self.close(rd=self.target)
        self.vote()
        self.transfer()
        response = self.close(rd=self.target)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['winners'][0]['votes'], 1)
        self.assertEqual(RoundResultSnapshot.objects.filter(round_id=self.target.pk).count(), 2)

    def test_closed_rejection_has_specific_reason(self):
        self.close()
        response = self.vote()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'round_closed')
        self.assertTrue(AuditEvent.objects.filter(event='vote.rejected', round_id=self.rd.pk, reason_code='round_closed').exists())

    def test_update_fields_does_not_reopen_stale_round(self):
        stale = Round.objects.get(pk=self.rd.pk)
        self.close()
        stale.winners_count = 4
        stale.save(update_fields=['winners_count'])
        self.rd.refresh_from_db()
        self.assertEqual(self.rd.status, 'published')
        self.assertEqual(self.rd.cycle, 1)
        row = AuditEvent.objects.filter(event='round.updated', round_id=self.rd.pk).first()
        self.assertEqual(row.details['after']['status'], 'published')
        self.assertEqual(RoundResultSnapshot.objects.count(), 1)

    def test_moderator_cannot_reopen_via_intermediate_status(self):
        self.rd.status = 'ended'
        self.rd.save()
        self.actor(202)
        response = self.api.patch(f'/voting/api/rounds/{self.rd.pk}/', {'status': 'published'})
        self.assertEqual(response.status_code, 403)


class LoggingTests(TestCase):
    def test_traffic_is_separate_rotated_and_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LOG_DIR': directory, 'TELEGRAM_TOKEN': 'private-token', 'TRAFFIC_LOG_MAX_BYTES': '500', 'TRAFFIC_LOG_BACKUPS': '2'}):
                logging.config.dictConfig(logging_config('web'))
                try:
                    for _ in range(20):
                        event('no_active_round', traffic=True)
                    event('api.failed', level=logging.ERROR, diagnostic='private-token')
                    main = ''.join(p.read_text() for p in Path(directory).glob('*.jsonl'))
                    traffic = ''.join(p.read_text() for p in (Path(directory)/'traffic').glob('*'))
                    self.assertNotIn('no_active_round', main)
                    self.assertIn('no_active_round', traffic)
                    self.assertNotIn('api.failed', traffic)
                    self.assertNotIn('private-token', main)
                    self.assertIn('[REDACTED]', main)
                    self.assertLessEqual(len(list((Path(directory)/'traffic').glob('*'))), 3)
                    for line in main.splitlines():
                        json.loads(line)
                finally:
                    logging.config.dictConfig(settings.LOGGING)

    def test_pruning_does_not_delete_live_or_other_service_logs(self):
        import time
        from core.observability import prune_logs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'traffic').mkdir()
            stale = root/'traffic'/'web-99999999.jsonl'
            live = root/'traffic'/f'web-{os.getpid()}.jsonl'
            other = root/'traffic'/'bot-99999999.jsonl'
            for file in (stale, live, other):
                file.write_text('{}\n')
                os.utime(file, (time.time()-86400*10,)*2)
            self.assertEqual(prune_logs(root, 'web', dry_run=True), [str(stale)])
            self.assertTrue(stale.exists())
            prune_logs(root, 'web')
            self.assertFalse(stale.exists())
            self.assertTrue(live.exists())
            self.assertTrue(other.exists())
