from django.core.management.base import BaseCommand
from django.db import connection, transaction
import json


class Command(BaseCommand):
    help = "Crea/actualiza ingreso_tests (con schema_snapshot) y catalogo editable de protocolos de test."

    @staticmethod
    def _sqlite_column_exists(cur, table_name: str, column_name: str) -> bool:
        cur.execute(f"PRAGMA table_info({table_name})")
        rows = cur.fetchall() or []
        return any((len(row) > 1 and str(row[1]) == column_name) for row in rows)

    def handle(self, *args, **opts):
        seeded = 0
        with transaction.atomic():
            with connection.cursor() as cur:
                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ingreso_tests (
                          id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          ingreso_id           INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                          template_key         TEXT NOT NULL,
                          template_version     TEXT NOT NULL,
                          tipo_equipo_snapshot TEXT,
                          payload              JSONB NOT NULL DEFAULT '{}'::jsonb,
                          schema_snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,
                          references_snapshot  JSONB NOT NULL DEFAULT '[]'::jsonb,
                          resultado_global     TEXT NOT NULL DEFAULT 'pendiente',
                          conclusion           TEXT,
                          instrumentos         TEXT,
                          firmado_por          TEXT,
                          fecha_ejecucion      TIMESTAMPTZ NULL,
                          tecnico_id           INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cur.execute(
                        """
                        ALTER TABLE ingreso_tests
                        ADD COLUMN IF NOT EXISTS schema_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
                        """
                    )
                else:
                    # SQLite fallback for local tests/dev.
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ingreso_tests (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          ingreso_id INTEGER NOT NULL UNIQUE,
                          template_key TEXT NOT NULL,
                          template_version TEXT NOT NULL,
                          tipo_equipo_snapshot TEXT,
                          payload TEXT NOT NULL DEFAULT '{}',
                          schema_snapshot TEXT NOT NULL DEFAULT '{}',
                          references_snapshot TEXT NOT NULL DEFAULT '[]',
                          resultado_global TEXT NOT NULL DEFAULT 'pendiente',
                          conclusion TEXT,
                          instrumentos TEXT,
                          firmado_por TEXT,
                          fecha_ejecucion DATETIME NULL,
                          tecnico_id INTEGER NULL,
                          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    if not self._sqlite_column_exists(cur, "ingreso_tests", "schema_snapshot"):
                        cur.execute(
                            """
                            ALTER TABLE ingreso_tests
                            ADD COLUMN schema_snapshot TEXT NOT NULL DEFAULT '{}'
                            """
                        )

                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ingreso_tests_ingreso ON ingreso_tests(ingreso_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_ingreso_tests_template_key ON ingreso_tests(template_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_ingreso_tests_updated_at ON ingreso_tests(updated_at)")

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS test_protocol_templates (
                          id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          type_key     TEXT NOT NULL,
                          template_key TEXT NOT NULL,
                          active       BOOLEAN NOT NULL DEFAULT TRUE,
                          doc          JSONB NOT NULL DEFAULT '{}'::jsonb,
                          created_by   INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          updated_by   INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                else:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS test_protocol_templates (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          type_key TEXT NOT NULL,
                          template_key TEXT NOT NULL,
                          active BOOLEAN NOT NULL DEFAULT 1,
                          doc TEXT NOT NULL DEFAULT '{}',
                          created_by INTEGER NULL,
                          updated_by INTEGER NULL,
                          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_test_protocol_templates_type_key ON test_protocol_templates(type_key)"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_test_protocol_templates_template_key ON test_protocol_templates(template_key)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_test_protocol_templates_active ON test_protocol_templates(active)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_test_protocol_templates_updated_at ON test_protocol_templates(updated_at)"
                )

                cur.execute("SELECT COUNT(*) FROM test_protocol_templates")
                row = cur.fetchone()
                count = int(row[0]) if row else 0
                if count == 0:
                    from service.test_protocols import build_seed_protocol_documents

                    docs = build_seed_protocol_documents()
                    for doc in docs:
                        type_key = str(doc.get("type_key") or "").strip()
                        template_key = str(doc.get("template_key") or "").strip()
                        if not type_key or not template_key:
                            continue
                        active = bool(doc.get("active", True))
                        raw_doc = json.dumps(doc, ensure_ascii=False)
                        if connection.vendor == "postgresql":
                            cur.execute(
                                """
                                INSERT INTO test_protocol_templates(
                                  type_key, template_key, active, doc, created_by, updated_by
                                ) VALUES (%s,%s,%s,%s::jsonb,NULL,NULL)
                                """,
                                [type_key, template_key, active, raw_doc],
                            )
                        else:
                            cur.execute(
                                """
                                INSERT INTO test_protocol_templates(
                                  type_key, template_key, active, doc, created_by, updated_by
                                ) VALUES (%s,%s,%s,%s,NULL,NULL)
                                """,
                                [type_key, template_key, int(active), raw_doc],
                            )
                        seeded += 1

        self.stdout.write(
            f"APLICADO OK: esquema ingreso_tests + test_protocol_templates (seeded={seeded})"
        )
