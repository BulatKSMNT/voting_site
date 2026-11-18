import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from voting.reports import csv_cell, recount


class Command(BaseCommand):
    help = 'Пересчитать голоса из Данные.json без записи в базу. Вывод: CSV.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('file', type=Path)

    def handle(self, *args, **options):
        try:
            rows = recount(json.loads(options['file'].read_text(encoding='utf-8')))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError(f'Не удалось проверить отчёт: {exc}') from exc
        writer = csv.writer(self.stdout, delimiter=';')
        writer.writerow(['Раунд ID', 'Тур / раунд', 'Участник ID', 'Участник', 'За', 'Против'])
        for row in rows:
            writer.writerow([csv_cell(cell) for cell in row])
