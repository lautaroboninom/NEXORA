-- schema/postgres.sql
-- Esquema consolidado para PostgreSQL (12+)
-- Unifica DDL, índices, vistas y triggers necesarios para la app.
-- Objetivo: base prolija y mínima sin parches adicionales.

SET TIME ZONE 'America/Argentina/Buenos_Aires';
CREATE EXTENSION IF NOT EXISTS citext;

-- =============================
-- Tipos enumerados (dominios)
-- =============================
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticket_state') THEN
    CREATE TYPE ticket_state AS ENUM (
      'ingresado','diagnosticado','presupuestado','reparar','controlado_sin_defecto','reparado','entregado','baja','derivado','liberado','alquilado','vendido_pendiente_entrega','vendido_entregado'
    );
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
    CREATE TYPE motivo_ingreso AS ENUM (
      'reparación','service preventivo','baja alquiler','reparación alquiler','urgente control','devolución demo','cotización de equipo','Revisión Técnica','otros'
    );
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'disposicion_type') THEN
    CREATE TYPE disposicion_type AS ENUM ('normal','para_repuesto');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'quote_estado') THEN
    CREATE TYPE quote_estado AS ENUM ('pendiente','emitido','aprobado','rechazado','presupuestado','no_aplica');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'quote_item_tipo') THEN
    CREATE TYPE quote_item_tipo AS ENUM ('repuesto','mano_obra','servicio');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'deriv_estado') THEN
    CREATE TYPE deriv_estado AS ENUM ('derivado','en_servicio','devuelto','entregado_cliente');
  END IF;
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

-- Agregar motivo de revisión técnica en bases ya existentes.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
    IF NOT EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = 'Revisión Técnica'
    ) THEN
      ALTER TYPE motivo_ingreso ADD VALUE 'Revisión Técnica';
    END IF;
  END IF;
END $$;

-- Estados de venta de equipos propios MG.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticket_state') THEN
    IF NOT EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'ticket_state'
         AND e.enumlabel = 'vendido_pendiente_entrega'
    ) THEN
      ALTER TYPE ticket_state ADD VALUE 'vendido_pendiente_entrega' AFTER 'alquilado';
    END IF;

    IF NOT EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'ticket_state'
         AND e.enumlabel = 'vendido_entregado'
    ) THEN
      ALTER TYPE ticket_state ADD VALUE 'vendido_entregado' AFTER 'vendido_pendiente_entrega';
    END IF;
  END IF;
END $$;

-- Corrección idempotente para bases existentes creadas con etiquetas de enum mal codificadas.
DO $$
DECLARE
  bad_reparacion TEXT := convert_from(decode('7265706172616369c383c2b36e', 'hex'), 'UTF8');
  bad_reparacion_alquiler TEXT := convert_from(decode('7265706172616369c383c2b36e20616c7175696c6572', 'hex'), 'UTF8');
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'motivo_ingreso') THEN
    IF EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = bad_reparacion
    ) AND NOT EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = 'reparación'
    ) THEN
      EXECUTE format('ALTER TYPE motivo_ingreso RENAME VALUE %L TO %L', bad_reparacion, 'reparación');
    END IF;

    IF EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = bad_reparacion_alquiler
    ) AND NOT EXISTS (
      SELECT 1
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
       WHERE t.typname = 'motivo_ingreso'
         AND e.enumlabel = 'reparación alquiler'
    ) THEN
      EXECUTE format('ALTER TYPE motivo_ingreso RENAME VALUE %L TO %L', bad_reparacion_alquiler, 'reparación alquiler');
    END IF;
  END IF;
END $$;

-- =============================
-- Funciones utilitarias / triggers
-- =============================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION audit_log_no_update()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION sync_quote_with_ingreso()
RETURNS TRIGGER AS $$
DECLARE
  v_ingreso_id INTEGER;
  v_cur_estado ticket_state;
  v_quote_estado quote_estado := 'pendiente'::quote_estado;
  v_permite_reparacion BOOLEAN := TRUE;
  v_motivo TEXT := '';
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_ingreso_id := OLD.ingreso_id;
  ELSE
    v_ingreso_id := NEW.ingreso_id;
  END IF;
  SELECT
    estado,
    COALESCE(permite_reparacion, TRUE),
    COALESCE(CAST(motivo AS TEXT), '')
  INTO v_cur_estado, v_permite_reparacion, v_motivo
  FROM ingresos
  WHERE id = v_ingreso_id;

  SELECT q.estado
  INTO v_quote_estado
  FROM quotes q
  WHERE q.ingreso_id = v_ingreso_id
  ORDER BY COALESCE(q.version_num, 1) DESC, q.id DESC
  LIMIT 1;

  v_quote_estado := COALESCE(v_quote_estado, 'pendiente'::quote_estado);
  UPDATE ingresos
     SET presupuesto_estado = (
            CASE v_quote_estado
              WHEN 'emitido' THEN 'presupuestado'::quote_estado
              WHEN 'presupuestado' THEN 'presupuestado'::quote_estado
              WHEN 'aprobado' THEN 'aprobado'::quote_estado
              WHEN 'rechazado' THEN 'rechazado'::quote_estado
              WHEN 'no_aplica' THEN 'no_aplica'::quote_estado
              ELSE 'pendiente'::quote_estado
            END
         ),
         estado = (
            CASE
              WHEN v_quote_estado = 'aprobado'
                   AND v_cur_estado IN ('ingresado','diagnosticado','presupuestado')
                   AND NOT (
                     LOWER(v_motivo) IN ('cotización de equipo', 'cotizacion de equipo')
                     AND NOT COALESCE(v_permite_reparacion, TRUE)
                   )
              THEN 'reparar'::ticket_state
              ELSE v_cur_estado
            END
         )
   WHERE id = v_ingreso_id;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =============================
-- Tablas base
-- =============================
CREATE TABLE IF NOT EXISTS users (
  id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre           TEXT        NOT NULL,
  email            CITEXT NOT NULL UNIQUE,
  hash_pw          TEXT,
  rol              TEXT NOT NULL,
  activo           BOOLEAN     NOT NULL DEFAULT TRUE,
  creado_en        TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_permission_overrides (
  id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  permission_code  TEXT    NOT NULL,
  effect           TEXT    NOT NULL,
  updated_by       INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_user_permission_overrides UNIQUE (user_id, permission_code),
  CONSTRAINT chk_user_permission_effect CHECK (effect IN ('allow','deny'))
);

CREATE INDEX IF NOT EXISTS idx_user_permission_overrides_user_id
  ON user_permission_overrides(user_id);
CREATE INDEX IF NOT EXISTS idx_user_permission_overrides_permission_code
  ON user_permission_overrides(permission_code);
CREATE OR REPLACE FUNCTION trg_set_updated_at_user_permission_overrides()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS set_updated_at_user_permission_overrides ON user_permission_overrides;
CREATE TRIGGER set_updated_at_user_permission_overrides
BEFORE UPDATE ON user_permission_overrides
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_user_permission_overrides();

CREATE TABLE IF NOT EXISTS marcas (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre      TEXT NOT NULL,
  tecnico_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT uq_marcas_nombre UNIQUE (nombre)
);

CREATE TABLE IF NOT EXISTS models (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  marca_id    INTEGER NOT NULL REFERENCES marcas(id) ON DELETE RESTRICT,
  nombre      TEXT NOT NULL,
  tecnico_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  tipo_equipo TEXT NULL,
  variante    TEXT NULL,
  CONSTRAINT uq_models_marca_nombre UNIQUE (marca_id, nombre)
);

CREATE TABLE IF NOT EXISTS locations (
  id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre  TEXT NOT NULL UNIQUE
);

-- Seed mínimo indispensable
INSERT INTO locations(nombre) VALUES
  ('Taller'),
  ('Sarmiento'),
  ('Estantería de Alquiler'),
  ('-')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS customers (
  id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cod_empresa   TEXT,
  razon_social  TEXT NOT NULL,
  cuit          TEXT,
  contacto      TEXT,
  telefono      TEXT,
  telefono_2    TEXT,
  email         TEXT
);

-- Seed minimo para flujos de equipos particulares
INSERT INTO customers(cod_empresa, razon_social)
SELECT NULL, 'Particular'
WHERE NOT EXISTS (
  SELECT 1 FROM customers WHERE LOWER(razon_social) = 'particular'
);

-- TODO agregar etiq_garantia_ok a NuevoIgreso
CREATE TABLE IF NOT EXISTS devices (
  id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id      INTEGER  NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
  marca_id         INTEGER  NULL REFERENCES marcas(id) ON DELETE SET NULL,
  model_id         INTEGER  NULL REFERENCES models(id) ON DELETE SET NULL,
  numero_serie     TEXT,
  numero_interno   TEXT,    -- MG|NM|NV|CE #### (normalizado)
  mg_estado        TEXT NOT NULL DEFAULT 'activo' CHECK (mg_estado IN ('activo','inactivo_venta')),
  mg_inactivo_desde TIMESTAMPTZ,
  mg_venta_fecha   TIMESTAMPTZ,
  mg_venta_factura_numero TEXT,
  mg_venta_remito_numero TEXT,
  mg_venta_observaciones TEXT,
  mg_venta_usuario_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  mg_venta_customer_id INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL,
  mg_venta_numero_alternativo TEXT,
  tipo_equipo      TEXT,
  variante         TEXT,
  garantia_vence   DATE,
  ubicacion_id     INTEGER NULL REFERENCES locations(id) ON DELETE SET NULL,
  propietario      TEXT,
  propietario_nombre   TEXT,
  propietario_contacto TEXT,
  propietario_doc      TEXT,
  n_de_control     TEXT,    -- N° faja garantía (snapshot del último ingreso)
  alquilado        BOOLEAN NOT NULL DEFAULT FALSE,
  alquiler_a       TEXT
);

-- índices funcionales y unicidad (normalizados)
-- Unicidad por número de serie normalizado (UPPER, sin espacios ni guiones)
CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_ns_norm
  ON devices ((UPPER(REPLACE(REPLACE(numero_serie, ' ', ''), '-', ''))))
  WHERE NULLIF(TRIM(numero_serie), '') IS NOT NULL;

-- Unicidad por número interno normalizado a 'XX ####' (MG|NM|NV|CE)
DO $$
BEGIN
  BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_numint_norm
      ON devices ((UPPER(REGEXP_REPLACE(numero_interno,
           '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0')))))
      WHERE numero_interno ~* '^(MG|NM|NV|CE)\s*\d{1,4}$';
  EXCEPTION WHEN OTHERS THEN
    -- Si hay duplicados en bases legacy, mantener al menos indice no-unico.
    CREATE INDEX IF NOT EXISTS idx_devices_numint_norm
      ON devices ((UPPER(REGEXP_REPLACE(numero_interno,
           '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0')))))
      WHERE numero_interno ~* '^(MG|NM|NV|CE)\s*\d{1,4}$';
  END;
