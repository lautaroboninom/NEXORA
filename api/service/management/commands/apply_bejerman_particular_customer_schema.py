from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica el esquema de auditoría de habilitación/deshabilitación de fichas de particulares"

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_particular_customer_log (
                      id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      documento      TEXT NOT NULL,
                      company_key    TEXT NOT NULL,
                      customer_code  TEXT NOT NULL DEFAULT '',
                      action         TEXT NOT NULL,
                      reason         TEXT NOT NULL DEFAULT '',
                      created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_particular_customer_log_action
                        CHECK (action IN ('enable','disable','skipped'))
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_particular_customer_log_doc
                      ON bejerman_particular_customer_log(documento)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_particular_customer_log_created_at
                      ON bejerman_particular_customer_log(created_at)
                    """
                )

        self.stdout.write("APLICADO OK: esquema de auditoría de fichas de particulares")
