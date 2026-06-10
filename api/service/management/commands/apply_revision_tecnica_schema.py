from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


ADD_REVISION_TECNICA_SQL = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
    IF NOT EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = 'Revisión Técnica'
    ) THEN
      ALTER TYPE motivo_ingreso ADD VALUE 'Revisión Técnica';
    END IF;
  END IF;
END
$$;
"""


MOTIVO_INGRESO_LABELS_SQL = """
SELECT e.enumlabel
  FROM pg_type t
  JOIN pg_enum e ON e.enumtypid = t.oid
 WHERE t.typname = 'motivo_ingreso'
 ORDER BY e.enumsortorder
"""


class Command(BaseCommand):
    help = "Agrega el motivo 'Revisión Técnica' al enum motivo_ingreso."

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(ADD_REVISION_TECNICA_SQL)
                cur.execute(MOTIVO_INGRESO_LABELS_SQL)
                labels = [row[0] for row in cur.fetchall()]

        if "Revisión Técnica" not in labels:
            raise CommandError("No se pudo aplicar el motivo 'Revisión Técnica' en motivo_ingreso.")

        self.stdout.write(
            "APLICADO OK: motivo_ingreso incluye 'Revisión Técnica'. Valores actuales: "
            + ", ".join(labels)
        )
