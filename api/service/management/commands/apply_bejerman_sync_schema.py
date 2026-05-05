from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = ["bejerman_sync_jobs"]


class Command(BaseCommand):
    help = "Aplica el esquema de sincronizacion con Bejerman"

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
                    CREATE TABLE IF NOT EXISTS bejerman_sync_jobs (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      sync_type         TEXT NOT NULL,
                      ingreso_id        INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                      device_id         INTEGER NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
                      ingreso_event_id  INTEGER NULL REFERENCES ingreso_events(id) ON DELETE SET NULL,
                      numero_serie      TEXT NOT NULL DEFAULT '',
                      source_deposit    TEXT NOT NULL DEFAULT 'STR',
                      target_deposit    TEXT NOT NULL DEFAULT 'STL',
                      status            TEXT NOT NULL DEFAULT 'pending',
                      attempts          INTEGER NOT NULL DEFAULT 0,
                      next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      last_error        TEXT NULL,
                      request_payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
                      response_payload  JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_sync_jobs_type CHECK (NULLIF(TRIM(sync_type), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_sync_jobs_status CHECK (status IN ('pending','running','succeeded','failed','blocked')),
                      CONSTRAINT chk_bejerman_sync_jobs_attempts CHECK (attempts >= 0),
                      CONSTRAINT chk_bejerman_sync_jobs_deposits CHECK (
                        NULLIF(TRIM(source_deposit), '') IS NOT NULL
                        AND NULLIF(TRIM(target_deposit), '') IS NOT NULL
                        AND source_deposit <> target_deposit
                      )
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_sync_jobs_type_ingreso
                      ON bejerman_sync_jobs(sync_type, ingreso_id)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_due
                      ON bejerman_sync_jobs(status, next_attempt_at, id)
                      WHERE status IN ('pending','failed')
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_ingreso
                      ON bejerman_sync_jobs(ingreso_id)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_device
                      ON bejerman_sync_jobs(device_id)
                    """
                )

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_sync_jobs_set_updated_at') THEN
                            CREATE TRIGGER trg_bejerman_sync_jobs_set_updated_at
                            BEFORE UPDATE ON bejerman_sync_jobs
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

        self.stdout.write("APLICADO OK: esquema de sincronizacion Bejerman")
