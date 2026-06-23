from django.core.management.base import BaseCommand
from django.db import connection, transaction


TEXT_COLUMNS = (
    "bejerman_nombre_fantasia",
    "bejerman_tipo_documento",
    "bejerman_domicilio",
    "bejerman_localidad",
    "bejerman_provincia",
    "bejerman_codigo_postal",
    "bejerman_pais",
    "bejerman_condicion_iva",
    "bejerman_numero_iibb",
    "bejerman_condicion_venta",
    "bejerman_vendedor",
    "bejerman_lista_precio",
    "bejerman_contacto",
    "bejerman_telefono",
    "bejerman_telefono_2",
    "bejerman_email",
)


class Command(BaseCommand):
    help = "Aplica columnas de sincronización fiscal y comercial de clientes Bejerman"

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                existing = {
                    column.name
                    for column in connection.introspection.get_table_description(cur, "customers")
                }
                for column in TEXT_COLUMNS:
                    if column not in existing:
                        cur.execute(f"ALTER TABLE customers ADD COLUMN {column} TEXT")
                if "bejerman_synced_at" not in existing:
                    cur.execute("ALTER TABLE customers ADD COLUMN bejerman_synced_at TIMESTAMPTZ NULL")
                if connection.vendor == "postgresql":
                    if "bejerman_raw" not in existing:
                        cur.execute("ALTER TABLE customers ADD COLUMN bejerman_raw JSONB NOT NULL DEFAULT '{}'::jsonb")
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS ix_customers_bejerman_condicion_iva
                          ON customers(bejerman_condicion_iva)
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS ix_customers_bejerman_synced_at
                          ON customers(bejerman_synced_at)
                        """
                    )
                else:
                    if "bejerman_raw" not in existing:
                        cur.execute("ALTER TABLE customers ADD COLUMN bejerman_raw TEXT NOT NULL DEFAULT '{}'")

                required_columns = {*TEXT_COLUMNS, "bejerman_synced_at", "bejerman_raw"}
                existing_after = {
                    column.name
                    for column in connection.introspection.get_table_description(cur, "customers")
                }
                missing = sorted(required_columns - existing_after)
                if missing:
                    raise RuntimeError("No se aplicaron columnas en customers: " + ", ".join(missing))

        self.stdout.write("APLICADO OK: esquema de clientes Bejerman")
