from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

from service.delivery_orders import (
    _ENTREGA_SOURCE_ESTADOS,
    _service_release_ingreso_ids_for_order_ids,
    _sync_service_release_ingresos_delivery,
)


class Command(BaseCommand):
    help = (
        "Sincroniza hacia `ingresos` las ordenes de entrega service_release que ya "
        "figuran entregadas (delivered_at) pero cuya hoja de servicio sigue en "
        "estado liberado / vendido_pendiente_entrega (caso OS-29473: remito cargado "
        "a mano en la orden de entrega, nunca reflejado en la hoja de servicio). "
        "Dry-run por defecto; usar --apply para escribir."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios (sin esto solo muestra).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.id AS order_id, o.remito_number, o.delivered_at
                  FROM delivery_orders o
                 WHERE o.delivery_type = 'service_release'
                   AND o.delivered_at IS NOT NULL
                   AND o.remito_number IS NOT NULL
                 ORDER BY o.delivered_at
                """
            )
            columns = [col[0] for col in cur.description]
            orders = [dict(zip(columns, row)) for row in cur.fetchall()]

        self.stdout.write(self.style.MIGRATE_HEADING("Ordenes de entrega service_release ya entregadas"))
        total_drifted = 0
        for order in orders:
            order_id = str(order["order_id"])
            ingreso_ids = _service_release_ingreso_ids_for_order_ids([order_id])
            if not ingreso_ids:
                continue
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT id, estado FROM ingresos WHERE id = ANY(%s) ORDER BY id",
                    [ingreso_ids],
                )
                ingresos = [{"id": row[0], "estado": row[1]} for row in cur.fetchall()]
            drifted = [i for i in ingresos if (i["estado"] or "").strip().lower() in _ENTREGA_SOURCE_ESTADOS]
            if not drifted:
                continue

            total_drifted += len(drifted)
            self.stdout.write(
                f"Orden {order_id} (remito {order['remito_number']}, entregada {order['delivered_at']}): "
                + ", ".join(f"OS-{i['id']} [{i['estado']}]" for i in drifted)
            )
            if apply_changes:
                synced = _sync_service_release_ingresos_delivery(
                    [i["id"] for i in drifted], order["remito_number"], None
                )
                self.stdout.write(self.style.SUCCESS(f"  -> sincronizadas: {synced}"))

        if total_drifted == 0:
            self.stdout.write(self.style.SUCCESS("Nada para sincronizar."))
        elif not apply_changes:
            self.stdout.write(
                self.style.WARNING(f"DRY-RUN: {total_drifted} OS detectadas, no se aplicaron cambios (usar --apply).")
            )
