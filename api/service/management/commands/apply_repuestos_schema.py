from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica schema de catalogo de repuestos y costos en quote_items"

    def handle(self, *args, **opts):
        subrubros = [
            ("1201", "Mascara nasal"),
            ("1202", "Mascara buconasal"),
            ("1203", "Tubuladura"),
            ("1204", "Jarra"),
            ("1205", "Camaras"),
            ("1206", "Canulas"),
            ("1207", "Adaptador"),
            ("1208", "Filtro"),
            ("1209", "Kit"),
            ("1210", "Modulo"),
            ("1211", "Banda toracica"),
            ("1212", "Sensor"),
            ("1213", "Insumos varios"),
            ("1214", "Pie de suero"),
            ("1215", "Resucitador"),
            ("1216", "Conector"),
            ("1217", "Mascara total face"),
            ("1218", "Prolongador"),
            ("1219", "Bolso"),
            ("1220", "Frasco"),
            ("1221", "Circuito"),
            ("1222", "Sonda"),
            ("1223", "Acc. Monitor"),
            ("1224", "Acc. Videolaring."),
            ("1225", "Lamparas"),
            ("1401", "A-220"),
            ("1402", "A-550"),
            ("1403", "Generico"),
            ("1404", "C-500"),
            ("1405", "A-600"),
            ("1406", "G3"),
            ("1407", "G4"),
            ("1408", "G5"),
            ("1409", "INOGEN"),
            ("1410", "324"),
            ("1501", "Turbina"),
            ("1502", "Placa"),
            ("1503", "Zeolita"),
            ("1504", "Canister"),
            ("1505", "Ventilador"),
            ("1506", "Teclado"),
            ("1507", "Conector"),
            ("1508", "Cable"),
            ("1509", "Baterias"),
            ("1510", "Compresor"),
            ("1511", "Interfaz de usuario"),
            ("1512", "Panel de acceso"),
            ("1513", "Columnas"),
            ("1514", "Compresor"),
            ("1515", "Celda de O2"),
            ("1516", "Acc. Magnamed"),
            ("1517", "Repuesto generico"),
            ("1518", "Labios"),
            ("1519", "Valvulas"),
            ("1520", "Transformador"),
            ("1521", "Capacitor"),
            ("1522", "Flowmeter"),
            ("1601", "Instalaciones de equip."),
            ("1602", "Aspirador de uso continuo"),
        ]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    """
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
                    """
                )
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS costo_usd NUMERIC(12,2)")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS costo_moneda VARCHAR(3)")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS precio_venta NUMERIC(12,2)")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS multiplicador NUMERIC(10,4)")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS stock_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS stock_min NUMERIC(12,2) NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS tipo_articulo TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS categoria TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS unidad_medida TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS marca_fabricante TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS nro_parte TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS ubicacion_deposito TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS estado TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS notas TEXT")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS fecha_ultima_compra DATE")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS fecha_ultimo_conteo DATE")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS fecha_vencimiento DATE")
                cur.execute(
                    """
                    UPDATE catalogo_repuestos
                       SET costo_moneda = CASE
                            WHEN costo_usd IS NOT NULL THEN 'USD'
                            WHEN costo_usd IS NULL AND COALESCE(costo_neto, 0) > 0 THEN 'ARS'
                            ELSE 'USD'
                          END
                     WHERE costo_moneda IS NULL OR TRIM(costo_moneda) = ''
                    """
                )
                cur.execute(
                    """
                    UPDATE catalogo_repuestos
                       SET costo_moneda = UPPER(TRIM(costo_moneda))
                     WHERE costo_moneda IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    UPDATE catalogo_repuestos
                       SET costo_moneda = CASE
                            WHEN costo_usd IS NOT NULL THEN 'USD'
                            WHEN COALESCE(costo_neto, 0) > 0 THEN 'ARS'
                            ELSE 'USD'
                          END
                     WHERE costo_moneda NOT IN ('USD', 'ARS')
                    """
                )
                cur.execute("ALTER TABLE catalogo_repuestos ALTER COLUMN costo_moneda SET DEFAULT 'USD'")
                cur.execute("ALTER TABLE catalogo_repuestos ALTER COLUMN costo_moneda SET NOT NULL")
                cur.execute(
                    """
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
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalogo_repuestos_codigo_ci ON catalogo_repuestos ((LOWER(codigo)))"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catalogo_repuestos_nombre_ci ON catalogo_repuestos ((LOWER(nombre)))"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS repuestos_subrubros (
                      codigo TEXT PRIMARY KEY,
                      nombre TEXT NOT NULL,
                      activo BOOLEAN NOT NULL DEFAULT TRUE,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_repuestos_subrubros_nombre_ci ON repuestos_subrubros ((LOWER(nombre)))"
                )
                for codigo, nombre in subrubros:
                    cur.execute(
                        """
                        INSERT INTO repuestos_subrubros (codigo, nombre, activo, updated_at)
                        VALUES (%s,%s,TRUE,NOW())
                        ON CONFLICT (codigo) DO UPDATE SET
                          nombre=EXCLUDED.nombre,
                          activo=TRUE,
                          updated_at=NOW()
                        """,
                        [codigo, nombre],
                    )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS repuestos_config (
                      id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      dolar_ars             NUMERIC(12,4) NOT NULL DEFAULT 0,
                      multiplicador_general NUMERIC(10,4) NOT NULL DEFAULT 1,
                      updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_by            INTEGER NULL REFERENCES users(id)
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS repuestos_config_history (
                      id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      dolar_ars             NUMERIC(12,4) NOT NULL,
                      multiplicador_general NUMERIC(10,4) NOT NULL,
                      changed_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      changed_by            INTEGER NULL REFERENCES users(id)
                    );
                    """
                )
                cur.execute(
                    """
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
                    """
                )
                cur.execute("ALTER TABLE repuestos_movimientos ADD COLUMN IF NOT EXISTS fecha_compra DATE")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_movimientos_repuesto_id ON repuestos_movimientos(repuesto_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_movimientos_created_at ON repuestos_movimientos(created_at)")
                cur.execute(
                    """
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
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_cambios_created_at ON repuestos_cambios(created_at)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_repuestos_cambios_codigo_ci ON repuestos_cambios ((LOWER(codigo)))"
                )
                cur.execute(
                    """
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
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_proveedores_repuesto_id ON repuestos_proveedores(repuesto_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_proveedores_proveedor_id ON repuestos_proveedores(proveedor_id)")
                cur.execute(
                    """
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
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_stock_permisos_tecnico_id ON repuestos_stock_permisos(tecnico_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_repuestos_stock_permisos_expires_at ON repuestos_stock_permisos(expires_at)")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS repuesto_codigo TEXT")
                cur.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS costo_u_neto NUMERIC(12,2)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quote_items_repuesto_codigo ON quote_items(repuesto_codigo)"
                )
        self.stdout.write("APLICADO OK: catalogo_repuestos / quote_items")
