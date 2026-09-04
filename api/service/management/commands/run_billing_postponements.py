import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from service.delivery_orders import process_due_delivery_order_billing_postponements


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Procesa los vencimientos de remitos pospuestos para facturación"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        interval = max(10, int(opts["interval"] or 60))
        limit = max(1, min(500, int(opts["limit"] or 100)))
        once = bool(opts["once"])
        self.stdout.write(f"Procesando postergaciones de facturación cada {interval} segundos.")
        while True:
            # Reciclar la conexión antes de cada vuelta. Si Postgres se reinicia,
            # Django deja la conexión rota marcada como rota y no la reemplaza solo
            # fuera del ciclo de request: el worker quedaba vivo, con el contenedor
            # en verde, tirando el mismo error para siempre y sin procesar nada.
            close_old_connections()
            try:
                result = process_due_delivery_order_billing_postponements(limit=limit)
                if result["due"]:
                    self.stdout.write(
                        f"Postergaciones vencidas: {result['due']}; notificaciones creadas: {result['notifications']}."
                    )
            except Exception:
                logger.exception("No se pudieron procesar las postergaciones de facturación")
            if once:
                return
            time.sleep(interval)
