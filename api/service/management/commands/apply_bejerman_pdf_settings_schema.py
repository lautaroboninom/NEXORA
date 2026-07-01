from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = ["bejerman_pdf_output_settings"]


class Command(BaseCommand):
    help = "Aplica el esquema de configuración de carpetas PDF de Bejerman"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_bejerman_pdf_settings_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
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
                    CREATE TABLE IF NOT EXISTS bejerman_pdf_output_settings (
                      id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      company_key    TEXT NOT NULL,
                      document_kind  TEXT NOT NULL,
                      output_dir     TEXT NOT NULL DEFAULT '',
                      updated_by     INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_pdf_output_settings_company
                        CHECK (NULLIF(TRIM(company_key), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_output_settings_kind
                        CHECK (document_kind IN ('REMITOS','FACTURAS')),
                      CONSTRAINT chk_bejerman_pdf_output_settings_dir
                        CHECK (output_dir IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE bejerman_pdf_output_settings
                    DROP CONSTRAINT IF EXISTS chk_bejerman_pdf_output_settings_company
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE bejerman_pdf_output_settings
                    ADD CONSTRAINT chk_bejerman_pdf_output_settings_company
                    CHECK (NULLIF(TRIM(company_key), '') IS NOT NULL)
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_pdf_output_settings_company_kind
                      ON bejerman_pdf_output_settings(company_key, document_kind)
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                          FROM pg_trigger
                         WHERE tgname = 'trg_bejerman_pdf_output_settings_set_updated_at'
                      ) THEN
                        CREATE TRIGGER trg_bejerman_pdf_output_settings_set_updated_at
                        BEFORE UPDATE ON bejerman_pdf_output_settings
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
                found = {row[0] for row in cur.fetchall()}
                missing = [name for name in TABLES if name not in found]
                if missing:
                    raise RuntimeError(f"No se aplicaron tablas requeridas: {', '.join(missing)}")

                required_columns = {
                    "id",
                    "company_key",
                    "document_kind",
                    "output_dir",
                    "updated_by",
                    "created_at",
                    "updated_at",
                }
                placeholders = ",".join(["%s"] * len(required_columns))
                cur.execute(
                    f"""
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'bejerman_pdf_output_settings'
                       AND column_name IN ({placeholders})
                    """,
                    list(required_columns),
                )
                found_columns = {row[0] for row in cur.fetchall()}
                missing_columns = sorted(required_columns - found_columns)
                if missing_columns:
                    raise RuntimeError(
                        "Faltan columnas en bejerman_pdf_output_settings: " + ", ".join(missing_columns)
                    )

        self.stdout.write(self.style.SUCCESS("Esquema Bejerman PDF settings aplicado"))
