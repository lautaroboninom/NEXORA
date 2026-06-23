from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = [
    "delivery_orders",
    "delivery_order_items",
    "delivery_order_item_partidas",
    "bejerman_remito_groups",
    "delivery_order_events",
]

REQUIRED_COLUMNS = {
    "delivery_orders": {
        "id",
        "order_number",
        "customer_id",
        "bejerman_customer_code",
        "company_key",
        "delivery_type",
        "status",
        "remito_number",
        "invoice_number",
        "source_system",
        "source_external_id",
    },
    "delivery_order_items": {
        "id",
        "order_id",
        "ingreso_id",
        "device_id",
        "article_code",
        "quantity",
        "unit_price",
        "discount_percent",
        "partida",
    },
    "delivery_order_item_partidas": {
        "id",
        "order_item_id",
        "partida",
        "assigned_quantity",
    },
    "bejerman_remito_groups": {
        "id",
        "remito_number",
        "company_key",
        "customer_code",
        "order_ids",
        "response_summary",
    },
    "delivery_order_events": {"id", "order_id", "event_type", "metadata"},
}


class Command(BaseCommand):
    help = "Aplica el esquema de órdenes de entrega y cobranzas de NEXORA"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_delivery_orders_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
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

                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code TEXT")
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL"
                )
                cur.execute(
                    """
                    SELECT UPPER(TRIM(bejerman_seller_code)) AS code, COUNT(*) AS count
                      FROM users
                     WHERE NULLIF(TRIM(bejerman_seller_code), '') IS NOT NULL
                     GROUP BY UPPER(TRIM(bejerman_seller_code))
                    HAVING COUNT(*) > 1
                     ORDER BY COUNT(*) DESC, code ASC
                     LIMIT 1
                    """
                )
                duplicate_seller_code = cur.fetchone()
                if duplicate_seller_code:
                    raise RuntimeError(
                        "No se puede crear la unicidad de código de vendedor Bejerman: "
                        f"el código {duplicate_seller_code[0]} está asignado a {duplicate_seller_code[1]} usuarios"
                    )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_orders (
                      id                              TEXT PRIMARY KEY,
                      order_number                    TEXT NOT NULL UNIQUE,
                      customer_id                     INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL,
                      bejerman_customer_code          TEXT NULL,
                      customer_name                   TEXT NOT NULL DEFAULT '',
                      delivery_type                   TEXT NOT NULL DEFAULT 'sale',
                      company_key                     TEXT NULL,
                      source_system                   TEXT NOT NULL DEFAULT 'nexora',
                      source_external_id              TEXT NULL,
                      source_reference                TEXT NULL,
                      source_company_id               TEXT NULL,
                      source_sheet                    TEXT NULL,
                      source_row                      INTEGER NULL,
                      source_color                    TEXT NULL,
                      ingreso_id                      INTEGER NULL REFERENCES ingresos(id) ON DELETE SET NULL,
                      device_id                       INTEGER NULL REFERENCES devices(id) ON DELETE SET NULL,
                      equipment_model                 TEXT NULL,
                      equipment_serial                TEXT NULL,
                      equipment_internal_number       TEXT NULL,
                      seller_name                     TEXT NOT NULL DEFAULT '',
                      seller_code                     TEXT NULL,
                      order_date                      DATE NOT NULL DEFAULT CURRENT_DATE,
                      operation_company_label         TEXT NOT NULL DEFAULT '',
                      raw_pedido                      TEXT NOT NULL DEFAULT '',
                      commercial_terms                TEXT NULL,
                      commercial_price                TEXT NULL,
                      commercial_exchange_rate        TEXT NULL,
                      price_currency                  VARCHAR(3) NOT NULL DEFAULT 'ARS',
                      commercial_condition            TEXT NULL,
                      commercial_deadline             TEXT NULL,
                      status                          TEXT NOT NULL DEFAULT 'pendiente_armado',
                      priority                        TEXT NOT NULL DEFAULT 'normal',
                      remito_number                   TEXT NULL,
                      bejerman_remito_group_id        TEXT NULL,
                      remito_location                 TEXT NULL,
                      remito_location_updated_by      INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      remito_location_updated_at      TIMESTAMPTZ NULL,
                      invoice_number                  TEXT NULL,
                      imported_delivered_flag         BOOLEAN NULL,
                      created_by_user_id              INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      prepared_by_user_id             INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      delivered_by_user_id            INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      invoiced_by_user_id             INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      cancelled_by_user_id            INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      source_created_by_user_id       TEXT NULL,
                      source_prepared_by_user_id      TEXT NULL,
                      source_delivered_by_user_id     TEXT NULL,
                      source_invoiced_by_user_id      TEXT NULL,
                      source_cancelled_by_user_id     TEXT NULL,
                      created_at                      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      prepared_at                     TIMESTAMPTZ NULL,
                      delivered_at                    TIMESTAMPTZ NULL,
                      invoiced_at                     TIMESTAMPTZ NULL,
                      cancelled_at                    TIMESTAMPTZ NULL,
                      CONSTRAINT chk_delivery_orders_type CHECK (delivery_type IN ('sale','service_release','rental','demo')),
                      CONSTRAINT chk_delivery_orders_company_key CHECK (company_key IS NULL OR company_key IN ('SEPID','MGBIO')),
                      CONSTRAINT chk_delivery_orders_price_currency CHECK (price_currency IN ('ARS','USD')),
                      CONSTRAINT chk_delivery_orders_status CHECK (status IN ('pendiente_armado','armado_pendiente_entrega','entregado_pendiente_facturacion','entregado_no_facturable','facturado','cancelado')),
                      CONSTRAINT chk_delivery_orders_priority CHECK (priority IN ('normal','urgente')),
                      CONSTRAINT chk_delivery_orders_remito_location CHECK (remito_location IS NULL OR remito_location IN ('recepcion','oficina'))
                    )
                    """
                )

                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    ADD COLUMN IF NOT EXISTS company_key TEXT NULL
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    ADD COLUMN IF NOT EXISTS price_currency VARCHAR(3) NOT NULL DEFAULT 'ARS'
                    """
                )
                cur.execute(
                    """
                    UPDATE delivery_orders
                       SET price_currency = 'ARS'
                     WHERE price_currency IS NULL OR TRIM(price_currency) = ''
                    """
                )
                cur.execute("UPDATE delivery_orders SET price_currency = UPPER(TRIM(price_currency))")
                cur.execute("UPDATE delivery_orders SET price_currency = 'ARS' WHERE price_currency NOT IN ('ARS','USD')")
                cur.execute("ALTER TABLE delivery_orders ALTER COLUMN price_currency SET DEFAULT 'ARS'")
                cur.execute("ALTER TABLE delivery_orders ALTER COLUMN price_currency SET NOT NULL")
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    DROP CONSTRAINT IF EXISTS chk_delivery_orders_price_currency
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    ADD CONSTRAINT chk_delivery_orders_price_currency
                    CHECK (price_currency IN ('ARS','USD'))
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    DROP CONSTRAINT IF EXISTS chk_delivery_orders_type
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    ADD CONSTRAINT chk_delivery_orders_type
                    CHECK (delivery_type IN ('sale','service_release','rental','demo'))
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    DROP CONSTRAINT IF EXISTS chk_delivery_orders_company_key
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    ADD CONSTRAINT chk_delivery_orders_company_key
                    CHECK (company_key IS NULL OR company_key IN ('SEPID','MGBIO'))
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    DROP CONSTRAINT IF EXISTS chk_delivery_orders_status
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_orders
                    ADD CONSTRAINT chk_delivery_orders_status
                    CHECK (status IN ('pendiente_armado','armado_pendiente_entrega','entregado_pendiente_facturacion','entregado_no_facturable','facturado','cancelado'))
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_order_items (
                      id                        TEXT PRIMARY KEY,
                      order_id                  TEXT NOT NULL REFERENCES delivery_orders(id) ON DELETE CASCADE,
                      ingreso_id                INTEGER NULL REFERENCES ingresos(id) ON DELETE SET NULL,
                      device_id                 INTEGER NULL REFERENCES devices(id) ON DELETE SET NULL,
                      article_code              TEXT NULL,
                      article_name              TEXT NULL,
                      description               TEXT NOT NULL DEFAULT '',
                      quantity                  NUMERIC NOT NULL DEFAULT 1,
                      unit_price                NUMERIC NULL,
                      price_currency            VARCHAR(3) NOT NULL DEFAULT 'ARS',
                      discount_percent          NUMERIC(6,2) NOT NULL DEFAULT 0,
                      source_text               TEXT NULL,
                      partida                   TEXT NULL,
                      partida_expiration_date   DATE NULL,
                      stock_deposit_code        TEXT NULL,
                      stock_available_quantity  NUMERIC NULL,
                      stock_checked_at          TIMESTAMPTZ NULL,
                      sort_order                INTEGER NOT NULL DEFAULT 0,
                      created_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_delivery_order_items_quantity CHECK (quantity > 0),
                      CONSTRAINT chk_delivery_order_items_price_currency CHECK (price_currency IN ('ARS','USD')),
                      CONSTRAINT chk_delivery_order_items_discount_percent CHECK (discount_percent >= 0 AND discount_percent <= 100)
                    )
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    ADD COLUMN IF NOT EXISTS ingreso_id INTEGER NULL REFERENCES ingresos(id) ON DELETE SET NULL
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    ADD COLUMN IF NOT EXISTS device_id INTEGER NULL REFERENCES devices(id) ON DELETE SET NULL
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    ADD COLUMN IF NOT EXISTS price_currency VARCHAR(3) NOT NULL DEFAULT 'ARS'
                    """
                )
                cur.execute(
                    """
                    UPDATE delivery_order_items doi
                       SET price_currency = COALESCE(NULLIF(UPPER(TRIM(o.price_currency)), ''), 'ARS')
                      FROM delivery_orders o
                     WHERE o.id = doi.order_id
                       AND (doi.price_currency IS NULL OR TRIM(doi.price_currency) = '')
                    """
                )
                cur.execute("UPDATE delivery_order_items SET price_currency = UPPER(TRIM(price_currency))")
                cur.execute("UPDATE delivery_order_items SET price_currency = 'ARS' WHERE price_currency NOT IN ('ARS','USD')")
                cur.execute("ALTER TABLE delivery_order_items ALTER COLUMN price_currency SET DEFAULT 'ARS'")
                cur.execute("ALTER TABLE delivery_order_items ALTER COLUMN price_currency SET NOT NULL")
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    DROP CONSTRAINT IF EXISTS chk_delivery_order_items_price_currency
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    ADD CONSTRAINT chk_delivery_order_items_price_currency
                    CHECK (price_currency IN ('ARS','USD'))
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(6,2) NOT NULL DEFAULT 0
                    """
                )
                cur.execute("UPDATE delivery_order_items SET discount_percent = 0 WHERE discount_percent IS NULL")
                cur.execute("ALTER TABLE delivery_order_items ALTER COLUMN discount_percent SET DEFAULT 0")
                cur.execute("ALTER TABLE delivery_order_items ALTER COLUMN discount_percent SET NOT NULL")
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    DROP CONSTRAINT IF EXISTS chk_delivery_order_items_discount_percent
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE delivery_order_items
                    ADD CONSTRAINT chk_delivery_order_items_discount_percent
                    CHECK (discount_percent >= 0 AND discount_percent <= 100)
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_order_item_partidas (
                      id                        TEXT PRIMARY KEY,
                      order_item_id             TEXT NOT NULL REFERENCES delivery_order_items(id) ON DELETE CASCADE,
                      partida                   TEXT NOT NULL,
                      assigned_quantity         NUMERIC NOT NULL DEFAULT 1,
                      partida_expiration_date   DATE NULL,
                      stock_deposit_code        TEXT NULL,
                      stock_available_quantity  NUMERIC NULL,
                      stock_checked_at          TIMESTAMPTZ NULL,
                      sort_order                INTEGER NOT NULL DEFAULT 0,
                      created_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_delivery_order_item_partidas_quantity CHECK (assigned_quantity > 0)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bejerman_remito_groups (
                      id                         TEXT PRIMARY KEY,
                      company_key                TEXT NULL,
                      comprobante_tipo           TEXT NOT NULL,
                      comprobante_letra          TEXT NOT NULL DEFAULT 'R',
                      comprobante_pto_venta      TEXT NULL,
                      comprobante_numero         TEXT NULL,
                      remito_number              TEXT NULL,
                      customer_code              TEXT NOT NULL,
                      customer_name              TEXT NOT NULL,
                      seller_code                TEXT NOT NULL,
                      payment_term_code          TEXT NOT NULL,
                      operation_code             TEXT NOT NULL,
                      deposit_code               TEXT NOT NULL,
                      status                     TEXT NOT NULL DEFAULT 'pending',
                      order_ids                  JSONB NOT NULL DEFAULT '[]'::jsonb,
                      response_summary           JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_by_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      source_created_by_user_id  TEXT NULL,
                      created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      generated_at               TIMESTAMPTZ NULL,
                      CONSTRAINT chk_bejerman_remito_groups_company_key CHECK (company_key IS NULL OR company_key IN ('SEPID','MGBIO')),
                      CONSTRAINT chk_bejerman_remito_groups_status CHECK (status IN ('pending','generated','failed'))
                    )
                    """
                )

                cur.execute(
                    """
                    ALTER TABLE bejerman_remito_groups
                    ADD COLUMN IF NOT EXISTS company_key TEXT NULL
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE bejerman_remito_groups
                    DROP CONSTRAINT IF EXISTS chk_bejerman_remito_groups_company_key
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE bejerman_remito_groups
                    ADD CONSTRAINT chk_bejerman_remito_groups_company_key
                    CHECK (company_key IS NULL OR company_key IN ('SEPID','MGBIO'))
                    """
                )
                cur.execute(
                    """
                    UPDATE delivery_orders AS o
                       SET status = 'entregado_no_facturable'
                      FROM bejerman_remito_groups AS g
                     WHERE o.bejerman_remito_group_id = g.id
                       AND o.status = 'entregado_pendiente_facturacion'
                       AND UPPER(COALESCE(
                             NULLIF(g.comprobante_tipo, ''),
                             NULLIF(g.response_summary -> 'profile' ->> 'type', ''),
                             NULLIF(g.response_summary ->> 'comprobanteTipo', '')
                           )) IS DISTINCT FROM 'RT'
                    """
                )
                cur.execute(
                    """
                    UPDATE bejerman_remito_groups AS g
                       SET response_summary = jsonb_set(
                             COALESCE(g.response_summary, '{}'::jsonb),
                             '{billingRequired}',
                             'false'::jsonb,
                             true
                           )
                     WHERE UPPER(COALESCE(
                             NULLIF(g.comprobante_tipo, ''),
                             NULLIF(g.response_summary -> 'profile' ->> 'type', ''),
                             NULLIF(g.response_summary ->> 'comprobanteTipo', '')
                           )) IS DISTINCT FROM 'RT'
                       AND COALESCE(g.response_summary ->> 'billingRequired', '') IS DISTINCT FROM 'false'
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_order_events (
                      id                    BIGSERIAL PRIMARY KEY,
                      order_id              TEXT NOT NULL REFERENCES delivery_orders(id) ON DELETE CASCADE,
                      actor_user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      source_actor_user_id  TEXT NULL,
                      event_type            TEXT NOT NULL,
                      note                  TEXT NULL,
                      metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                cur.execute(
                    """
                    DO $$ BEGIN
                      ALTER TABLE delivery_orders
                        ADD CONSTRAINT fk_delivery_orders_remito_group
                        FOREIGN KEY (bejerman_remito_group_id)
                        REFERENCES bejerman_remito_groups(id)
                        ON DELETE SET NULL
                        DEFERRABLE INITIALLY DEFERRED;
                    EXCEPTION
                      WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      ALTER TABLE delivery_orders
                        ADD CONSTRAINT uq_delivery_orders_external_source
                        UNIQUE (source_system, source_external_id);
                    EXCEPTION
                      WHEN duplicate_table THEN NULL;
                      WHEN duplicate_object THEN NULL;
                    END $$;
                    """
                )

                for sql in (
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_status_priority ON delivery_orders(status, priority, order_date DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_customer ON delivery_orders(customer_id, status, order_date DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_bejerman_customer_code ON delivery_orders(bejerman_customer_code)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_company_key ON delivery_orders(company_key, status, order_date DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_delivery_type ON delivery_orders(delivery_type, status, order_date DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_remito_group ON delivery_orders(bejerman_remito_group_id)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_ingreso ON delivery_orders(ingreso_id) WHERE ingreso_id IS NOT NULL",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_items_order ON delivery_order_items(order_id, sort_order)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_items_article ON delivery_order_items(article_code)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_items_ingreso ON delivery_order_items(ingreso_id) WHERE ingreso_id IS NOT NULL",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_items_device ON delivery_order_items(device_id) WHERE device_id IS NOT NULL",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_item_partidas_item ON delivery_order_item_partidas(order_item_id, sort_order, created_at)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_events_order ON delivery_order_events(order_id, created_at)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_remito_groups_status ON bejerman_remito_groups(status, created_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_remito_groups_company_key ON bejerman_remito_groups(company_key, created_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_remito_groups_customer ON bejerman_remito_groups(customer_code, created_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_users_bejerman_seller_code ON users(bejerman_seller_code)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_bejerman_seller_code_ci ON users ((UPPER(TRIM(bejerman_seller_code)))) WHERE NULLIF(TRIM(bejerman_seller_code), '') IS NOT NULL",
                ):
                    cur.execute(sql)

                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_delivery_orders_set_updated_at') THEN
                        CREATE TRIGGER trg_delivery_orders_set_updated_at
                        BEFORE UPDATE ON delivery_orders
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_delivery_order_items_set_updated_at') THEN
                        CREATE TRIGGER trg_delivery_order_items_set_updated_at
                        BEFORE UPDATE ON delivery_order_items
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_delivery_order_item_partidas_set_updated_at') THEN
                        CREATE TRIGGER trg_delivery_order_item_partidas_set_updated_at
                        BEFORE UPDATE ON delivery_order_item_partidas
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

                for table_name, columns in REQUIRED_COLUMNS.items():
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

                cur.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'users'
                       AND column_name IN ('bejerman_seller_code', 'bejerman_seller_code_confirmed_at')
                    """
                )
                user_columns = {row[0] for row in cur.fetchall()}
                missing_user_columns = sorted(
                    {"bejerman_seller_code", "bejerman_seller_code_confirmed_at"} - user_columns
                )
                if missing_user_columns:
                    raise RuntimeError(f"No se aplicaron columnas en users: {', '.join(missing_user_columns)}")

                cur.execute(
                    """
                    SELECT 1
                      FROM pg_class idx
                      JOIN pg_namespace ns ON ns.oid = idx.relnamespace
                     WHERE idx.relkind = 'i'
                       AND idx.relname = 'uq_users_bejerman_seller_code_ci'
                       AND ns.nspname = ANY(current_schemas(true))
                    """
                )
                if not cur.fetchone():
                    raise RuntimeError("No se aplicó el índice uq_users_bejerman_seller_code_ci")

        self.stdout.write("APLICADO OK: esquema de órdenes de entrega NEXORA")
