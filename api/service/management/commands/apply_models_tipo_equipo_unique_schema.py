from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Cambia la unicidad de models de (marca, nombre) a (marca, nombre, tipo de equipo) "
        "para que dos equipos distintos puedan compartir el nombre de modelo "
        "(ej. el CPAP 'DM26' y el calentador humidificador 'DM26' que va con él)."
    )

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT marca_id, LOWER(TRIM(nombre)) AS nombre,
                       LOWER(COALESCE(TRIM(tipo_equipo), '')) AS tipo,
                       COUNT(*) AS cnt
                FROM models
                GROUP BY 1, 2, 3
                HAVING COUNT(*) > 1
                """
            )
            duplicados = cur.fetchall()

        if duplicados:
            self.stderr.write(
                "ABORTADO: hay modelos duplicados con la misma marca+nombre+tipo. "
                "Unificarlos primero (marca/modelo -> Unificar):"
            )
            for marca_id, nombre, tipo, cnt in duplicados:
                self.stderr.write(f"  marca_id={marca_id} nombre={nombre!r} tipo={tipo!r} x{cnt}")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("ALTER TABLE models DROP CONSTRAINT IF EXISTS uq_models_marca_nombre")
                cur.execute("DROP INDEX IF EXISTS uq_models_marca_nombre_ci")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_models_marca_nombre_tipo_ci
                      ON models (marca_id, (LOWER(nombre)), (LOWER(COALESCE(TRIM(tipo_equipo), ''))))
                    """
                )

        self.stdout.write("APLICADO OK: models ahora es unico por marca + nombre + tipo de equipo")
