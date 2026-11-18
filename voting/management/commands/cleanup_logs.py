from pathlib import Path
from decouple import config
from django.core.management.base import BaseCommand, CommandError
from core.observability import prune_logs


class Command(BaseCommand):
    help = 'Удалить старые журналы завершённых процессов выбранного сервиса. Запускать внутри его контейнера.'

    def add_arguments(self, parser):
        parser.add_argument('--traffic-days', type=int, default=7)
        parser.add_argument('--app-days', type=int, default=30)
        parser.add_argument('--service', choices=['web', 'bot'], default='web')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        from django.conf import settings
        if options['traffic_days'] < 1 or options['app_days'] < 1:
            raise CommandError('Срок хранения должен быть не меньше суток.')
        root = Path(config('LOG_DIR', default=str(settings.BASE_DIR / 'logs')))
        removed = prune_logs(root, options['service'], options['traffic_days'], options['app_days'], options['dry_run'])
        for name in removed:
            self.stdout.write(name)
        self.stdout.write(f'Файлов: {len(removed)}')
