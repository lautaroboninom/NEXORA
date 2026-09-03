from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


INDEX_NAME = "uq_equipos_derivados_ingreso_abierto"


class Command(BaseCommand):
    help = (
        "Asegura una única derivación activa por OS, considerando los estados "
        "'derivado' y 'en_servicio'."
    )

    def handle(self, *args, **options):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ingreso_id, COUNT(*) AS cantidad
                      FROM equipos_derivados
                     WHERE fecha_entrega IS NULL
                       AND estado IN ('derivado','en_servicio')
                     GROUP BY ingreso_id
                    HAVING COUNT(*) > 1
                     ORDER BY ingreso_id
                    """
                )
                duplicates = cursor.fetchall()
                if duplicates:
                    detail = ", ".join(f"OS {row[0]} ({row[1]})" for row in duplicates)
                    raise CommandError(
                        "No se puede crear el índice: existen derivaciones activas duplicadas: "
                        f"{detail}."
                    )

                if connection.vendor == "postgresql":
                    cursor.execute(
                        """
                        SELECT pg_get_expr(index_definition.indpred, index_definition.indrelid)
                          FROM pg_index index_definition
                          JOIN pg_class index_class ON index_class.oid = index_definition.indexrelid
                         WHERE index_class.relname = %s
                        """,
                        [INDEX_NAME],
                    )
                    row = cursor.fetchone()
                    current_predicate = str(row[0] or "").lower() if row else ""
                    if row and "en_servicio" not in current_predicate:
                        cursor.execute(f'DROP INDEX "{INDEX_NAME}"')

                cursor.execute(
                    f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
                      ON equipos_derivados(ingreso_id)
                     WHERE fecha_entrega IS NULL
                       AND estado IN ('derivado','en_servicio')
                    """
                )

                if connection.vendor == "postgresql":
                    cursor.execute(
                        f"""
                        COMMENT ON INDEX {INDEX_NAME} IS
                        'Una sola derivación activa por OS; el estado operativo vive en ingresos.estado.'
                        """
                    )
                    cursor.execute(
                        """
                        SELECT pg_get_expr(index_definition.indpred, index_definition.indrelid)
                          FROM pg_index index_definition
                          JOIN pg_class index_class ON index_class.oid = index_definition.indexrelid
                         WHERE index_class.relname = %s
                        """,
                        [INDEX_NAME],
                    )
                    verified = cursor.fetchone()
                    predicate = str(verified[0] or "").lower() if verified else ""
                    if "en_servicio" not in predicate or "fecha_entrega" not in predicate:
                        raise CommandError("El índice de derivación activa no quedó aplicado correctamente.")

        self.stdout.write(
            self.style.SUCCESS(
                "APLICADO OK: una única derivación activa por OS para derivado/en_servicio."
            )
        )