END $$;

CREATE TABLE IF NOT EXISTS ingresos (
  id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id            INTEGER NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  estado               ticket_state NOT NULL DEFAULT 'ingresado',
  motivo               motivo_ingreso NOT NULL,
  permite_reparacion   BOOLEAN NOT NULL DEFAULT TRUE,
  fecha_ingreso        TIMESTAMPTZ NULL,
  fecha_creacion       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  fecha_servicio       TIMESTAMPTZ NULL,
  fecha_entrega        TIMESTAMPTZ NULL,
  ubicacion_id         INTEGER NULL REFERENCES locations(id) ON DELETE SET NULL,
  disposicion          disposicion_type NOT NULL DEFAULT 'normal',
  informe_preliminar   TEXT,
  accesorios           TEXT,
  equipo_variante      TEXT,
  remito_ingreso       TEXT,
  remito_salida        TEXT,
  factura_numero       TEXT,
  recibido_por         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  comentarios          TEXT,
  garantia_reparacion  BOOLEAN,
  garantia_fabrica     BOOLEAN,
  faja_garantia        TEXT,
  etiq_garantia_ok     BOOLEAN,
  presupuesto_estado   quote_estado NOT NULL DEFAULT 'pendiente',
  presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL,
  presupuesto_rechazado_quote_id INTEGER NULL,
  asignado_a           INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  etiqueta_qr          TEXT NULL,
  alquilado            BOOLEAN,
  alquiler_a           TEXT,
  alquiler_remito      TEXT,
  alquiler_fecha       DATE,
  propietario_nombre   TEXT,
  propietario_contacto TEXT,
  propietario_doc      TEXT,
  descripcion_problema  TEXT,
  trabajos_realizados   TEXT,
  serial_cambio         TEXT,
  resolucion           TEXT NULL
);

ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_cobro_neto NUMERIC(12,2);
ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_quote_id INTEGER;

-- Compat de schema para bases legacy (evita parches manuales fase 1/2)
ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_estado TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_inactivo_desde TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_fecha TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_factura_numero TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_remito_numero TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_observaciones TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_usuario_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_customer_id INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_numero_alternativo TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS garantia_vence DATE;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL REFERENCES locations(id) ON DELETE SET NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_nombre TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_contacto TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_doc TEXT;
UPDATE devices SET mg_estado = 'activo' WHERE mg_estado IS NULL OR TRIM(mg_estado) = '';
UPDATE devices SET mg_estado = 'activo' WHERE mg_estado NOT IN ('activo','inactivo_venta');
ALTER TABLE devices ALTER COLUMN mg_estado SET DEFAULT 'activo';
ALTER TABLE devices ALTER COLUMN mg_estado SET NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'chk_devices_mg_estado'
       AND conrelid = 'devices'::regclass
  ) THEN
    ALTER TABLE devices
      ADD CONSTRAINT chk_devices_mg_estado
      CHECK (mg_estado IN ('activo','inactivo_venta'));
  END IF;
END
$$;

ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN;
ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN;
ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT;
ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_contacto TEXT;
ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_doc TEXT;
ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS permite_reparacion BOOLEAN NOT NULL DEFAULT TRUE;
UPDATE ingresos
   SET permite_reparacion = FALSE
 WHERE LOWER(CAST(motivo AS TEXT)) IN ('cotización de equipo', 'cotizacion de equipo');

CREATE TABLE IF NOT EXISTS device_mg_events (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  accion TEXT NOT NULL CHECK (accion IN ('venta', 'reactivacion')),
  numero_interno_snapshot TEXT,
  fecha_evento TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  factura_numero TEXT,
  remito_numero TEXT,
  observaciones TEXT,
  usuario_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  ingreso_id INTEGER NULL REFERENCES ingresos(id) ON DELETE SET NULL,
  venta_customer_id INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL,
  venta_numero_alternativo TEXT,
  source TEXT NOT NULL DEFAULT 'equipos' CHECK (source IN ('equipos', 'service_sheet'))
);
ALTER TABLE device_mg_events ADD COLUMN IF NOT EXISTS venta_customer_id INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL;
ALTER TABLE device_mg_events ADD COLUMN IF NOT EXISTS venta_numero_alternativo TEXT;
CREATE INDEX IF NOT EXISTS idx_device_mg_events_device_fecha_desc
  ON device_mg_events (device_id, fecha_evento DESC);
CREATE INDEX IF NOT EXISTS idx_device_mg_events_fecha_desc
  ON device_mg_events (fecha_evento DESC);

-- Backfill minimo para bases antiguas
WITH cand AS (
  SELECT d.id,
         UPPER(REGEXP_REPLACE(NULLIF(d.n_de_control,''),
           '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0'))) AS norm
    FROM devices d
   WHERE (d.numero_interno IS NULL OR d.numero_interno = '')
     AND NULLIF(d.n_de_control,'') IS NOT NULL
)
UPDATE devices d
   SET numero_interno = c.norm
  FROM cand c
 WHERE d.id = c.id
   AND c.norm IS NOT NULL
   AND NOT EXISTS (
     SELECT 1
       FROM devices x
      WHERE x.id <> d.id
        AND UPPER(REGEXP_REPLACE(x.numero_interno,
            '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0'))) = c.norm
   );

UPDATE devices d
   SET numero_interno = UPPER(REGEXP_REPLACE(d.numero_serie, '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0')))
 WHERE d.numero_serie ~* '^(MG|NM|NV|CE)\s*\d{1,4}$'
   AND (d.numero_interno IS NULL OR d.numero_interno = '')
   AND NOT EXISTS (
     SELECT 1
       FROM devices x
      WHERE x.id <> d.id
        AND UPPER(REGEXP_REPLACE(x.numero_interno,
            '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0'))) =
            UPPER(REGEXP_REPLACE(d.numero_serie, '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\1 ' || LPAD('\2',4,'0')))
   );

UPDATE devices d
   SET tipo_equipo = COALESCE(d.tipo_equipo, m.tipo_equipo),
       variante    = COALESCE(d.variante, m.variante)
  FROM models m
 WHERE m.id = d.model_id
   AND (d.tipo_equipo IS NULL OR d.variante IS NULL);

WITH last_ingreso AS (
  SELECT DISTINCT ON (t.device_id)
         t.device_id,
         NULLIF(t.faja_garantia,'') AS faja
    FROM ingresos t
   ORDER BY t.device_id, COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
)
UPDATE devices d
   SET n_de_control = COALESCE(last_ingreso.faja, d.n_de_control)
  FROM last_ingreso
 WHERE d.id = last_ingreso.device_id;

