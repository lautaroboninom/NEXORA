from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica el esquema de solicitudes de asignación de técnico"

    @staticmethod
    def _sqlite_column_exists(cur, table_name: str, column_name: str) -> bool:
        cur.execute(f"PRAGMA table_info({table_name})")
        rows = cur.fetchall() or []
        return any(len(row) > 1 and str(row[1]) == column_name for row in rows)

    def handle(self, *args, **opts):
        deduped = 0
        with transaction.atomic():
            with connection.cursor() as cur:
                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ingreso_assignment_requests (
                          id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          ingreso_id  INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                          usuario_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                          status      TEXT NOT NULL DEFAULT 'pendiente',
                          created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          accepted_at TIMESTAMPTZ NULL,
                          canceled_at TIMESTAMPTZ NULL
                        )
                        """
                    )
                    for statement in (
                        "ALTER TABLE ingreso_assignment_requests ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pendiente'",
                        "ALTER TABLE ingreso_assignment_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
                        "ALTER TABLE ingreso_assignment_requests ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ NULL",
                        "ALTER TABLE ingreso_assignment_requests ADD COLUMN IF NOT EXISTS canceled_at TIMESTAMPTZ NULL",
                    ):
                        cur.execute(statement)
                    cur.execute(
                        """
                        WITH ranked AS (
                            SELECT id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY ingreso_id, usuario_id
                                       ORDER BY created_at DESC, id DESC
                                   ) AS rn
                              FROM ingreso_assignment_requests
                             WHERE accepted_at IS NULL
                               AND canceled_at IS NULL
                        )
                        UPDATE ingreso_assignment_requests r
                           SET canceled_at = CURRENT_TIMESTAMP,
                               status = 'cancelado'
                          FROM ranked
                         WHERE r.id = ranked.id
                           AND ranked.rn > 1
                        """
                    )
                    deduped = max(int(cur.rowcount or 0), 0)
                else:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ingreso_assignment_requests (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          ingreso_id INTEGER NOT NULL,
                          usuario_id INTEGER NOT NULL,
                          status TEXT NOT NULL DEFAULT 'pendiente',
                          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          accepted_at DATETIME NULL,
                          canceled_at DATETIME NULL
                        )
                        """
                    )
                    for column, ddl in (
                        ("status", "ALTER TABLE ingreso_assignment_requests ADD COLUMN status TEXT NOT NULL DEFAULT 'pendiente'"),
                        ("created_at", "ALTER TABLE ingreso_assignment_requests ADD COLUMN created_at DATETIME NULL"),
                        ("accepted_at", "ALTER TABLE ingreso_assignment_requests ADD COLUMN accepted_at DATETIME NULL"),
                        ("canceled_at", "ALTER TABLE ingreso_assignment_requests ADD COLUMN canceled_at DATETIME NULL"),
                    ):
                        if not self._sqlite_column_exists(cur, "ingreso_assignment_requests", column):
                            cur.execute(ddl)

                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_iars_ingreso_created ON ingreso_assignment_requests(ingreso_id, created_at DESC)"
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_iars_ingreso_usuario_pending
                      ON ingreso_assignment_requests(ingreso_id, usuario_id)
                      WHERE accepted_at IS NULL AND canceled_at IS NULL
                    """
                )

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        SELECT 1
                          FROM information_schema.tables
                         WHERE table_schema = ANY(current_schemas(true))
                           AND table_name = 'ingreso_assignment_requests'
                         LIMIT 1
                        """
                    )
                    if not cur.fetchone():
                        raise RuntimeError("No se aplicó ingreso_assignment_requests")

        self.stdout.write(f"APLICADO OK: esquema de solicitudes de asignación (deduplicadas={deduped})")
