from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


TECH_FINAL_STATES = {"reparado", "controlado_sin_defecto", "no_se_repara"}
CLOSED_STATES = {
    "liberado",
    "entregado",
    "alquilado",
    "baja",
    "vendido_pendiente_entrega",
    "vendido_entregado",
}
RESTORABLE_STATES = {
    "ingresado",
    "diagnosticado",
    "presupuestado",
    "reparar",
    *TECH_FINAL_STATES,
}
RESOLUTION_STATES = {
    "reparado": "reparado",
    "cambio": "reparado",
    "no_se_encontro_falla": "controlado_sin_defecto",
    "no_reparado": "no_se_repara",
    "presupuesto_rechazado": "no_se_repara",
}
MIGRATION_COMMENT = "Migración: se separó la derivación del estado operativo."


def _clean(value):
    return str(value or "").strip().lower()


def choose_reconciled_state(row):
    """Devuelve (estado, evidencia, conflicto) para una fila heredada."""
    closed_event = _clean(row.get("closed_event_state"))
    if row.get("fecha_entrega") is not None:
        return None, "fecha de entrega registrada", True
    if _clean(row.get("remito_salida")):
        return None, "orden de salida registrada", True
    if closed_event in CLOSED_STATES:
        return None, f"evento posterior de cierre: {closed_event}", True

    technical_event = _clean(row.get("technical_event_state"))
    if technical_event in TECH_FINAL_STATES:
        return technical_event, f"evento técnico posterior: {technical_event}", False

    resolution = _clean(row.get("resolucion"))
    if resolution in RESOLUTION_STATES:
        return RESOLUTION_STATES[resolution], f"resolución: {resolution}", False

    presupuesto = _clean(row.get("presupuesto_estado"))
    quote_state = _clean(row.get("quote_estado"))
    if "aprobado" in {presupuesto, quote_state}:
        return "reparar", "presupuesto aprobado", False

    diagnosis_event = bool(row.get("diagnosis_event"))
    if (
        quote_state in {"emitido", "enviado", "presupuestado", "rechazado", "no_aplica"}
        or presupuesto in {"emitido", "enviado", "presupuestado", "rechazado", "no_aplica"}
        or row.get("fecha_servicio") is not None
        or diagnosis_event
    ):
        return "diagnosticado", "diagnóstico o presupuesto registrado", False

    previous_state = _clean(row.get("previous_state"))
    if previous_state in CLOSED_STATES:
        return None, f"estado previo cerrado: {previous_state}", True
    if previous_state in RESTORABLE_STATES:
        normalized = "diagnosticado" if previous_state == "presupuestado" else previous_state
        return normalized, f"estado previo a la derivación: {previous_state}", False

    return "ingresado", "sin evidencia adicional", False


