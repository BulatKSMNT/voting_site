from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.exceptions import ValidationError, PermissionDenied
from .models import Campaign, Round, Participant, Vote, RoundResultSnapshot
from .serializers import VoteCreateSerializer
from .services import end_and_transfer


def isolated(fn):
    try:
        return fn()
    finally:
        connections.close_all()


@skipUnlessDBFeature('has_select_for_update')
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.campaign = Campaign.objects.create(name='Concurrent', admin_telegram_id=101)
        self.rd = Round.objects.create(campaign=self.campaign, number=1, status='active')
        self.participant = Participant.objects.create(round=self.rd, full_name='Candidate')

    def serializer(self):
        serializer = VoteCreateSerializer(data={'round': self.rd.pk, 'participant': self.participant.pk, 'user_telegram_id': 303})
        serializer.is_valid(raise_exception=True)
        return serializer

    def test_close_commits_before_blocked_vote(self):
        validated = Event()
        def voting():
            serializer = self.serializer()
            validated.set()
            try:
                serializer.save()
                return 'accepted'
            except ValidationError:
                return 'closed'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Campaign.objects.select_for_update().get(pk=self.campaign.pk)
                Round.objects.select_for_update().get(pk=self.rd.pk)
                future = pool.submit(isolated, voting)
                self.assertTrue(validated.wait(5))
                end_and_transfer(self.rd.pk, {'action_type': 'end_standard'})
            self.assertEqual(future.result(timeout=10), 'closed')
        self.assertEqual(Vote.objects.count(), 0)
        self.assertEqual(RoundResultSnapshot.objects.get().data['total_votes'], 0)

    def test_revocation_commits_before_blocked_vote(self):
        self.campaign.access_mode = 'private'
        self.campaign.allowed_telegram_ids = [303]
        self.campaign.save()
        validated = Event()

        def voting():
            serializer = self.serializer()
            validated.set()
            try:
                serializer.save()
                return 'accepted'
            except PermissionDenied as exc:
                return exc.get_codes()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                campaign = Campaign.objects.select_for_update().get(pk=self.campaign.pk)
                future = pool.submit(isolated, voting)
                self.assertTrue(validated.wait(5))
                campaign.allowed_telegram_ids = []
                campaign.save(update_fields=['allowed_telegram_ids'])
            self.assertEqual(future.result(timeout=10), 'voter_not_allowed')
        self.assertEqual(Vote.objects.count(), 0)

    def test_simultaneous_duplicate_vote(self):
        barrier = Barrier(2)
        def voting():
            serializer = self.serializer()
            barrier.wait(timeout=5)
            try:
                serializer.save()
                return 'accepted'
            except ValidationError:
                return 'duplicate'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(isolated, voting) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(outcomes, ['accepted', 'duplicate'])
        self.assertEqual(Vote.objects.count(), 1)

    def test_simultaneous_same_transfer(self):
        target = Round.objects.create(campaign=self.campaign, number=2, status='published')
        Vote.objects.create(round=self.rd, participant=self.participant, user_telegram_id=303)
        barrier = Barrier(2)
        payload = {'action_type': 'transfer_standard', 'target_round_id': target.pk, 'winners_ids': [self.participant.pk]}
        def transferring():
            barrier.wait(timeout=5)
            return end_and_transfer(self.rd.pk, payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(isolated, transferring) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(target.participants.count(), 1)
        self.assertEqual(target.votes.count(), 1)

    def test_two_simultaneous_requests_cannot_spend_last_slot_twice(self):
        people = [self.participant] + [Participant.objects.create(round=self.rd, full_name=f'Candidate {i}') for i in range(3)]
        for p in people[:2]:
            Vote.objects.create(round=self.rd, participant=p, user_telegram_id=303)
        barrier = Barrier(2)

        def voting(pid):
            serializer = VoteCreateSerializer(data={'round': self.rd.pk, 'participant': pid, 'user_telegram_id': 303})
            serializer.is_valid(raise_exception=True)
            barrier.wait(timeout=5)
            try:
                serializer.save()
                return 'accepted'
            except ValidationError as exc:
                return exc.get_codes()[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(isolated, lambda pid=p.pk: voting(pid)) for p in people[2:]]
            outcomes = [f.result(timeout=10) for f in futures]
        self.assertCountEqual(outcomes, ['accepted', 'vote_limit_reached'])
        self.assertEqual(self.rd.votes.count(), 3)

    def test_simultaneous_event_finish_creates_one_final(self):
        from .tournament import finish_round
        self.rd.event_stage, self.rd.winners_count = 'semifinal', 2
        self.rd.save()
        second = Participant.objects.create(round=self.rd, full_name='Second')
        Participant.objects.create(round=self.rd, full_name='Third')
        Vote.objects.create(round=self.rd, participant=self.participant, user_telegram_id=303)
        Vote.objects.create(round=self.rd, participant=second, user_telegram_id=303)
        barrier = Barrier(2)

        def finish():
            barrier.wait(timeout=5)
            return finish_round(self.rd.pk)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(isolated, finish) for _ in range(2)]
            responses = [f.result(timeout=10) for f in futures]
        self.assertEqual(responses[0], responses[1])
        final = Round.objects.get(event_stage='final')
        # Scores 1, 1, 0 comprise two levels, so all three pass.
        self.assertEqual(final.participants.count(), 3)
        self.assertEqual(final.votes.count(), 0)

    def test_starting_final_prevents_concurrent_roster_change(self):
        from .tournament import finish_round, start_round, set_jury
        self.rd.event_stage, self.rd.winners_count = 'semifinal', 2
        self.rd.save()
        second = Participant.objects.create(round=self.rd, full_name='Second')
        Participant.objects.create(round=self.rd, full_name='Third')
        for p in (self.participant, second):
            Vote.objects.create(round=self.rd, participant=p, user_telegram_id=303)
        final = Round.objects.get(pk=finish_round(self.rd.pk)['next_round_id'])
        ready = Event()

        def extra():
            ready.set()
            try:
                set_jury(self.rd.pk, full_name='Late Finalist')
                return 'added'
            except ValidationError:
                return 'closed'

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Campaign.objects.select_for_update().get(pk=self.campaign.pk)
                future = pool.submit(isolated, extra)
                self.assertTrue(ready.wait(5))
                start_round(final.pk)
            self.assertEqual(future.result(timeout=10), 'closed')
        # Scores 1, 1, 0 comprise two levels, so all three pass.
        self.assertEqual(final.participants.count(), 3)