WITH last_i AS (
  SELECT d.id AS device_id,
         (
           SELECT t.propietario_nombre
             FROM ingresos t
            WHERE t.device_id = d.id
            ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
            LIMIT 1
         ) AS p_nombre,
         (
           SELECT t.propietario_contacto
             FROM ingresos t
            WHERE t.device_id = d.id
            ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
            LIMIT 1
         ) AS p_contacto,
         (
           SELECT t.propietario_doc
             FROM ingresos t
            WHERE t.device_id = d.id
            ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
            LIMIT 1
         ) AS p_doc
    FROM devices d
)
UPDATE devices d
   SET propietario = COALESCE(NULLIF(last_i.p_nombre,''), d.propietario),
       propietario_nombre = COALESCE(NULLIF(last_i.p_nombre,''), d.propietario_nombre),
       propietario_contacto = COALESCE(NULLIF(last_i.p_contacto,''), d.propietario_contacto),
       propietario_doc = COALESCE(NULLIF(last_i.p_doc,''), d.propietario_doc)
  FROM last_i
 WHERE d.id = last_i.device_id;

UPDATE devices
   SET propietario_nombre = COALESCE(propietario_nombre, propietario)
 WHERE propietario_nombre IS NULL
   AND NULLIF(COALESCE(propietario,''),'') <> '';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = ANY(current_schemas(true))
      AND table_name='devices'
      AND column_name='etiq_garantia_ok'
  ) THEN
    WITH last_ingreso AS (
      SELECT d.id AS device_id,
             (
               SELECT t.id
               FROM ingresos t
               WHERE t.device_id = d.id
               ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
               LIMIT 1
             ) AS ingreso_id
      FROM devices d
    )
    UPDATE ingresos t
       SET etiq_garantia_ok = d.etiq_garantia_ok
      FROM devices d
      JOIN last_ingreso li ON li.device_id = d.id
     WHERE t.id = li.ingreso_id
       AND d.etiq_garantia_ok IS NOT NULL
       AND (t.etiq_garantia_ok IS DISTINCT FROM d.etiq_garantia_ok);
  END IF;

  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = ANY(current_schemas(true))
      AND table_name='devices'
      AND column_name='garantia_bool'
  ) THEN
    UPDATE ingresos i
       SET garantia_fabrica = COALESCE(d.garantia_bool, FALSE)
      FROM devices d
     WHERE d.id = i.device_id
       AND i.garantia_fabrica IS NULL;
  END IF;
END $$;

ALTER TABLE devices DROP COLUMN IF EXISTS etiq_garantia_ok;
ALTER TABLE devices DROP COLUMN IF EXISTS garantia_bool;

DO $$
DECLARE
  v_id_dash INTEGER;
  v_id_desguace INTEGER;
  v_id_alquilado INTEGER;
BEGIN
  INSERT INTO locations(nombre) VALUES ('-')
    ON CONFLICT (nombre) DO NOTHING;

  SELECT id INTO v_id_dash FROM locations WHERE nombre = '-' LIMIT 1;
  SELECT id INTO v_id_desguace FROM locations WHERE LOWER(nombre) = LOWER('Desguace') LIMIT 1;
  SELECT id INTO v_id_alquilado FROM locations WHERE LOWER(nombre) = LOWER('Alquilado') LIMIT 1;

  IF v_id_dash IS NOT NULL THEN
    IF v_id_desguace IS NOT NULL THEN
      UPDATE ingresos SET estado='baja', ubicacion_id = v_id_dash WHERE ubicacion_id = v_id_desguace;
      UPDATE devices SET ubicacion_id = v_id_dash WHERE ubicacion_id = v_id_desguace;
      DELETE FROM locations WHERE id = v_id_desguace;
    END IF;
    IF v_id_alquilado IS NOT NULL THEN
      UPDATE ingresos SET estado='alquilado', ubicacion_id = v_id_dash WHERE ubicacion_id = v_id_alquilado;
      UPDATE devices SET ubicacion_id = v_id_dash WHERE ubicacion_id = v_id_alquilado;
      DELETE FROM locations WHERE id = v_id_alquilado;
    END IF;
  END IF;
END $$;

-- Reglas de garantía (excepciones administrables) - Parte 2 editará, Parte 1 solo lectura
CREATE TABLE IF NOT EXISTS warranty_rules (
  id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  brand_id      INTEGER NULL REFERENCES marcas(id) ON DELETE SET NULL,
  model_id      INTEGER NULL REFERENCES models(id) ON DELETE SET NULL,
  serial_prefix TEXT,
  days          INTEGER NOT NULL,
  notas         TEXT,
  activo        BOOLEAN NOT NULL DEFAULT TRUE,
  created_by    INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by    INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  updated_at    TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_wr_brand ON warranty_rules(brand_id);
CREATE INDEX IF NOT EXISTS idx_wr_model ON warranty_rules(model_id);
CREATE INDEX IF NOT EXISTS idx_wr_activo ON warranty_rules(activo);

-- =============================
-- Sincronización snapshot devices <- último ingreso
-- =============================
CREATE OR REPLACE FUNCTION sync_device_snapshot()
RETURNS TRIGGER AS $$
DECLARE
  v_device_id INTEGER;
  v_last_id INTEGER;
  v_alquilado BOOLEAN;
  v_alquiler_a TEXT;
  v_propietario_nombre TEXT;
  v_propietario_contacto TEXT;
  v_propietario_doc TEXT;
  v_faja TEXT;
  v_ubic_id INTEGER;
  v_is_own BOOLEAN;
  v_mgbio_id INTEGER;
BEGIN
  v_device_id := COALESCE(NEW.device_id, OLD.device_id);

  -- último ingreso del equipo afectado
  SELECT t.id, t.alquilado, t.alquiler_a, t.propietario_nombre, t.propietario_contacto, t.propietario_doc, t.faja_garantia, t.ubicacion_id
    INTO v_last_id, v_alquilado, v_alquiler_a, v_propietario_nombre, v_propietario_contacto, v_propietario_doc, v_faja, v_ubic_id
    FROM ingresos t
   WHERE t.device_id = v_device_id
   ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
   LIMIT 1;

  -- Determinar si es equipo propio por patrón del número de serie
  SELECT (CASE WHEN d.numero_serie ~* '^(MG|NM|NV)\s*\d{1,4}$' THEN TRUE ELSE FALSE END)
    INTO v_is_own
    FROM devices d
   WHERE d.id = v_device_id;

  -- Buscar id de MGBIO si aplica (heurístico por nombre)
  IF v_is_own THEN
    SELECT id INTO v_mgbio_id FROM customers
     WHERE LOWER(razon_social) LIKE '%mg%bio%'
     ORDER BY id ASC LIMIT 1;
  END IF;

  -- Actualizar snapshot en devices
  UPDATE devices d
     SET alquilado = COALESCE(v_alquilado, FALSE),
         alquiler_a = v_alquiler_a,
         ubicacion_id = COALESCE(v_ubic_id, d.ubicacion_id),
         n_de_control = COALESCE(NULLIF(v_faja, ''), d.n_de_control),
         propietario = CASE WHEN v_is_own THEN COALESCE(NULLIF(v_propietario_nombre, ''), d.propietario) ELSE d.propietario END,
         propietario_nombre = COALESCE(NULLIF(v_propietario_nombre, ''), d.propietario_nombre),
         propietario_contacto = COALESCE(NULLIF(v_propietario_contacto, ''), d.propietario_contacto),
         propietario_doc = COALESCE(NULLIF(v_propietario_doc, ''), d.propietario_doc),
         customer_id = CASE WHEN v_is_own AND v_mgbio_id IS NOT NULL THEN v_mgbio_id ELSE d.customer_id END
   WHERE d.id = v_device_id;

  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Triggers sobre ingresos para mantener snapshot del device
DROP TRIGGER IF EXISTS trg_sync_device_snapshot_ins ON ingresos;
DROP TRIGGER IF EXISTS trg_sync_device_snapshot_upd ON ingresos;
DROP TRIGGER IF EXISTS trg_sync_device_snapshot_del ON ingresos;

CREATE TRIGGER trg_sync_device_snapshot_ins
AFTER INSERT ON ingresos
FOR EACH ROW EXECUTE FUNCTION sync_device_snapshot();

CREATE TRIGGER trg_sync_device_snapshot_upd
AFTER UPDATE OF device_id, fecha_ingreso, fecha_creacion, ubicacion_id, alquiler_a, alquilado, faja_garantia, propietario_nombre, propietario_contacto, propietario_doc ON ingresos
FOR EACH ROW EXECUTE FUNCTION sync_device_snapshot();

CREATE TRIGGER trg_sync_device_snapshot_del
AFTER DELETE ON ingresos
FOR EACH ROW EXECUTE FUNCTION sync_device_snapshot();

CREATE TABLE IF NOT EXISTS quotes (
  id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id      INTEGER  NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  version_num     INTEGER NOT NULL DEFAULT 1,
  origen_quote_id INTEGER NULL REFERENCES quotes(id) ON DELETE SET NULL,
  estado          quote_estado NOT NULL DEFAULT 'pendiente',
  moneda          VARCHAR(10) NOT NULL DEFAULT 'ARS',
  subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
  iva_21          NUMERIC(12,2) GENERATED ALWAYS AS (round((subtotal * 0.21), 2)) STORED,
  total           NUMERIC(12,2) GENERATED ALWAYS AS (round((subtotal * 1.21), 2)) STORED,
  autorizado_por  TEXT,
  forma_pago      TEXT,
  plazo_entrega_txt TEXT,
  garantia_txt    TEXT,
  mant_oferta_txt TEXT,
  fecha_emitido   TIMESTAMPTZ NULL,
  fecha_aprobado  TIMESTAMPTZ NULL,
  fecha_rechazado TIMESTAMPTZ NULL,
  rechazo_comentario TEXT,
  pdf_url         TEXT
);

-- Compat con schemas previos de quotes
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INTEGER;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS origen_quote_id INTEGER;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS plazo_entrega_txt TEXT;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS garantia_txt TEXT;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS mant_oferta_txt TEXT;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS fecha_rechazado TIMESTAMPTZ;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS rechazo_comentario TEXT;
UPDATE quotes q
   SET version_num = ranked.version_num_rank
  FROM (
    SELECT
      id,
      ROW_NUMBER() OVER (
        PARTITION BY ingreso_id
        ORDER BY COALESCE(version_num, 1), id
      ) AS version_num_rank
    FROM quotes
  ) ranked
 WHERE ranked.id = q.id
   AND COALESCE(q.version_num, 0) <> ranked.version_num_rank;
UPDATE quotes SET version_num = 1 WHERE version_num IS NULL;
ALTER TABLE quotes ALTER COLUMN version_num SET DEFAULT 1;
ALTER TABLE quotes ALTER COLUMN version_num SET NOT NULL;
ALTER TABLE quotes DROP CONSTRAINT IF EXISTS uq_quotes_ingreso;
DROP INDEX IF EXISTS uq_quotes_ingreso;
DROP INDEX IF EXISTS quotes_ingreso_id_key;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conname = 'fk_quotes_origen_quote'
  ) THEN
    ALTER TABLE quotes
      ADD CONSTRAINT fk_quotes_origen_quote
      FOREIGN KEY (origen_quote_id)
      REFERENCES quotes(id)
      ON DELETE SET NULL;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conname = 'fk_ingresos_presupuesto_rechazado_quote'
  ) THEN
    ALTER TABLE ingresos
      ADD CONSTRAINT fk_ingresos_presupuesto_rechazado_quote
      FOREIGN KEY (presupuesto_rechazado_quote_id)
      REFERENCES quotes(id)
      ON DELETE SET NULL;
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_ingresos_presupuesto_rechazado_quote_id
  ON ingresos(presupuesto_rechazado_quote_id);

