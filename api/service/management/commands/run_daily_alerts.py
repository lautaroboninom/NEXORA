import logging
import os
import time
from datetime import datetime, time as daily_time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


logger = logging.getLogger(__name__)

# Comandos diarios que hasta ahora dependían de un cron/Task Scheduler que nunca
# se creó en el servidor: sus notificaciones (presupuesto_pendiente,
# preventivo_vencido, preventivo_proximo, resumen_operativo) no se emitían nunca.
DAILY_ALERT_COMMANDS = (
    "send_presupuesto_alerts",
    "send_preventivo_alerts",
    "send_work_alerts",
)


def parse_daily_time(value: str) -> daily_time:
    clean_value = str(value or "").strip()
    try:
        return datetime.strptime(clean_value, "%H:%M").time()
    except ValueError as exc:
        raise CommandError("DAILY_ALERTS_AT debe tener formato HH:MM.") from exc


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
    help = "Ejecuta diariamente las alertas de presupuestos, preventivos y resumen operativo"

    def add_arguments(self, parser):
        parser.add_argument(
            "--at",
            default=os.getenv("DAILY_ALERTS_AT", "07:30"),
            help="Hora local de ejecución en formato HH:MM (predeterminado: 07:30).",
        )
        parser.add_argument("--once", action="store_true")
        parser.add_argument(
            "--only",
            default="",
            help="Coma-separado: corre sólo estos comandos de la lista diaria.",
        )

    def _selected_commands(self, only):
        requested = [name.strip() for name in str(only or "").split(",") if name.strip()]
        if not requested:
            return list(DAILY_ALERT_COMMANDS)
        unknown = [name for name in requested if name not in DAILY_ALERT_COMMANDS]
        if unknown:
            raise CommandError(f"Comando diario desconocido: {', '.join(unknown)}")
        return requested

    def _run_once(self, commands):
        for name in commands:
            try:
                call_command(name, stdout=self.stdout, stderr=self.stderr)
            except Exception:
                # Un comando que falla no debe dejar sin correr a los otros dos.
                logger.exception("Falló la alerta diaria", extra={"command": name})
                self.stderr.write(f"Falló {name}; se sigue con el resto.")

    def handle(self, *args, **opts):
        target_time = parse_daily_time(opts["at"])
        commands = self._selected_commands(opts.get("only"))

        if opts["once"]:
            self._run_once(commands)
            return

        while True:
            scheduled_at = next_daily_run(timezone.now(), target_time)
            wait_seconds = max(1, (scheduled_at - timezone.now()).total_seconds())
            self.stdout.write(
                f"Próximas alertas diarias: {scheduled_at.isoformat(timespec='minutes')} "
                f"({', '.join(commands)})."
            )
            time.sleep(wait_seconds)
            self._run_once(commands)
