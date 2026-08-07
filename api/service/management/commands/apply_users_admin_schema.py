from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica el esquema de seguimiento del último acceso de usuarios"

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_users_admin_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ NULL"
                )
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_ip INET NULL"
                )
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_user_agent TEXT NULL"
                )
                cur.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = ANY(current_schemas(true))
                       AND table_name = 'users'
                       AND column_name IN (
                         'last_login_at',
                         'last_login_ip',
                         'last_login_user_agent'
                       )
                    """
                )
                columns = {row[0] for row in cur.fetchall()}
                required = {
                    "last_login_at",
                    "last_login_ip",
                    "last_login_user_agent",
                }
                missing = sorted(required - columns)
                if missing:
                    raise RuntimeError(
                        "No se aplicaron columnas en users: " + ", ".join(missing)
                    )

        self.stdout.write(
            self.style.SUCCESS("Esquema administrativo de usuarios aplicado")
        )
