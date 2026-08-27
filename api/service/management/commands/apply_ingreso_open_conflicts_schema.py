from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Aplica el esquema de conflictos de ingreso abierto: equipos que entraron mientras "
        "su ingreso anterior seguia abierto. El RIS sale igual, sin numero de OS en ese "
        "renglon, y el caso queda anotado para resolverlo despues."
    )

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ingreso_open_conflicts (
                          id                     INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          ingreso_id             INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                          conflicting_ingreso_id INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                          status                 TEXT NOT NULL DEFAULT 'pending',
                          remito_number          TEXT NULL,
                          numero_serie           TEXT NOT NULL DEFAULT '',
                          tipo_equipo            TEXT NOT NULL DEFAULT '',
                          evidence               JSONB NOT NULL DEFAULT '{}'::jsonb,
                          resolution_note        TEXT NULL,
                          resolved_by            INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          resolved_at            TIMESTAMPTZ NULL,
                          created_by             INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    for statement in (
                        "ALTER TABLE ingreso_open_conflicts ADD COLUMN IF NOT EXISTS remito_number TEXT NULL",
                        "ALTER TABLE ingreso_open_conflicts ADD COLUMN IF NOT EXISTS numero_serie TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE ingreso_open_conflicts ADD COLUMN IF NOT EXISTS tipo_equipo TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE ingreso_open_conflicts ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb",
                        "ALTER TABLE ingreso_open_conflicts ADD COLUMN IF NOT EXISTS resolution_note TEXT NULL",
                        "ALTER TABLE ingreso_open_conflicts DROP CONSTRAINT IF EXISTS chk_ingreso_open_conflicts_status",
                        "ALTER TABLE ingreso_open_conflicts ADD CONSTRAINT chk_ingreso_open_conflicts_status "
                        "CHECK (status IN ('pending','resolved','dismissed'))",
                    ):
                        cur.execute(statement)
                else:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ingreso_open_conflicts (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          ingreso_id INTEGER NOT NULL,
                          conflicting_ingreso_id INTEGER NOT NULL,
                          status TEXT NOT NULL DEFAULT 'pending',
                          remito_number TEXT NULL,
                          numero_serie TEXT NOT NULL DEFAULT '',
                          tipo_equipo TEXT NOT NULL DEFAULT '',
                          evidence TEXT NOT NULL DEFAULT '{}',
                          resolution_note TEXT NULL,
                          resolved_by INTEGER NULL,
                          resolved_at DATETIME NULL,
                          created_by INTEGER NULL,
                          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                # Un ingreso arrastra un solo conflicto: reintentar la emisión no duplica la fila.
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ingreso_open_conflicts_ingreso "
                    "ON ingreso_open_conflicts (ingreso_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ingreso_open_conflicts_status "
                    "ON ingreso_open_conflicts (status)"
                )

        self.stdout.write("APLICADO OK: esquema de conflictos de ingreso abierto")
