CREATE TABLE IF NOT EXISTS technical_work_catalog (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre TEXT NOT NULL CHECK (BTRIM(nombre) <> ''),
  activo BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_technical_work_name ON technical_work_catalog (LOWER(BTRIM(nombre)));
INSERT INTO technical_work_catalog(nombre) VALUES
 ('Limpieza y desinfección interna y externa'), ('Revisión técnica') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS technical_labor_rates (
  tipo_equipo TEXT PRIMARY KEY,
  primera_hora NUMERIC(12,2) NOT NULL CHECK (primera_hora >= 0),
  hora_adicional NUMERIC(12,2) NOT NULL CHECK (hora_adicional >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS technical_billing_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  diagnostico_limpieza NUMERIC(12,2) NULL CHECK (diagnostico_limpieza >= 0)
);
INSERT INTO technical_billing_config(id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS technical_interventions (
  ingreso_id INTEGER PRIMARY KEY REFERENCES ingresos(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL DEFAULT 1,
  scope_revision INTEGER NOT NULL DEFAULT 1,
  submitted_revision INTEGER NULL,
  minutos_estimados INTEGER NOT NULL DEFAULT 0 CHECK (minutos_estimados BETWEEN 0 AND 525600),
  minutos_reales INTEGER NOT NULL DEFAULT 0 CHECK (minutos_reales BETWEEN 0 AND 525600),
  sin_falla BOOLEAN NOT NULL DEFAULT FALSE,
  observaciones TEXT NOT NULL DEFAULT '',
  submitted_at TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by INTEGER NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS technical_intervention_items (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id INTEGER NOT NULL REFERENCES technical_interventions(ingreso_id) ON DELETE CASCADE,
  tipo TEXT NOT NULL CHECK (tipo IN ('trabajo','repuesto')),
  trabajo_id INTEGER NULL REFERENCES technical_work_catalog(id),
  repuesto_id INTEGER NULL REFERENCES catalogo_repuestos(id),
  cantidad_prevista NUMERIC(10,2) NOT NULL CHECK (cantidad_prevista > 0),
  cantidad_realizada NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (cantidad_realizada >= 0),
  observaciones TEXT NOT NULL DEFAULT '',
  CHECK ((tipo='trabajo' AND trabajo_id IS NOT NULL AND repuesto_id IS NULL)
      OR (tipo='repuesto' AND repuesto_id IS NOT NULL AND trabajo_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_technical_items_ingreso ON technical_intervention_items(ingreso_id);
CREATE TABLE IF NOT EXISTS technical_catalog_requests (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id INTEGER NOT NULL REFERENCES technical_interventions(ingreso_id) ON DELETE CASCADE,
  tipo TEXT NOT NULL CHECK (tipo IN ('trabajo','repuesto')),
  nombre TEXT NOT NULL CHECK (BTRIM(nombre) <> ''),
  cantidad NUMERIC(10,2) NOT NULL DEFAULT 1 CHECK (cantidad > 0),
  observaciones TEXT NOT NULL DEFAULT '',
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','aprobada','rechazada')),
  resolved_catalog_id INTEGER NULL,
  respuesta TEXT NOT NULL DEFAULT '',
  created_by INTEGER NOT NULL REFERENCES users(id),
  resolved_by INTEGER NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_technical_requests_ingreso ON technical_catalog_requests(ingreso_id,estado);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS technical_revision INTEGER NULL;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS technical_snapshot JSONB NULL;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS labor_snapshot JSONB NULL;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS supersedes_quote_id INTEGER NULL REFERENCES quotes(id);
CREATE TABLE IF NOT EXISTS technical_quote_documents (
  quote_id INTEGER PRIMARY KEY REFERENCES quotes(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  pdf BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS stock_applied BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS technical_item_id INTEGER NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_quote_technical_item ON quote_items(quote_id,technical_item_id)
  WHERE technical_item_id IS NOT NULL;

-- audit.log_row_change identifica los registros por su columna numérica id.
ALTER TABLE technical_interventions ADD COLUMN IF NOT EXISTS id INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
ALTER TABLE technical_labor_rates ADD COLUMN IF NOT EXISTS id INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;

-- Auditar tiempos, alcance, altas y tarifas con el mismo mecanismo que el resto de NEXORA.
DO $$ DECLARE tabla TEXT; BEGIN
 IF to_regprocedure('audit.log_row_change()') IS NOT NULL THEN
  FOREACH tabla IN ARRAY ARRAY['technical_interventions','technical_intervention_items',
     'technical_work_catalog','technical_labor_rates','technical_billing_config','technical_catalog_requests'] LOOP
   IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_' || tabla) THEN
    EXECUTE format('CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION audit.log_row_change()', 'trg_audit_' || tabla, tabla);
   END IF;
  END LOOP;
 END IF;
END $$;
