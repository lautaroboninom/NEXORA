from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = [
    "route_locations",
    "route_stops",
    "route_stop_events",
]

REQUIRED_COLUMNS = {
    "route_locations": {
        "id",
        "name_key",
        "address_key",
        "customer_id",
        "name",
        "address",
        "notes",
        "active",
        "last_used_at",
        "usage_count",
        "source_system",
        "source_sheet",
        "source_row",
        "metadata",
        "created_at",
        "updated_at",
    },
    "route_stops": {
        "id",
        "route_date",
        "requested_date",
        "requester_name",
        "time_window",
        "location_id",
        "place_name",
        "address",
        "task",
        "sort_order",
        "status",
        "delivery_order_id",
        "source_system",
        "source_sheet",
        "source_row",
        "metadata",
        "created_by_user_id",
        "updated_by_user_id",
        "completed_by_user_id",
        "postponed_by_user_id",
        "cancelled_by_user_id",
        "completed_note",
        "postpone_note",
        "cancelled_note",
        "created_at",
        "updated_at",
        "completed_at",
        "postponed_at",
        "cancelled_at",
    },
    "route_stop_events": {
        "id",
        "stop_id",
        "actor_user_id",
        "event_type",
        "note",
        "metadata",
        "created_at",
    },
}

REQUIRED_INDEXES = {
    "uq_route_locations_name_address_key",
    "ix_route_locations_search",
    "ix_route_locations_customer",
    "ix_route_locations_active",
    "ix_route_stops_route_date_status",
    "ix_route_stops_delivery_order",
    "uq_route_stops_active_delivery_order",
    "ix_route_stop_events_stop",
}


