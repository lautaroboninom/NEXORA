from django.core.management.base import BaseCommand
from django.db import connection, transaction


SYNC_QUOTE_WITH_INGRESO_SQL = """
CREATE OR REPLACE FUNCTION sync_quote_with_ingreso()
RETURNS TRIGGER AS $$
DECLARE
  v_ingreso_id INTEGER;
  v_cur_estado ticket_state;
  v_quote_estado quote_estado := 'pendiente'::quote_estado;
  v_permite_reparacion BOOLEAN := TRUE;
  v_motivo TEXT := '';
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_ingreso_id := OLD.ingreso_id;
  ELSE
    v_ingreso_id := NEW.ingreso_id;
  END IF;

  SELECT
    estado,
    COALESCE(permite_reparacion, TRUE),
    COALESCE(CAST(motivo AS TEXT), '')
  INTO v_cur_estado, v_permite_reparacion, v_motivo
  FROM ingresos
  WHERE id = v_ingreso_id;

  SELECT q.estado
    INTO v_quote_estado
    FROM quotes q
   WHERE q.ingreso_id = v_ingreso_id
   ORDER BY COALESCE(q.version_num, 1) DESC, q.id DESC
   LIMIT 1;

  v_quote_estado := COALESCE(v_quote_estado, 'pendiente'::quote_estado);

  UPDATE ingresos
     SET presupuesto_estado = (
            CASE v_quote_estado
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
              WHEN v_quote_estado = 'aprobado'
                   AND v_cur_estado IN ('ingresado', 'diagnosticado', 'presupuestado')
                   AND NOT (
                     LOWER(v_motivo) IN ('cotización de equipo', 'cotizacion de equipo')
                     AND NOT COALESCE(v_permite_reparacion, TRUE)
                   )
              THEN 'reparar'::ticket_state
              ELSE v_cur_estado
            END
         )
   WHERE id = v_ingreso_id;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Command(BaseCommand):
    help = "Aplica versionado de presupuestos en quotes"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INTEGER")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS origen_quote_id INTEGER")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS fecha_rechazado TIMESTAMPTZ")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS rechazo_comentario TEXT")

                cur.execute(
                    """
                    WITH ranked AS (
                      SELECT
                        id,
                        ROW_NUMBER() OVER (
                          PARTITION BY ingreso_id
                          ORDER BY COALESCE(version_num, 1), id
                        ) AS version_num_rank
                      FROM quotes
                    )
                    UPDATE quotes q
                       SET version_num = ranked.version_num_rank
                      FROM ranked
                     WHERE ranked.id = q.id
                       AND COALESCE(q.version_num, 0) <> ranked.version_num_rank
                    """
                )
                cur.execute("UPDATE quotes SET version_num = 1 WHERE version_num IS NULL")
                cur.execute("ALTER TABLE quotes ALTER COLUMN version_num SET DEFAULT 1")
                cur.execute("ALTER TABLE quotes ALTER COLUMN version_num SET NOT NULL")

                cur.execute("ALTER TABLE quotes DROP CONSTRAINT IF EXISTS uq_quotes_ingreso")
                cur.execute("DROP INDEX IF EXISTS uq_quotes_ingreso")
                cur.execute("DROP INDEX IF EXISTS quotes_ingreso_id_key")

                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                          FROM pg_constraint
                         WHERE conname = 'fk_quotes_origen_quote'
                      ) THEN
                        ALTER TABLE quotes
                          ADD CONSTRAINT fk_quotes_origen_quote
                          FOREIGN KEY (origen_quote_id)
                          REFERENCES quotes(id)
                          ON DELETE SET NULL;
                      END IF;
                    END
                    $$;
                    """
                )

                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_ingreso_version ON quotes(ingreso_id, version_num)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_quotes_ingreso_version_desc ON quotes(ingreso_id, version_num DESC, id DESC)"
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ix_quotes_rechazado ON quotes(fecha_rechazado)")

                cur.execute(SYNC_QUOTE_WITH_INGRESO_SQL)
                cur.execute("DROP TRIGGER IF EXISTS trg_quote_sync_ins ON quotes")
                cur.execute(
                    """
                    CREATE TRIGGER trg_quote_sync_ins
                    AFTER INSERT ON quotes
                    FOR EACH ROW EXECUTE FUNCTION sync_quote_with_ingreso()
                    """
                )
                cur.execute("DROP TRIGGER IF EXISTS trg_quote_sync_upd ON quotes")
                cur.execute(
                    """
                    CREATE TRIGGER trg_quote_sync_upd
                    AFTER UPDATE OF estado, subtotal, fecha_emitido, fecha_aprobado, fecha_rechazado, version_num ON quotes
                    FOR EACH ROW EXECUTE FUNCTION sync_quote_with_ingreso()
                    """
                )
                cur.execute("UPDATE quotes SET estado='presupuestado' WHERE estado::text='emitido'")
                cur.execute(
                    """
                    WITH current_quotes AS (
                      SELECT DISTINCT ON (ingreso_id)
                        ingreso_id,
                        estado
                      FROM quotes
                      ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
                    ),
                    normalized AS (
                      SELECT
                        ingreso_id,
                        CASE estado
                          WHEN 'emitido' THEN 'presupuestado'::quote_estado
                          WHEN 'presupuestado' THEN 'presupuestado'::quote_estado
                          WHEN 'aprobado' THEN 'aprobado'::quote_estado
                          WHEN 'rechazado' THEN 'rechazado'::quote_estado
                          WHEN 'no_aplica' THEN 'no_aplica'::quote_estado
                          ELSE 'pendiente'::quote_estado
                        END AS presupuesto_estado
                      FROM current_quotes
                    )
                    UPDATE ingresos i
                       SET presupuesto_estado = normalized.presupuesto_estado
                      FROM normalized
                     WHERE i.id = normalized.ingreso_id
                       AND i.presupuesto_estado IS DISTINCT FROM normalized.presupuesto_estado
                    """
                )

        self.stdout.write(
            "APLICADO OK: quotes versionadas por ingreso + sync_quote_with_ingreso actualizado."
        )
