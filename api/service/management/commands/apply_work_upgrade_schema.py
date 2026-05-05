from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = [
    "work_alert_rules",
    "work_objectives",
    "work_alert_snoozes",
    "work_notification_state",
]

DEFAULT_RULES = [
    (
        "presupuesto_sin_aprobar",
        "Presupuesto emitido sin aprobar",
        "Presupuestos emitidos que llevan demasiados días sin aprobación.",
        7,
        "dias",
        "critical",
        True,
        True,
    ),
    (
        "liberado_sin_entregar",
        "Liberado sin entregar",
        "Equipos liberados que siguen en taller.",
        3,
        "dias",
        "warning",
        True,
        True,
    ),
    (
        "derivado_sin_devolucion",
        "Derivado con espera",
        "Equipos derivados a proveedor externo sin devolución.",
        7,
        "dias",
        "warning",
        True,
        True,
    ),
    (
        "wip_critico",
        "Trabajo con espera crítica",
        "Equipos en taller con demasiados días desde el ingreso.",
        16,
        "dias",
        "critical",
        True,
        True,
    ),
    (
        "sin_tecnico",
        "Sin técnico asignado",
        "Equipos en taller que todavía no tienen técnico responsable.",
        1,
        "dias",
        "warning",
        True,
        False,
    ),
    (
        "preventivo_vencido",
        "Preventivo vencido",
        "Planes de mantenimiento preventivo con fecha vencida.",
        0,
        "dias",
        "critical",
        True,
        True,
    ),
    (
        "preventivo_proximo",
        "Preventivo próximo",
        "Planes de mantenimiento preventivo próximos a vencer.",
        30,
        "dias",
        "warning",
        True,
        True,
    ),
]


class Command(BaseCommand):
    help = "Aplica el esquema del centro de trabajo, reglas de alerta y objetivos operativos"

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
                    CREATE TABLE IF NOT EXISTS work_alert_rules (
                      id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      rule_key        TEXT NOT NULL UNIQUE,
                      label           TEXT NOT NULL,
                      description     TEXT NULL,
                      threshold_value NUMERIC(12,2) NOT NULL,
                      threshold_unit  TEXT NOT NULL DEFAULT 'dias',
                      severity        TEXT NOT NULL DEFAULT 'warning',
                      enabled         BOOLEAN NOT NULL DEFAULT TRUE,
                      email_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
                      created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_work_alert_rules_threshold CHECK (threshold_value >= 0),
                      CONSTRAINT chk_work_alert_rules_unit CHECK (threshold_unit IN ('horas','dias','dias_habiles','cantidad','porcentaje')),
                      CONSTRAINT chk_work_alert_rules_severity CHECK (severity IN ('info','warning','critical'))
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_objectives (
                      id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      scope_type      TEXT NOT NULL,
                      technician_id   INTEGER NULL REFERENCES users(id) ON DELETE CASCADE,
                      period_type     TEXT NOT NULL,
                      metric_key      TEXT NOT NULL,
                      label           TEXT NOT NULL,
                      target_value    NUMERIC(12,2) NOT NULL,
                      direction       TEXT NOT NULL DEFAULT 'gte',
                      active          BOOLEAN NOT NULL DEFAULT TRUE,
                      valid_from      DATE NOT NULL DEFAULT CURRENT_DATE,
                      valid_to        DATE NULL,
                      created_by      INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      updated_by      INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_work_objectives_scope CHECK (
                        (scope_type = 'global' AND technician_id IS NULL)
                        OR (scope_type = 'technician' AND technician_id IS NOT NULL)
                      ),
                      CONSTRAINT chk_work_objectives_period CHECK (period_type IN ('daily','weekly')),
                      CONSTRAINT chk_work_objectives_direction CHECK (direction IN ('gte','lte')),
                      CONSTRAINT chk_work_objectives_target CHECK (target_value >= 0),
                      CONSTRAINT chk_work_objectives_validity CHECK (valid_to IS NULL OR valid_to >= valid_from)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_alert_snoozes (
                      id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                      alert_key      TEXT NOT NULL,
                      alert_ref      TEXT NOT NULL,
                      snoozed_until  TIMESTAMPTZ NOT NULL,
                      created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_work_alert_snoozes_ref CHECK (NULLIF(TRIM(alert_ref), '') IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_notification_state (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      channel           TEXT NOT NULL,
                      notification_key  TEXT NOT NULL,
                      user_id           INTEGER NULL REFERENCES users(id) ON DELETE CASCADE,
                      last_sent_at      TIMESTAMPTZ NULL,
                      payload_hash      TEXT NULL,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_work_notification_state_channel CHECK (channel IN ('email','internal'))
                    )
                    """
                )

                cur.execute("CREATE INDEX IF NOT EXISTS ix_work_alert_rules_enabled ON work_alert_rules(enabled)")
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_work_objectives_active_period
                      ON work_objectives(period_type, active, valid_from, valid_to)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_work_objectives_technician
                      ON work_objectives(technician_id, period_type, active)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_work_alert_snoozes_user_until
                      ON work_alert_snoozes(user_id, snoozed_until)
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_work_alert_snoozes_user_alert
                      ON work_alert_snoozes(user_id, alert_key, alert_ref)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_work_notification_state_last_sent
                      ON work_notification_state(last_sent_at)
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_work_notification_state_key
                      ON work_notification_state(channel, notification_key, COALESCE(user_id, -1))
                    """
                )

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_work_alert_rules_set_updated_at') THEN
                            CREATE TRIGGER trg_work_alert_rules_set_updated_at
                            BEFORE UPDATE ON work_alert_rules
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_work_objectives_set_updated_at') THEN
                            CREATE TRIGGER trg_work_objectives_set_updated_at
                            BEFORE UPDATE ON work_objectives
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_work_notification_state_set_updated_at') THEN
                            CREATE TRIGGER trg_work_notification_state_set_updated_at
                            BEFORE UPDATE ON work_notification_state
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                        END $$;
                        """
                    )

                for row in DEFAULT_RULES:
                    cur.execute(
                        """
                        INSERT INTO work_alert_rules(
                          rule_key, label, description, threshold_value, threshold_unit,
                          severity, enabled, email_enabled
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (rule_key) DO NOTHING
                        """,
                        row,
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

        self.stdout.write("APLICADO OK: esquema del centro de trabajo y objetivos operativos")