CREATE TABLE IF NOT EXISTS quote_items (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  quote_id    INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
  tipo        quote_item_tipo NOT NULL,
  descripcion TEXT NOT NULL,
  qty         NUMERIC(10,2) NOT NULL DEFAULT 1,
  precio_u    NUMERIC(12,2) NOT NULL,
  repuesto_id INTEGER NULL,
  repuesto_codigo TEXT NULL,
  costo_u_neto NUMERIC(12,2) NULL
);

-- Compat con schemas previos de quote_items
ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS repuesto_codigo TEXT;
ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS costo_u_neto NUMERIC(12,2);

CREATE TABLE IF NOT EXISTS ingreso_events (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticket_id   INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  ingreso_id  INTEGER GENERATED ALWAYS AS (ticket_id) STORED,
  de_estado   ticket_state NULL,
  a_estado    ticket_state NOT NULL,
  usuario_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  ts          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  comentario  TEXT
);
CREATE INDEX IF NOT EXISTS ix_ingreso_events_usuario_ts ON ingreso_events(usuario_id, ts DESC);

CREATE TABLE IF NOT EXISTS bejerman_sync_jobs (
  id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sync_type         TEXT NOT NULL,
  ingreso_id        INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  device_id         INTEGER NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  ingreso_event_id  INTEGER NULL REFERENCES ingreso_events(id) ON DELETE SET NULL,
  numero_serie      TEXT NOT NULL DEFAULT '',
  source_deposit    TEXT NOT NULL DEFAULT 'STR',
  target_deposit    TEXT NOT NULL DEFAULT 'STL',
  status            TEXT NOT NULL DEFAULT 'pending',
  attempts          INTEGER NOT NULL DEFAULT 0,
  next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_error        TEXT NULL,
  article_code      TEXT NULL,
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
);

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
);

CREATE TABLE IF NOT EXISTS bejerman_ingreso_remitos (
  id                     INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id             INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  status                 TEXT NOT NULL DEFAULT 'pending',
  pdf_status             TEXT NOT NULL DEFAULT 'pending',
  attempts               INTEGER NOT NULL DEFAULT 0,
  last_error             TEXT NULL,
  request_payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
  comprobante_tipo       TEXT NULL,
  comprobante_letra      TEXT NULL,
  comprobante_pto_venta  TEXT NULL,
  comprobante_numero     TEXT NULL,
  remito_number          TEXT NULL,
  customer_code          TEXT NULL,
  customer_name          TEXT NULL,
  issue_date             DATE NULL,
  generated_at           TIMESTAMPTZ NULL,
  created_by             INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT chk_bejerman_ingreso_remitos_status
    CHECK (status IN ('pending','running','generated','failed')),
  CONSTRAINT chk_bejerman_ingreso_remitos_pdf_status
    CHECK (pdf_status IN ('pending','ready','failed')),
  CONSTRAINT chk_bejerman_ingreso_remitos_attempts CHECK (attempts >= 0)
);

