from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Establece la contraseña de un usuario existente (útil para dev cuando email no funciona)"

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email del usuario")
        parser.add_argument("password", help="Nueva contraseña")
        parser.add_argument(
            "--nombre",
            default=None,
            help="Opcional: actualizar nombre del usuario"
        )
        parser.add_argument(
            "--rol",
            default=None,
            help="Opcional: actualizar rol del usuario"
        )

    @staticmethod
    def _fetchone_dict(cur):
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    def handle(self, *args, **options):
        email = str(options.get("email") or "").strip().lower()
        password = str(options.get("password") or "").strip()
        nombre = options.get("nombre")
        rol = options.get("rol")

        if not email:
            self.stderr.write(self.style.ERROR("Email requerido"))
            return

        if len(password) < 1:
            self.stderr.write(self.style.ERROR("Contraseña requerida"))
            return

        hashed = make_password(password)

        with transaction.atomic():
            with connection.cursor() as cur:
                # Buscar el usuario
                cur.execute(
                    """
                    SELECT id, nombre, rol
                    FROM users
                    WHERE LOWER(email) = %s
                    LIMIT 1
                    """,
                    [email],
                )
                row = self._fetchone_dict(cur)

                if not row:
                    self.stderr.write(self.style.ERROR(f"Usuario con email '{email}' no encontrado"))
                    return

                user_id = row["id"]
                current_nombre = row.get("nombre")
                current_rol = row.get("rol")

                # Preparar UPDATE
                updates = ["hash_pw=%s", "activo=TRUE"]
                params = [hashed]

                if nombre:
                    updates.append("nombre=%s")
                    params.append(nombre)
                else:
                    nombre = current_nombre

                if rol:
                    updates.append("rol=%s")
                    params.append(rol)
                else:
                    rol = current_rol

                params.append(user_id)

                # Ejecutar UPDATE
                cur.execute(
                    f"""
                    UPDATE users
                    SET {", ".join(updates)}
                    WHERE id=%s
                    """,
                    params,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Contraseña actualizada para {email}\n"
                f"  ID: {user_id}\n"
                f"  Nombre: {nombre}\n"
                f"  Rol: {rol}\n"
                f"  Contraseña: {password}"
            )
        )
