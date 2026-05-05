from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


RENAME_MOTIVO_INGRESO_UTF8_SQL = """
DO $$
DECLARE
  bad_reparacion TEXT := convert_from(decode('7265706172616369c383c2b36e', 'hex'), 'UTF8');
  bad_reparacion_alquiler TEXT := convert_from(decode('7265706172616369c383c2b36e20616c7175696c6572', 'hex'), 'UTF8');
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
    IF EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = bad_reparacion
    ) THEN
      IF EXISTS (
        SELECT 1
          FROM pg_type t
          JOIN pg_enum e ON e.enumtypid = t.oid
         WHERE t.typname = 'motivo_ingreso'
           AND e.enumlabel = 'reparación'
      ) THEN
        RAISE EXCEPTION 'No se puede renombrar motivo_ingreso: ya existe el valor correcto reparación.';
      END IF;

      EXECUTE format(
        'ALTER TYPE motivo_ingreso RENAME VALUE %L TO %L',
        bad_reparacion,
        'reparación'
      );
    END IF;

    IF EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = bad_reparacion_alquiler
    ) THEN
      IF EXISTS (
        SELECT 1
          FROM pg_type t
          JOIN pg_enum e ON e.enumtypid = t.oid
         WHERE t.typname = 'motivo_ingreso'
           AND e.enumlabel = 'reparación alquiler'
      ) THEN
        RAISE EXCEPTION 'No se puede renombrar motivo_ingreso: ya existe el valor correcto reparación alquiler.';
      END IF;

      EXECUTE format(
        'ALTER TYPE motivo_ingreso RENAME VALUE %L TO %L',
        bad_reparacion_alquiler,
        'reparación alquiler'
      );
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


MOTIVO_INGRESO_BAD_LABELS_SQL = """
SELECT e.enumlabel
  FROM pg_type t
  JOIN pg_enum e ON e.enumtypid = t.oid
 WHERE t.typname = 'motivo_ingreso'
   AND (
        POSITION(chr(195) IN e.enumlabel) > 0
     OR POSITION(chr(194) IN e.enumlabel) > 0
     OR POSITION(chr(226) IN e.enumlabel) > 0
     OR POSITION(chr(65533) IN e.enumlabel) > 0
   )
 ORDER BY e.enumsortorder
"""


class Command(BaseCommand):
    help = "Corrige etiquetas UTF-8 del enum motivo_ingreso."

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(RENAME_MOTIVO_INGRESO_UTF8_SQL)
                cur.execute(MOTIVO_INGRESO_BAD_LABELS_SQL)
                bad_labels = [row[0] for row in cur.fetchall()]
                if bad_labels:
                    raise CommandError(
                        "Quedaron etiquetas con mojibake en motivo_ingreso: "
                        + ", ".join(bad_labels)
                    )
                cur.execute(MOTIVO_INGRESO_LABELS_SQL)
                labels = [row[0] for row in cur.fetchall()]

        self.stdout.write(
            "APLICADO OK: motivo_ingreso con etiquetas UTF-8 válidas: "
            + ", ".join(labels)
        )
