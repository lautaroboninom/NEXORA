import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from service.notification_email_outbox import (
    process_notification_email_outbox,
    retry_failed_notification_emails,
)


logger = logging.getLogger(__name__)

# Doce intentos se reparten en unas 28 horas. Más que eso caído —un fin de semana
# con el servidor de correo mal, una clave vencida— y todo lo de esa ventana quedaba
# en `failed`, que no lo volvía a mirar nadie: el aviso no llegaba y tampoco llegaba
# el aviso de que no había llegado. Reactivar cada tanto lo arregla; la ventana es lo
# que evita que reactivar se convierta en insistirle de por vida a una casilla que no
# existe.
DEFAULT_RETRY_FAILED_INTERVAL_HOURS = 12
DEFAULT_RETRY_FAILED_WINDOW_HOURS = 48


class Command(BaseCommand):
    help = "Procesa la cola durable de correos de notificaciones"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=10)
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--once", action="store_true")
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="Reactiva ahora TODOS los correos fallidos, sin ventana de tiempo.",
        )
        parser.add_argument(
            "--retry-failed-interval-hours",
            type=float,
            default=None,
            help="Cada cuánto reactivar los fallidos recientes (0 lo desactiva).",
        )
        parser.add_argument(
            "--retry-failed-window-hours",
            type=float,
            default=None,
            help="Sólo se reactivan los que fallaron dentro de esta ventana.",
        )

    def _hours(self, valor, nombre, predeterminado):
        if valor is None:
            valor = getattr(settings, nombre, predeterminado)
        try:
            return max(0.0, float(valor))
        except (TypeError, ValueError):
            return float(predeterminado)

    def _reactivar_fallidos(self, ventana_horas):
        # Sin `close_old_connections()` acá: el loop ya recicla al empezar cada vuelta
        # y el arranque corre con la conexión recién abierta. Cerrarla en este punto
        # además rompería una transacción abierta por quien llame al comando.
        try:
            reactivados = retry_failed_notification_emails(failed_within_hours=ventana_horas)
        except Exception:
            logger.exception("No se pudieron reactivar los correos fallidos")
            return
        if reactivados:
            self.stdout.write(f"Correos fallidos reactivados: {reactivados}.")
            logger.info(
                "notification_email_outbox_fallidos_reactivados",
                extra={"cantidad": reactivados, "ventana_horas": ventana_horas},
            )

    def handle(self, *args, **opts):
        interval = max(2, int(opts["interval"] or 10))
        limit = max(1, min(500, int(opts["limit"] or 50)))
        once = bool(opts["once"])
        reintento_intervalo = self._hours(
            opts.get("retry_failed_interval_hours"),
            "NOTIFICATION_EMAIL_RETRY_FAILED_INTERVAL_HOURS",
            DEFAULT_RETRY_FAILED_INTERVAL_HOURS,
        )
        reintento_ventana = self._hours(
            opts.get("retry_failed_window_hours"),
            "NOTIFICATION_EMAIL_RETRY_FAILED_WINDOW_HOURS",
            DEFAULT_RETRY_FAILED_WINDOW_HOURS,
        )

        if opts["retry_failed"]:
            # El pedido explícito no lleva ventana: es alguien mirando la cola.
            reactivated = retry_failed_notification_emails()
            self.stdout.write(f"Correos fallidos reactivados: {reactivated}.")

        self.stdout.write(f"Procesando cola de correos cada {interval} segundos.")
        if reintento_intervalo > 0:
            self.stdout.write(
                f"Los fallidos de las últimas {reintento_ventana:.0f} h se reintentan "
                f"cada {reintento_intervalo:.0f} h."
            )
            # Arrancar es el mejor momento para reintentar: si el contenedor se
            # reinició, algo cambió.
            self._reactivar_fallidos(reintento_ventana)
        proximo_reintento = time.monotonic() + reintento_intervalo * 3600

        while True:
            # Reciclar la conexión antes de cada vuelta. Si Postgres se reinicia,
            # Django deja la conexión rota marcada como rota y no la reemplaza solo
            # fuera del ciclo de request: el worker quedaba vivo, con el contenedor
            # en verde, tirando el mismo error para siempre y sin procesar nada.
            close_old_connections()
            try:
                result = process_notification_email_outbox(limit=limit)
                if result["claimed"]:
                    self.stdout.write(
                        "Cola procesada: "
                        f"tomados={result['claimed']} enviados={result['sent']} "
                        f"reintentos={result['retry']} fallidos={result['failed']}."
                    )
            except Exception:
                logger.exception("No se pudo procesar la cola de correos")
            if once:
                return
            if reintento_intervalo > 0 and time.monotonic() >= proximo_reintento:
                self._reactivar_fallidos(reintento_ventana)
                proximo_reintento = time.monotonic() + reintento_intervalo * 3600
            time.sleep(interval)
