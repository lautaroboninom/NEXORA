from django.core.management.base import BaseCommand

from service.bejerman_sync import run_bejerman_sync_loop


class Command(BaseCommand):
    help = "Ejecuta el worker continuo de sincronizacion con Bejerman"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=30)
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **opts):
        self.stdout.write(
            f"Iniciando worker Bejerman interval={opts['interval']}s limit={opts['limit']}"
        )
        run_bejerman_sync_loop(interval_seconds=opts["interval"], limit=opts["limit"])
