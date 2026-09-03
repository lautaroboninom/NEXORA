from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.views.ingresos_views import TECH_FINAL_REPAIR_STATES, TICKET_STATES

# Estados cuya transición no dejaba traza en `ingreso_events`: los endpoints hacían el
# UPDATE de `ingresos.estado` y nada más. Por eso los hitos "Diagnóstico" y "Reparación"
# de la hoja de servicio salían siempre vacíos, y las métricas de tiempos también.
#
# `reparar` no alimenta ningún hito del panel pero sí es el **inicio** del MTTR
# (`metricas_views`, JOIN contra `a_estado='reparar'`): sin él la métrica da cero.
ESTADOS_A_RECUPERAR = ("diagnosticado", "reparar", *TECH_FINAL_REPAIR_STATES)


class Command(BaseCommand):
    help = (
        "Reconstruye los eventos de estado que nunca se escribieron en `ingreso_events`, "
        "leyéndolos de `audit.change_log` (el trigger de DB sí registró todos los cambios "
        "de `ingresos.estado`). Sin --apply sólo informa."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Escribe los eventos. Sin este flag sólo informa (dry-run).",
        )
        parser.add_argument(
            "--estado",
            action="append",
            default=[],
            help=(
                "Limita a un estado (repetible). Por defecto: "
                + ", ".join(ESTADOS_A_RECUPERAR)
            ),
        )
        parser.add_argument(
            "--ingreso",
            type=int,
            default=None,
            help="Limita a una sola OS, para probar antes de correrlo entero.",
        )

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        estados = [e.strip().lower() for e in (opts.get("estado") or []) if e.strip()]
        estados = estados or list(ESTADOS_A_RECUPERAR)
        ingreso_id = opts.get("ingreso")

        desconocidos = [e for e in estados if e not in TICKET_STATES]
        if desconocidos:
            self.stderr.write(f"Estados que no existen en ticket_state: {', '.join(desconocidos)}")
            return

        if connection.vendor != "postgresql":
            self.stderr.write("Este backfill necesita PostgreSQL (audit.change_log).")
            return

        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('audit.change_log')")
            if cur.fetchone()[0] is None:
                self.stderr.write("No existe audit.change_log: no hay de dónde reconstruir.")
                return

        faltantes = _fetch_faltantes(estados, ingreso_id)
        if not faltantes:
            self.stdout.write("No falta ningún evento: nada que hacer.")
            return

        por_estado = {}
        for row in faltantes:
            por_estado[row["a_estado"]] = por_estado.get(row["a_estado"], 0) + 1

        self.stdout.write(f"Eventos faltantes: {len(faltantes)}")
        for estado in sorted(por_estado):
            self.stdout.write(f"  {estado}: {por_estado[estado]}")

        if not apply_changes:
            self.stdout.write("DRY-RUN: no se escribió nada. Repetí con --apply.")
            return

        insertados = 0
        fallidos = 0
        for row in faltantes:
            # Uno por transacción: un ingreso borrado o un estado raro no debe tirar abajo
            # el resto del backfill.
            try:
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
                            VALUES (%s, NULLIF(%s,'')::ticket_state, %s::ticket_state, %s, %s, %s)
                            """,
                            [
                                row["ingreso_id"],
                                row["de_estado"],
                                row["a_estado"],
                                row["user_id"],
                                row["ts"],
                                "Evento reconstruido desde audit.change_log",
                            ],
                        )
                insertados += 1
            except Exception as exc:
                fallidos += 1
                self.stderr.write(f"  OS {row['ingreso_id']} -> {row['a_estado']}: {exc}")

        self.stdout.write(f"Eventos insertados: {insertados}")
        if fallidos:
            self.stdout.write(f"Eventos que fallaron: {fallidos}")


def _fetch_faltantes(estados, ingreso_id):
    """Primer cambio a cada estado según el audit log, para las OS que no tienen el evento.

    Se toma el primero (`DISTINCT ON ... ORDER BY ts`) porque los hitos de diagnóstico y
    reparación se leen con MIN: reponer sólo esa transición alcanza y evita duplicar
    idas y vueltas del mismo equipo.
    """
    params = [list(estados)]
    extra = ""
    if ingreso_id:
        extra = "AND cl.record_id = %s"
        params.append(ingreso_id)

    sql = f"""
        WITH cambios AS (
          SELECT DISTINCT ON (cl.record_id, LOWER(cl.new_value))
                 cl.record_id AS ingreso_id,
                 LOWER(cl.new_value) AS a_estado,
                 COALESCE(LOWER(cl.old_value), '') AS de_estado,
                 cl.user_id,
                 cl.ts
            FROM audit.change_log cl
            JOIN ingresos i ON i.id = cl.record_id
           WHERE cl.table_name = 'ingresos'
             AND cl.column_name = 'estado'
             AND LOWER(COALESCE(cl.new_value, '')) = ANY(%s)
             {extra}
           ORDER BY cl.record_id, LOWER(cl.new_value), cl.ts, cl.id
        )
        SELECT c.ingreso_id, c.a_estado, c.de_estado, c.user_id, c.ts
          FROM cambios c
         WHERE NOT EXISTS (
           SELECT 1 FROM ingreso_events e
            WHERE e.ingreso_id = c.ingreso_id
              AND CAST(e.a_estado AS TEXT) = c.a_estado
         )
         ORDER BY c.ingreso_id, c.ts
    """

    with connection.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for row in rows:
        if row["de_estado"] not in TICKET_STATES:
            row["de_estado"] = ""
    return rows
