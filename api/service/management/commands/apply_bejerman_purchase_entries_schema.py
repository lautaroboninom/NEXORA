from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = [
    "bejerman_purchase_entries",
    "bejerman_purchase_entry_lines",
    "bejerman_purchase_entry_scans",
    "bejerman_purchase_entry_events",
]

REQUIRED_COLUMNS = {
    "bejerman_purchase_entries": {
        "id",
        "company_key",
        "status",
        "comprobante_tipo",
        "comprobante_letra",
        "comprobante_pto_venta",
        "comprobante_numero",
        "supplier_code",
        "supplier_code_raw",
        "supplier_name",
        "payment_term_code",
        "operation_code",
        "deposit_code",
        "request_payload",
        "response_payload",
    },
    "bejerman_purchase_entry_lines": {
        "id",
        "entry_id",
        "article_code",
        "article_description",
        "default_conversion_factor",
        "default_unit_value",
        "deposit_code",
    },
    "bejerman_purchase_entry_scans": {
        "id",
        "entry_id",
        "line_id",
        "barcode",
        "barcode_norm",
        "article_code",
        "article_description",
        "conversion_factor",
        "unit_value",
        "total_value",
        "is_manual_quantity",
    },
    "bejerman_purchase_entry_events": {"id", "entry_id", "event_type", "metadata"},
}