CREATE TABLE IF NOT EXISTS ingreso_historical_corrections (
  id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id      INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  accion          TEXT NOT NULL CHECK (accion IN ('entrega','alta_alquiler','baja_alquiler','baja_ingreso','alta_ingreso')),
  fecha_efectiva  TIMESTAMPTZ NOT NULL,
  motivo          TEXT NOT NULL,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  notificar       BOOLEAN NOT NULL DEFAULT TRUE,
  usuario_id      INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ingreso_hist_corr_ingreso_fecha
  ON ingreso_historical_corrections (ingreso_id, fecha_efectiva DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ingreso_hist_corr_created_at
  ON ingreso_historical_corrections (created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS ingreso_media (
  id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id     INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  usuario_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  storage_path   TEXT NOT NULL,
  thumbnail_path TEXT NOT NULL,
  original_name  TEXT,
  mime_type      VARCHAR(80) NOT NULL,
  size_bytes     BIGINT NOT NULL,
  width          INTEGER NOT NULL,
  height         INTEGER NOT NULL,
  comentario     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Solicitudes de asignación de técnico (simple, una fila por solicitud)
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
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingreso_tests_ingreso ON ingreso_tests(ingreso_id);
CREATE INDEX IF NOT EXISTS ix_ingreso_tests_template_key ON ingreso_tests(template_key);
CREATE INDEX IF NOT EXISTS ix_ingreso_tests_updated_at ON ingreso_tests(updated_at DESC);

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
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_test_protocol_templates_type_key ON test_protocol_templates(type_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_test_protocol_templates_template_key ON test_protocol_templates(template_key);
CREATE INDEX IF NOT EXISTS ix_test_protocol_templates_active ON test_protocol_templates(active);
CREATE INDEX IF NOT EXISTS ix_test_protocol_templates_updated_at ON test_protocol_templates(updated_at DESC);

CREATE TABLE IF NOT EXISTS ingreso_assignment_requests (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id  INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  usuario_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status      TEXT NOT NULL DEFAULT 'pendiente',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  accepted_at TIMESTAMPTZ NULL,
  canceled_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS ix_iars_ingreso_created ON ingreso_assignment_requests(ingreso_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ingreso_baja_requests (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id  INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  usuario_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  motivo      TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  accepted_at TIMESTAMPTZ NULL,
  canceled_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS ix_ibr_ingreso_created ON ingreso_baja_requests(ingreso_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_ibr_ingreso_pending ON ingreso_baja_requests(ingreso_id) WHERE accepted_at IS NULL AND canceled_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ibr_ingreso_pending ON ingreso_baja_requests(ingreso_id) WHERE accepted_at IS NULL AND canceled_at IS NULL;

-- Alertas por presupuestos pendientes (uno por ingreso; guarda ultimo envio)
CREATE TABLE IF NOT EXISTS ingreso_presupuesto_alerts (
  id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id   INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  last_sent_at TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingreso_presupuesto_alerts_ingreso ON ingreso_presupuesto_alerts(ingreso_id);
CREATE INDEX IF NOT EXISTS ix_ingreso_presupuesto_alerts_last_sent ON ingreso_presupuesto_alerts(last_sent_at);

-- Centro de trabajo: reglas, objetivos y estado de avisos operativos
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
);

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
);

CREATE TABLE IF NOT EXISTS work_alert_snoozes (
  id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  alert_key      TEXT NOT NULL,
  alert_ref      TEXT NOT NULL,
  snoozed_until  TIMESTAMPTZ NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT chk_work_alert_snoozes_ref CHECK (NULLIF(TRIM(alert_ref), '') IS NOT NULL)
);

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
);

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
);

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
);

CREATE INDEX IF NOT EXISTS ix_work_alert_rules_enabled ON work_alert_rules(enabled);
CREATE INDEX IF NOT EXISTS ix_work_objectives_active_period
  ON work_objectives(period_type, active, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_work_objectives_technician
  ON work_objectives(technician_id, period_type, active);
CREATE INDEX IF NOT EXISTS ix_work_alert_snoozes_user_until
  ON work_alert_snoozes(user_id, snoozed_until);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_alert_snoozes_user_alert
  ON work_alert_snoozes(user_id, alert_key, alert_ref);
CREATE INDEX IF NOT EXISTS ix_work_notification_state_last_sent
  ON work_notification_state(last_sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_notification_state_key
  ON work_notification_state(channel, notification_key, COALESCE(user_id, -1));
CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_user_key_dedupe
  ON notifications(user_id, notification_key, dedupe_key);
CREATE INDEX IF NOT EXISTS ix_notifications_user_unread_created
  ON notifications(user_id, created_at DESC)
  WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_notifications_entity
  ON notifications(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_notification_preferences_user
  ON notification_user_preferences(user_id);

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
END $$;

INSERT INTO work_alert_rules(rule_key, label, description, threshold_value, threshold_unit, severity, enabled, email_enabled) VALUES
  ('presupuesto_sin_aprobar', 'Presupuesto emitido sin aprobar', 'Presupuestos emitidos que llevan demasiados días sin aprobación.', 7, 'dias', 'critical', TRUE, TRUE),
  ('liberado_sin_entregar', 'Liberado sin entregar', 'Equipos liberados que siguen en taller.', 3, 'dias', 'warning', TRUE, TRUE),
  ('derivado_sin_devolucion', 'Derivado con espera', 'Equipos derivados a proveedor externo sin devolución.', 7, 'dias', 'warning', TRUE, TRUE),
  ('wip_critico', 'Trabajo con espera crítica', 'Equipos en taller con demasiados días desde el ingreso.', 16, 'dias', 'critical', TRUE, TRUE),
  ('sin_tecnico', 'Sin técnico asignado', 'Equipos en taller que todavía no tienen técnico responsable.', 1, 'dias', 'warning', TRUE, FALSE),
  ('preventivo_vencido', 'Preventivo vencido', 'Planes de mantenimiento preventivo con fecha vencida.', 0, 'dias', 'critical', TRUE, TRUE),
  ('preventivo_proximo', 'Preventivo próximo', 'Planes de mantenimiento preventivo próximos a vencer.', 30, 'dias', 'warning', TRUE, TRUE)
ON CONFLICT (rule_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS proveedores_externos (
  id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre    TEXT NOT NULL,
  contacto  TEXT,
  telefono  TEXT,
  email     TEXT,
  direccion TEXT,
  notas     TEXT,
  CONSTRAINT uq_prov_ext_nombre UNIQUE (nombre)
);

CREATE TABLE IF NOT EXISTS equipos_derivados (
  id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id    INTEGER  NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  proveedor_id  INTEGER  NOT NULL REFERENCES proveedores_externos(id) ON DELETE RESTRICT,
  remit_deriv   TEXT,
  fecha_deriv   DATE NOT NULL DEFAULT CURRENT_DATE,
  fecha_entrega DATE,
  estado        deriv_estado NOT NULL DEFAULT 'derivado',
  comentarios   TEXT
);

-- Evitar mas de una derivacion "abierta" por ingreso a la vez
CREATE UNIQUE INDEX IF NOT EXISTS uq_equipos_derivados_ingreso_abierto
  ON equipos_derivados(ingreso_id)
  WHERE estado = 'derivado' AND fecha_entrega IS NULL;

CREATE TABLE IF NOT EXISTS handoffs (
  id                     INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id             INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  pdf_orden_salida       TEXT,
  firmado_cliente        BOOLEAN,
  firmado_empresa        BOOLEAN,
  fecha                  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  n_factura              TEXT,
  factura_url            TEXT,
  orden_taller           TEXT,
  remito_impreso         BOOLEAN,
  fecha_impresion_remito DATE,
  impresion_remito_url   TEXT
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id      INTEGER      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT     NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at   TIMESTAMPTZ NOT NULL,
  used_at      TIMESTAMPTZ NULL,
  ip           TEXT,
  user_agent   TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  user_id      INTEGER,
  role         TEXT,
  method       TEXT,
  path         TEXT,
  ip           TEXT,
  user_agent   TEXT,
  status_code  INTEGER,
  body         JSONB
);
CREATE INDEX IF NOT EXISTS ix_audit_log_user_ts_method ON audit_log(user_id, ts DESC, method);

-- ===============
-- Audit (change log por columna) para PostgreSQL
-- ===============
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.change_log (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  user_id      INTEGER NULL,
  user_role    TEXT NULL,
  table_name   TEXT NOT NULL,
  record_id    INTEGER NOT NULL,
  column_name  TEXT NOT NULL,
  old_value    TEXT NULL,
  new_value    TEXT NULL,
  ingreso_id   INTEGER NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_change_log_ts ON audit.change_log(ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_change_log_ingreso ON audit.change_log(ingreso_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_change_log_table ON audit.change_log(table_name, record_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_change_log_user_ts ON audit.change_log(user_id, ts DESC);

-- Función gen de auditoría por fila (INSERT/UPDATE/DELETE)
CREATE OR REPLACE FUNCTION audit.log_row_change()
RETURNS TRIGGER AS $$
DECLARE
  jold jsonb;
  jnew jsonb;
  k text;
  oval text;
  nval text;
  rec_id integer;
  tname text := TG_TABLE_NAME;
  _user_id text := current_setting('app.user_id', true);
  _user_role text := current_setting('app.user_role', true);
  _ingreso_id text := current_setting('app.ingreso_id', true);
  v_ingreso_id integer;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    jold := to_jsonb(OLD);
    jnew := to_jsonb(NEW);
    rec_id := COALESCE((to_jsonb(NEW)->>'id')::int, (to_jsonb(OLD)->>'id')::int);
  ELSIF TG_OP = 'INSERT' THEN
    jold := '{}'::jsonb;
    jnew := to_jsonb(NEW);
    rec_id := (to_jsonb(NEW)->>'id')::int;
  ELSE
    jold := to_jsonb(OLD);
    jnew := '{}'::jsonb;
    rec_id := (to_jsonb(OLD)->>'id')::int;
  END IF;

  v_ingreso_id := NULL;
  IF tname = 'ingresos' THEN
    v_ingreso_id := rec_id;
  ELSIF tname = 'ingreso_accesorios' THEN
    IF TG_OP = 'DELETE' THEN
      v_ingreso_id := (to_jsonb(OLD)->>'ingreso_id')::int;
    ELSE
      v_ingreso_id := (to_jsonb(NEW)->>'ingreso_id')::int;
    END IF;
  ELSE
    IF COALESCE(_ingreso_id,'') <> '' THEN
      v_ingreso_id := NULLIF(_ingreso_id,'')::int;
    END IF;
  END IF;

  FOR k IN
    SELECT key FROM (
      SELECT jsonb_object_keys(jold) AS key
      UNION
      SELECT jsonb_object_keys(jnew) AS key
    ) s
  LOOP
    -- omitir campos de timestamp de sistema comunes
    IF k IN ('updated_at','created_at') THEN CONTINUE; END IF;
    oval := jold->>k;
    nval := jnew->>k;
    IF (oval IS DISTINCT FROM nval) THEN
      INSERT INTO audit.change_log(ts, user_id, user_role, table_name, record_id, column_name, old_value, new_value, ingreso_id)
      VALUES (
        now(),
        NULLIF(_user_id,'')::int,
        NULLIF(_user_role,''),
        tname,
        rec_id,
        k,
        oval,
        nval,
        v_ingreso_id
      );
    END IF;
  END LOOP;

  IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$ LANGUAGE plpgsql;

-- Accesorios (catálogo y vínculo con ingreso)
CREATE TABLE IF NOT EXISTS catalogo_accesorios (
  id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre  TEXT NOT NULL,
  activo  BOOLEAN NOT NULL DEFAULT TRUE,
  CONSTRAINT uq_catalogo_accesorios_nombre UNIQUE (nombre)
);

-- Repuestos (catalogo para costos y codigos)
CREATE TABLE IF NOT EXISTS catalogo_repuestos (
  id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  codigo       TEXT NOT NULL,
  nombre       TEXT NOT NULL,
  costo_neto   NUMERIC(12,2) NOT NULL DEFAULT 0,
  costo_usd    NUMERIC(12,2) NULL,
  costo_moneda VARCHAR(3) NOT NULL DEFAULT 'USD',
  precio_venta NUMERIC(12,2) NULL,
  multiplicador NUMERIC(10,4) NULL,
  stock_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0,
  stock_min   NUMERIC(12,2) NOT NULL DEFAULT 0,
  activo       BOOLEAN NOT NULL DEFAULT TRUE,
  source_mtime TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_catalogo_repuestos_codigo UNIQUE (codigo)
);

-- Compat con schemas previos de repuestos
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS costo_usd NUMERIC(12,2);
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS costo_moneda VARCHAR(3);
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS precio_venta NUMERIC(12,2);
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS multiplicador NUMERIC(10,4);
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS stock_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS stock_min NUMERIC(12,2) NOT NULL DEFAULT 0;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS tipo_articulo TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS categoria TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS unidad_medida TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS marca_fabricante TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS nro_parte TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS ubicacion_deposito TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS estado TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS notas TEXT;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS fecha_ultima_compra DATE;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS fecha_ultimo_conteo DATE;
ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS fecha_vencimiento DATE;
UPDATE catalogo_repuestos
   SET costo_moneda = CASE
        WHEN costo_usd IS NOT NULL THEN 'USD'
        WHEN costo_usd IS NULL AND COALESCE(costo_neto, 0) > 0 THEN 'ARS'
        ELSE 'USD'
      END
 WHERE costo_moneda IS NULL OR TRIM(costo_moneda) = '';
UPDATE catalogo_repuestos
   SET costo_moneda = UPPER(TRIM(costo_moneda))
 WHERE costo_moneda IS NOT NULL;
UPDATE catalogo_repuestos
   SET costo_moneda = CASE
        WHEN costo_usd IS NOT NULL THEN 'USD'
        WHEN COALESCE(costo_neto, 0) > 0 THEN 'ARS'
        ELSE 'USD'
      END
 WHERE costo_moneda NOT IN ('USD', 'ARS');
ALTER TABLE catalogo_repuestos ALTER COLUMN costo_moneda SET DEFAULT 'USD';
ALTER TABLE catalogo_repuestos ALTER COLUMN costo_moneda SET NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conname = 'chk_catalogo_repuestos_costo_moneda'
       AND conrelid = 'catalogo_repuestos'::regclass
  ) THEN
    ALTER TABLE catalogo_repuestos
      ADD CONSTRAINT chk_catalogo_repuestos_costo_moneda
      CHECK (costo_moneda IN ('USD', 'ARS'));
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS repuestos_subrubros (
  codigo TEXT PRIMARY KEY,
  nombre TEXT NOT NULL,
  activo BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO repuestos_subrubros(codigo, nombre, activo, updated_at) VALUES
  ('1201','Mascara nasal',TRUE,NOW()),
  ('1202','Mascara buconasal',TRUE,NOW()),
  ('1203','Tubuladura',TRUE,NOW()),
  ('1204','Jarra',TRUE,NOW()),
  ('1205','Camaras',TRUE,NOW()),
  ('1206','Canulas',TRUE,NOW()),
  ('1207','Adaptador',TRUE,NOW()),
  ('1208','Filtro',TRUE,NOW()),
  ('1209','Kit',TRUE,NOW()),
  ('1210','Modulo',TRUE,NOW()),
  ('1211','Banda toracica',TRUE,NOW()),
  ('1212','Sensor',TRUE,NOW()),
  ('1213','Insumos varios',TRUE,NOW()),
  ('1214','Pie de suero',TRUE,NOW()),
  ('1215','Resucitador',TRUE,NOW()),
  ('1216','Conector',TRUE,NOW()),
  ('1217','Mascara total face',TRUE,NOW()),
  ('1218','Prolongador',TRUE,NOW()),
  ('1219','Bolso',TRUE,NOW()),
  ('1220','Frasco',TRUE,NOW()),
  ('1221','Circuito',TRUE,NOW()),
  ('1222','Sonda',TRUE,NOW()),
  ('1223','Acc. Monitor',TRUE,NOW()),
  ('1224','Acc. Videolaring.',TRUE,NOW()),
  ('1225','Lamparas',TRUE,NOW()),
  ('1401','A-220',TRUE,NOW()),
  ('1402','A-550',TRUE,NOW()),
  ('1403','Generico',TRUE,NOW()),
  ('1404','C-500',TRUE,NOW()),
  ('1405','A-600',TRUE,NOW()),
  ('1406','G3',TRUE,NOW()),
  ('1407','G4',TRUE,NOW()),
  ('1408','G5',TRUE,NOW()),
  ('1409','INOGEN',TRUE,NOW()),
  ('1410','324',TRUE,NOW()),
  ('1501','Turbina',TRUE,NOW()),
  ('1502','Placa',TRUE,NOW()),
  ('1503','Zeolita',TRUE,NOW()),
  ('1504','Canister',TRUE,NOW()),
  ('1505','Ventilador',TRUE,NOW()),
  ('1506','Teclado',TRUE,NOW()),
  ('1507','Conector',TRUE,NOW()),
  ('1508','Cable',TRUE,NOW()),
  ('1509','Baterias',TRUE,NOW()),
  ('1510','Compresor',TRUE,NOW()),
  ('1511','Interfaz de usuario',TRUE,NOW()),
  ('1512','Panel de acceso',TRUE,NOW()),
  ('1513','Columnas',TRUE,NOW()),
  ('1514','Compresor',TRUE,NOW()),
  ('1515','Celda de O2',TRUE,NOW()),
  ('1516','Acc. Magnamed',TRUE,NOW()),
  ('1517','Repuesto generico',TRUE,NOW()),
  ('1518','Labios',TRUE,NOW()),
  ('1519','Valvulas',TRUE,NOW()),
  ('1520','Transformador',TRUE,NOW()),
  ('1521','Capacitor',TRUE,NOW()),
  ('1522','Flowmeter',TRUE,NOW()),
  ('1601','Instalaciones de equip.',TRUE,NOW()),
  ('1602','Aspirador de uso continuo',TRUE,NOW())
ON CONFLICT (codigo) DO UPDATE SET
  nombre = EXCLUDED.nombre,
  activo = TRUE,
  updated_at = NOW();

CREATE TABLE IF NOT EXISTS repuestos_config (
  id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  dolar_ars             NUMERIC(12,4) NOT NULL DEFAULT 0,
  multiplicador_general NUMERIC(10,4) NOT NULL DEFAULT 1,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by            INTEGER NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS repuestos_config_history (
  id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  dolar_ars             NUMERIC(12,4) NOT NULL,
  multiplicador_general NUMERIC(10,4) NOT NULL,
  changed_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  changed_by            INTEGER NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS repuestos_movimientos (
  id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  repuesto_id INTEGER NOT NULL REFERENCES catalogo_repuestos(id) ON DELETE CASCADE,
  tipo       TEXT NOT NULL,
  qty        NUMERIC(12,2) NOT NULL,
  stock_prev NUMERIC(12,2) NULL,
  stock_new  NUMERIC(12,2) NULL,
  ref_tipo   TEXT NULL,
  ref_id     INTEGER NULL,
  nota       TEXT NULL,
  fecha_compra DATE NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_by INTEGER NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS repuestos_cambios (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  repuesto_id INTEGER NULL REFERENCES catalogo_repuestos(id) ON DELETE SET NULL,
  codigo TEXT NULL,
  accion TEXT NOT NULL,
  nombre_prev TEXT NULL,
  nombre_new TEXT NULL,
  nota TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_by INTEGER NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS repuestos_proveedores (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  repuesto_id INTEGER NOT NULL REFERENCES catalogo_repuestos(id) ON DELETE CASCADE,
  proveedor_id INTEGER NOT NULL REFERENCES proveedores_externos(id) ON DELETE RESTRICT,
  sku_proveedor TEXT NULL,
  lead_time_dias INTEGER NULL,
  prioridad INTEGER NULL,
  ultima_compra DATE NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_repuestos_proveedores UNIQUE (repuesto_id, proveedor_id)
);

CREATE TABLE IF NOT EXISTS repuestos_stock_permisos (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tecnico_id INTEGER NOT NULL REFERENCES users(id),
  enabled_by INTEGER NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ NULL,
  revoked_by INTEGER NULL REFERENCES users(id),
  nota TEXT NULL
);

CREATE TABLE IF NOT EXISTS ingreso_accesorios (
  id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id    INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  accesorio_id  INTEGER NOT NULL REFERENCES catalogo_accesorios(id) ON DELETE RESTRICT,
  referencia    TEXT NULL,
  descripcion   TEXT NULL
);

-- Accesorios asociados específicamente a alquileres de equipos
CREATE TABLE IF NOT EXISTS ingreso_alquiler_accesorios (
  id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingreso_id    INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
  accesorio_id  INTEGER NOT NULL REFERENCES catalogo_accesorios(id) ON DELETE RESTRICT,
  referencia    TEXT NULL,
  descripcion   TEXT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Catálogo general de tipos de equipo
CREATE TABLE IF NOT EXISTS catalogo_tipos_equipo (
  id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  nombre     VARCHAR(160) NOT NULL UNIQUE,
  activo     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Catálogo por marca/tipo/serie/variante (jerárquico) y mapeo de models
CREATE TABLE IF NOT EXISTS marca_tipos_equipo (
  id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  marca_id   INTEGER NOT NULL REFERENCES marcas(id) ON DELETE CASCADE,
  nombre     VARCHAR(160) NOT NULL,
  activo     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_marca_tipos_equipo UNIQUE (marca_id, nombre)
);

CREATE TABLE IF NOT EXISTS marca_series (
  id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  marca_id   INTEGER NOT NULL REFERENCES marcas(id) ON DELETE CASCADE,
  tipo_id    INTEGER NOT NULL REFERENCES marca_tipos_equipo(id) ON DELETE CASCADE,
  nombre     VARCHAR(160) NOT NULL,
  alias      VARCHAR(160) NULL,
  activo     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_marca_series UNIQUE (marca_id, tipo_id, nombre)
);

CREATE TABLE IF NOT EXISTS marca_series_variantes (
  id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  marca_id   INTEGER NOT NULL REFERENCES marcas(id) ON DELETE CASCADE,
  tipo_id    INTEGER NOT NULL REFERENCES marca_tipos_equipo(id) ON DELETE CASCADE,
  serie_id   INTEGER NOT NULL REFERENCES marca_series(id) ON DELETE CASCADE,
  nombre     VARCHAR(160) NOT NULL,
  activo     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_marca_series_variantes UNIQUE (marca_id, tipo_id, serie_id, nombre)
);

CREATE TABLE IF NOT EXISTS model_hierarchy (
  id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  model_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  marca_id    INTEGER NOT NULL REFERENCES marcas(id) ON DELETE CASCADE,
  tipo_id     INTEGER NOT NULL REFERENCES marca_tipos_equipo(id) ON DELETE CASCADE,
  serie_id    INTEGER NOT NULL REFERENCES marca_series(id) ON DELETE CASCADE,
  variante_id INTEGER NULL REFERENCES marca_series_variantes(id) ON DELETE CASCADE,
  full_name   VARCHAR(240) NOT NULL,
  variant_key INTEGER GENERATED ALWAYS AS (COALESCE(variante_id, 0)) STORED,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_model_hierarchy_model UNIQUE (model_id),
  CONSTRAINT uq_model_hierarchy_combo UNIQUE (marca_id, tipo_id, serie_id, variant_key)
);

CREATE OR REPLACE VIEW vw_model_hierarchy_detail AS
SELECT
    mh.model_id,
    mh.marca_id,
    mh.tipo_id,
    mh.serie_id,
    mh.variante_id,
    mh.full_name,
    mt.nombre AS tipo_nombre,
    ms.nombre AS serie_nombre,
    mv.nombre AS variante_nombre
FROM model_hierarchy mh
JOIN marca_tipos_equipo mt ON mt.id = mh.tipo_id
JOIN marca_series ms ON ms.id = mh.serie_id
LEFT JOIN marca_series_variantes mv ON mv.id = mh.variante_id;

-- Feriados (calendario laboral)
CREATE TABLE IF NOT EXISTS feriados (
  fecha DATE PRIMARY KEY,
  nombre TEXT NOT NULL
);

-- Mantenimientos preventivos
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
);

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
);

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
);

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
);

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
);

-- =============================
-- índices
-- =============================
CREATE INDEX IF NOT EXISTS idx_models_marca ON models(marca_id);
CREATE INDEX IF NOT EXISTS idx_models_tecnico ON models(tecnico_id);

CREATE INDEX IF NOT EXISTS idx_devices_customer ON devices(customer_id);
CREATE INDEX IF NOT EXISTS idx_devices_marca ON devices(marca_id);
CREATE INDEX IF NOT EXISTS idx_devices_model ON devices(model_id);
CREATE INDEX IF NOT EXISTS idx_devices_nro_serie ON devices(numero_serie);

CREATE INDEX IF NOT EXISTS idx_ingresos_device ON ingresos(device_id);
CREATE INDEX IF NOT EXISTS idx_ingresos_ubicacion ON ingresos(ubicacion_id);
CREATE INDEX IF NOT EXISTS idx_ingresos_asignado ON ingresos(asignado_a);
CREATE INDEX IF NOT EXISTS ix_ingresos_asignado_estado ON ingresos(asignado_a, estado);

CREATE INDEX IF NOT EXISTS idx_quotes_ingreso ON quotes(ingreso_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_ingreso_version ON quotes(ingreso_id, version_num);
CREATE INDEX IF NOT EXISTS ix_quotes_ingreso_version_desc ON quotes(ingreso_id, version_num DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_quotes_emitido ON quotes(fecha_emitido);
CREATE INDEX IF NOT EXISTS ix_quotes_aprobado ON quotes(fecha_aprobado);
CREATE INDEX IF NOT EXISTS ix_quotes_rechazado ON quotes(fecha_rechazado);

CREATE INDEX IF NOT EXISTS idx_items_quote ON quote_items(quote_id);
CREATE INDEX IF NOT EXISTS idx_quote_items_repuesto_codigo ON quote_items(repuesto_codigo);
CREATE INDEX IF NOT EXISTS ix_events_ingreso_estado_ts ON ingreso_events(ingreso_id, a_estado, ts);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_sync_jobs_type_ingreso
  ON bejerman_sync_jobs(sync_type, ingreso_id);
CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_due
  ON bejerman_sync_jobs(status, next_attempt_at, id)
  WHERE status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_ingreso
  ON bejerman_sync_jobs(ingreso_id);
CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_device
  ON bejerman_sync_jobs(device_id);
CREATE INDEX IF NOT EXISTS ix_bejerman_sync_jobs_article_code
  ON bejerman_sync_jobs(article_code);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_article_mappings_model_variant
  ON bejerman_article_mappings(model_id, variante_norm);
CREATE INDEX IF NOT EXISTS ix_bejerman_article_mappings_article_code
  ON bejerman_article_mappings(article_code);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bejerman_ingreso_remitos_ingreso
  ON bejerman_ingreso_remitos(ingreso_id);
CREATE INDEX IF NOT EXISTS ix_bejerman_ingreso_remitos_status
  ON bejerman_ingreso_remitos(status, pdf_status, updated_at);
CREATE INDEX IF NOT EXISTS ix_bejerman_ingreso_remitos_remito
  ON bejerman_ingreso_remitos(comprobante_tipo, comprobante_pto_venta, comprobante_numero);

CREATE INDEX IF NOT EXISTS idx_ingreso_acc_ingreso ON ingreso_accesorios(ingreso_id);
CREATE INDEX IF NOT EXISTS idx_ingreso_acc_accesorio ON ingreso_accesorios(accesorio_id);

CREATE INDEX IF NOT EXISTS idx_ingreso_alq_acc_ingreso ON ingreso_alquiler_accesorios(ingreso_id);

CREATE INDEX IF NOT EXISTS idx_catalogo_repuestos_codigo_ci ON catalogo_repuestos ((LOWER(codigo)));
CREATE INDEX IF NOT EXISTS idx_catalogo_repuestos_nombre_ci ON catalogo_repuestos ((LOWER(nombre)));
CREATE INDEX IF NOT EXISTS idx_repuestos_subrubros_nombre_ci ON repuestos_subrubros ((LOWER(nombre)));
CREATE INDEX IF NOT EXISTS idx_repuestos_movimientos_repuesto_id ON repuestos_movimientos(repuesto_id);
CREATE INDEX IF NOT EXISTS idx_repuestos_movimientos_created_at ON repuestos_movimientos(created_at);
CREATE INDEX IF NOT EXISTS ix_repuestos_movimientos_created_by_ts ON repuestos_movimientos(created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_repuestos_cambios_created_at ON repuestos_cambios(created_at);
CREATE INDEX IF NOT EXISTS idx_repuestos_cambios_codigo_ci ON repuestos_cambios ((LOWER(codigo)));
CREATE INDEX IF NOT EXISTS idx_repuestos_proveedores_repuesto_id ON repuestos_proveedores(repuesto_id);
CREATE INDEX IF NOT EXISTS idx_repuestos_proveedores_proveedor_id ON repuestos_proveedores(proveedor_id);
CREATE INDEX IF NOT EXISTS idx_repuestos_stock_permisos_tecnico_id ON repuestos_stock_permisos(tecnico_id);
CREATE INDEX IF NOT EXISTS idx_repuestos_stock_permisos_expires_at ON repuestos_stock_permisos(expires_at);
CREATE INDEX IF NOT EXISTS idx_ingreso_alq_acc_accesorio ON ingreso_alquiler_accesorios(accesorio_id);

CREATE INDEX IF NOT EXISTS idx_preventivo_planes_device ON preventivo_planes(device_id);
CREATE INDEX IF NOT EXISTS idx_preventivo_planes_customer ON preventivo_planes(customer_id);
CREATE INDEX IF NOT EXISTS idx_preventivo_planes_next_active
  ON preventivo_planes(proxima_revision_fecha)
  WHERE activa = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_planes_device_active
  ON preventivo_planes(device_id)
  WHERE activa = TRUE AND device_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_planes_customer_active
  ON preventivo_planes(customer_id)
  WHERE activa = TRUE AND customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_preventivo_revisiones_plan_fecha
  ON preventivo_revisiones(plan_id, fecha_programada DESC);
CREATE INDEX IF NOT EXISTS idx_preventivo_revisiones_plan_estado
  ON preventivo_revisiones(plan_id, estado);

CREATE INDEX IF NOT EXISTS idx_preventivo_revision_items_revision_orden
  ON preventivo_revision_items(revision_id, orden);
CREATE INDEX IF NOT EXISTS idx_preventivo_revision_items_revision_estado
  ON preventivo_revision_items(revision_id, estado_item);
CREATE INDEX IF NOT EXISTS idx_preventivo_plan_repuestos_plan
  ON preventivo_plan_repuestos(plan_id);
CREATE INDEX IF NOT EXISTS idx_preventivo_plan_repuestos_key
  ON preventivo_plan_repuestos(repuesto_key);
CREATE INDEX IF NOT EXISTS idx_preventivo_plantillas_signature
  ON preventivo_repuesto_plantillas(signature_key);
CREATE INDEX IF NOT EXISTS idx_preventivo_plantillas_repuesto_key
  ON preventivo_repuesto_plantillas(repuesto_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_plan_repuestos_plan_key
  ON preventivo_plan_repuestos(plan_id, repuesto_key)
  WHERE activa = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_preventivo_plantillas_signature_key
  ON preventivo_repuesto_plantillas(signature_key, repuesto_key)
  WHERE activa = TRUE;

CREATE INDEX IF NOT EXISTS idx_mte_marca ON marca_tipos_equipo(marca_id);
CREATE INDEX IF NOT EXISTS idx_ms_tipo   ON marca_series(tipo_id);
CREATE INDEX IF NOT EXISTS idx_msv_tipo  ON marca_series_variantes(tipo_id);
CREATE INDEX IF NOT EXISTS idx_msv_serie ON marca_series_variantes(serie_id);
CREATE INDEX IF NOT EXISTS idx_mh_tipo   ON model_hierarchy(tipo_id);
CREATE INDEX IF NOT EXISTS idx_mh_serie  ON model_hierarchy(serie_id);
CREATE INDEX IF NOT EXISTS idx_mh_var    ON model_hierarchy(variante_id);

-- Unicidad case-insensitive (nombres) mediante índices únicos funcionales
CREATE UNIQUE INDEX IF NOT EXISTS uq_marcas_nombre_ci ON marcas ((LOWER(nombre)));
CREATE UNIQUE INDEX IF NOT EXISTS uq_models_marca_nombre_ci ON models (marca_id, (LOWER(nombre)));
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalogo_accesorios_nombre_ci ON catalogo_accesorios ((LOWER(nombre)));
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalogo_tipos_equipo_nombre_ci ON catalogo_tipos_equipo ((LOWER(nombre)));
CREATE UNIQUE INDEX IF NOT EXISTS uq_mte_ci ON marca_tipos_equipo (marca_id, (LOWER(nombre)));
CREATE UNIQUE INDEX IF NOT EXISTS uq_ms_ci ON marca_series (marca_id, tipo_id, (LOWER(nombre)));
CREATE UNIQUE INDEX IF NOT EXISTS uq_msv_ci ON marca_series_variantes (marca_id, tipo_id, serie_id, (LOWER(nombre)));

-- =============================
-- Triggers
-- =============================
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_ingreso_media_set_updated_at') THEN
    CREATE TRIGGER trg_ingreso_media_set_updated_at
    BEFORE UPDATE ON ingreso_media
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
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
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_bejerman_ingreso_remitos_set_updated_at') THEN
    CREATE TRIGGER trg_bejerman_ingreso_remitos_set_updated_at
    BEFORE UPDATE ON bejerman_ingreso_remitos
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_ingreso_presupuesto_alerts_set_updated_at') THEN
    CREATE TRIGGER trg_ingreso_presupuesto_alerts_set_updated_at
    BEFORE UPDATE ON ingreso_presupuesto_alerts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_cte_updated_at') THEN
    CREATE TRIGGER trg_cte_updated_at BEFORE UPDATE ON catalogo_tipos_equipo
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_mte_updated_at') THEN
    CREATE TRIGGER trg_mte_updated_at BEFORE UPDATE ON marca_tipos_equipo
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_ms_updated_at') THEN
    CREATE TRIGGER trg_ms_updated_at BEFORE UPDATE ON marca_series
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_msv_updated_at') THEN
    CREATE TRIGGER trg_msv_updated_at BEFORE UPDATE ON marca_series_variantes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_mh_updated_at') THEN
    CREATE TRIGGER trg_mh_updated_at BEFORE UPDATE ON model_hierarchy
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
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
  -- audit_log append-only
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_log_no_update') THEN
    CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_no_update();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_log_no_delete') THEN
    CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_no_update();
  END IF;
  -- sync de quotes -> ingresos
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_quote_sync_ins') THEN
    CREATE TRIGGER trg_quote_sync_ins AFTER INSERT ON quotes
    FOR EACH ROW EXECUTE FUNCTION sync_quote_with_ingreso();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_quote_sync_upd') THEN
    CREATE TRIGGER trg_quote_sync_upd AFTER UPDATE OF estado, subtotal, fecha_emitido, fecha_aprobado, fecha_rechazado, version_num ON quotes
    FOR EACH ROW EXECUTE FUNCTION sync_quote_with_ingreso();
  END IF;
END $$;

-- Normalizar presupuestos emitidos por versiones anteriores y sincronizar el estado visible del ingreso.
UPDATE quotes
   SET estado='presupuestado'
 WHERE estado::text='emitido';

WITH current_quotes AS (
  SELECT DISTINCT ON (ingreso_id)
    ingreso_id,
    estado
  FROM quotes
  ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
),
normalized AS (
  SELECT
    ingreso_id,
    CASE estado
      WHEN 'emitido' THEN 'presupuestado'::quote_estado
      WHEN 'presupuestado' THEN 'presupuestado'::quote_estado
      WHEN 'aprobado' THEN 'aprobado'::quote_estado
      WHEN 'rechazado' THEN 'rechazado'::quote_estado
      WHEN 'no_aplica' THEN 'no_aplica'::quote_estado
      ELSE 'pendiente'::quote_estado
    END AS presupuesto_estado
  FROM current_quotes
)
UPDATE ingresos i
   SET presupuesto_estado = normalized.presupuesto_estado
  FROM normalized
 WHERE i.id = normalized.ingreso_id
   AND i.presupuesto_estado IS DISTINCT FROM normalized.presupuesto_estado;

-- Activar triggers de auditoría por fila (si no existen)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_ingresos') THEN
    CREATE TRIGGER trg_audit_ingresos
    AFTER INSERT OR UPDATE OR DELETE ON ingresos
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_devices') THEN
    CREATE TRIGGER trg_audit_devices
    AFTER INSERT OR UPDATE OR DELETE ON devices
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_ingreso_accesorios') THEN
    CREATE TRIGGER trg_audit_ingreso_accesorios
    AFTER INSERT OR UPDATE OR DELETE ON ingreso_accesorios
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_ingreso_alquiler_accesorios') THEN
    CREATE TRIGGER trg_audit_ingreso_alquiler_accesorios
    AFTER INSERT OR UPDATE OR DELETE ON ingreso_alquiler_accesorios
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_quotes') THEN
    CREATE TRIGGER trg_audit_quotes
    AFTER INSERT OR UPDATE OR DELETE ON quotes
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_quote_items') THEN
    CREATE TRIGGER trg_audit_quote_items
    AFTER INSERT OR UPDATE OR DELETE ON quote_items
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_marcas') THEN
    CREATE TRIGGER trg_audit_marcas
    AFTER INSERT OR UPDATE OR DELETE ON marcas
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_models') THEN
    CREATE TRIGGER trg_audit_models
    AFTER INSERT OR UPDATE OR DELETE ON models
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_customers') THEN
    CREATE TRIGGER trg_audit_customers
    AFTER INSERT OR UPDATE OR DELETE ON customers
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_users') THEN
    CREATE TRIGGER trg_audit_users
    AFTER INSERT OR UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_proveedores_externos') THEN
    CREATE TRIGGER trg_audit_proveedores_externos
    AFTER INSERT OR UPDATE OR DELETE ON proveedores_externos
    FOR EACH ROW EXECUTE FUNCTION audit.log_row_change();
  END IF;
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
END $$;


