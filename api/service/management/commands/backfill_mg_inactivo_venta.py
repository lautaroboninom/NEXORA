from django.core.management.base import BaseCommand
from django.db import connection, transaction


def _fetchall_dicts(cur):
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class Command(BaseCommand):
    help = (
        "Backfill mg_estado=inactivo_venta por heuristica legacy "
        "(MG/BIO distinto y no alquilado). Default dry-run; usar --apply para persistir."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica cambios (sin este flag solo informa candidatos).")
        parser.add_argument("--limit", type=int, default=0, help="Limita cantidad de candidatos procesados/listados.")

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        limit = max(0, int(opts.get("limit") or 0))

        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_name='devices'
                       AND column_name='mg_estado'
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_name='devices'
                       AND column_name='mg_estado'
                     LIMIT 1
                    """
                )
            if not cur.fetchone():
                self.stderr.write("No existe columna devices.mg_estado. Ejecuta primero apply_mg_sale_schema.")
                return

            cur.execute(
                "SELECT id, razon_social FROM customers WHERE LOWER(razon_social) LIKE %s ORDER BY id ASC LIMIT 1",
                ["%mg%bio%"],
            )
            owner = cur.fetchone()
            if not owner:
                self.stderr.write(
                    "No se encontró cliente MG/BIO. Abortado para evitar falsos positivos."
                )
                return
            mg_owner_id = int(owner[0])
            mg_owner_name = owner[1]

            limit_sql = " LIMIT %s" if limit > 0 else ""
            params = [mg_owner_id]
            if limit > 0:
                params.append(limit)
            cur.execute(
                f"""
                SELECT
                  d.id,
                  COALESCE(d.numero_interno, '') AS numero_interno,
                  d.customer_id,
                  COALESCE(c.razon_social, '') AS customer_nombre
                FROM devices d
                LEFT JOIN customers c ON c.id = d.customer_id
                WHERE d.numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{{1,4}}$'
                  AND COALESCE(d.alquilado,false) = false
                  AND d.customer_id IS NOT NULL
                  AND d.customer_id <> %s
                  AND COALESCE(d.mg_estado, 'activo') <> 'inactivo_venta'
                ORDER BY d.id ASC
                {limit_sql}
                """,
                params,
            )
            candidates = _fetchall_dicts(cur)

        total = len(candidates)
        self.stdout.write(
            f"Cliente MG/BIO base: #{mg_owner_id} {mg_owner_name}. Candidatos detectados: {total}."
        )
        preview = candidates[:20]
        for row in preview:
            self.stdout.write(
                f"- device #{row['id']}: {row['numero_interno'] or '-'} -> cliente #{row['customer_id']} {row['customer_nombre'] or '-'}"
            )
        if total > len(preview):
            self.stdout.write(f"... y {total - len(preview)} más.")

        if not apply_changes:
            self.stdout.write("Dry-run finalizado. Ejecuta con --apply para persistir.")
            return

        obs = (
            "Backfill automático: marcado como MG histórico inactivo por venta "
            "usando heurística (cliente distinto de MG/BIO y no alquilado)."
        )
        applied = 0
        with transaction.atomic():
            with connection.cursor() as cur:
                for row in candidates:
                    cur.execute(
                        """
                        UPDATE devices
                           SET mg_estado = 'inactivo_venta',
                               mg_inactivo_desde = COALESCE(mg_inactivo_desde, CURRENT_TIMESTAMP),
                               mg_venta_fecha = COALESCE(mg_venta_fecha, CURRENT_TIMESTAMP),
                               mg_venta_observaciones = COALESCE(NULLIF(mg_venta_observaciones,''), %s)
                         WHERE id = %s
                        """,
                        [obs, row["id"]],
                    )
                    cur.execute(
                        """
                        INSERT INTO device_mg_events(
                          device_id, accion, numero_interno_snapshot, fecha_evento,
                          observaciones, usuario_id, ingreso_id, source
                        )
                        VALUES (%s, 'venta', NULLIF(%s,''), CURRENT_TIMESTAMP, %s, NULL, NULL, 'equipos')
                        """,
                        [row["id"], row["numero_interno"] or "", obs],
                    )
                    applied += 1

        self.stdout.write(f"Backfill aplicado. Equipos actualizados: {applied}.")