class Command(BaseCommand):
    help = "Aplica el esquema de ingresos de mercadería Bejerman por compras"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_bejerman_purchase_entries_schema requiere PostgreSQL")

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
                    CREATE TABLE IF NOT EXISTS bejerman_purchase_entries (
                      id                         TEXT PRIMARY KEY,
                      company_key                TEXT NOT NULL DEFAULT 'SEPID',
                      status                     TEXT NOT NULL DEFAULT 'draft',
                      comprobante_tipo           TEXT NOT NULL DEFAULT 'RT',
                      comprobante_letra          TEXT NOT NULL DEFAULT 'R',
                      comprobante_pto_venta      TEXT NOT NULL DEFAULT '',
                      comprobante_numero         TEXT NOT NULL DEFAULT '',
                      remito_number              TEXT NULL,
                      supplier_code              TEXT NOT NULL DEFAULT '',
                      supplier_code_raw          TEXT NOT NULL DEFAULT '',
                      supplier_name              TEXT NOT NULL DEFAULT '',
                      supplier_tax_id            TEXT NOT NULL DEFAULT '',
                      supplier_snapshot          JSONB NOT NULL DEFAULT '{}'::jsonb,
                      payment_term_code          TEXT NOT NULL DEFAULT '',
                      issue_date                 DATE NOT NULL DEFAULT CURRENT_DATE,
                      accounting_date            DATE NULL,
                      ddjj_date                  DATE NULL,
                      operation_code             TEXT NOT NULL DEFAULT 'MC',
                      deposit_code               TEXT NOT NULL DEFAULT 'VAL',
                      notes                      TEXT NOT NULL DEFAULT '',
                      total_quantity             NUMERIC(14,4) NOT NULL DEFAULT 0,
                      total_value                NUMERIC(14,2) NOT NULL DEFAULT 0,
                      request_payload            JSONB NOT NULL DEFAULT '{}'::jsonb,
                      response_payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
                      last_error                 TEXT NULL,
                      created_by_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      updated_by_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      generated_by_user_id       INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      generated_at               TIMESTAMPTZ NULL,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_purchase_entries_status CHECK (status IN ('draft','validated','running','generated','failed','cancelled')),
                      CONSTRAINT chk_bejerman_purchase_entries_company CHECK (company_key = 'SEPID'),
                      CONSTRAINT chk_bejerman_purchase_entries_type CHECK (comprobante_tipo = 'RT'),
                      CONSTRAINT chk_bejerman_purchase_entries_operation CHECK (operation_code = 'MC'),
                      CONSTRAINT chk_bejerman_purchase_entries_totals CHECK (total_quantity >= 0 AND total_value >= 0)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_purchase_entry_lines (
                      id                         TEXT PRIMARY KEY,
                      entry_id                   TEXT NOT NULL REFERENCES bejerman_purchase_entries(id) ON DELETE CASCADE,
                      article_code               TEXT NOT NULL,
                      article_description        TEXT NOT NULL DEFAULT '',
                      default_conversion_factor  NUMERIC(14,4) NOT NULL DEFAULT 1,
                      default_unit_value         NUMERIC(14,2) NOT NULL DEFAULT 0,
                      deposit_code               TEXT NOT NULL DEFAULT 'VAL',
                      sort_order                 INTEGER NOT NULL DEFAULT 0,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_purchase_lines_factor CHECK (default_conversion_factor > 0),
                      CONSTRAINT chk_bejerman_purchase_lines_value CHECK (default_unit_value >= 0)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_purchase_entry_scans (
                      id                         TEXT PRIMARY KEY,
                      entry_id                   TEXT NOT NULL REFERENCES bejerman_purchase_entries(id) ON DELETE CASCADE,
                      line_id                    TEXT NOT NULL REFERENCES bejerman_purchase_entry_lines(id) ON DELETE CASCADE,
                      barcode                    TEXT NOT NULL DEFAULT '',
                      barcode_norm               TEXT NOT NULL DEFAULT '',
                      article_code               TEXT NOT NULL DEFAULT '',
                      article_description        TEXT NOT NULL DEFAULT '',
                      conversion_factor          NUMERIC(14,4) NOT NULL DEFAULT 1,
                      unit_value                 NUMERIC(14,2) NOT NULL DEFAULT 0,
                      total_value                NUMERIC(14,2) NOT NULL DEFAULT 0,
                      is_manual_quantity         BOOLEAN NOT NULL DEFAULT FALSE,
                      note                       TEXT NOT NULL DEFAULT '',
                      sort_order                 INTEGER NOT NULL DEFAULT 0,
                      scanned_by_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_purchase_scans_factor CHECK (conversion_factor > 0),
                      CONSTRAINT chk_bejerman_purchase_scans_values CHECK (unit_value >= 0 AND total_value >= 0)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_purchase_entry_events (
                      id                         BIGSERIAL PRIMARY KEY,
                      entry_id                   TEXT NOT NULL REFERENCES bejerman_purchase_entries(id) ON DELETE CASCADE,
                      actor_user_id              INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      event_type                 TEXT NOT NULL,
                      note                       TEXT NOT NULL DEFAULT '',
                      metadata                   JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                for sql in (
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_entries_status ON bejerman_purchase_entries(status, created_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_entries_document ON bejerman_purchase_entries(company_key, supplier_code, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_lines_entry ON bejerman_purchase_entry_lines(entry_id, sort_order)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_scans_entry ON bejerman_purchase_entry_scans(entry_id, sort_order)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_scans_line ON bejerman_purchase_entry_scans(line_id, sort_order)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_events_entry ON bejerman_purchase_entry_events(entry_id, created_at DESC)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_purchase_entry_scans_barcode ON bejerman_purchase_entry_scans(entry_id, barcode_norm) WHERE barcode_norm <> ''",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_purchase_entries_document_active ON bejerman_purchase_entries(company_key, supplier_code, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero) WHERE status IN ('running','generated') AND supplier_code <> '' AND comprobante_numero <> ''",
                ):
                    cur.execute(sql)

                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_purchase_entries_set_updated_at') THEN
                        CREATE TRIGGER trg_bejerman_purchase_entries_set_updated_at
                        BEFORE UPDATE ON bejerman_purchase_entries
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_purchase_lines_set_updated_at') THEN
                        CREATE TRIGGER trg_bejerman_purchase_lines_set_updated_at
                        BEFORE UPDATE ON bejerman_purchase_entry_lines
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_purchase_scans_set_updated_at') THEN
                        CREATE TRIGGER trg_bejerman_purchase_scans_set_updated_at
                        BEFORE UPDATE ON bejerman_purchase_entry_scans
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

                for table_name, columns in REQUIRED_COLUMNS.items():
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
                    found_columns = {row[0] for row in cur.fetchall()}
                    missing_columns = sorted(columns - found_columns)
                    if missing_columns:
                        raise RuntimeError(
                            f"No se aplicaron columnas requeridas en {table_name}: {', '.join(missing_columns)}"
                        )

        self.stdout.write("APLICADO OK: esquema de ingresos de mercadería Bejerman")