class Command(BaseCommand):
    help = (
        "Previsualiza o aplica la conciliación de ingresos cuyo estado heredado es "
        "'derivado'. Por defecto no escribe."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", dest="apply_changes")
        parser.add_argument(
            "--confirm",
            default="",
            help="Para aplicar, indicar literalmente APLICAR.",
        )
        parser.add_argument("--actor-user-id", type=int, default=None)

    def _audit_schema_available(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('audit.change_log')")
            row = cursor.fetchone()
        return bool(row and row[0])

    def _load_rows(self):
        has_audit = self._audit_schema_available()
        audit_select = (
            """
            LEFT JOIN LATERAL (
              SELECT NULLIF(TRIM(change.old_value), '') AS previous_state
                FROM audit.change_log change
               WHERE LOWER(change.table_name) = 'ingresos'
                 AND change.record_id = ingreso.id
                 AND LOWER(change.column_name) = 'estado'
                 AND LOWER(COALESCE(change.new_value, '')) = 'derivado'
               ORDER BY change.ts DESC, change.id DESC
               LIMIT 1
            ) audit_state ON TRUE
            """
            if has_audit
            else ""
        )
        audit_previous = "audit_state.previous_state" if has_audit else "NULL::text"
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT ingreso.id,
                       ingreso.estado::text AS estado_actual,
                       ingreso.resolucion,
                       ingreso.presupuesto_estado::text AS presupuesto_estado,
                       ingreso.fecha_servicio,
                       ingreso.fecha_entrega,
                       ingreso.remito_salida,
                       derivacion.id AS derivacion_id,
                       derivacion.estado::text AS derivacion_estado,
                       derivacion.fecha_deriv,
                       COALESCE(proveedor.nombre, '') AS proveedor,
                       quote.estado::text AS quote_estado,
                       technical_event.a_estado::text AS technical_event_state,
                       closed_event.a_estado::text AS closed_event_state,
                       (diagnosis_event.id IS NOT NULL) AS diagnosis_event,
                       COALESCE(derived_event.de_estado::text, {audit_previous}) AS previous_state
                  FROM ingresos ingreso
                  LEFT JOIN LATERAL (
                    SELECT derived.*
                      FROM equipos_derivados derived
                     WHERE derived.ingreso_id = ingreso.id
                     ORDER BY
                       (derived.fecha_entrega IS NULL AND derived.estado IN ('derivado','en_servicio')) DESC,
                       derived.fecha_deriv DESC,
                       derived.id DESC
                     LIMIT 1
                  ) derivacion ON TRUE
                  LEFT JOIN proveedores_externos proveedor ON proveedor.id = derivacion.proveedor_id
                  LEFT JOIN LATERAL (
                    SELECT budget.estado
                      FROM quotes budget
                     WHERE budget.ingreso_id = ingreso.id
                     ORDER BY COALESCE(budget.version_num, 1) DESC, budget.id DESC
                     LIMIT 1
                  ) quote ON TRUE
                  LEFT JOIN LATERAL (
                    SELECT event.id, event.a_estado
                      FROM ingreso_events event
                     WHERE event.ingreso_id = ingreso.id
                       AND event.a_estado IN ('reparado','controlado_sin_defecto','no_se_repara')
                       AND (derivacion.fecha_deriv IS NULL OR event.ts::date >= derivacion.fecha_deriv)
                     ORDER BY event.ts DESC, event.id DESC
                     LIMIT 1
                  ) technical_event ON TRUE
                  LEFT JOIN LATERAL (
                    SELECT event.id, event.a_estado
                      FROM ingreso_events event
                     WHERE event.ingreso_id = ingreso.id
                       AND event.a_estado IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
                       AND (derivacion.fecha_deriv IS NULL OR event.ts::date >= derivacion.fecha_deriv)
                     ORDER BY event.ts DESC, event.id DESC
                     LIMIT 1
                  ) closed_event ON TRUE
                  LEFT JOIN LATERAL (
                    SELECT event.id
                      FROM ingreso_events event
                     WHERE event.ingreso_id = ingreso.id
                       AND event.a_estado = 'diagnosticado'
                       AND (derivacion.fecha_deriv IS NULL OR event.ts::date >= derivacion.fecha_deriv)
                     ORDER BY event.ts DESC, event.id DESC
                     LIMIT 1
                  ) diagnosis_event ON TRUE
                  LEFT JOIN LATERAL (
                    SELECT event.de_estado
                      FROM ingreso_events event
                     WHERE event.ingreso_id = ingreso.id
                       AND event.a_estado = 'derivado'
                     ORDER BY event.ts DESC, event.id DESC
                     LIMIT 1
                  ) derived_event ON TRUE
                  {audit_select}
                 WHERE ingreso.estado = 'derivado'
                 ORDER BY ingreso.id
                """
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("La conciliación requiere PostgreSQL.")
        apply_changes = bool(options.get("apply_changes"))
        if apply_changes and str(options.get("confirm") or "").strip().upper() != "APLICAR":
            raise CommandError(
                "Primero ejecutá la vista previa. Para confirmar usá --apply --confirm APLICAR."
            )

        with transaction.atomic():
            rows = self._load_rows()
            decisions = []
            for row in rows:
                target, evidence, conflict = choose_reconciled_state(row)
                decisions.append((row, target, evidence, conflict))

            if not decisions:
                self.stdout.write("No hay ingresos con estado heredado 'derivado'.")
                return

            self.stdout.write("OS | Proveedor | Fecha derivación | Estado elegido | Evidencia")
            for row, target, evidence, conflict in decisions:
                selected = "CONFLICTO" if conflict else target
                self.stdout.write(
                    f"{row['id']} | {row.get('proveedor') or '-'} | "
                    f"{row.get('fecha_deriv') or '-'} | {selected} | {evidence}"
                )

            conflicts = [decision for decision in decisions if decision[3]]
            if conflicts:
                ids = ", ".join(str(decision[0]["id"]) for decision in conflicts)
                if apply_changes:
                    raise CommandError(
                        "Conciliación cancelada sin cambios por evidencia de cierre en las OS: "
                        f"{ids}."
                    )
                self.stdout.write(
                    self.style.WARNING(
                        f"Vista previa con conflictos en {len(conflicts)} OS: {ids}. No se aplicaron cambios."
                    )
                )

            if not apply_changes:
                self.stdout.write(
                    self.style.WARNING(
                        "VISTA PREVIA: no se aplicaron cambios. Para aplicar, resolvé los conflictos y usá "
                        "--apply --confirm APLICAR."
                    )
                )
                return

            actor_user_id = options.get("actor_user_id")
            updated = 0
            with connection.cursor() as cursor:
                for row, target, evidence, conflict in decisions:
                    if conflict:
                        continue
                    cursor.execute(
                        """
                        UPDATE ingresos
                           SET estado = %s
                         WHERE id = %s
                           AND estado = 'derivado'
                        """,
                        [target, row["id"]],
                    )
                    if cursor.rowcount != 1:
                        continue
                    updated += 1
                    cursor.execute(
                        """
                        INSERT INTO ingreso_events
                            (ticket_id, de_estado, a_estado, usuario_id, comentario)
                        SELECT %s, 'derivado', %s, %s, %s
                         WHERE NOT EXISTS (
                               SELECT 1
                                 FROM ingreso_events event
                                WHERE event.ingreso_id = %s
                                  AND event.comentario = %s
                              )
                        """,
                        [
                            row["id"],
                            target,
                            actor_user_id,
                            MIGRATION_COMMENT,
                            row["id"],
                            MIGRATION_COMMENT,
                        ],
                    )

        self.stdout.write(self.style.SUCCESS(f"Conciliación aplicada: {updated} OS actualizadas."))
