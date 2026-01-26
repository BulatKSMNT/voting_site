from django.db import models


class Campaign(models.Model):
    """Группа/кампания голосования"""
    name = models.CharField(max_length=120, verbose_name="Название кампании")
    admin_telegram_id = models.BigIntegerField(verbose_name="Telegram ID админа")
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Кампания"
        verbose_name_plural = "Кампании"

    def __str__(self):
        return self.name


class Round(models.Model):
    """Раунд голосования"""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="rounds")
    number = models.PositiveSmallIntegerField(verbose_name="Номер раунда")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершён в")  # только дата завершения, без таймера
    status = models.CharField(
        max_length=20,
        choices=[("pending", "Ожидание"), ("active", "Активен"), ("ended", "Завершён")],
        default="pending"
    )
    winners_count = models.PositiveSmallIntegerField(default=3, verbose_name="Сколько победителей выбрать")

    class Meta:
        verbose_name = "Раунд"
        verbose_name_plural = "Раунды"
        unique_together = ["campaign", "number"]

    def __str__(self):
        return f"{self.campaign} — раунд {self.number}"


class Participant(models.Model):
    """Участник раунда"""
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="participants")
    full_name = models.CharField(max_length=200, verbose_name="ФИО")
    description = models.TextField(blank=True, verbose_name="Описание (опционально)")

    class Meta:
        verbose_name = "Участник"
        verbose_name_plural = "Участники"
        ordering = ["full_name"]

    def save(self, *args, **kwargs):
        if self.full_name:
            self.full_name = ' '.join(
                word.capitalize() if not word.startswith('(') else word
                for word in self.full_name.split()
            ).strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name


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
        return f"{self.user_telegram_id} → {self.participant}"