"""Completa `ingresos.cliente_original_id` para las conversiones ya hechas.

Los papeles de la OS resuelven el cliente por `devices.customer_id`, y tomar un equipo en
parte de pago (o convertirlo a propio MG) lo pasa a MG BIO. Desde ahora el pase guarda quién
era el dueño, pero las OS anteriores quedaron sin el dato y su orden de salida sigue saliendo
a nombre de MG BIO.

El dato está en la auditoría: cada cambio de `devices.customer_id` deja su `old_value`, y las
filas escritas desde una vista de ingreso traen el `ingreso_id`. Se toma **el primer** pase a
MG BIO de cada OS, que es el que dice de quién era el equipo antes de que fuera nuestro.

Dry-run por defecto, como `backfill_device_cambio`.
"""

import csv

from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Completa `ingresos.cliente_original_id` leyendo de `audit.change_log` quién era el "
        "dueño del equipo antes de que pasara al patrimonio de MG BIO."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Escribe los cambios. Sin este flag sólo informa (dry-run).",
        )
        parser.add_argument(
            "--csv",
            dest="csv_path",
            default="",
            help="Archivo donde volcar los casos que no se pudieron resolver.",
        )

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        csv_path = (opts.get("csv_path") or "").strip()

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_name='ingresos' AND column_name='cliente_original_id'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """
            )
            if cur.fetchone() is None:
                self.stderr.write(
                    "Falta `ingresos.cliente_original_id`: corré antes apply_device_swap_schema."
                )
                return

            # El mismo criterio que usa la app para decidir a quién se le pasa el equipo: si
            # acá se eligiera otro cliente, el backfill leería pases que nunca ocurrieron.
            from service.views.ingresos_views import _mg_owner_customer_id

            mg_owner_id = _mg_owner_customer_id()
            if not mg_owner_id:
                self.stderr.write("No existe el cliente base 'MG BIO'.")
                return
            mg_owner_id = int(mg_owner_id)

            # Primer pase a MG BIO de cada OS. `old_value` es el dueño anterior.
            cur.execute(
                """
                SELECT DISTINCT ON (cl.ingreso_id)
                       cl.ingreso_id,
                       cl.record_id AS device_id,
                       cl.old_value,
                       cl.ts
                  FROM audit.change_log cl
                  JOIN ingresos t ON t.id = cl.ingreso_id
                 WHERE cl.table_name = 'devices'
                   AND cl.column_name = 'customer_id'
                   AND cl.new_value = %s
                   AND cl.ingreso_id IS NOT NULL
                   AND NULLIF(TRIM(COALESCE(cl.old_value,'')), '') IS NOT NULL
                   AND cl.old_value IS DISTINCT FROM cl.new_value
                   AND t.cliente_original_id IS NULL
                 ORDER BY cl.ingreso_id, cl.ts ASC
                """,
                [str(mg_owner_id)],
            )
            candidatos = cur.fetchall()

            resueltos = []
            sin_resolver = []
            for ingreso_id, device_id, old_value, ts in candidatos:
                try:
                    original_id = int(str(old_value).strip())
                except (TypeError, ValueError):
                    sin_resolver.append((ingreso_id, device_id, old_value, "old_value no numérico"))
                    continue
                if original_id == mg_owner_id:
                    sin_resolver.append((ingreso_id, device_id, old_value, "ya era MG BIO"))
                    continue
                cur.execute("SELECT razon_social FROM customers WHERE id = %s", [original_id])
                cliente = cur.fetchone()
                if not cliente:
                    sin_resolver.append((ingreso_id, device_id, old_value, "cliente inexistente"))
                    continue
                resueltos.append((ingreso_id, device_id, original_id, cliente[0], ts))

            self.stdout.write(
                f"OS con pase a MG BIO y sin dueño original: {len(candidatos)} | "
                f"resueltas: {len(resueltos)} | sin resolver: {len(sin_resolver)}"
            )
            for ingreso_id, _device_id, original_id, razon_social, _ts in resueltos[:20]:
                self.stdout.write(f"  OS {ingreso_id} -> cliente {original_id} ({razon_social})")
            if len(resueltos) > 20:
                self.stdout.write(f"  ... y {len(resueltos) - 20} más")

            if csv_path and sin_resolver:
                with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["ingreso_id", "device_id", "old_value", "motivo"])
                    writer.writerows(sin_resolver)
                self.stdout.write(f"Sin resolver volcados en {csv_path}")

            if not apply_changes:
                self.stdout.write("DRY-RUN: no se escribió nada. Repetir con --apply.")
                return

            with transaction.atomic():
                for ingreso_id, _device_id, original_id, _razon_social, _ts in resueltos:
                    cur.execute(
                        """
                        UPDATE ingresos
                           SET cliente_original_id = %s
                         WHERE id = %s
                           AND cliente_original_id IS NULL
                        """,
                        [original_id, ingreso_id],
                    )
            self.stdout.write(f"APLICADO OK: {len(resueltos)} OS con su dueño original")