class Command(BaseCommand):
    help = "Aplica el esquema de Hoja de ruta de NEXORA"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_route_sheet_schema requiere PostgreSQL")

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

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS route_locations (
                      id              BIGSERIAL PRIMARY KEY,
                      name_key        TEXT NOT NULL,
                      address_key     TEXT NOT NULL DEFAULT '',
                      customer_id     BIGINT NULL,
                      name            TEXT NOT NULL,
                      address         TEXT NOT NULL DEFAULT '',
                      notes           TEXT NULL,
                      active          BOOLEAN NOT NULL DEFAULT TRUE,
                      last_used_at    TIMESTAMPTZ NULL,
                      usage_count     INTEGER NOT NULL DEFAULT 0,
                      source_system   TEXT NOT NULL DEFAULT 'nexora',
                      source_sheet    TEXT NULL,
                      source_row      INTEGER NULL,
                      metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_route_locations_name
                        CHECK (NULLIF(TRIM(name), '') IS NOT NULL),
                      CONSTRAINT chk_route_locations_name_key
                        CHECK (NULLIF(TRIM(name_key), '') IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE route_locations
                      ADD COLUMN IF NOT EXISTS address_key TEXT NOT NULL DEFAULT '',
                      ADD COLUMN IF NOT EXISTS customer_id BIGINT NULL,
                      ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE,
                      ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ NULL,
                      ADD COLUMN IF NOT EXISTS usage_count INTEGER NOT NULL DEFAULT 0
                    """
                )
                cur.execute(
                    """
                    UPDATE route_locations
                       SET address_key = UPPER(REGEXP_REPLACE(TRIM(COALESCE(address, '')), '\\s+', ' ', 'g'))
                     WHERE address_key IS NULL OR address_key = ''
                    """
                )
                cur.execute(
                    """
                    UPDATE route_locations
                       SET active = TRUE
                     WHERE active IS NULL
                    """
                )
                cur.execute(
                    """
                    UPDATE route_locations
                       SET usage_count = 0
                     WHERE usage_count IS NULL
                    """
                )
                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                          FROM pg_constraint
                         WHERE conname = 'fk_route_locations_customer'
                      ) THEN
                        ALTER TABLE route_locations
                          ADD CONSTRAINT fk_route_locations_customer
                          FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL;
                      END IF;
                    END $$;
                    """
                )
                cur.execute("DROP INDEX IF EXISTS uq_route_locations_name_key")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_route_locations_name_address_key
                      ON route_locations(name_key, address_key)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_route_locations_search
                      ON route_locations USING gin (
                        to_tsvector('simple', COALESCE(name, '') || ' ' || COALESCE(address, ''))
                      )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_route_locations_customer
                      ON route_locations(customer_id)
                      WHERE customer_id IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_route_locations_active
                      ON route_locations(active, updated_at DESC)
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS route_stops (
                      id                    TEXT PRIMARY KEY,
                      route_date            DATE NOT NULL,
                      requested_date        DATE NULL,
                      requester_name        TEXT NOT NULL DEFAULT '',
                      time_window           TEXT NOT NULL DEFAULT '',
                      location_id           BIGINT NULL REFERENCES route_locations(id) ON DELETE SET NULL,
                      place_name            TEXT NOT NULL DEFAULT '',
                      address               TEXT NOT NULL DEFAULT '',
                      task                  TEXT NOT NULL DEFAULT '',
                      sort_order            INTEGER NOT NULL DEFAULT 0,
                      status                TEXT NOT NULL DEFAULT 'pendiente',
                      delivery_order_id     TEXT NULL REFERENCES delivery_orders(id) ON DELETE SET NULL,
                      source_system         TEXT NOT NULL DEFAULT 'nexora',
                      source_sheet          TEXT NULL,
                      source_row            INTEGER NULL,
                      metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_by_user_id    INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      updated_by_user_id    INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      completed_by_user_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      postponed_by_user_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      cancelled_by_user_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      completed_note        TEXT NULL,
                      postpone_note         TEXT NULL,
                      cancelled_note        TEXT NULL,
                      created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      completed_at          TIMESTAMPTZ NULL,
                      postponed_at          TIMESTAMPTZ NULL,
                      cancelled_at          TIMESTAMPTZ NULL,
                      CONSTRAINT chk_route_stops_status
                        CHECK (status IN ('pendiente','completado','pospuesto','cancelado')),
                      CONSTRAINT chk_route_stops_content
                        CHECK (
                          NULLIF(TRIM(COALESCE(place_name, '')), '') IS NOT NULL
                          OR NULLIF(TRIM(COALESCE(task, '')), '') IS NOT NULL
                        ),
                      CONSTRAINT uq_route_stops_source UNIQUE (source_system, source_sheet, source_row)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_route_stops_route_date_status
                      ON route_stops(route_date, status, sort_order, created_at)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_route_stops_delivery_order
                      ON route_stops(delivery_order_id)
                      WHERE delivery_order_id IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_route_stops_active_delivery_order
                      ON route_stops(delivery_order_id)
                      WHERE delivery_order_id IS NOT NULL
                        AND status IN ('pendiente','pospuesto')
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS route_stop_events (
                      id             BIGSERIAL PRIMARY KEY,
                      stop_id        TEXT NOT NULL REFERENCES route_stops(id) ON DELETE CASCADE,
                      actor_user_id  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      event_type     TEXT NOT NULL,
                      note           TEXT NULL,
                      metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      CONSTRAINT chk_route_stop_events_type
                        CHECK (NULLIF(TRIM(event_type), '') IS NOT NULL)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ix_route_stop_events_stop
                      ON route_stop_events(stop_id, created_at, id)
                    """
                )

                cur.execute(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_route_locations_set_updated_at') THEN
                        CREATE TRIGGER trg_route_locations_set_updated_at
                        BEFORE UPDATE ON route_locations
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
                      END IF;
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_route_stops_set_updated_at') THEN
                        CREATE TRIGGER trg_route_stops_set_updated_at
                        BEFORE UPDATE ON route_stops
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
                found = {row[0] for row in cur.fetchall()}
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
                    found_columns = {row[0] for row in cur.fetchall()}
                    missing_columns = sorted(columns - found_columns)
                    if missing_columns:
                        raise RuntimeError(
                            f"No se aplicaron columnas requeridas en {table_name}: {', '.join(missing_columns)}"
                        )

                placeholders = ",".join(["%s"] * len(REQUIRED_INDEXES))
                cur.execute(
                    f"""
                    SELECT indexname
                      FROM pg_indexes
                     WHERE schemaname = ANY(current_schemas(true))
                       AND indexname IN ({placeholders})
                    """,
                    list(REQUIRED_INDEXES),
                )
                found_indexes = {row[0] for row in cur.fetchall()}
                missing_indexes = sorted(REQUIRED_INDEXES - found_indexes)
                if missing_indexes:
                    raise RuntimeError(f"No se aplicaron indices requeridos: {', '.join(missing_indexes)}")

        self.stdout.write("APLICADO OK: Hoja de ruta (schema)")
