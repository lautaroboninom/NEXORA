import logging
import os
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from service.daily_schedule import already_due_today, next_daily_run
from service.daily_schedule import parse_daily_time as _parse_daily_time


logger = logging.getLogger(__name__)

# Comandos diarios que hasta ahora dependían de un cron/Task Scheduler que nunca
# se creó en el servidor: sus notificaciones (presupuesto_pendiente,
# preventivo_vencido, preventivo_proximo, resumen_operativo) no se emitían nunca.
DAILY_ALERT_COMMANDS = (
    "send_presupuesto_alerts",
    "send_preventivo_alerts",
    "send_work_alerts",
)


def parse_daily_time(value: str):
    return _parse_daily_time(value, setting_name="DAILY_ALERTS_AT")


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
            "--no-catchup",
            action="store_true",
            dest="no_catchup",
            help="No correr al arrancar aunque la hora de hoy ya haya pasado.",
        )
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
        # Después de dormir horas, la conexión a Postgres puede estar muerta sin que
        # nadie lo note: basta que se reinicie el contenedor de la base. Django sólo
        # recicla conexiones en el ciclo de request, así que en un comando de consola
        # la conexión rota se queda rota para siempre y el trabajo diario falla todos
        # los días con el mismo error.
        close_old_connections()
        for name in commands:
            try:
                call_command(name, stdout=self.stdout, stderr=self.stderr)
            except Exception:
                # Un comando que falla no debe dejar sin correr a los otros dos.
                logger.exception("Falló la alerta diaria", extra={"command": name})
                self.stderr.write(f"Falló {name}; se sigue con el resto.")
        close_old_connections()

    def handle(self, *args, **opts):
        target_time = parse_daily_time(opts["at"])
        commands = self._selected_commands(opts.get("only"))

        if opts["once"]:
            self._run_once(commands)
            return

        # Arrancar después de la hora dejaba el día entero sin alertas. Correrlas acá
        # no duplica nada: las notificaciones se deduplican por día.
        if not opts.get("no_catchup") and already_due_today(timezone.now(), target_time):
            self.stdout.write(
                f"El horario de hoy ({target_time.strftime('%H:%M')}) ya pasó: se corren las alertas ahora."
            )
            self._run_once(commands)

        while True:
            scheduled_at = next_daily_run(timezone.now(), target_time)
            wait_seconds = max(1, (scheduled_at - timezone.now()).total_seconds())
            self.stdout.write(
                f"Próximas alertas diarias: {scheduled_at.isoformat(timespec='minutes')} "
                f"({', '.join(commands)})."
            )
            time.sleep(wait_seconds)
            self._run_once(commands)
