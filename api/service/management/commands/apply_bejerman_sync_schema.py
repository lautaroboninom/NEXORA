from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = ["bejerman_sync_jobs", "bejerman_article_mappings", "bejerman_stock_lotes"]


class Command(BaseCommand):
    help = "Aplica el esquema de sincronización con Bejerman"

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
                      company_key       TEXT NOT NULL DEFAULT 'SEPID',
                      status            TEXT NOT NULL DEFAULT 'pending',
                      attempts          INTEGER NOT NULL DEFAULT 0,
                      next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      last_error        TEXT NULL,
                      article_code      TEXT NULL,
                      actor_user_id     INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
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
                cur.execute("ALTER TABLE bejerman_sync_jobs ADD COLUMN IF NOT EXISTS article_code TEXT")
                cur.execute("ALTER TABLE bejerman_sync_jobs ADD COLUMN IF NOT EXISTS company_key TEXT")
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'ingresos'
                       AND column_name = 'empresa_bejerman'
                     LIMIT 1
                    """
                )
                if cur.fetchone():
                    cur.execute(
                        """
                        UPDATE bejerman_sync_jobs j
                           SET company_key = COALESCE(NULLIF(t.empresa_bejerman, ''), 'SEPID')
                          FROM ingresos t
                         WHERE t.id = j.ingreso_id
                           AND NULLIF(TRIM(COALESCE(j.company_key, '')), '') IS NULL
                        """
                    )
                cur.execute("ALTER TABLE bejerman_sync_jobs ALTER COLUMN company_key SET DEFAULT 'SEPID'")
                cur.execute(
                    "UPDATE bejerman_sync_jobs SET company_key = 'SEPID' WHERE NULLIF(TRIM(COALESCE(company_key, '')), '') IS NULL"
                )
                cur.execute("ALTER TABLE bejerman_sync_jobs ALTER COLUMN company_key SET NOT NULL")
                cur.execute(
                    "ALTER TABLE bejerman_sync_jobs ADD COLUMN IF NOT EXISTS actor_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL"
                )
                # Un job cuyo `sync_type` ya no tiene implementacion -- los
                # `manual_stock_restore_str_audit_20260701` que dejo una auditoria vieja --
                # no puede terminar nunca: falla, se bloquea y se queda en el panel
                # ofreciendo un "Reintentar" que vuelve a fallar. `cancelled` es la salida
                # terminal que faltaba para poder cerrarlos sin borrar el rastro.
                cur.execute("ALTER TABLE bejerman_sync_jobs DROP CONSTRAINT IF EXISTS chk_bejerman_sync_jobs_status")
                cur.execute(
                    "ALTER TABLE bejerman_sync_jobs ADD CONSTRAINT chk_bejerman_sync_jobs_status "
                    "CHECK (status IN ('pending','running','succeeded','failed','blocked','cancelled'))"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_article_mappings (
                      id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      model_id             INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
                      variante             TEXT NOT NULL DEFAULT '',
                      variante_norm        TEXT NOT NULL DEFAULT '',
                      article_code         TEXT NOT NULL,
                      article_description  TEXT NULL,
                      match_source         TEXT NOT NULL DEFAULT 'manual',
                      source_payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
                      confirmed_by         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      confirmed_at         TIMESTAMPTZ NULL,
                      created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_article_mappings_code CHECK (NULLIF(TRIM(article_code), '') IS NOT NULL),
                      CONSTRAINT chk_bejerman_article_mappings_source CHECK (match_source IN ('manual','auto'))
                    )
                    """
                )
                # Upgrade de tablas creadas por versiones anteriores del comando.
                for statement in (
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS variante TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS variante_norm TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS article_description TEXT NULL",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS match_source TEXT NOT NULL DEFAULT 'manual'",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS source_payload JSONB NOT NULL DEFAULT '{}'::jsonb",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS confirmed_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ NULL",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
                    "ALTER TABLE bejerman_article_mappings ADD COLUMN IF NOT EXISTS company_key TEXT",
                ):
                    cur.execute(statement)
                # SEPID y MG BIO son catálogos de artículos independientes en Bejerman: un mismo
                # código numérico puede significar artículos distintos en cada empresa. Antes esta
                # tabla mapeaba (model_id, variante) -> código sin distinguir empresa, lo que hacía
                # que un código "aprendido" en una empresa se reutilizara para emitir documentos en
                # la otra (causa del RDA con descripción/código no relacionados de Praxair/MG BIO).
                cur.execute(
                    "UPDATE bejerman_article_mappings SET company_key = 'SEPID' "
                    "WHERE NULLIF(TRIM(COALESCE(company_key, '')), '') IS NULL"
                )
                cur.execute("ALTER TABLE bejerman_article_mappings ALTER COLUMN company_key SET DEFAULT 'SEPID'")
                cur.execute("ALTER TABLE bejerman_article_mappings ALTER COLUMN company_key SET NOT NULL")
                cur.execute(
                    "DROP INDEX IF EXISTS uq_bejerman_article_mappings_model_variant"
                )
                # Un equipo propio nuevo se da de alta en Equipos con su MG y sin ingreso: la
                # transferencia VAL->STL que lo deja disponible para alquiler no tiene ninguna OS
                # de la que colgarse. `device_id` alcanza para identificar la fila.
                cur.execute("ALTER TABLE bejerman_sync_jobs ALTER COLUMN ingreso_id DROP NOT NULL")
                # El índice viejo era total, así que con `ingreso_id` NULL dejaba de deduplicar:
                # dos altas de la misma partida crearían dos jobs y duplicarían el ENT (es el
                # incidente de sql/ops/bejerman_borrar_ent_duplicados_20260608.sql). Se parte en
                # dos parciales que cubren las dos formas de identificar el trabajo.
                cur.execute("DROP INDEX IF EXISTS uq_bejerman_sync_jobs_type_ingreso")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_sync_jobs_type_ingreso
                      ON bejerman_sync_jobs(sync_type, ingreso_id)
                      WHERE ingreso_id IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_sync_jobs_type_device
                      ON bejerman_sync_jobs(sync_type, device_id)
                      WHERE ingreso_id IS NULL
                    """
                )
                # Alta masiva de equipos propios: la transferencia VAL->STL de todos los equipos
                # viaja en UNA SAL y UNA ENT. El lote guarda la composición congelada y la fase,
                # porque un reenvío tiene que repetir exactamente el mismo comprobante (Bejerman
                # deduplica por IdOrigen). Los jobs por equipo siguen existiendo y apuntan acá.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_stock_lotes (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      sync_type         TEXT NOT NULL,
                      company_key       TEXT NOT NULL DEFAULT 'SEPID',
                      source_deposit    TEXT NOT NULL,
                      target_deposit    TEXT NOT NULL,
                      phase             TEXT NOT NULL DEFAULT 'pending',
                      last_error        TEXT NULL,
                      actor_user_id     INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      request_payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
                      response_payload  JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_bejerman_stock_lotes_phase CHECK (
                        phase IN ('pending','sal_pending','sal_done','ent_pending','done','review')
                      ),
                      CONSTRAINT chk_bejerman_stock_lotes_deposits CHECK (
                        NULLIF(TRIM(source_deposit), '') IS NOT NULL
                        AND NULLIF(TRIM(target_deposit), '') IS NOT NULL
                        AND source_deposit <> target_deposit
                      )
                    )
                    """
                )
                cur.execute(
                    "ALTER TABLE bejerman_sync_jobs ADD COLUMN IF NOT EXISTS lote_id INTEGER NULL "
                    "REFERENCES bejerman_stock_lotes(id) ON DELETE SET NULL"
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_lote
                      ON bejerman_sync_jobs(lote_id)
                      WHERE lote_id IS NOT NULL
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
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_article_code
                      ON bejerman_sync_jobs(article_code)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_actor_user
                      ON bejerman_sync_jobs(actor_user_id)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_company_key
                      ON bejerman_sync_jobs(company_key)
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_article_mappings_model_variant_company
                      ON bejerman_article_mappings(model_id, variante_norm, company_key)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_article_mappings_article_code
                      ON bejerman_article_mappings(article_code)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_bejerman_article_mappings_company_key
                      ON bejerman_article_mappings(company_key)
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
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_article_mappings_set_updated_at') THEN
                            CREATE TRIGGER trg_bejerman_article_mappings_set_updated_at
                            BEFORE UPDATE ON bejerman_article_mappings
                            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                          END IF;
                          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_stock_lotes_set_updated_at') THEN
                            CREATE TRIGGER trg_bejerman_stock_lotes_set_updated_at
                            BEFORE UPDATE ON bejerman_stock_lotes
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

                required_columns = {
                    "bejerman_sync_jobs": {
                        "article_code",
                        "actor_user_id",
                        "company_key",
                        "request_payload",
                        "response_payload",
                        "lote_id",
                    },
                    "bejerman_stock_lotes": {
                        "phase",
                        "last_error",
                        "request_payload",
                        "response_payload",
                    },
                    "bejerman_article_mappings": {
                        "model_id",
                        "variante_norm",
                        "article_code",
                        "match_source",
                        "source_payload",
                        "company_key",
                    },
                }
                for table_name, columns in required_columns.items():
                    placeholders = ",".join(["%s"] * len(columns))
                    cur.execute(
                        f"""
                        SELECT column_name
                          FROM information_schema.columns
                         WHERE table_schema = ANY(current_schemas(true))
                           AND table_name = %s
                           AND column_name IN ({placeholders})
                        """,
                        [table_name, *columns],
                    )
                    found_columns = {r[0] for r in cur.fetchall()}
                    missing_columns = sorted(columns - found_columns)
                    if missing_columns:
                        raise RuntimeError(
                            f"No se aplicaron columnas requeridas en {table_name}: {', '.join(missing_columns)}"
                        )

        self.stdout.write("APLICADO OK: esquema de sincronización Bejerman")
