from django.db import models
from django.db.models import Max
from django.db import transaction
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MinValueValidator, MaxValueValidator
from .access import normalize_telegram_ids


class AuditedModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        # Signals and the write commit or roll back together.
        with transaction.atomic():
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if isinstance(self, Campaign):
                Campaign.objects.select_for_update().get(pk=self.pk)
                rounds = Round.objects.filter(campaign=self)
            elif isinstance(self, Round):
                rounds = Round.objects.filter(pk=self.pk)
            else:
                rounds = Round.objects.filter(pk=self.round_id)
            list(rounds.select_for_update().order_by('pk'))
            return super().delete(*args, **kwargs)


class Campaign(AuditedModel):
    ACCESS_MODES = [("public", "Общедоступный"), ("private", "Приватный (по списку Telegram ID)")]
    name = models.CharField(max_length=120, verbose_name="Название кампании")
    admin_telegram_id = models.BigIntegerField(verbose_name="Telegram ID админа")
    order_number = models.PositiveIntegerField(verbose_name="Номер", unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    access_mode = models.CharField(max_length=7, choices=ACCESS_MODES, default="public", verbose_name="Доступ к голосованию")
    allowed_telegram_ids = models.JSONField(default=list, blank=True, verbose_name="Допущенные Telegram ID")

    class Meta:
        ordering = ['order_number']

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                Campaign.objects.select_for_update().get(pk=self.pk)
            self.allowed_telegram_ids = normalize_telegram_ids(self.allowed_telegram_ids)
            if not self.order_number:
                max_num = Campaign.objects.aggregate(max_num=Max('order_number'))['max_num'] or 0
                self.order_number = max_num + 1
            super().save(*args, **kwargs)

    def can_vote(self, telegram_id):
        return self.access_mode == "public" or telegram_id in self.allowed_telegram_ids

    def __str__(self):
        return f"Кампания #{self.order_number}: {self.name}"


class Round(AuditedModel):
    EVENT_STAGES = [("", "Обычный раунд"), ("performance", "Выступление: да/нет"),
                    ("qualification", "Первый тур: топ-8"), ("semifinal", "Второй тур: топ-2"), ("final", "Финал")]
    ROUND_TYPES = [("standard", "Стандартный"), ("individual", "Индивидуальный")]
    STATUS_CHOICES = [
        ("pending", "Ожидание"),
        ("active", "Активен"),
        ("ended", "Завершён (Скрыт)"),
        ("published", "Опубликован (Результаты открыты)")
    ]

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="rounds")
    number = models.PositiveSmallIntegerField(verbose_name="Номер раунда")
    type = models.CharField(max_length=20, choices=ROUND_TYPES, default="standard")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    winners_count = models.PositiveSmallIntegerField(default=3)
    is_current = models.BooleanField(default=False)
    cycle = models.PositiveIntegerField(default=1, editable=False)
    max_choices = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(3)], verbose_name="Максимум выбранных участников")
    event_stage = models.CharField(max_length=16, choices=EVENT_STAGES, blank=True, default="", editable=False)
    next_round = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='previous_rounds', editable=False)
    event_completed = models.BooleanField(default=False, editable=False)
    tie_break_ids = models.JSONField(default=list, blank=True, editable=False)
    jury_choice = models.ForeignKey('Participant', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+', verbose_name="Дополнительный победитель по выбору администратора")
    jury_name = models.CharField(max_length=200, blank=True, verbose_name="Дополнительный финалист: фамилия имя")

    class Meta:
        unique_together = ["campaign", "number"]
        ordering = ['-started_at']
        indexes = [models.Index(fields=['status', 'is_current'])]

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                previous = Round.objects.select_for_update().get(pk=self.pk)
                self.cycle = previous.cycle
                if previous.status != "active" and self.status == "active" and (kwargs.get("update_fields") is None or "status" in kwargs["update_fields"]):
                    self.cycle += 1
                    if kwargs.get("update_fields") is not None:
                        kwargs["update_fields"] = set(kwargs["update_fields"]) | {"cycle"}
            if not self.number:
                max_num = Round.objects.filter(campaign=self.campaign).aggregate(m=Max('number'))['m'] or 0
                self.number = max_num + 1
            if self.pk is None:
                self.is_current = False

            if self.is_current and (kwargs.get("update_fields") is None or "is_current" in kwargs["update_fields"]):
                from .audit import record
                others = list(Round.objects.filter(is_current=True).exclude(pk=self.pk))
                Round.objects.filter(pk__in=[rd.pk for rd in others]).update(is_current=False)
                for rd in others:
                    record("round.display_changed", round_obj=rd,
                           details={"before": {"is_current": True}, "after": {"is_current": False}})
            super().save(*args, **kwargs)

    def __str__(self):
        return f"Раунд №{self.number} ({self.campaign.name})"

class Participant(AuditedModel):
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="participants")
    full_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    source_participant_id = models.BigIntegerField(null=True, blank=True, editable=False)
    transfer_key = models.CharField(max_length=64, blank=True, editable=False)
    order_number = models.PositiveIntegerField(editable=False)
    is_admin_choice = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ['order_number', 'full_name']

    def save(self, *args, **kwargs):
        with transaction.atomic():
            old_round = Participant.objects.filter(pk=self.pk).values_list('round_id', flat=True).first() if self.pk else None
            ids = {self.round_id} | ({old_round} if old_round else set())
            list(Round.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
            if not self.order_number:
                max_num = Participant.objects.filter(round=self.round).aggregate(m=Max('order_number'))['m'] or 0
                self.order_number = max_num + 1
            super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name

class Vote(AuditedModel):
    VOTE_CHOICES = [("yes", "Да"), ("no", "Нет")]
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="votes")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE)
    user_telegram_id = models.BigIntegerField()
    choice = models.CharField(max_length=3, choices=VOTE_CHOICES, null=True, blank=True)
    source_vote_id = models.BigIntegerField(null=True, blank=True, editable=False)
    source_created_at = models.DateTimeField(null=True, blank=True, editable=False)
    transfer_key = models.CharField(max_length=64, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            old_round = Vote.objects.filter(pk=self.pk).values_list('round_id', flat=True).first() if self.pk else None
            ids = {self.round_id} | ({old_round} if old_round else set())
            list(Round.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
            return super().save(*args, **kwargs)

    class Meta:
        unique_together = ["round", "user_telegram_id", "participant"]
        indexes = [models.Index(fields=['round', 'user_telegram_id'])]

    def __str__(self):
        return f"Голос от {self.user_telegram_id} за {self.participant.full_name}"

class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event = models.CharField(max_length=80, db_index=True)
    source = models.CharField(max_length=20)
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_role = models.CharField(max_length=20, blank=True)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    campaign_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    round_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    participant_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    vote_id = models.BigIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=20, default="success")
    reason_code = models.CharField(max_length=80, blank=True)
    details = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    class Meta:
        ordering = ['-id']
        verbose_name = "событие голосования"
        verbose_name_plural = "Журнал голосований"


class RoundResultSnapshot(models.Model):
    # Scalar IDs deliberately survive deletion of the original campaign/round.
    campaign_id = models.BigIntegerField(db_index=True)
    round_id = models.BigIntegerField(db_index=True)
    version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField(encoder=DjangoJSONEncoder)

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['round_id', 'version'], name='unique_round_snapshot_version')]
        verbose_name = "снимок результатов"
        verbose_name_plural = "Снимки результатов"


class RoundOperation(models.Model):
    key = models.CharField(max_length=64, unique=True)
    round_id = models.BigIntegerField(db_index=True)
    cycle = models.PositiveIntegerField()
    action = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
    response = models.JSONField(encoder=DjangoJSONEncoder)

    class Meta:
        ordering = ['-id']
        verbose_name = "операция раунда"
        verbose_name_plural = "Операции раундов"
