from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = [
    "notifications",
    "notification_user_preferences",
    "notification_push_subscriptions",
]


class Command(BaseCommand):
    help = "Aplica el esquema de notificaciones internas y preferencias por usuario"

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
                    CREATE TABLE IF NOT EXISTS notifications (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                      notification_key  TEXT NOT NULL,
                      dedupe_key        TEXT NOT NULL,
                      title             TEXT NOT NULL,
                      body              TEXT NOT NULL DEFAULT '',
                      href              TEXT NOT NULL DEFAULT '',
                      severity          TEXT NOT NULL DEFAULT 'info',
                      entity_type       TEXT NULL,
                      entity_id         TEXT NULL,
                      payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
                      read_at           TIMESTAMPTZ NULL,
                      clicked_at        TIMESTAMPTZ NULL,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_notifications_key CHECK (NULLIF(TRIM(notification_key), '') IS NOT NULL),
                      CONSTRAINT chk_notifications_dedupe CHECK (NULLIF(TRIM(dedupe_key), '') IS NOT NULL),
                      CONSTRAINT chk_notifications_severity CHECK (severity IN ('info','warning','critical'))
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notification_user_preferences (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                      notification_key  TEXT NOT NULL,
                      enabled           BOOLEAN NULL,
                      updated_by        INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_notification_preferences_key CHECK (NULLIF(TRIM(notification_key), '') IS NOT NULL),
                      CONSTRAINT uq_notification_user_preferences UNIQUE (user_id, notification_key)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notification_push_subscriptions (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                      endpoint          TEXT NOT NULL,
                      p256dh            TEXT NOT NULL,
                      auth              TEXT NOT NULL,
                      content_encoding  TEXT NOT NULL DEFAULT 'aes128gcm',
                      user_agent        TEXT NOT NULL DEFAULT '',
                      disabled_at       TIMESTAMPTZ NULL,
                      failure_count     INTEGER NOT NULL DEFAULT 0,
                      last_error        TEXT NOT NULL DEFAULT '',
                      last_success_at   TIMESTAMPTZ NULL,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_notification_push_endpoint CHECK (NULLIF(TRIM(endpoint), '') IS NOT NULL),
                      CONSTRAINT chk_notification_push_p256dh CHECK (NULLIF(TRIM(p256dh), '') IS NOT NULL),
                      CONSTRAINT chk_notification_push_auth CHECK (NULLIF(TRIM(auth), '') IS NOT NULL),
                      CONSTRAINT chk_notification_push_failure_count CHECK (failure_count >= 0)
                    )
                    """
                )
                for _column, statement in (
                    ("user_id", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
                    ("endpoint", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS endpoint TEXT"),
                    ("p256dh", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS p256dh TEXT"),
                    ("auth", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS auth TEXT"),
                    ("content_encoding", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS content_encoding TEXT NOT NULL DEFAULT 'aes128gcm'"),
                    ("user_agent", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS user_agent TEXT NOT NULL DEFAULT ''"),
                    ("disabled_at", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ NULL"),
                    ("failure_count", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0"),
                    ("last_error", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT ''"),
                    ("last_success_at", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ NULL"),
                    ("created_at", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"),
                    ("updated_at", "ALTER TABLE notification_push_subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"),
                ):
                    cur.execute(statement)

                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_user_key_dedupe
                      ON notifications(user_id, notification_key, dedupe_key)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_notifications_user_unread_created
                      ON notifications(user_id, created_at DESC)
                      WHERE read_at IS NULL
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_notifications_entity
                      ON notifications(entity_type, entity_id)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_notification_preferences_user
                      ON notification_user_preferences(user_id)
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_push_endpoint
                      ON notification_push_subscriptions(endpoint)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_notification_push_active_user
                      ON notification_push_subscriptions(user_id, updated_at DESC)
                      WHERE disabled_at IS NULL
                    """
                )

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notifications_set_updated_at') THEN
                            CREATE TRIGGER trg_notifications_set_updated_at
                            BEFORE UPDATE ON notifications
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notification_preferences_set_updated_at') THEN
                            CREATE TRIGGER trg_notification_preferences_set_updated_at
                            BEFORE UPDATE ON notification_user_preferences
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_notification_push_subscriptions_set_updated_at') THEN
                            CREATE TRIGGER trg_notification_push_subscriptions_set_updated_at
                            BEFORE UPDATE ON notification_push_subscriptions
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

        self.stdout.write("APLICADO OK: esquema de notificaciones internas")
