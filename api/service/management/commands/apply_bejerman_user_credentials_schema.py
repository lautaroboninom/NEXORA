from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica el esquema de credenciales Bejerman por usuario"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_bejerman_user_credentials_schema requiere PostgreSQL")

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
                    CREATE TABLE IF NOT EXISTS user_bejerman_credentials (
                      user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                      bejerman_username   TEXT NOT NULL,
                      encrypted_password  TEXT NOT NULL,
                      is_valid            BOOLEAN NOT NULL DEFAULT FALSE,
                      validated_at        TIMESTAMPTZ NULL,
                      last_error          TEXT NULL,
                      created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_user_bejerman_credentials_username
                        CHECK (NULLIF(TRIM(bejerman_username), '') IS NOT NULL),
                      CONSTRAINT chk_user_bejerman_credentials_password
                        CHECK (NULLIF(TRIM(encrypted_password), '') IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_user_bejerman_credentials_valid
                      ON user_bejerman_credentials(is_valid, updated_at)
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (
                        SELECT 1 FROM pg_trigger
                         WHERE tgname = 'trg_user_bejerman_credentials_set_updated_at'
                      ) THEN
                        CREATE TRIGGER trg_user_bejerman_credentials_set_updated_at
                        BEFORE UPDATE ON user_bejerman_credentials
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                    END $$;
                    """
                )
                cur.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'user_bejerman_credentials'
                    """
                )
                columns = {row[0] for row in cur.fetchall()}
                required = {
                    "user_id",
                    "bejerman_username",
                    "encrypted_password",
                    "is_valid",
                    "validated_at",
                    "last_error",
                    "created_at",
                    "updated_at",
                }
                missing = sorted(required - columns)
                if missing:
                    raise RuntimeError("No se aplicaron columnas: " + ", ".join(missing))

        self.stdout.write(self.style.SUCCESS("Esquema de credenciales Bejerman aplicado"))
