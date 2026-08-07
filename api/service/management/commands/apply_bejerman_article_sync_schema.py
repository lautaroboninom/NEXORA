from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica el esquema de auditoría de sincronización de artículos SEPID -> MGBIO"

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_article_sync_log (
                      id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      article_code    TEXT NOT NULL,
                      company_key     TEXT NOT NULL,
                      classification  TEXT NOT NULL,
                      fields_changed  JSONB NOT NULL DEFAULT '{}'::jsonb,
                      applied         BOOLEAN NOT NULL DEFAULT false,
                      skip_reason     TEXT NULL,
                      created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_article_sync_log_classification
                        CHECK (classification IN ('cosmetic','sustantive'))
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_article_sync_log_code
                      ON bejerman_article_sync_log(article_code)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_article_sync_log_created_at
                      ON bejerman_article_sync_log(created_at)
                    """
                )

        self.stdout.write("APLICADO OK: esquema de auditoría de sync de artículos SEPID/MGBIO")
