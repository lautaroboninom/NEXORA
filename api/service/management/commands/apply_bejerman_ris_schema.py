from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = ["bejerman_ingreso_remitos"]


class Command(BaseCommand):
    help = "Aplica el esquema de remitos RIS de ingreso Bejerman"

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        CREATE OR REPLACE FUNCTION set_updated_at()
                        RETURNS TRIGGER AS $$
                        BEGIN
                          NEW.updated_at := CURRENT_TIMESTAMP;
                          RETURN NEW;
                        END;
                        $$ LANGUAGE plpgsql;
                        """
                    )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_ingreso_remitos (
                      id                     INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      ingreso_id             INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                      status                 TEXT NOT NULL DEFAULT 'pending',
                      pdf_status             TEXT NOT NULL DEFAULT 'pending',
                      attempts               INTEGER NOT NULL DEFAULT 0,
                      last_error             TEXT NULL,
                      request_payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
                      response_payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
                      comprobante_tipo       TEXT NULL,
                      comprobante_letra      TEXT NULL,
                      comprobante_pto_venta  TEXT NULL,
                      comprobante_numero     TEXT NULL,
                      remito_number          TEXT NULL,
                      customer_code          TEXT NULL,
                      customer_name          TEXT NULL,
                      issue_date             DATE NULL,
                      generated_at           TIMESTAMPTZ NULL,
                      created_by             INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_ingreso_remitos_status
                        CHECK (status IN ('pending','running','generated','failed')),
                      CONSTRAINT chk_bejerman_ingreso_remitos_pdf_status
                        CHECK (pdf_status IN ('pending','ready','failed')),
                      CONSTRAINT chk_bejerman_ingreso_remitos_attempts CHECK (attempts >= 0)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_ingreso_remitos_ingreso
                      ON bejerman_ingreso_remitos(ingreso_id)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_ingreso_remitos_status
                      ON bejerman_ingreso_remitos(status, pdf_status, updated_at)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_ingreso_remitos_remito
                      ON bejerman_ingreso_remitos(comprobante_tipo, comprobante_pto_venta, comprobante_numero)
                    """
                )

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_ingreso_remitos_set_updated_at') THEN
                            CREATE TRIGGER trg_bejerman_ingreso_remitos_set_updated_at
                            BEFORE UPDATE ON bejerman_ingreso_remitos
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                        END $$;
                        """
                    )

                placeholders = ",".join(["%s"] * len(TABLES))
                cur.execute(
                    f"""
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name IN ({placeholders})
                    """,
                    TABLES,
                )
                found = {r[0] for r in cur.fetchall()}
                missing = [name for name in TABLES if name not in found]
                if missing:
                    raise RuntimeError(f"No se aplicaron tablas requeridas: {', '.join(missing)}")

                required_columns = {
                    "bejerman_ingreso_remitos": {
                        "ingreso_id",
                        "status",
                        "pdf_status",
                        "attempts",
                        "request_payload",
                        "response_payload",
                        "comprobante_tipo",
                        "comprobante_letra",
                        "comprobante_pto_venta",
                        "comprobante_numero",
                        "remito_number",
                        "issue_date",
                    },
                }
                for table_name, columns in required_columns.items():
                    placeholders = ",".join(["%s"] * len(columns))
                    cur.execute(
                        f"""
                        SELECT column_name
                          FROM information_schema.columns
                         WHERE table_schema = ANY(current_schemas(true))
                           AND table_name = %s
                           AND column_name IN ({placeholders})
                        """,
                        [table_name, *columns],
                    )
                    found_columns = {r[0] for r in cur.fetchall()}
                    missing_columns = sorted(columns - found_columns)
                    if missing_columns:
                        raise RuntimeError(
                            f"No se aplicaron columnas requeridas en {table_name}: {', '.join(missing_columns)}"
                        )

        self.stdout.write("APLICADO OK: esquema de RIS de ingreso Bejerman")
