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
        "comprobante_tipo_origen",
        "comprobante_letra_origen",
        "comprobante_pto_venta_origen",
        "comprobante_numero_origen",
        "supplier_code",
        "supplier_code_raw",
        "supplier_name",
        "payment_term_code",
        "operation_code",
        "deposit_code",
        "request_payload",
        "response_payload",
        "attempt_count",
        "last_attempt_at",
        "request_fingerprint",
    },
    "bejerman_purchase_entry_lines": {
        "id",
        "entry_id",
        "article_code",
        "article_description",
        "default_conversion_factor",
        "default_unit_value",
        "deposit_code",
        "expected_quantity",
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
        "partida_expiration_date",
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
                      comprobante_tipo_origen      TEXT NOT NULL DEFAULT '',
                      comprobante_letra_origen     TEXT NOT NULL DEFAULT '',
                      comprobante_pto_venta_origen TEXT NOT NULL DEFAULT '',
                      comprobante_numero_origen    TEXT NOT NULL DEFAULT '',
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
                      attempt_count              INTEGER NOT NULL DEFAULT 0,
                      last_attempt_at            TIMESTAMPTZ NULL,
                      request_fingerprint        VARCHAR(64) NOT NULL DEFAULT '',
                      created_by_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      updated_by_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      generated_by_user_id       INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      generated_at               TIMESTAMPTZ NULL,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_purchase_entries_status CHECK (status IN ('draft','validated','running','verifying','generated','failed','failed_review','cancelled')),
                      CONSTRAINT chk_bejerman_purchase_entries_company CHECK (company_key = 'SEPID'),
                      CONSTRAINT chk_bejerman_purchase_entries_type CHECK (comprobante_tipo = 'RT'),
                      CONSTRAINT chk_bejerman_purchase_entries_operation CHECK (operation_code = 'MC'),
                      CONSTRAINT chk_bejerman_purchase_entries_totals CHECK (total_quantity >= 0 AND total_value >= 0),
                      CONSTRAINT chk_bejerman_purchase_entries_origin_type CHECK (comprobante_tipo_origen IN ('', 'OC')),
                      CONSTRAINT chk_bejerman_purchase_entries_origin_pair CHECK ((comprobante_tipo_origen = '') = (comprobante_numero_origen = ''))
                    )
                    """
                )

                cur.execute(
                    """
                    ALTER TABLE bejerman_purchase_entries
                      ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ NULL,
                      ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS comprobante_tipo_origen TEXT NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS comprobante_letra_origen TEXT NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS comprobante_pto_venta_origen TEXT NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS comprobante_numero_origen TEXT NOT NULL DEFAULT '';

                    UPDATE bejerman_purchase_entries
                       SET attempt_count = 0
                     WHERE attempt_count IS NULL;

                    UPDATE bejerman_purchase_entries
                       SET request_fingerprint = ''
                     WHERE request_fingerprint IS NULL;

                    ALTER TABLE bejerman_purchase_entries
                      ALTER COLUMN attempt_count SET DEFAULT 0,
                      ALTER COLUMN attempt_count SET NOT NULL,
                      ALTER COLUMN request_fingerprint SET DEFAULT '',
                      ALTER COLUMN request_fingerprint SET NOT NULL;

                    ALTER TABLE bejerman_purchase_entries
                      DROP CONSTRAINT IF EXISTS chk_bejerman_purchase_entries_status;

                    ALTER TABLE bejerman_purchase_entries
                      ADD CONSTRAINT chk_bejerman_purchase_entries_status
                      CHECK (status IN ('draft','validated','running','verifying','generated','failed','failed_review','cancelled'));

                    ALTER TABLE bejerman_purchase_entries
                      DROP CONSTRAINT IF EXISTS chk_bejerman_purchase_entries_origin_type;

                    ALTER TABLE bejerman_purchase_entries
                      ADD CONSTRAINT chk_bejerman_purchase_entries_origin_type
                      CHECK (comprobante_tipo_origen IN ('', 'OC'));

                    ALTER TABLE bejerman_purchase_entries
                      DROP CONSTRAINT IF EXISTS chk_bejerman_purchase_entries_origin_pair;

                    ALTER TABLE bejerman_purchase_entries
                      ADD CONSTRAINT chk_bejerman_purchase_entries_origin_pair
                      CHECK ((comprobante_tipo_origen = '') = (comprobante_numero_origen = ''));
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
                      expected_quantity          NUMERIC(14,4) NULL,
                      sort_order                 INTEGER NOT NULL DEFAULT 0,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_purchase_lines_factor CHECK (default_conversion_factor > 0),
                      CONSTRAINT chk_bejerman_purchase_lines_value CHECK (default_unit_value >= 0),
                      CONSTRAINT chk_bejerman_purchase_lines_expected CHECK (expected_quantity IS NULL OR expected_quantity > 0)
                    )
                    """
                )

                cur.execute(
                    """
                    ALTER TABLE bejerman_purchase_entry_lines
                      ADD COLUMN IF NOT EXISTS expected_quantity NUMERIC(14,4) NULL;
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
                      partida_expiration_date    DATE NULL,
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
                    ALTER TABLE bejerman_purchase_entry_scans
                      ADD COLUMN IF NOT EXISTS partida_expiration_date DATE NULL;
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
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_scans_barcode_norm ON bejerman_purchase_entry_scans(barcode_norm) WHERE barcode_norm <> ''",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_purchase_events_entry ON bejerman_purchase_entry_events(entry_id, created_at DESC)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_purchase_entry_scans_barcode ON bejerman_purchase_entry_scans(entry_id, barcode_norm) WHERE barcode_norm <> ''",
                ):
                    cur.execute(sql)

                cur.execute(
                    """
                    DROP INDEX IF EXISTS uq_bejerman_purchase_entries_document_active;
                    CREATE UNIQUE INDEX uq_bejerman_purchase_entries_document_active
                      ON bejerman_purchase_entries(
                        company_key, supplier_code, comprobante_tipo, comprobante_letra,
                        comprobante_pto_venta, comprobante_numero
                      )
                      WHERE status IN ('running','verifying','generated','failed_review')
                        AND supplier_code <> ''
                        AND comprobante_numero <> '';
                    """
                )

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

                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable, character_maximum_length, column_default
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'bejerman_purchase_entries'
                       AND column_name IN ('attempt_count', 'last_attempt_at', 'request_fingerprint')
                    """
                )
                physical_columns = {
                    row[0]: {
                        "data_type": row[1],
                        "is_nullable": row[2],
                        "maximum_length": row[3],
                        "default": row[4],
                    }
                    for row in cur.fetchall()
                }
                expected_columns = {
                    "attempt_count": ("integer", "NO", None),
                    "last_attempt_at": ("timestamp with time zone", "YES", None),
                    "request_fingerprint": ("character varying", "NO", 64),
                }
                for column_name, expected in expected_columns.items():
                    actual = physical_columns.get(column_name)
                    if not actual:
                        raise RuntimeError(
                            f"No se aplicó la columna física {column_name} en bejerman_purchase_entries"
                        )
                    definition = (
                        actual["data_type"],
                        actual["is_nullable"],
                        actual["maximum_length"],
                    )
                    if definition != expected:
                        raise RuntimeError(
                            f"Definición inválida de bejerman_purchase_entries.{column_name}: "
                            f"se esperaba {expected} y se obtuvo {definition}"
                        )

                attempt_default = str(physical_columns["attempt_count"]["default"] or "").strip()
                fingerprint_default = str(physical_columns["request_fingerprint"]["default"] or "").strip()
                if not attempt_default.startswith("0"):
                    raise RuntimeError(
                        "Definición inválida de bejerman_purchase_entries.attempt_count: falta DEFAULT 0"
                    )
                if not fingerprint_default.startswith("''"):
                    raise RuntimeError(
                        "Definición inválida de bejerman_purchase_entries.request_fingerprint: falta DEFAULT ''"
                    )

                cur.execute(
                    """
                    SELECT pg_get_constraintdef(oid)
                      FROM pg_constraint
                     WHERE conrelid = 'bejerman_purchase_entries'::regclass
                       AND conname = 'chk_bejerman_purchase_entries_status'
                       AND contype = 'c'
                    """
                )
                constraint_row = cur.fetchone()
                if not constraint_row:
                    raise RuntimeError(
                        "No se aplicó chk_bejerman_purchase_entries_status"
                    )
                constraint_definition = str(constraint_row[0] or "").lower()
                required_statuses = (
                    "draft",
                    "validated",
                    "running",
                    "verifying",
                    "generated",
                    "failed",
                    "failed_review",
                    "cancelled",
                )
                missing_statuses = [
                    status
                    for status in required_statuses
                    if f"'{status}'" not in constraint_definition
                ]
                if missing_statuses:
                    raise RuntimeError(
                        "chk_bejerman_purchase_entries_status no admite: "
                        + ", ".join(missing_statuses)
                    )

                cur.execute(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                      FROM pg_constraint
                     WHERE conrelid = 'bejerman_purchase_entries'::regclass
                       AND conname IN (
                           'chk_bejerman_purchase_entries_origin_type',
                           'chk_bejerman_purchase_entries_origin_pair'
                       )
                       AND contype = 'c'
                    """
                )
                origin_constraints = {row[0]: str(row[1] or "").lower() for row in cur.fetchall()}
                missing_origin_constraints = sorted(
                    {"chk_bejerman_purchase_entries_origin_type", "chk_bejerman_purchase_entries_origin_pair"}
                    - origin_constraints.keys()
                )
                if missing_origin_constraints:
                    raise RuntimeError(
                        "No se aplicaron constraints requeridas: " + ", ".join(missing_origin_constraints)
                    )
                if "'oc'" not in origin_constraints["chk_bejerman_purchase_entries_origin_type"]:
                    raise RuntimeError(
                        "chk_bejerman_purchase_entries_origin_type no admite el tipo 'OC'"
                    )

                cur.execute(
                    """
                    SELECT index_class.relname,
                           index_meta.indisunique,
                           index_meta.indisvalid,
                           index_meta.indisready,
                           pg_get_indexdef(index_meta.indexrelid),
                           pg_get_expr(index_meta.indpred, index_meta.indrelid)
                      FROM pg_index index_meta
                      JOIN pg_class index_class
                        ON index_class.oid = index_meta.indexrelid
                     WHERE index_meta.indexrelid IN (
                           to_regclass('uq_bejerman_purchase_entries_document_active'),
                           to_regclass('ix_bejerman_purchase_scans_barcode_norm')
                     )
                    """
                )
                physical_indexes = {
                    row[0]: {
                        "is_unique": bool(row[1]),
                        "is_valid": bool(row[2]),
                        "is_ready": bool(row[3]),
                        "definition": " ".join(str(row[4] or "").lower().split()),
                        "predicate": " ".join(str(row[5] or "").lower().split()),
                    }
                    for row in cur.fetchall()
                }

                active_document_index = physical_indexes.get(
                    "uq_bejerman_purchase_entries_document_active"
                )
                if not active_document_index:
                    raise RuntimeError(
                        "No se aplicó uq_bejerman_purchase_entries_document_active"
                    )
                if (
                    not active_document_index["is_unique"]
                    or not active_document_index["is_valid"]
                    or not active_document_index["is_ready"]
                ):
                    raise RuntimeError(
                        "uq_bejerman_purchase_entries_document_active no está único, válido y listo"
                    )
                active_document_columns = (
                    "(company_key, supplier_code, comprobante_tipo, comprobante_letra, "
                    "comprobante_pto_venta, comprobante_numero)"
                )
                if active_document_columns not in active_document_index["definition"]:
                    raise RuntimeError(
                        "uq_bejerman_purchase_entries_document_active no contiene "
                        "las columnas esperadas en el orden requerido"
                    )
                active_document_predicate = active_document_index["predicate"]
                for required_status in ("running", "verifying", "generated", "failed_review"):
                    if f"'{required_status}'" not in active_document_predicate:
                        raise RuntimeError(
                            "uq_bejerman_purchase_entries_document_active no protege "
                            f"el estado {required_status}"
                        )
                for required_filter in ("supplier_code", "comprobante_numero"):
                    if required_filter not in active_document_predicate:
                        raise RuntimeError(
                            "uq_bejerman_purchase_entries_document_active no contiene "
                            f"el filtro requerido para {required_filter}"
                        )
                if active_document_predicate.count("<> ''") < 2:
                    raise RuntimeError(
                        "uq_bejerman_purchase_entries_document_active no excluye "
                        "proveedores o números de comprobante vacíos"
                    )
                for excluded_status in ("draft", "validated", "failed", "cancelled"):
                    if f"'{excluded_status}'" in active_document_predicate:
                        raise RuntimeError(
                            "uq_bejerman_purchase_entries_document_active incluye "
                            f"indebidamente el estado {excluded_status}"
                        )

                barcode_index = physical_indexes.get(
                    "ix_bejerman_purchase_scans_barcode_norm"
                )
                if not barcode_index:
                    raise RuntimeError(
                        "No se aplicó ix_bejerman_purchase_scans_barcode_norm"
                    )
                if (
                    barcode_index["is_unique"]
                    or not barcode_index["is_valid"]
                    or not barcode_index["is_ready"]
                ):
                    raise RuntimeError(
                        "ix_bejerman_purchase_scans_barcode_norm no está válido y listo "
                        "como índice no único"
                    )
                if "(barcode_norm)" not in barcode_index["definition"]:
                    raise RuntimeError(
                        "ix_bejerman_purchase_scans_barcode_norm no indexa barcode_norm"
                    )
                barcode_predicate = barcode_index["predicate"]
                if "barcode_norm" not in barcode_predicate or "<> ''" not in barcode_predicate:
                    raise RuntimeError(
                        "ix_bejerman_purchase_scans_barcode_norm no excluye códigos vacíos"
                    )

        self.stdout.write("APLICADO OK: esquema de ingresos de mercadería Bejerman")
