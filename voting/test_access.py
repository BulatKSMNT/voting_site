from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings, RequestFactory
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from .admin import CampaignAdmin
from .forms import CampaignAdminForm
from .models import Campaign, Round, Participant, Vote, AuditEvent, RoundResultSnapshot
from .serializers import VoteCreateSerializer
from .services import end_and_transfer


@override_settings(DJANGO_API_TOKEN='a' * 40, FULL_ADMIN_IDS={101}, LIMITED_ADMIN_IDS={202})
class CampaignAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        service = get_user_model().objects.create_user('bot')
        Token.objects.create(user=service, key='a' * 40)
        self.api = APIClient()
        self.campaign = Campaign.objects.create(name='Private event', admin_telegram_id=101)
        self.rd = Round.objects.create(campaign=self.campaign, number=1, status='active')
        self.p = Participant.objects.create(round=self.rd, full_name='Candidate')
        self.url = f'/voting/api/campaigns/{self.campaign.pk}/'

    def actor(self, uid):
        self.api.credentials(HTTP_AUTHORIZATION='Token ' + 'a' * 40, HTTP_X_TELEGRAM_USER_ID=str(uid))

    def configure(self, ids=(), mode='private'):
        self.campaign.access_mode = mode
        self.campaign.allowed_telegram_ids = list(ids)
        self.campaign.save()

    def vote(self, uid=303, rd=None, participant=None, **extra):
        self.actor(uid)
        return self.api.post('/voting/api/votes/', {
            'round': (rd or self.rd).pk, 'participant': (participant or self.p).pk,
            'user_telegram_id': uid, **extra,
        }, format='json')

    def test_public_is_default_and_accepts_any_voter(self):
        self.assertEqual(self.campaign.access_mode, 'public')
        self.assertEqual(self.vote().status_code, 201)
        self.assertEqual(self.vote(404).status_code, 201)

    def test_self_access_works_without_open_round_and_never_exposes_allowlist(self):
        self.configure([303])
        self.rd.status = 'pending'
        self.rd.save()
        self.actor(303)
        before_events = AuditEvent.objects.count()
        response = self.api.get('/voting/api/campaigns/my_access/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before_events)
        self.assertEqual(response.data, {'campaigns': [
            {'id': self.campaign.pk, 'name': self.campaign.name, 'can_vote': True}]})
        self.actor(404)
        response = self.api.get('/voting/api/campaigns/my_access/?user_id=303&user_telegram_id=303')
        self.assertFalse(response.data['campaigns'][0]['can_vote'])
        self.configure([], mode='public')
        self.assertTrue(self.api.get('/voting/api/campaigns/my_access/').data['campaigns'][0]['can_vote'])

    def test_self_access_is_campaign_specific_and_admins_have_no_vote_bypass(self):
        self.configure([303])
        other = Campaign.objects.create(name='Other', admin_telegram_id=101, access_mode='private')
        self.actor(303)
        url = '/voting/api/campaigns/my_access/'
        self.assertEqual(len(self.api.get(url).data['campaigns']), 2)
        response = self.api.get(url, {'campaign_id': other.pk})
        self.assertEqual(response.data['campaigns'], [{'id': other.pk, 'name': 'Other', 'can_vote': False}])
        for actor in (101, 202):
            self.actor(actor)
            self.assertFalse(self.api.get(url, {'campaign_id': self.campaign.pk}).data['campaigns'][0]['can_vote'])
        self.campaign.is_active = False
        self.campaign.save()
        self.assertEqual(self.api.get(url, {'campaign_id': self.campaign.pk}).status_code, 404)
        for invalid in ('bad', '-1', str(2**63)):
            self.assertEqual(self.api.get(url, {'campaign_id': invalid}).status_code, 400)
        other.is_active = False
        other.save()
        self.assertEqual(self.api.get(url).data, {'campaigns': []})

    def test_self_access_requires_authenticated_bot_identity(self):
        url = '/voting/api/campaigns/my_access/'
        self.api.credentials()
        self.assertEqual(self.api.get(url).status_code, 401)
        self.api.credentials(HTTP_AUTHORIZATION='Token '+'a'*40)
        self.assertEqual(self.api.get(url).status_code, 403)
        user = get_user_model().objects.create_user('not_bot', is_staff=True)
        self.api.force_authenticate(user)
        self.assertEqual(self.api.get(url).status_code, 403)

    def test_private_accepts_only_listed_ids_and_audits_rejections(self):
        self.configure([303])
        self.assertEqual(self.vote().status_code, 201)
        denied = self.vote(404)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data['code'], 'voter_not_allowed')
        event = AuditEvent.objects.get(event='vote.rejected')
        self.assertEqual((event.actor_id, event.campaign_id, event.round_id), (404, self.campaign.pk, self.rd.pk))
        self.assertEqual(event.reason_code, 'voter_not_allowed')
        self.assertEqual(Vote.objects.count(), 1)

    def test_empty_private_list_includes_no_admin_bypass(self):
        self.configure()
        for uid in (101, 202, 303):
            self.assertEqual(self.vote(uid).status_code, 403)
        self.configure([101])
        self.assertEqual(self.vote(101).status_code, 201)

    def test_individual_choices_still_work_for_listed_voters(self):
        self.configure([303, 404])
        self.rd.type = 'individual'
        self.rd.save()
        self.assertEqual(self.vote(choice='yes').status_code, 201)
        self.assertEqual(self.vote(404, choice='no').status_code, 201)
        self.assertEqual(self.vote(505, choice='yes').status_code, 403)
        self.assertEqual(list(Vote.objects.order_by('pk').values_list('choice', flat=True)), ['yes', 'no'])

    def test_access_is_shared_by_campaign_rounds_but_not_other_campaigns(self):
        self.configure([303])
        rd2 = Round.objects.create(campaign=self.campaign, number=2, status='active')
        p2 = Participant.objects.create(round=rd2, full_name='Second')
        other = Campaign.objects.create(name='Other event', admin_telegram_id=101, access_mode='private')
        rd3 = Round.objects.create(campaign=other, number=1, status='active')
        p3 = Participant.objects.create(round=rd3, full_name='Other')
        self.assertEqual(self.vote().status_code, 201)
        self.assertEqual(self.vote(rd=rd2, participant=p2).status_code, 201)
        self.assertEqual(self.vote(rd=rd3, participant=p3).status_code, 403)

    def test_revocation_keeps_existing_votes_results_and_transfers(self):
        self.configure([303])
        self.assertEqual(self.vote().status_code, 201)
        original = Vote.objects.get()
        self.configure([])
        second = Participant.objects.create(round=self.rd, full_name='Second')
        self.assertEqual(self.vote(participant=second).status_code, 403)
        self.assertTrue(Vote.objects.filter(pk=original.pk).exists())
        end_and_transfer(self.rd.pk, {'action_type': 'end_standard'})
        snapshot = RoundResultSnapshot.objects.get(round_id=self.rd.pk)
        self.assertEqual(snapshot.data['total_votes'], 1)
        target = Round.objects.create(campaign=self.campaign, number=2, status='published')
        end_and_transfer(self.rd.pk, {'action_type': 'transfer_standard', 'target_round_id': target.pk,
                                     'winners_ids': [self.p.pk], 'keep_votes': True})
        copied = target.votes.get()
        self.assertEqual((copied.user_telegram_id, copied.source_vote_id), (303, original.pk))

    def test_access_is_rechecked_after_initial_validation(self):
        self.configure([303])
        serializer = VoteCreateSerializer(data={'round': self.rd.pk, 'participant': self.p.pk, 'user_telegram_id': 303})
        serializer.is_valid(raise_exception=True)
        self.configure([])
        with self.assertRaises(PermissionDenied):
            serializer.save()
        self.assertFalse(Vote.objects.exists())

    def test_api_can_add_remove_and_switch_without_losing_saved_list(self):
        self.actor(101)
        response = self.api.patch(self.url + 'access/', {'operation': 'add', 'telegram_ids': ['404', 303, 303]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['allowed_telegram_ids'], [303, 404])
        self.api.patch(self.url + 'access/', {'access_mode': 'private'}, format='json')
        self.api.patch(self.url + 'access/', {'operation': 'remove', 'telegram_ids': [303, 999]}, format='json')
        self.api.patch(self.url + 'access/', {'access_mode': 'public'}, format='json')
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.allowed_telegram_ids, [404])
        self.assertEqual(self.vote().status_code, 201)
        updates = AuditEvent.objects.filter(event='campaign.updated', campaign_id=self.campaign.pk)
        self.assertEqual(updates.count(), 4)
        self.assertEqual(updates.first().actor_id, 101)

    def test_bad_ids_and_unknown_modes_are_rejected_atomically(self):
        self.actor(101)
        self.configure([303])
        for bad in ([404, '@user'], [True], [1.5], [-1], [0], [2**63], '303,404', None):
            with self.subTest(bad=bad):
                response = self.api.patch(self.url + 'access/', {
                    'access_mode': 'public', 'operation': 'add', 'telegram_ids': bad,
                }, format='json')
                self.assertEqual(response.status_code, 400)
        for data in ({}, {'operation': 'add'}, {'telegram_ids': [404]}, {'operation': 'remove', 'telegram_ids': []}, {'access_mode': 'typo'}):
            self.assertEqual(self.api.patch(self.url + 'access/', data, format='json').status_code, 400)
        self.campaign.refresh_from_db()
        self.assertEqual((self.campaign.access_mode, self.campaign.allowed_telegram_ids), ('private', [303]))

    def test_direct_campaign_create_and_patch_validate_ids(self):
        self.actor(101)
        created = self.api.post('/voting/api/campaigns/', {'name': 'New', 'admin_telegram_id': 101,
                                'access_mode': 'private', 'allowed_telegram_ids': ['404', 303, 404]}, format='json')
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['allowed_telegram_ids'], [303, 404])
        bad = self.api.patch(self.url, {'allowed_telegram_ids': ['nope']}, format='json')
        self.assertEqual(bad.status_code, 400)
        updated = self.api.patch(self.url, {'access_mode': 'private', 'allowed_telegram_ids': []}, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.vote().status_code, 403)

    def test_non_full_admins_cannot_read_or_edit_access_settings(self):
        for uid in (202, 303):
            self.actor(uid)
            self.assertEqual(self.api.get(self.url).status_code, 403)
            self.assertEqual(self.api.get('/voting/api/campaigns/').status_code, 403)
            self.assertEqual(self.api.patch(self.url, {'access_mode': 'private'}).status_code, 403)
            self.assertEqual(self.api.patch(self.url + 'access/', {'access_mode': 'private'}).status_code, 403)
        user = get_user_model().objects.create_user('ordinary')
        token = Token.objects.create(user=user)
        self.api.credentials(HTTP_AUTHORIZATION='Token ' + token.key, HTTP_X_TELEGRAM_USER_ID='101')
        self.assertEqual(self.api.patch(self.url + 'access/', {'access_mode': 'private'}).status_code, 403)

    def test_public_info_never_contains_allowlist_or_trusts_query_user_id(self):
        self.configure([303])
        self.rd.is_current = True
        self.rd.save()
        url = '/voting/api/rounds/active_info/?user_id=303'
        public = self.api.get(url)
        self.assertFalse(public.data['can_vote'])
        self.assertNotIn('allowed_telegram_ids', public.data)
        self.assertNotIn('allowed_telegram_ids', self.api.get('/voting/api/campaigns/active_list/').data['campaigns'][0])
        self.actor(404)
        self.assertFalse(self.api.get(url).data['can_vote'])
        self.actor(303)
        self.assertTrue(self.api.get(url).data['can_vote'])

    def test_admin_textarea_normalizes_and_reports_invalid_ids(self):
        data = {'name': self.campaign.name, 'admin_telegram_id': 101, 'access_mode': 'private',
                'is_active': True, 'allowed_telegram_ids': '303\n404, 303; 505'}
        form = CampaignAdminForm(data, instance=self.campaign)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['allowed_telegram_ids'], [303, 404, 505])
        form.save()
        self.assertEqual(CampaignAdminForm(instance=self.campaign).initial['allowed_telegram_ids'], '303\n404\n505')
        bad = CampaignAdminForm({**data, 'allowed_telegram_ids': '303\n@invalid'}, instance=self.campaign)
        self.assertFalse(bad.is_valid())
        self.assertIn('allowed_telegram_ids', bad.errors)

    def test_admin_editing_name_preserves_concurrent_allowlist_change(self):
        self.configure([303])
        form = CampaignAdminForm({'name': 'Renamed', 'admin_telegram_id': 101, 'is_active': True,
                                  'access_mode': 'private', 'allowed_telegram_ids': '303'}, instance=self.campaign)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        fresh = Campaign.objects.get(pk=self.campaign.pk)
        fresh.allowed_telegram_ids = [303, 404]
        fresh.save()
        request = RequestFactory().post('/')
        request.user = get_user_model().objects.create_superuser('admin', password='test')
        CampaignAdmin(Campaign, admin.site).save_model(request, obj, form, True)
        fresh.refresh_from_db()
        self.assertEqual((fresh.name, fresh.allowed_telegram_ids), ('Renamed', [303, 404]))
