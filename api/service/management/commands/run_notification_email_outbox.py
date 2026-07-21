import logging
import time

from django.core.management.base import BaseCommand

from service.notification_email_outbox import (
    process_notification_email_outbox,
    retry_failed_notification_emails,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Procesa la cola durable de correos de notificaciones"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=10)
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--retry-failed", action="store_true")

    def handle(self, *args, **opts):
        interval = max(2, int(opts["interval"] or 10))
        limit = max(1, min(500, int(opts["limit"] or 50)))
        once = bool(opts["once"])
        if opts["retry_failed"]:
            reactivated = retry_failed_notification_emails()
            self.stdout.write(f"Correos fallidos reactivados: {reactivated}.")

        self.stdout.write(f"Procesando cola de correos cada {interval} segundos.")
        while True:
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
            time.sleep(interval)
