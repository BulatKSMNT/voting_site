from django.db.models.signals import pre_save, post_save, pre_delete
from django.dispatch import receiver
from .models import Campaign, Round, Participant, Vote
from .audit import record, snapshot, state, vote_event

MODELS = (Campaign, Round, Participant, Vote)


@receiver(pre_save)
def remember_previous(sender, instance, **kwargs):
    if sender in MODELS:
        old = sender.objects.filter(pk=instance.pk).first() if instance.pk else None
        instance._audit_before = state(old) if old else None


def object_event(instance, action, details):
    if isinstance(instance, Campaign):
        return record(f"campaign.{action}", campaign_id=instance.pk, details=details)
    rd = instance if isinstance(instance, Round) else instance.round
    return record(f"{instance._meta.model_name}.{action}", round_obj=rd,
                  participant_id=instance.pk if isinstance(instance, Participant) else getattr(instance, "participant_id", None),
                  vote_id=instance.pk if isinstance(instance, Vote) else None, details=details)


@receiver(post_save)
def saved(sender, instance, created, raw=False, **kwargs):
    if sender not in MODELS or raw:
        return
    before = getattr(instance, "_audit_before", None)
    if isinstance(instance, Vote) and created:
        vote_event(instance)
        return
    instance = sender.objects.get(pk=instance.pk)
    after = state(instance)
    if before == after:
        return
    object_event(instance, "created" if created else "updated", {"before": before, "after": after})
    if isinstance(instance, Round) and before and (
        (before["status"] == "active" and instance.status in {"ended", "published"})
        or (instance.ended_at and instance.ended_at != before["ended_at"])
    ):
        snap = snapshot(instance)
        record("round.closed", round_obj=instance, details={"snapshot_id": snap.pk, "version": snap.version})


@receiver(pre_delete)
def deleting(sender, instance, **kwargs):
    if sender in MODELS:
        object_event(instance, "deleted", {"before": state(instance)})
