"""Rescata las OS que quedaron atascadas en 'vendido_pendiente_entrega'.

Registrar la venta de un MG desde la hoja de servicio dejaba dos secuelas (las dos
corregidas en `devices_views.py` el 08/09/2026, pero las filas viejas siguen mal):

1. La OS se mandaba a la ubicacion '-'. Todas las colas filtran por ubicacion 'taller',
   asi que la OS desaparecia de la aplicacion entera: ni "Pendientes", ni "Listos para
   liberar", ni "Listos" -que si contempla 'vendido_pendiente_entrega' en su WHERE-.
2. El estado nuevo se decidia mirando solo `alquilado`, asi que una OS **ya entregada**
   volvia para atras a "pendiente de entrega" con el equipo en poder del cliente.

Que hace este comando, decidido con el historial de la propia OS y no a ojo:

- Si el evento que la llevo a 'vendido_pendiente_entrega' venia de 'entregado' o
  'vendido_entregado', la venta se registro sobre una entrega ya hecha: la OS se cierra
  como 'vendido_entregado' y se va a la ubicacion '-'.
- Si no, el equipo sigue aca esperando el RT: la OS vuelve al **taller** para que aparezca
  en "Listos" y alguien la cierre.

Sin `--apply` no escribe nada.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction


ESTADO_PENDIENTE = "vendido_pendiente_entrega"
ESTADO_ENTREGADO = "vendido_entregado"
# Estados desde los que una venta significa "el cliente ya lo tiene".
ESTADOS_YA_ENTREGADOS = ("entregado", "vendido_entregado")


def _fetchall_dicts(cur):
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _location_id(cur, *, nombre_exacto=None, nombre_lower=None):
    if nombre_exacto is not None:
        cur.execute("SELECT id FROM locations WHERE nombre=%s ORDER BY id LIMIT 1", [nombre_exacto])
    else:
        cur.execute("SELECT id FROM locations WHERE LOWER(nombre)=%s ORDER BY id LIMIT 1", [nombre_lower])
    row = cur.fetchone()
    return int(row[0]) if row else None


class Command(BaseCommand):
    help = "Devuelve a una cola visible las OS trabadas en 'vendido_pendiente_entrega'."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persiste los cambios. Sin este flag solo informa.",
        )
        parser.add_argument(
            "--ids",
            default="",
            help="Limita la corrida a estas OS (separadas por coma).",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        solo_ids = [int(x) for x in (options.get("ids") or "").replace(" ", "").split(",") if x]

        with connection.cursor() as cur:
            taller_id = _location_id(cur, nombre_lower="taller")
            dash_id = _location_id(cur, nombre_exacto="-")
            if not taller_id:
                self.stderr.write("No existe la ubicacion 'Taller'. Nada que hacer.")
                return

            filtro_ids = "AND t.id = ANY(%s)" if solo_ids else ""
            params = [ESTADO_PENDIENTE]
            if solo_ids:
                params.append(solo_ids)
            cur.execute(
                f"""
                SELECT t.id,
                       t.ubicacion_id,
                       COALESCE(loc.nombre, '(sin ubicacion)') AS ubicacion_nombre,
                       t.fecha_entrega,
                       COALESCE(t.remito_salida, '') AS remito_salida,
                       COALESCE(d.numero_interno, '') AS numero_interno,
                       COALESCE(d.numero_serie, '') AS numero_serie,
                       ev.de_estado AS venia_de
                  FROM ingresos t
                  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                  LEFT JOIN devices d ON d.id = t.device_id
                  LEFT JOIN LATERAL (
                      SELECT e.de_estado
                        FROM ingreso_events e
                       WHERE e.ticket_id = t.id
                         AND e.a_estado::text = %s
                       ORDER BY e.ts DESC, e.id DESC
                       LIMIT 1
                  ) ev ON TRUE
                 WHERE t.estado::text = %s
                   {filtro_ids}
                 ORDER BY t.id
                """,
                [ESTADO_PENDIENTE, *params],
            )
            candidatos = _fetchall_dicts(cur)

        if not candidatos:
            self.stdout.write("No hay OS en 'vendido_pendiente_entrega'.")
            return

        cerrar, al_taller, sin_cambios = [], [], []
        for row in candidatos:
            venia_de = (row.get("venia_de") or "").strip().lower()
            if venia_de in ESTADOS_YA_ENTREGADOS:
                cerrar.append(row)
            elif int(row.get("ubicacion_id") or 0) != taller_id:
                al_taller.append(row)
            else:
                sin_cambios.append(row)

        def _describe(row):
            equipo = row.get("numero_interno") or row.get("numero_serie") or "?"
            return (
                f"  OS {row['id']:>6}  {equipo:<16} ubicacion={row['ubicacion_nombre']:<24}"
                f" venia_de={row.get('venia_de') or '(sin evento)'}"
            )

        self.stdout.write(f"OS en '{ESTADO_PENDIENTE}': {len(candidatos)}.")
        self.stdout.write("")
        self.stdout.write(f"[cerrar como {ESTADO_ENTREGADO}] la venta se registro sobre una entrega ya hecha: {len(cerrar)}")
        for row in cerrar:
            self.stdout.write(_describe(row))
        self.stdout.write(f"[mover al taller] el equipo sigue aca esperando el RT: {len(al_taller)}")
        for row in al_taller:
            self.stdout.write(_describe(row))
        if sin_cambios:
            self.stdout.write(f"[sin cambios] ya estan en el taller: {len(sin_cambios)}")
            for row in sin_cambios:
                self.stdout.write(_describe(row))

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write("Dry-run: no se escribio nada. Volve a correrlo con --apply.")
            return

        if cerrar and not dash_id:
            self.stderr.write("No existe la ubicacion '-': no se pueden cerrar las ventas ya entregadas.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                for row in cerrar:
                    cur.execute(
                        """
                        UPDATE ingresos
                           SET estado = %s::ticket_state,
                               ubicacion_id = %s
                         WHERE id = %s
                           AND estado::text = %s
                        """,
                        [ESTADO_ENTREGADO, dash_id, row["id"], ESTADO_PENDIENTE],
                    )
                    cur.execute(
                        """
                        INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, comentario)
                        VALUES (%s, %s::ticket_state, %s::ticket_state, %s)
                        """,
                        [
                            row["id"],
                            ESTADO_PENDIENTE,
                            ESTADO_ENTREGADO,
                            "Correccion: la venta se habia registrado sobre una entrega ya hecha",
                        ],
                    )
                for row in al_taller:
                    cur.execute(
                        """
                        UPDATE ingresos
                           SET ubicacion_id = %s
                         WHERE id = %s
                           AND estado::text = %s
                        """,
                        [taller_id, row["id"], ESTADO_PENDIENTE],
                    )

        self.stdout.write("")
        self.stdout.write(
            f"Aplicado: {len(cerrar)} cerradas como {ESTADO_ENTREGADO}, {len(al_taller)} devueltas al taller."
        )
