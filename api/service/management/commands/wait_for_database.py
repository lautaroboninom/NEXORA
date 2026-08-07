import time

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection


class Command(BaseCommand):
    help = "Espera de forma acotada hasta que PostgreSQL acepte consultas."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=180)
        parser.add_argument("--interval", type=float, default=2.0)

    def handle(self, *args, **options):
        timeout = max(1, int(options["timeout"]))
        interval = max(0.2, float(options["interval"]))
        deadline = time.monotonic() + timeout
        last_error = ""

        while time.monotonic() < deadline:
            try:
                connection.close_if_unusable_or_obsolete()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                self.stdout.write(self.style.SUCCESS("PostgreSQL está disponible."))
                return
            except OperationalError as exc:
                last_error = str(exc).strip()
                connection.close()
                time.sleep(interval)

        detail = f" Detalle: {last_error}" if last_error else ""
        raise CommandError(
            f"PostgreSQL no respondió dentro de {timeout} segundos.{detail}"
        )
