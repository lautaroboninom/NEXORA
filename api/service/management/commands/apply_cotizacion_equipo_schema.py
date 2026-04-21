from django.core.management.base import BaseCommand
from django.db import connection, transaction


SYNC_QUOTE_WITH_INGRESO_SQL = """
CREATE OR REPLACE FUNCTION sync_quote_with_ingreso()
RETURNS TRIGGER AS $$
DECLARE
  v_cur_estado ticket_state;
  v_new_estado quote_estado;
  v_permite_reparacion BOOLEAN := TRUE;
  v_motivo TEXT := '';
BEGIN
  v_new_estado := NEW.estado;
  SELECT
    estado,
    COALESCE(permite_reparacion, TRUE),
    COALESCE(CAST(motivo AS TEXT), '')
  INTO v_cur_estado, v_permite_reparacion, v_motivo
  FROM ingresos
  WHERE id = NEW.ingreso_id;

  UPDATE ingresos
     SET presupuesto_estado = (
            CASE v_new_estado
              WHEN 'emitido' THEN 'presupuestado'::quote_estado
              WHEN 'presupuestado' THEN 'presupuestado'::quote_estado
              WHEN 'aprobado' THEN 'aprobado'::quote_estado
              WHEN 'rechazado' THEN 'rechazado'::quote_estado
              WHEN 'no_aplica' THEN 'no_aplica'::quote_estado
              ELSE 'pendiente'::quote_estado
            END
         ),
         estado = (
            CASE
              WHEN v_new_estado = 'aprobado'
                   AND v_cur_estado IN ('ingresado','diagnosticado','presupuestado')
                   AND NOT (
                     LOWER(v_motivo) IN ('cotización de equipo', 'cotizacion de equipo')
                     AND NOT COALESCE(v_permite_reparacion, TRUE)
                   )
              THEN 'reparar'::ticket_state
              ELSE v_cur_estado
            END
         )
   WHERE id = NEW.ingreso_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Command(BaseCommand):
    help = "Aplica schema para motivo 'Cotización de equipo' y bloqueo de reparación."

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
                        IF NOT EXISTS (
                          SELECT 1
                            FROM pg_type t
                            JOIN pg_enum e ON e.enumtypid = t.oid
                           WHERE t.typname = 'motivo_ingreso'
                             AND e.enumlabel = 'cotización de equipo'
                        ) THEN
                          ALTER TYPE motivo_ingreso ADD VALUE 'cotización de equipo';
                        END IF;
                      END IF;
                    END
                    $$;
                    """
                )

                cur.execute(
                    """
                    ALTER TABLE ingresos
                    ADD COLUMN IF NOT EXISTS permite_reparacion BOOLEAN NOT NULL DEFAULT TRUE
                    """
                )

                cur.execute(
                    """
                    UPDATE ingresos
                       SET permite_reparacion = FALSE
                     WHERE LOWER(CAST(motivo AS TEXT)) IN ('cotización de equipo', 'cotizacion de equipo')
                    """
                )

                cur.execute(SYNC_QUOTE_WITH_INGRESO_SQL)

        self.stdout.write(
            "APLICADO OK: motivo_ingreso(cotización de equipo) + ingresos.permite_reparacion + sync_quote_with_ingreso."
        )
