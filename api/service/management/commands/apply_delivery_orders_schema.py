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
        "article_code",
        "quantity",
        "unit_price",
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
                    """
                    CREATE TABLE IF NOT EXISTS delivery_orders (
                      id                              TEXT PRIMARY KEY,
                      order_number                    TEXT NOT NULL UNIQUE,
                      customer_id                     INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL,
                      bejerman_customer_code          TEXT NULL,
                      customer_name                   TEXT NOT NULL DEFAULT '',
                      delivery_type                   TEXT NOT NULL DEFAULT 'sale',
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
                      CONSTRAINT chk_delivery_orders_type CHECK (delivery_type IN ('sale','service_release','rental')),
                      CONSTRAINT chk_delivery_orders_status CHECK (status IN ('pendiente_armado','armado_pendiente_entrega','entregado_pendiente_facturacion','facturado','cancelado')),
                      CONSTRAINT chk_delivery_orders_priority CHECK (priority IN ('normal','urgente')),
                      CONSTRAINT chk_delivery_orders_remito_location CHECK (remito_location IS NULL OR remito_location IN ('recepcion','oficina'))
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_order_items (
                      id                        TEXT PRIMARY KEY,
                      order_id                  TEXT NOT NULL REFERENCES delivery_orders(id) ON DELETE CASCADE,
                      article_code              TEXT NULL,
                      article_name              TEXT NULL,
                      description               TEXT NOT NULL DEFAULT '',
                      quantity                  NUMERIC NOT NULL DEFAULT 1,
                      unit_price                NUMERIC NULL,
                      source_text               TEXT NULL,
                      partida                   TEXT NULL,
                      partida_expiration_date   DATE NULL,
                      stock_deposit_code        TEXT NULL,
                      stock_available_quantity  NUMERIC NULL,
                      stock_checked_at          TIMESTAMPTZ NULL,
                      sort_order                INTEGER NOT NULL DEFAULT 0,
                      created_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_delivery_order_items_quantity CHECK (quantity > 0)
                    )
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
                      CONSTRAINT chk_bejerman_remito_groups_status CHECK (status IN ('pending','generated','failed'))
                    )
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
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_delivery_type ON delivery_orders(delivery_type, status, order_date DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_remito_group ON delivery_orders(bejerman_remito_group_id)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_orders_ingreso ON delivery_orders(ingreso_id) WHERE ingreso_id IS NOT NULL",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_items_order ON delivery_order_items(order_id, sort_order)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_items_article ON delivery_order_items(article_code)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_item_partidas_item ON delivery_order_item_partidas(order_item_id, sort_order, created_at)",
                    "CREATE INDEX IF NOT EXISTS ix_delivery_order_events_order ON delivery_order_events(order_id, created_at)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_remito_groups_status ON bejerman_remito_groups(status, created_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_bejerman_remito_groups_customer ON bejerman_remito_groups(customer_code, created_at DESC)",
                    "CREATE INDEX IF NOT EXISTS ix_users_bejerman_seller_code ON users(bejerman_seller_code)",
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
                       AND column_name = 'bejerman_seller_code'
                    """
                )
                if not cur.fetchone():
                    raise RuntimeError("No se aplicó users.bejerman_seller_code")

        self.stdout.write("APLICADO OK: esquema de órdenes de entrega NEXORA")
