from django.core.management.base import BaseCommand
from django.db import connection, transaction


def _fetchall_dicts(cur):
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _table_exists(table_name: str) -> bool:
    with connection.cursor() as cur:
        if connection.vendor == "postgresql":
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_name=%s
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                [table_name],
            )
        else:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_name=%s
                 LIMIT 1
                """,
                [table_name],
            )
        return cur.fetchone() is not None


def _has_table_column(table_name: str, column_name: str) -> bool:
    with connection.cursor() as cur:
        if connection.vendor == "postgresql":
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name=%s
                   AND column_name=%s
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                [table_name, column_name],
            )
        else:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name=%s
                   AND column_name=%s
                 LIMIT 1
                """,
                [table_name, column_name],
            )
        return cur.fetchone() is not None


def _dash_location_id():
    if not _table_exists("locations"):
        return None
    with connection.cursor() as cur:
        cur.execute("SELECT id FROM locations WHERE nombre='-' ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None


class Command(BaseCommand):
    help = (
        "Corrige ingresos alquilados cuyo equipo MG ya esta vendido: "
        "estado -> vendido_entregado. Por defecto es dry-run; usar --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica cambios. Sin este flag solo informa candidatos.")
        parser.add_argument("--limit", type=int, default=0, help="Limita cantidad de candidatos procesados/listados.")

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        limit = max(0, int(opts.get("limit") or 0))

        if not _has_table_column("devices", "mg_estado"):
            self.stderr.write("No existe devices.mg_estado. Ejecuta primero apply_mg_sale_schema.")
            return
        if not _has_table_column("ingresos", "estado"):
            self.stderr.write("No existe ingresos.estado. Abortado.")
            return

        venta_fecha_sql = "d.mg_venta_fecha" if _has_table_column("devices", "mg_venta_fecha") else "NULL"
        venta_factura_sql = (
            "COALESCE(d.mg_venta_factura_numero,'')"
            if _has_table_column("devices", "mg_venta_factura_numero")
            else "''"
        )
        venta_remito_sql = (
            "COALESCE(d.mg_venta_remito_numero,'')"
            if _has_table_column("devices", "mg_venta_remito_numero")
            else "''"
        )

        limit_sql = " LIMIT %s" if limit > 0 else ""
        params = [limit] if limit > 0 else []
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  t.id AS ingreso_id,
                  t.device_id,
                  t.estado::text AS estado,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(d.numero_serie,'') AS numero_serie,
                  COALESCE(d.mg_estado,'activo') AS mg_estado,
                  {venta_fecha_sql} AS mg_venta_fecha,
                  {venta_factura_sql} AS mg_venta_factura_numero,
                  {venta_remito_sql} AS mg_venta_remito_numero
                FROM ingresos t
                JOIN devices d ON d.id = t.device_id
                WHERE LOWER(t.estado::text) = 'alquilado'
                  AND COALESCE(d.mg_estado,'activo') = 'inactivo_venta'
                ORDER BY t.id ASC
                {limit_sql}
                """,
                params,
            )
            candidates = _fetchall_dicts(cur)

        self.stdout.write(f"Candidatos detectados: {len(candidates)}.")
        for row in candidates[:30]:
            self.stdout.write(
                "- ingreso #{ingreso_id} device #{device_id}: {mg} / {ns}".format(
                    ingreso_id=row.get("ingreso_id"),
                    device_id=row.get("device_id"),
                    mg=row.get("numero_interno") or "-",
                    ns=row.get("numero_serie") or "-",
                )
            )
        if len(candidates) > 30:
            self.stdout.write(f"... y {len(candidates) - 30} mas.")

        if not apply_changes:
            self.stdout.write("Dry-run finalizado. Ejecuta con --apply para persistir.")
            return
        if not candidates:
            self.stdout.write("No hay cambios para aplicar.")
            return

        ingreso_has_alquilado = _has_table_column("ingresos", "alquilado")
        ingreso_has_alquiler_a = _has_table_column("ingresos", "alquiler_a")
        ingreso_has_alquiler_remito = _has_table_column("ingresos", "alquiler_remito")
        ingreso_has_alquiler_fecha = _has_table_column("ingresos", "alquiler_fecha")
        ingreso_has_fecha_entrega = _has_table_column("ingresos", "fecha_entrega")
        ingreso_has_factura = _has_table_column("ingresos", "factura_numero")
        ingreso_has_remito = _has_table_column("ingresos", "remito_salida")
        ingreso_has_ubicacion = _has_table_column("ingresos", "ubicacion_id")
        devices_has_alquilado = _has_table_column("devices", "alquilado")
        devices_has_alquiler_a = _has_table_column("devices", "alquiler_a")
        devices_has_ubicacion = _has_table_column("devices", "ubicacion_id")
        events_available = _table_exists("ingreso_events")
        dash_id = _dash_location_id()

        applied = 0
        with transaction.atomic():
            with connection.cursor() as cur:
                for row in candidates:
                    set_parts = ["estado = 'vendido_entregado'"]
                    update_params = []
                    if ingreso_has_alquilado:
                        set_parts.append("alquilado = FALSE")
                    if ingreso_has_alquiler_a:
                        set_parts.append("alquiler_a = NULL")
                    if ingreso_has_alquiler_remito:
                        set_parts.append("alquiler_remito = NULL")
                    if ingreso_has_alquiler_fecha:
                        set_parts.append("alquiler_fecha = NULL")
                    if ingreso_has_fecha_entrega:
                        set_parts.append("fecha_entrega = COALESCE(fecha_entrega, %s, CURRENT_TIMESTAMP)")
                        update_params.append(row.get("mg_venta_fecha"))
                    if ingreso_has_factura:
                        set_parts.append("factura_numero = COALESCE(factura_numero, NULLIF(%s,''))")
                        update_params.append(row.get("mg_venta_factura_numero") or "")
                    if ingreso_has_remito:
                        set_parts.append("remito_salida = COALESCE(remito_salida, NULLIF(%s,''))")
                        update_params.append(row.get("mg_venta_remito_numero") or "")
                    if ingreso_has_ubicacion and dash_id:
                        set_parts.append("ubicacion_id = %s")
                        update_params.append(dash_id)
                    update_params.append(row["ingreso_id"])
                    cur.execute(
                        "UPDATE ingresos SET " + ", ".join(set_parts) + " WHERE id=%s",
                        update_params,
                    )

                    device_parts = []
                    device_params = []
                    if devices_has_alquilado:
                        device_parts.append("alquilado = FALSE")
                    if devices_has_alquiler_a:
                        device_parts.append("alquiler_a = NULL")
                    if devices_has_ubicacion and dash_id:
                        device_parts.append("ubicacion_id = %s")
                        device_params.append(dash_id)
                    if device_parts:
                        device_params.append(row["device_id"])
                        cur.execute(
                            "UPDATE devices SET " + ", ".join(device_parts) + " WHERE id=%s",
                            device_params,
                        )

                    if events_available:
                        try:
                            with transaction.atomic():
                                cur.execute(
                                    """
                                    INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
                                    VALUES (
                                      %s,
                                      'alquilado'::ticket_state,
                                      'vendido_entregado'::ticket_state,
                                      NULL,
                                      CURRENT_TIMESTAMP,
                                      'Backfill: MG vendido que figuraba como alquilado'
                                    )
                                    """,
                                    [row["ingreso_id"]],
                                )
                        except Exception:
                            with transaction.atomic():
                                cur.execute(
                                    """
                                    INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, ts, comentario)
                                    VALUES (
                                      %s,
                                      'vendido_entregado',
                                      NULL,
                                      CURRENT_TIMESTAMP,
                                      'Backfill: MG vendido que figuraba como alquilado'
                                    )
                                    """,
                                    [row["ingreso_id"]],
                                )
                    applied += 1

        self.stdout.write(f"Backfill aplicado. Ingresos actualizados: {applied}.")
