from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Crea/asegura schema de mantenimientos preventivos"

    def handle(self, *args, **opts):
        vendor = connection.vendor
        with transaction.atomic():
            with connection.cursor() as cur:
                if vendor == "postgresql":
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'preventivo_scope_type') THEN
                            CREATE TYPE preventivo_scope_type AS ENUM ('device','customer');
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'preventivo_period_unit') THEN
                            CREATE TYPE preventivo_period_unit AS ENUM ('dias','meses','anios');
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'preventivo_revision_state') THEN
                            CREATE TYPE preventivo_revision_state AS ENUM ('borrador','cerrada','cancelada');
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'preventivo_item_state') THEN
                            CREATE TYPE preventivo_item_state AS ENUM ('pendiente','ok','retirado','no_controlado');
                          END IF;
                        END $$;
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_planes (
                          id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          scope_type               preventivo_scope_type NOT NULL,
                          device_id                INTEGER NULL REFERENCES devices(id) ON DELETE CASCADE,
                          customer_id              INTEGER NULL REFERENCES customers(id) ON DELETE CASCADE,
                          periodicidad_valor       INTEGER NOT NULL,
                          periodicidad_unidad      preventivo_period_unit NOT NULL,
                          aviso_anticipacion_dias  INTEGER NOT NULL DEFAULT 30,
                          ultima_revision_fecha    DATE NULL,
                          proxima_revision_fecha   DATE NULL,
                          activa                   BOOLEAN NOT NULL DEFAULT TRUE,
                          observaciones            TEXT NULL,
                          created_by               INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          updated_by               INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          CONSTRAINT chk_preventivo_planes_scope
                            CHECK (
                              (scope_type = 'device' AND device_id IS NOT NULL AND customer_id IS NULL)
                              OR
                              (scope_type = 'customer' AND customer_id IS NOT NULL AND device_id IS NULL)
                            ),
                          CONSTRAINT chk_preventivo_planes_periodicidad CHECK (periodicidad_valor > 0),
                          CONSTRAINT chk_preventivo_planes_aviso CHECK (aviso_anticipacion_dias >= 0)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_revisiones (
                          id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          plan_id           INTEGER NOT NULL REFERENCES preventivo_planes(id) ON DELETE CASCADE,
                          estado            preventivo_revision_state NOT NULL DEFAULT 'borrador',
                          fecha_programada  DATE NULL,
                          fecha_realizada   DATE NULL,
                          realizada_por     INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          resumen           TEXT NULL,
                          created_by        INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          updated_by        INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          CONSTRAINT chk_preventivo_revisiones_cerrada_fecha
                            CHECK (estado <> 'cerrada' OR fecha_realizada IS NOT NULL)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_revision_items (
                          id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          revision_id          INTEGER NOT NULL REFERENCES preventivo_revisiones(id) ON DELETE CASCADE,
                          orden                INTEGER NOT NULL DEFAULT 1,
                          device_id            INTEGER NULL REFERENCES devices(id) ON DELETE SET NULL,
                          equipo_snapshot      TEXT NULL,
                          serie_snapshot       TEXT NULL,
                          interno_snapshot     TEXT NULL,
                          estado_item          preventivo_item_state NOT NULL DEFAULT 'pendiente',
                          motivo_no_control    TEXT NULL,
                          ubicacion_detalle    TEXT NULL,
                          accesorios_cambiados BOOLEAN NOT NULL DEFAULT FALSE,
                          accesorios_detalle   TEXT NULL,
                          notas                TEXT NULL,
                          arrastrar_proxima    BOOLEAN NOT NULL DEFAULT TRUE,
                          created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          CONSTRAINT chk_preventivo_items_motivo_no_control
                            CHECK (
                              estado_item <> 'no_controlado'
                              OR NULLIF(TRIM(COALESCE(motivo_no_control, '')), '') IS NOT NULL
                            )
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_plan_repuestos (
                          id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          plan_id                  INTEGER NOT NULL REFERENCES preventivo_planes(id) ON DELETE CASCADE,
                          repuesto_key             TEXT NOT NULL,
                          catalogo_repuesto_id     INTEGER NULL REFERENCES catalogo_repuestos(id) ON DELETE SET NULL,
                          nombre_repuesto          TEXT NOT NULL,
                          periodicidad_valor       INTEGER NOT NULL,
                          periodicidad_unidad      preventivo_period_unit NOT NULL,
                          aviso_anticipacion_dias  INTEGER NOT NULL DEFAULT 30,
                          ultima_revision_fecha    DATE NULL,
                          proxima_revision_fecha   DATE NULL,
                          activa                   BOOLEAN NOT NULL DEFAULT TRUE,
                          created_by               INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          updated_by               INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          CONSTRAINT chk_preventivo_plan_repuestos_periodicidad CHECK (periodicidad_valor > 0),
                          CONSTRAINT chk_preventivo_plan_repuestos_aviso CHECK (aviso_anticipacion_dias >= 0)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_repuesto_plantillas (
                          id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                          signature_key            TEXT NOT NULL,
                          signature_tipo_equipo    TEXT NULL,
                          signature_marca          TEXT NULL,
                          signature_modelo         TEXT NULL,
                          signature_variante       TEXT NULL,
                          repuesto_key             TEXT NOT NULL,
                          catalogo_repuesto_id     INTEGER NULL REFERENCES catalogo_repuestos(id) ON DELETE SET NULL,
                          nombre_repuesto          TEXT NOT NULL,
                          periodicidad_valor       INTEGER NOT NULL,
                          periodicidad_unidad      preventivo_period_unit NOT NULL,
                          aviso_anticipacion_dias  INTEGER NOT NULL DEFAULT 30,
                          activa                   BOOLEAN NOT NULL DEFAULT TRUE,
                          created_by               INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          updated_by               INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                          created_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          updated_at               TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                          CONSTRAINT chk_preventivo_plantillas_periodicidad CHECK (periodicidad_valor > 0),
                          CONSTRAINT chk_preventivo_plantillas_aviso CHECK (aviso_anticipacion_dias >= 0)
                        )
                        """
                    )
                else:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_planes (
                          id INTEGER PRIMARY KEY,
                          scope_type TEXT NOT NULL,
                          device_id INTEGER NULL,
                          customer_id INTEGER NULL,
                          periodicidad_valor INTEGER NOT NULL,
                          periodicidad_unidad TEXT NOT NULL,
                          aviso_anticipacion_dias INTEGER NOT NULL DEFAULT 30,
                          ultima_revision_fecha DATE NULL,
                          proxima_revision_fecha DATE NULL,
                          activa BOOLEAN NOT NULL DEFAULT 1,
                          observaciones TEXT NULL,
                          created_by INTEGER NULL,
                          updated_by INTEGER NULL,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_revisiones (
                          id INTEGER PRIMARY KEY,
                          plan_id INTEGER NOT NULL,
                          estado TEXT NOT NULL DEFAULT 'borrador',
                          fecha_programada DATE NULL,
                          fecha_realizada DATE NULL,
                          realizada_por INTEGER NULL,
                          resumen TEXT NULL,
                          created_by INTEGER NULL,
                          updated_by INTEGER NULL,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_revision_items (
                          id INTEGER PRIMARY KEY,
                          revision_id INTEGER NOT NULL,
                          orden INTEGER NOT NULL DEFAULT 1,
                          device_id INTEGER NULL,
                          equipo_snapshot TEXT NULL,
                          serie_snapshot TEXT NULL,
                          interno_snapshot TEXT NULL,
                          estado_item TEXT NOT NULL DEFAULT 'pendiente',
                          motivo_no_control TEXT NULL,
                          ubicacion_detalle TEXT NULL,
                          accesorios_cambiados BOOLEAN NOT NULL DEFAULT 0,
                          accesorios_detalle TEXT NULL,
                          notas TEXT NULL,
                          arrastrar_proxima BOOLEAN NOT NULL DEFAULT 1,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_plan_repuestos (
                          id INTEGER PRIMARY KEY,
                          plan_id INTEGER NOT NULL,
                          repuesto_key TEXT NOT NULL,
                          catalogo_repuesto_id INTEGER NULL,
                          nombre_repuesto TEXT NOT NULL,
                          periodicidad_valor INTEGER NOT NULL,
                          periodicidad_unidad TEXT NOT NULL,
                          aviso_anticipacion_dias INTEGER NOT NULL DEFAULT 30,
                          ultima_revision_fecha DATE NULL,
                          proxima_revision_fecha DATE NULL,
                          activa BOOLEAN NOT NULL DEFAULT 1,
                          created_by INTEGER NULL,
                          updated_by INTEGER NULL,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS preventivo_repuesto_plantillas (
                          id INTEGER PRIMARY KEY,
                          signature_key TEXT NOT NULL,
                          signature_tipo_equipo TEXT NULL,
                          signature_marca TEXT NULL,
                          signature_modelo TEXT NULL,
                          signature_variante TEXT NULL,
                          repuesto_key TEXT NOT NULL,
                          catalogo_repuesto_id INTEGER NULL,
                          nombre_repuesto TEXT NOT NULL,
                          periodicidad_valor INTEGER NOT NULL,
                          periodicidad_unidad TEXT NOT NULL,
                          aviso_anticipacion_dias INTEGER NOT NULL DEFAULT 30,
                          activa BOOLEAN NOT NULL DEFAULT 1,
                          created_by INTEGER NULL,
                          updated_by INTEGER NULL,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_planes_device ON preventivo_planes(device_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_planes_customer ON preventivo_planes(customer_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_revisiones_plan_fecha ON preventivo_revisiones(plan_id, fecha_programada DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_revisiones_plan_estado ON preventivo_revisiones(plan_id, estado)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_revision_items_revision_orden ON preventivo_revision_items(revision_id, orden)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_revision_items_revision_estado ON preventivo_revision_items(revision_id, estado_item)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_plan_repuestos_plan ON preventivo_plan_repuestos(plan_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_plan_repuestos_key ON preventivo_plan_repuestos(repuesto_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_plantillas_signature ON preventivo_repuesto_plantillas(signature_key)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_preventivo_plantillas_repuesto_key ON preventivo_repuesto_plantillas(repuesto_key)")

                if vendor == "postgresql":
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_preventivo_planes_next_active ON preventivo_planes(proxima_revision_fecha) WHERE activa = TRUE"
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_planes_device_active ON preventivo_planes(device_id) WHERE activa = TRUE AND device_id IS NOT NULL"
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_planes_customer_active ON preventivo_planes(customer_id) WHERE activa = TRUE AND customer_id IS NOT NULL"
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_plan_repuestos_plan_key ON preventivo_plan_repuestos(plan_id, repuesto_key) WHERE activa = TRUE"
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_plantillas_signature_key ON preventivo_repuesto_plantillas(signature_key, repuesto_key) WHERE activa = TRUE"
                    )
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF EXISTS (SELECT 1 FROM pg_proc WHERE proname='set_updated_at') THEN
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_preventivo_planes_updated_at') THEN
                              CREATE TRIGGER trg_preventivo_planes_updated_at
                              BEFORE UPDATE ON preventivo_planes
                              FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_preventivo_revisiones_updated_at') THEN
                              CREATE TRIGGER trg_preventivo_revisiones_updated_at
                              BEFORE UPDATE ON preventivo_revisiones
                              FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_preventivo_revision_items_updated_at') THEN
                              CREATE TRIGGER trg_preventivo_revision_items_updated_at
                              BEFORE UPDATE ON preventivo_revision_items
                              FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_preventivo_plan_repuestos_updated_at') THEN
                              CREATE TRIGGER trg_preventivo_plan_repuestos_updated_at
                              BEFORE UPDATE ON preventivo_plan_repuestos
                              FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_preventivo_plantillas_updated_at') THEN
                              CREATE TRIGGER trg_preventivo_plantillas_updated_at
                              BEFORE UPDATE ON preventivo_repuesto_plantillas
                              FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                            END IF;
                          END IF;
                        END $$;
                        """
                    )
                    cur.execute(
                        """
                        DO $$ BEGIN
                          IF EXISTS (
                            SELECT 1
                            FROM pg_proc p
                            JOIN pg_namespace n ON n.oid = p.pronamespace
                            WHERE p.proname='log_row_change' AND n.nspname='audit'
                          ) THEN
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_preventivo_planes') THEN
                              CREATE TRIGGER trg_audit_preventivo_planes
                              AFTER INSERT OR UPDATE OR DELETE ON preventivo_planes
                              FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_preventivo_revisiones') THEN
                              CREATE TRIGGER trg_audit_preventivo_revisiones
                              AFTER INSERT OR UPDATE OR DELETE ON preventivo_revisiones
                              FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_preventivo_revision_items') THEN
                              CREATE TRIGGER trg_audit_preventivo_revision_items
                              AFTER INSERT OR UPDATE OR DELETE ON preventivo_revision_items
                              FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_preventivo_plan_repuestos') THEN
                              CREATE TRIGGER trg_audit_preventivo_plan_repuestos
                              AFTER INSERT OR UPDATE OR DELETE ON preventivo_plan_repuestos
                              FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
                            END IF;
                            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_preventivo_plantillas') THEN
                              CREATE TRIGGER trg_audit_preventivo_plantillas
                              AFTER INSERT OR UPDATE OR DELETE ON preventivo_repuesto_plantillas
                              FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
                            END IF;
                          END IF;
                        END $$;
                        """
                    )

        self.stdout.write("APLICADO OK: preventivos (schema)")
