from django.db import models
from django.db.models import Max
from django.db import transaction


class Campaign(models.Model):
    name = models.CharField(max_length=120, verbose_name="Название кампании")
    admin_telegram_id = models.BigIntegerField(verbose_name="Telegram ID админа")
    order_number = models.PositiveIntegerField(verbose_name="Номер", unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order_number']

    def save(self, *args, **kwargs):
        if not self.order_number:
            max_num = Campaign.objects.aggregate(max_num=Max('order_number'))['max_num'] or 0
            self.order_number = max_num + 1
        super().save(*args, **kwargs)


class Round(models.Model):
    ROUND_TYPES = [("standard", "Стандартный"), ("individual", "Индивидуальный")]
    STATUS_CHOICES = [
        ("pending", "Ожидание"),
        ("active", "Активен"),
        ("ended", "Завершён (Скрыт)"),
        ("published", "Опубликован (Результаты открыты)")  # НОВЫЙ СТАТУС
    ]

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="rounds")
    number = models.PositiveSmallIntegerField(verbose_name="Номер раунда")
    type = models.CharField(max_length=20, choices=ROUND_TYPES, default="standard")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    winners_count = models.PositiveSmallIntegerField(default=3)
    is_current = models.BooleanField(default=False)

    class Meta:
        unique_together = ["campaign", "number"]
        ordering = ['-started_at']
        indexes = [models.Index(fields=['status', 'is_current'])]

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if not self.number:
                max_num = Round.objects.filter(campaign=self.campaign).aggregate(m=Max('number'))['m'] or 0
                self.number = max_num + 1
            if self.pk is None:
                self.is_current = False

            if self.is_current:
                Round.objects.filter(is_current=True).exclude(pk=self.pk).update(is_current=False)
            super().save(*args, **kwargs)


class Participant(models.Model):
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="participants")
    full_name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order_number = models.PositiveIntegerField(editable=False)

    class Meta:
        ordering = ['order_number', 'full_name']

    def save(self, *args, **kwargs):
        if not self.order_number:
            max_num = Participant.objects.filter(round=self.round).aggregate(m=Max('order_number'))['m'] or 0
            self.order_number = max_num + 1
        super().save(*args, **kwargs)


class Vote(models.Model):
    VOTE_CHOICES = [("yes", "Да"), ("no", "Нет")]
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="votes")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE)
    user_telegram_id = models.BigIntegerField()
    choice = models.CharField(max_length=3, choices=VOTE_CHOICES, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["round", "user_telegram_id", "participant"]
        indexes = [models.Index(fields=['round', 'user_telegram_id'])]
