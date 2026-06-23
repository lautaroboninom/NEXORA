import unicodedata

from django.core.management.base import BaseCommand
from django.db import connection, transaction


EXAMPLE_ALIASES = (
    ("TMD", "terapias medicas domiciliarias"),
    ("ARGAS", "argentina de gases"),
)


def _norm(value):
    text = unicodedata.normalize("NFD", str(value or "").strip().lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


class Command(BaseCommand):
    help = "Aplica el alias interno de clientes y opcionalmente carga alias iniciales"

    def add_arguments(self, parser):
        parser.add_argument(
            "--seed-examples",
            action="store_true",
            help="Carga TMD y ARGAS si cada cliente se encuentra sin ambigüedad.",
        )

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                existing = {
                    column.name
                    for column in connection.introspection.get_table_description(cur, "customers")
                }
                if "alias_interno" not in existing:
                    cur.execute("ALTER TABLE customers ADD COLUMN alias_interno TEXT NULL")

                cur.execute(
                    """
                    UPDATE customers
                       SET alias_interno = NULLIF(BTRIM(alias_interno), '')
                     WHERE alias_interno IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_customers_alias_interno_ci
                      ON customers (LOWER(BTRIM(alias_interno)))
                     WHERE alias_interno IS NOT NULL
                       AND BTRIM(alias_interno) <> ''
                    """
                )

                existing_after = {
                    column.name
                    for column in connection.introspection.get_table_description(cur, "customers")
                }
                if "alias_interno" not in existing_after:
                    raise RuntimeError("No se aplicó customers.alias_interno")

                seeded = []
                skipped = []
                if opts.get("seed_examples"):
                    cur.execute("SELECT id, razon_social, alias_interno FROM customers ORDER BY id")
                    rows = [
                        {"id": row[0], "razon_social": row[1], "alias_interno": row[2]}
                        for row in cur.fetchall()
                    ]
                    for alias, name_key in EXAMPLE_ALIASES:
                        matches = [row for row in rows if name_key in _norm(row["razon_social"])]
                        if len(matches) != 1:
                            skipped.append(f"{alias}: coincidencias={len(matches)}")
                            continue
                        row = matches[0]
                        current = (row.get("alias_interno") or "").strip()
                        if current and current.upper() != alias:
                            skipped.append(f"{alias}: alias existente en cliente {row['id']}")
                            continue
                        alias_owner = next(
                            (
                                other
                                for other in rows
                                if other["id"] != row["id"]
                                and (other.get("alias_interno") or "").strip().upper() == alias
                            ),
                            None,
                        )
                        if alias_owner:
                            skipped.append(f"{alias}: alias ya usado por cliente {alias_owner['id']}")
                            continue
                        cur.execute(
                            "UPDATE customers SET alias_interno=%s WHERE id=%s",
                            [alias, row["id"]],
                        )
                        row["alias_interno"] = alias
                        seeded.append(f"{alias}: cliente {row['id']}")

        message = "APLICADO OK: alias interno de clientes"
        if opts.get("seed_examples"):
            message += f" | cargados={len(seeded)}"
            if skipped:
                message += " | pendientes=" + "; ".join(skipped)
        self.stdout.write(message)
