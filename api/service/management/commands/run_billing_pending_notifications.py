import logging
import os
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from service.daily_schedule import already_due_today, next_daily_run
from service.daily_schedule import parse_daily_time as _parse_daily_time


logger = logging.getLogger(__name__)


def parse_daily_time(value: str):
    return _parse_daily_time(value, setting_name="BILLING_SUMMARY_AT")


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
        parser.add_argument(
            "--no-catchup",
            action="store_true",
            dest="no_catchup",
            help="No correr al arrancar aunque la hora de hoy ya haya pasado.",
        )

    def _run_summary(self, limit):
        # La conexión a Postgres puede haber muerto mientras dormíamos (reinicio del
        # contenedor de la base). Django sólo la recicla en el ciclo de request: acá
        # hay que pedirlo a mano o el resumen falla todos los días con el mismo error.
        close_old_connections()
        try:
            call_command(
                "send_billing_pending_notifications",
                limit=limit,
                stdout=self.stdout,
                stderr=self.stderr,
            )
        except Exception:
            logger.exception("No se pudo ejecutar el resumen diario de Cobranzas")
        finally:
            close_old_connections()

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

        # Arrancar después de la hora dejaba a Cobranzas sin resumen ese día.
        # El resumen se deduplica por fecha, así que correrlo acá no manda dos veces.
        if not opts.get("no_catchup") and already_due_today(timezone.now(), target_time):
            self.stdout.write(
                f"El horario de hoy ({target_time.strftime('%H:%M')}) ya pasó: se manda el resumen ahora."
            )
            self._run_summary(limit)

        while True:
            scheduled_at = next_daily_run(timezone.now(), target_time)
            wait_seconds = max(1, (scheduled_at - timezone.now()).total_seconds())
            self.stdout.write(
                f"Próximo resumen de Cobranzas: {scheduled_at.isoformat(timespec='minutes')}."
            )
            time.sleep(wait_seconds)
            self._run_summary(limit)
