from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = ["bejerman_sale_items"]


class Command(BaseCommand):
    help = "Aplica el esquema de caché de renglones de venta Bejerman"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_bejerman_sale_items_schema requiere PostgreSQL")

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
                    CREATE TABLE IF NOT EXISTS bejerman_sale_items (
                      sale_item_key          TEXT PRIMARY KEY,
                      company_key            TEXT NOT NULL DEFAULT 'SEPID',
                      comprobante_id         TEXT NOT NULL DEFAULT '',
                      document_id            TEXT NULL,
                      document_type          TEXT NOT NULL DEFAULT '',
                      document_letter        TEXT NOT NULL DEFAULT '',
                      document_point_of_sale TEXT NOT NULL DEFAULT '',
                      document_number        TEXT NOT NULL DEFAULT '',
                      document_label         TEXT NOT NULL DEFAULT '',
                      issue_date             DATE NULL,
                      customer_code          TEXT NOT NULL DEFAULT '',
                      customer_name          TEXT NOT NULL DEFAULT '',
                      customer_cuit          TEXT NOT NULL DEFAULT '',
                      article_code           TEXT NOT NULL DEFAULT '',
                      article_description    TEXT NOT NULL DEFAULT '',
                      item_partida           TEXT NOT NULL DEFAULT '',
                      normalized_serial      TEXT NOT NULL DEFAULT '',
                      quantity               NUMERIC NULL,
                      unit_price             NUMERIC NULL,
                      total_amount           NUMERIC NULL,
                      currency               TEXT NOT NULL DEFAULT '',
                      line_index             INTEGER NOT NULL DEFAULT 0,
                      raw_document           JSONB NOT NULL DEFAULT '{}'::jsonb,
                      raw_item               JSONB NOT NULL DEFAULT '{}'::jsonb,
                      synced_at              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_sale_items_serial CHECK (NULLIF(TRIM(normalized_serial), '') IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sale_items_serial_date
                      ON bejerman_sale_items(company_key, normalized_serial, issue_date DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sale_items_normalized_serial
                      ON bejerman_sale_items(normalized_serial)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sale_items_customer_cuit
                      ON bejerman_sale_items(customer_cuit)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sale_items_customer_code
                      ON bejerman_sale_items(customer_code)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sale_items_article_code
                      ON bejerman_sale_items(article_code)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sale_items_document
                      ON bejerman_sale_items(document_type, document_letter, document_point_of_sale, document_number)
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_sale_items_set_updated_at') THEN
                        CREATE TRIGGER trg_bejerman_sale_items_set_updated_at
                        BEFORE UPDATE ON bejerman_sale_items
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
                    "sale_item_key",
                    "company_key",
                    "comprobante_id",
                    "document_label",
                    "issue_date",
                    "customer_code",
                    "customer_name",
                    "customer_cuit",
                    "article_code",
                    "article_description",
                    "item_partida",
                    "normalized_serial",
                    "quantity",
                    "raw_document",
                    "raw_item",
                    "synced_at",
                }
                placeholders = ",".join(["%s"] * len(required_columns))
                cur.execute(
                    f"""
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'bejerman_sale_items'
                       AND column_name IN ({placeholders})
                    """,
                    list(required_columns),
                )
                found_columns = {row[0] for row in cur.fetchall()}
                missing_columns = sorted(required_columns - found_columns)
                if missing_columns:
                    raise RuntimeError(
                        f"No se aplicaron columnas requeridas en bejerman_sale_items: {', '.join(missing_columns)}"
                    )

        self.stdout.write("APLICADO OK: esquema de ventas Bejerman")
