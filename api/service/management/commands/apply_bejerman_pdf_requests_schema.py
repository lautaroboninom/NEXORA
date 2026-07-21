from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLE_NAME = "bejerman_pdf_requests"

REQUIRED_COLUMNS = {
    "document_key",
    "remito_group_id",
    "actor_user_id",
    "company_key",
    "customer_code",
    "tipo",
    "letra",
    "pto_venta",
    "numero",
    "issue_date",
    "state",
    "generation_attempt_count",
    "generation_requested_at",
    "lease_expires_at",
    "deadline_at",
    "next_folder_check_at",
    "ready_at",
    "remote_path",
    "pdf_payload",
    "payload_sha256",
    "last_error_code",
    "last_error_detail",
    "created_at",
    "updated_at",
}

REQUIRED_INDEXES = {
    "bejerman_pdf_requests_pkey",
    "uq_bejerman_pdf_requests_document",
    "ix_bejerman_pdf_requests_state_next_check",
    "ix_bejerman_pdf_requests_group",
}


class Command(BaseCommand):
    help = "Aplica el esquema de solicitudes persistentes de PDF de Bejerman"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_bejerman_pdf_requests_schema requiere PostgreSQL")

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
                    CREATE TABLE IF NOT EXISTS bejerman_pdf_requests (
                      document_key                 TEXT PRIMARY KEY,
                      remito_group_id              TEXT NULL REFERENCES bejerman_remito_groups(id) ON DELETE SET NULL,
                      actor_user_id                INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      company_key                  TEXT NOT NULL,
                      customer_code               TEXT NOT NULL DEFAULT '',
                      tipo                         TEXT NOT NULL,
                      letra                        TEXT NOT NULL,
                      pto_venta                    TEXT NOT NULL,
                      numero                       TEXT NOT NULL,
                      issue_date                   DATE NOT NULL,
                      state                        TEXT NOT NULL DEFAULT 'queued',
                      generation_attempt_count     INTEGER NOT NULL DEFAULT 0,
                      generation_requested_at      TIMESTAMPTZ NULL,
                      lease_expires_at             TIMESTAMPTZ NULL,
                      deadline_at                  TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '10 minutes'),
                      next_folder_check_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      ready_at                     TIMESTAMPTZ NULL,
                      remote_path                  TEXT NULL,
                      pdf_payload                  BYTEA NULL,
                      payload_sha256               TEXT NULL,
                      last_error_code              TEXT NULL,
                      last_error_detail            TEXT NULL,
                      created_at                   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_pdf_requests_document_key
                        CHECK (NULLIF(TRIM(document_key), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_requests_company_key
                        CHECK (NULLIF(TRIM(company_key), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_requests_tipo
                        CHECK (NULLIF(TRIM(tipo), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_requests_letra
                        CHECK (NULLIF(TRIM(letra), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_requests_pto_venta
                        CHECK (NULLIF(TRIM(pto_venta), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_requests_numero
                        CHECK (NULLIF(TRIM(numero), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_pdf_requests_state
                        CHECK (state IN ('queued','requesting','waiting_sdk','waiting_folder','ready_direct','ready_folder','failed')),
                      CONSTRAINT chk_bejerman_pdf_requests_generation_attempts
                        CHECK (generation_attempt_count BETWEEN 0 AND 1)
                    )
                    """
                )
                cur.execute(
                    "ALTER TABLE bejerman_pdf_requests ADD COLUMN IF NOT EXISTS customer_code TEXT NOT NULL DEFAULT ''"
                )
                cur.execute(
                    """
                    UPDATE bejerman_pdf_requests AS request
                       SET customer_code = COALESCE(NULLIF(TRIM(remito.customer_code), ''), '')
                      FROM bejerman_remito_groups AS remito
                     WHERE request.remito_group_id = remito.id
                       AND NULLIF(TRIM(request.customer_code), '') IS NULL
                    """
                )
                cur.execute(
                    "ALTER TABLE bejerman_pdf_requests DROP CONSTRAINT IF EXISTS chk_bejerman_pdf_requests_state"
                )
                cur.execute(
                    """
                    ALTER TABLE bejerman_pdf_requests
                      ADD CONSTRAINT chk_bejerman_pdf_requests_state
                      CHECK (state IN ('queued','requesting','waiting_sdk','waiting_folder','ready_direct','ready_folder','failed'))
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_pdf_requests_document
                      ON bejerman_pdf_requests(company_key, tipo, letra, pto_venta, numero, issue_date)
                    """
                )
                cur.execute("DROP INDEX IF EXISTS ix_bejerman_pdf_requests_state_next_check")
                cur.execute(
                    """
                    CREATE INDEX ix_bejerman_pdf_requests_state_next_check
                      ON bejerman_pdf_requests(state, next_folder_check_at, created_at)
                      WHERE state IN ('queued','requesting','waiting_sdk','waiting_folder','ready_direct')
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_pdf_requests_group
                      ON bejerman_pdf_requests(remito_group_id)
                      WHERE remito_group_id IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                          FROM pg_trigger
                         WHERE tgname = 'trg_bejerman_pdf_requests_set_updated_at'
                      ) THEN
                        CREATE TRIGGER trg_bejerman_pdf_requests_set_updated_at
                        BEFORE UPDATE ON bejerman_pdf_requests
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                    END $$;
                    """
                )

                cur.execute(
                    """
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = %s
                    """,
                    [TABLE_NAME],
                )
                if cur.fetchone() is None:
                    raise RuntimeError(f"No se aplicó la tabla requerida: {TABLE_NAME}")

                placeholders = ",".join(["%s"] * len(REQUIRED_COLUMNS))
                cur.execute(
                    f"""
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = %s
                       AND column_name IN ({placeholders})
                    """,
                    [TABLE_NAME, *sorted(REQUIRED_COLUMNS)],
                )
                found_columns = {row[0] for row in cur.fetchall()}
                missing_columns = sorted(REQUIRED_COLUMNS - found_columns)
                if missing_columns:
                    raise RuntimeError(
                        f"Faltan columnas en {TABLE_NAME}: {', '.join(missing_columns)}"
                    )

                placeholders = ",".join(["%s"] * len(REQUIRED_INDEXES))
                cur.execute(
                    f"""
                    SELECT indexname
                      FROM pg_indexes
                     WHERE schemaname = ANY(current_schemas(true))
                       AND tablename = %s
                       AND indexname IN ({placeholders})
                    """,
                    [TABLE_NAME, *sorted(REQUIRED_INDEXES)],
                )
                found_indexes = {row[0] for row in cur.fetchall()}
                missing_indexes = sorted(REQUIRED_INDEXES - found_indexes)
                if missing_indexes:
                    raise RuntimeError(
                        f"Faltan índices en {TABLE_NAME}: {', '.join(missing_indexes)}"
                    )

                cur.execute(
                    """
                    SELECT 1
                      FROM pg_trigger
                     WHERE tgrelid = %s::regclass
                       AND tgname = 'trg_bejerman_pdf_requests_set_updated_at'
                       AND NOT tgisinternal
                    """,
                    [TABLE_NAME],
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        "No se aplicó el trigger trg_bejerman_pdf_requests_set_updated_at"
                    )

        self.stdout.write(
            self.style.SUCCESS("Esquema de solicitudes PDF de Bejerman aplicado")
        )
