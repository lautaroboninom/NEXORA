from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.views.tipo_equipo_utils import sentence_case_tipo_equipo

# (tabla, columna id, columna a normalizar)
_TARGETS = [
    ("catalogo_tipos_equipo", "id", "nombre"),
    ("marca_tipos_equipo", "id", "nombre"),
    ("models", "id", "tipo_equipo"),
    ("devices", "id", "tipo_equipo"),
]


class Command(BaseCommand):
    help = (
        "Normaliza tipo_equipo de MAYUSCULA SOSTENIDA a 'Primera letra mayuscula' en "
        "catalogo_tipos_equipo, marca_tipos_equipo, models y devices (preserva siglas "
        "como CPAP/BPAP/AVAPS). No corrige acentos ni typos, solo may/minuscula."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Muestra cambios sin aplicar (default)")
        parser.add_argument("--apply", action="store_true", help="Aplica cambios en BD")
        parser.add_argument(
            "--sample",
            type=int,
            default=8,
            help="Cantidad de ejemplos de cambio a imprimir por tabla (default 8)",
        )

    def handle(self, *args, **opts):
        dry = True
        if opts.get("apply"):
            dry = False
        if opts.get("dry_run"):
            dry = True
        sample_n = opts.get("sample") or 0

        with transaction.atomic():
            with connection.cursor() as cur:
                results = []
                for table, id_col, col in _TARGETS:
                    cur.execute(
                        f"SELECT {id_col}, {col} FROM {table} "
                        f"WHERE {col} IS NOT NULL AND TRIM({col}) <> ''"
                    )
                    rows = cur.fetchall()
                    changes = []
                    for row_id, value in rows:
                        proposed = sentence_case_tipo_equipo(value)
                        if proposed != value:
                            changes.append((row_id, value, proposed))

                    if not dry:
                        for row_id, _old, proposed in changes:
                            cur.execute(
                                f"UPDATE {table} SET {col}=%s WHERE {id_col}=%s",
                                [proposed, row_id],
                            )

                    results.append((table, col, len(rows), changes))

                if dry:
                    transaction.set_rollback(True)

        self.stdout.write(("DRY-RUN " if dry else "APLICADO ") + "tipo_equipo -> capitalizacion normal")
        for table, col, total, changes in results:
            self.stdout.write(f"  {table}.{col}: {len(changes)}/{total} filas cambian")
            for row_id, old, proposed in changes[:sample_n]:
                self.stdout.write(f"      [{row_id}] {old!r} -> {proposed!r}")
            if len(changes) > sample_n:
                self.stdout.write(f"      ... y {len(changes) - sample_n} mas")
