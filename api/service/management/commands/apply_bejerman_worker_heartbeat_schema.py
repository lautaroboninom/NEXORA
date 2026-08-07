from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLE_NAME = "bejerman_worker_heartbeats"

REQUIRED_COLUMNS = {
    "worker_key",
    "instance_id",
    "hostname",
    "pid",
    "started_at",
    "last_beat_at",
    "beat_expires_at",
    "stage",
    "current_document_key",
    "tick_count",
    "last_error_detail",
    "created_at",
    "updated_at",
}


class Command(BaseCommand):
    help = "Aplica el esquema de latidos de los workers de Bejerman"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_bejerman_worker_heartbeat_schema requiere PostgreSQL")

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
                # Sin FK a propósito: tiene que poder aplicarse suelta (tests, worker
                # que arranca antes que el resto del esquema) y no bloquear nunca.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_worker_heartbeats (
                      worker_key            TEXT PRIMARY KEY,
                      instance_id           TEXT NOT NULL DEFAULT '',
                      hostname              TEXT NOT NULL DEFAULT '',
                      pid                   INTEGER NULL,
                      started_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      last_beat_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      beat_expires_at       TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 minutes'),
                      stage                 TEXT NOT NULL DEFAULT '',
                      current_document_key  TEXT NULL,
                      tick_count            BIGINT NOT NULL DEFAULT 0,
                      last_error_detail     TEXT NULL,
                      created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_worker_heartbeats_key
                        CHECK (NULLIF(TRIM(worker_key), '') IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                          FROM pg_trigger
                         WHERE tgname = 'trg_bejerman_worker_heartbeats_set_updated_at'
                      ) THEN
                        CREATE TRIGGER trg_bejerman_worker_heartbeats_set_updated_at
                        BEFORE UPDATE ON bejerman_worker_heartbeats
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

                cur.execute(
                    """
                    SELECT 1
                      FROM pg_trigger
                     WHERE tgrelid = %s::regclass
                       AND tgname = 'trg_bejerman_worker_heartbeats_set_updated_at'
                       AND NOT tgisinternal
                    """,
                    [TABLE_NAME],
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        "No se aplicó el trigger trg_bejerman_worker_heartbeats_set_updated_at"
                    )

        self.stdout.write(
            self.style.SUCCESS("Esquema de latidos de workers Bejerman aplicado")
        )
