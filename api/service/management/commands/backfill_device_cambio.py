import csv

from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Vincula los cambios de equipo históricos: busca en `devices` la serie que quedó "
        "guardada como texto en `ingresos.serial_cambio` y completa `device_cambio_id`. "
        "Los que no matchean unívocamente quedan sin vincular y se reportan."
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
            help="Archivo donde volcar los casos que no se pudieron vincular.",
        )

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        csv_path = (opts.get("csv_path") or "").strip()

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_name='ingresos' AND column_name='device_cambio_id'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """
            )
            if cur.fetchone() is None:
                self.stderr.write(
                    "Falta la columna device_cambio_id. Ejecutá apply_device_swap_schema primero."
                )
                return

        # El match usa la misma normalización que el índice único `uq_devices_ns_tipo_norm`
        # (UPPER, sin espacios ni guiones). El snapshot puede venir como "NS | MG 0001",
        # así que se prueba cada parte por separado, contra serie y contra número interno.
        pendientes = _fetch_pendientes()
        vinculados = 0
        ambiguos = []
        sin_match = []

        for row in pendientes:
            ingreso_id = row["id"]
            candidatos = _match_devices(row["serial_cambio"], row["device_id"])
            if len(candidatos) == 1:
                vinculados += 1
                if apply_changes:
                    with transaction.atomic():
                        with connection.cursor() as cur:
                            cur.execute(
                                "UPDATE ingresos SET device_cambio_id=%s WHERE id=%s",
                                [candidatos[0], ingreso_id],
                            )
            elif len(candidatos) > 1:
                ambiguos.append((ingreso_id, row["serial_cambio"], candidatos))
            else:
                sin_match.append((ingreso_id, row["serial_cambio"]))

        self.stdout.write(f"Cambios históricos sin vincular: {len(pendientes)}")
        self.stdout.write(f"  Vinculados{'' if apply_changes else ' (dry-run)'}: {vinculados}")
        self.stdout.write(f"  Ambiguos (más de un equipo con esa serie): {len(ambiguos)}")
        self.stdout.write(f"  Sin equipo que matchee: {len(sin_match)}")

        if csv_path:
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ingreso_id", "serial_cambio", "motivo", "device_ids"])
                for ingreso_id, serial, candidatos in ambiguos:
                    writer.writerow([ingreso_id, serial, "ambiguo", " ".join(map(str, candidatos))])
                for ingreso_id, serial in sin_match:
                    writer.writerow([ingreso_id, serial, "sin_match", ""])
            self.stdout.write(f"Reporte de casos a revisar: {csv_path}")
        elif ambiguos or sin_match:
            self.stdout.write("Pasá --csv <ruta> para listar los casos a revisar a mano.")

        if not apply_changes:
            self.stdout.write("DRY-RUN: no se escribió nada. Repetí con --apply.")


def _fetch_pendientes():
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, device_id, COALESCE(serial_cambio,'') AS serial_cambio
              FROM ingresos
             WHERE LOWER(COALESCE(resolucion,'')) = 'cambio'
               AND device_cambio_id IS NULL
               AND NULLIF(TRIM(COALESCE(serial_cambio,'')), '') IS NOT NULL
             ORDER BY id
            """
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _norm(value: str) -> str:
    return (value or "").upper().replace(" ", "").replace("-", "").strip()


def _match_devices(serial_cambio: str, device_id) -> list:
    """IDs de devices que matchean el texto guardado. Vacío o >1 => revisión manual."""
    partes = [p.strip() for p in (serial_cambio or "").split("|") if p.strip()]
    encontrados = []
    for parte in partes:
        clave = _norm(parte)
        if not clave:
            continue
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM devices
                 WHERE id <> COALESCE(%s, 0)
                   AND (
                     UPPER(REPLACE(REPLACE(COALESCE(numero_serie,''),   ' ', ''), '-', '')) = %s
                     OR UPPER(REPLACE(REPLACE(COALESCE(numero_interno,''), ' ', ''), '-', '')) = %s
                   )
                """,
                [device_id, clave, clave],
            )
            encontrados.extend(r[0] for r in cur.fetchall())
    return sorted(set(encontrados))
