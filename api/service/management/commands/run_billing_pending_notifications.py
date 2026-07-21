import logging
import os
import time
from datetime import datetime, time as daily_time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


logger = logging.getLogger(__name__)


def parse_daily_time(value: str) -> daily_time:
    clean_value = str(value or "").strip()
    try:
        return datetime.strptime(clean_value, "%H:%M").time()
    except ValueError as exc:
        raise CommandError("BILLING_SUMMARY_AT debe tener formato HH:MM.") from exc


def next_daily_run(now: datetime, target_time: daily_time) -> datetime:
    current_timezone = timezone.get_current_timezone()
    local_now = timezone.localtime(now, current_timezone)
    candidate = timezone.make_aware(
        datetime.combine(local_now.date(), target_time),
        current_timezone,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


class Command(BaseCommand):
    help = "Ejecuta diariamente el resumen de facturación pendiente de Cobranzas"

    def add_arguments(self, parser):
        parser.add_argument(
            "--at",
            default=os.getenv("BILLING_SUMMARY_AT", "08:00"),
            help="Hora local de ejecución en formato HH:MM (predeterminado: 08:00).",
        )
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        target_time = parse_daily_time(opts["at"])
        limit = max(1, min(200, int(opts["limit"] or 50)))

        if opts["once"]:
            call_command(
                "send_billing_pending_notifications",
                limit=limit,
                stdout=self.stdout,
                stderr=self.stderr,
            )
            return

        while True:
            scheduled_at = next_daily_run(timezone.now(), target_time)
            wait_seconds = max(1, (scheduled_at - timezone.now()).total_seconds())
            self.stdout.write(
                f"Próximo resumen de Cobranzas: {scheduled_at.isoformat(timespec='minutes')}."
            )
            time.sleep(wait_seconds)
            try:
                call_command(
                    "send_billing_pending_notifications",
                    limit=limit,
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
            except Exception:
                logger.exception("No se pudo ejecutar el resumen diario de Cobranzas")
