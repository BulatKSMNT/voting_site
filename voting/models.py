from django.db import models
from django.db.models import Max

class Campaign(models.Model):
    """Кампания голосования"""
    name = models.CharField(max_length=120, verbose_name="Название кампании")
    admin_telegram_id = models.BigIntegerField(verbose_name="Telegram ID админа")
    order_number = models.PositiveIntegerField(
        verbose_name="Порядковый номер кампании", unique=True, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Кампания"
        verbose_name_plural = "Кампании"
        ordering = ['order_number']

    def save(self, *args, **kwargs):
        if not self.order_number:
            max_num = Campaign.objects.aggregate(max_num=Max('order_number'))['max_num'] or 0
            self.order_number = max_num + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"#{self.order_number} {self.name}"


class Round(models.Model):
    """Раунд голосования"""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="rounds")
    number = models.PositiveSmallIntegerField(verbose_name="Номер раунда")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён в")
    status = models.CharField(
        max_length=20,
        choices=[("pending", "Ожидание"), ("active", "Активен"), ("ended", "Завершён")],
        default="pending"
    )
    winners_count = models.PositiveSmallIntegerField(default=3, verbose_name="Сколько призовых мест")
    is_current = models.BooleanField(default=False, verbose_name="Текущий раунд для голосования")

    class Meta:
        verbose_name = "Раунд"
        verbose_name_plural = "Раунды"
        unique_together = ["campaign", "number"]
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.campaign} — раунд {self.number}"

    def save(self, *args, **kwargs):
        # При создании нового текущего раунда снимаем флаг с предыдущего
        if self.is_current:
            Round.objects.filter(is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)

class Participant(models.Model):
    """Участник раунда"""
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="participants")
    full_name = models.CharField(max_length=200, verbose_name="ФИО")
    description = models.TextField(blank=True, verbose_name="Описание (опционально)")
    order_number = models.PositiveIntegerField(
        verbose_name="Порядковый номер участника в раунде", editable=False
    )

    class Meta:
        verbose_name = "Участник"
        verbose_name_plural = "Участники"
        ordering = ['order_number', 'full_name']

    def save(self, *args, **kwargs):
        if not self.order_number:
            max_num = Participant.objects.filter(round=self.round).aggregate(
                max_num=Max('order_number')
            )['max_num'] or 0
            self.order_number = max_num + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"#{self.order_number} {self.full_name}"


class Vote(models.Model):
    """Голос пользователя"""
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="votes")
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE)
    user_telegram_id = models.BigIntegerField(verbose_name="Telegram ID проголосовавшего")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Голос"
        verbose_name_plural = "Голоса"
        unique_together = ["round", "user_telegram_id"]

    def __str__(self):
        return f"{self.user_telegram_id} → {self.participant.full_name}"
