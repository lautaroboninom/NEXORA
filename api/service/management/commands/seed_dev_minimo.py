import os

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.views.tipo_equipo_utils import clean_tipo_equipo, preferred_row, tipo_equipo_key


DEFAULT_EMAIL = "lbonino@sepid.com.ar"
DEFAULT_NOMBRE = "Lucas Bonino"
DEFAULT_ROL = "jefe"
DEFAULT_PASSWORD = "Sepid.dev.2026!"

SEED_CLIENTES = [
    {"cod_empresa": "PART", "razon_social": "Particular", "contacto": "", "telefono": "", "email": ""},
    {"cod_empresa": "SEPID", "razon_social": "SEPID", "contacto": "Administración", "telefono": "", "email": ""},
    {"cod_empresa": "DEMO", "razon_social": "Cliente Demo", "contacto": "Operaciones", "telefono": "", "email": ""},
]

SEED_TIPOS = [
    "CPAP",
    "Concentrador de Oxígeno",
    "Aspirador",
    "Monitor multiparamétrico",
]

SEED_MARCAS_MODELOS = {
    "BMC": [
        {"nombre": "G2S A20", "tipo_equipo": "CPAP"},
        {"nombre": "V5", "tipo_equipo": "CPAP"},
    ],
    "Longfian": [
        {"nombre": "JAY-5", "tipo_equipo": "Concentrador de Oxígeno"},
    ],
    "Inogen": [
        {"nombre": "ONE G3", "tipo_equipo": "Concentrador de Oxígeno"},
    ],
}


class Command(BaseCommand):
    help = "Seed mínimo de desarrollo: usuario base, clientes, tipos de equipo, marcas y modelos."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=DEFAULT_EMAIL)
        parser.add_argument("--nombre", default=DEFAULT_NOMBRE)
        parser.add_argument("--rol", default=DEFAULT_ROL)
        parser.add_argument(
            "--password",
            default=os.getenv("DEV_SEED_PASSWORD", DEFAULT_PASSWORD),
            help="Contraseña para el usuario seed de desarrollo",
        )
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Forzar reset de contraseña aunque el usuario ya exista",
        )

    @staticmethod
    def _fetchone_dict(cur):
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    @staticmethod
    def _fetchall_dict(cur):
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    @staticmethod
    def _norm(value):
        return clean_tipo_equipo(value)

    def _upsert_user(self, cur, *, nombre, email, rol, password, reset_password):
        cur.execute(
            """
            SELECT id, hash_pw
            FROM users
            WHERE LOWER(email) = LOWER(%s)
            LIMIT 1
            """,
            [email],
        )
        row = self._fetchone_dict(cur)
        hashed = make_password(password)

        if row:
            if reset_password or not (row.get("hash_pw") or "").strip():
                cur.execute(
                    """
                    UPDATE users
                       SET nombre=%s,
                           rol=%s,
                           activo=TRUE,
                           hash_pw=%s
                     WHERE id=%s
                    """,
                    [nombre, rol, hashed, row["id"]],
                )
                return row["id"], False, True

            cur.execute(
                """
                UPDATE users
                   SET nombre=%s,
                       rol=%s,
                       activo=TRUE
                 WHERE id=%s
                """,
                [nombre, rol, row["id"]],
            )
            return row["id"], False, False

        if connection.vendor == "postgresql":
            cur.execute(
                """
                INSERT INTO users(nombre, email, hash_pw, rol, activo)
                VALUES (%s, %s, %s, %s, TRUE)
                RETURNING id
                """,
                [nombre, email, hashed, rol],
            )
            new_id = cur.fetchone()[0]
        else:
            cur.execute(
                """
                INSERT INTO users(nombre, email, hash_pw, rol, activo)
                VALUES (%s, %s, %s, %s, TRUE)
                """,
                [nombre, email, hashed, rol],
            )
            cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", [email])
            new_id = cur.fetchone()[0]

        return int(new_id), True, True

    def _upsert_cliente(self, cur, cliente):
        razon = self._norm(cliente.get("razon_social"))
        if not razon:
            return False

        cur.execute(
            """
            SELECT id
            FROM customers
            WHERE LOWER(TRIM(razon_social)) = LOWER(TRIM(%s))
            LIMIT 1
            """,
            [razon],
        )
        row = self._fetchone_dict(cur)

        cod = self._norm(cliente.get("cod_empresa")) or None
        contacto = self._norm(cliente.get("contacto")) or None
        telefono = self._norm(cliente.get("telefono")) or None
        email = self._norm(cliente.get("email")) or None

        if row:
            cur.execute(
                """
                UPDATE customers
                   SET cod_empresa = COALESCE(NULLIF(cod_empresa, ''), %s),
                       contacto = COALESCE(NULLIF(contacto, ''), %s),
                       telefono = COALESCE(NULLIF(telefono, ''), %s),
                       email = COALESCE(NULLIF(email, ''), %s)
                 WHERE id=%s
                """,
                [cod, contacto, telefono, email, row["id"]],
            )
            return False

        cur.execute(
            """
            INSERT INTO customers(cod_empresa, razon_social, contacto, telefono, email)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [cod, razon, contacto, telefono, email],
        )
        return True

    def _upsert_tipo_global(self, cur, nombre):
        tipo = self._norm(nombre)
        if not tipo:
            return False

        cur.execute(
            """
            SELECT id, nombre, activo
            FROM catalogo_tipos_equipo
            ORDER BY id
            """,
            [],
        )
        row = preferred_row(self._fetchall_dict(cur), tipo)
        if row:
            cur.execute(
                "UPDATE catalogo_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s",
                [tipo, row["id"]],
            )
            return False

        cur.execute("INSERT INTO catalogo_tipos_equipo(nombre, activo) VALUES (%s, TRUE)", [tipo])
        return True

    def _upsert_marca(self, cur, nombre):
        marca = self._norm(nombre)
        cur.execute("SELECT id FROM marcas WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(%s)) LIMIT 1", [marca])
        row = self._fetchone_dict(cur)
        if row:
            cur.execute("UPDATE marcas SET nombre=%s WHERE id=%s", [marca, row["id"]])
            return int(row["id"]), False

        if connection.vendor == "postgresql":
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", [marca])
            marca_id = cur.fetchone()[0]
        else:
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", [marca])
            cur.execute("SELECT id FROM marcas WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(%s)) LIMIT 1", [marca])
            marca_id = cur.fetchone()[0]
        return int(marca_id), True

    def _upsert_marca_tipo(self, cur, marca_id, tipo_nombre):
        tipo = self._norm(tipo_nombre)
        cur.execute(
            """
            SELECT id, nombre, activo
            FROM marca_tipos_equipo
            WHERE marca_id=%s
            ORDER BY id
            """,
            [marca_id],
        )
        row = preferred_row(self._fetchall_dict(cur), tipo)
        if row:
            cur.execute(
                "UPDATE marca_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s",
                [tipo, row["id"]],
            )
            return False

        cur.execute(
            """
            INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
            VALUES (%s, %s, TRUE)
            """,
            [marca_id, tipo],
        )
        return True

    def _upsert_modelo(self, cur, marca_id, nombre, tipo_equipo):
        modelo = self._norm(nombre)
        tipo = self._norm(tipo_equipo)

        cur.execute(
            """
            SELECT id, COALESCE(TRIM(tipo_equipo), '') AS tipo_actual
            FROM models
            WHERE marca_id=%s AND LOWER(TRIM(nombre))=LOWER(TRIM(%s))
            LIMIT 1
            """,
            [marca_id, modelo],
        )
        row = self._fetchone_dict(cur)

        if row:
            tipo_actual = self._norm(row.get("tipo_actual"))
            if not tipo_actual or tipo_equipo_key(tipo_actual) == tipo_equipo_key(tipo):
                cur.execute(
                    "UPDATE models SET nombre=%s, tipo_equipo=%s WHERE id=%s",
                    [modelo, tipo or None, row["id"]],
                )
            return False

        cur.execute(
            """
            INSERT INTO models(marca_id, nombre, tipo_equipo)
            VALUES (%s, %s, %s)
            """,
            [marca_id, modelo, tipo or None],
        )
        return True

    def handle(self, *args, **options):
        email = self._norm(options.get("email")).lower()
        nombre = self._norm(options.get("nombre"))
        rol = self._norm(options.get("rol")).lower()
        password = str(options.get("password") or "")
        reset_password = bool(options.get("reset_password"))

        if not email:
            self.stderr.write(self.style.ERROR("Email requerido"))
            return
        if not nombre:
            self.stderr.write(self.style.ERROR("Nombre requerido"))
            return
        if not rol:
            self.stderr.write(self.style.ERROR("Rol requerido"))
            return
        if len(password) < 8:
            self.stderr.write(self.style.ERROR("La contraseña debe tener al menos 8 caracteres"))
            return

        created_counts = {
            "clientes": 0,
            "tipos": 0,
            "marcas": 0,
            "modelos": 0,
            "marca_tipos": 0,
        }

        with transaction.atomic():
            with connection.cursor() as cur:
                user_id, user_created, user_password_set = self._upsert_user(
                    cur,
                    nombre=nombre,
                    email=email,
                    rol=rol,
                    password=password,
                    reset_password=reset_password,
                )

                for cliente in SEED_CLIENTES:
                    if self._upsert_cliente(cur, cliente):
                        created_counts["clientes"] += 1

                for tipo in SEED_TIPOS:
                    if self._upsert_tipo_global(cur, tipo):
                        created_counts["tipos"] += 1

                for marca_nombre, modelos in SEED_MARCAS_MODELOS.items():
                    marca_id, marca_created = self._upsert_marca(cur, marca_nombre)
                    if marca_created:
                        created_counts["marcas"] += 1

                    for modelo in modelos:
                        tipo_modelo = self._norm(modelo.get("tipo_equipo"))
                        if tipo_modelo:
                            if self._upsert_tipo_global(cur, tipo_modelo):
                                created_counts["tipos"] += 1
                            if self._upsert_marca_tipo(cur, marca_id, tipo_modelo):
                                created_counts["marca_tipos"] += 1

                        if self._upsert_modelo(cur, marca_id, modelo.get("nombre"), tipo_modelo):
                            created_counts["modelos"] += 1

        self.stdout.write(
            self.style.SUCCESS(
                "SEED DEV OK: "
                f"user_id={user_id} "
                f"(created={str(user_created).lower()}, password_set={str(user_password_set).lower()}) | "
                f"clientes+={created_counts['clientes']} tipos+={created_counts['tipos']} "
                f"marcas+={created_counts['marcas']} modelos+={created_counts['modelos']} "
                f"marca_tipos+={created_counts['marca_tipos']}"
            )
        )
        self.stdout.write(f"Usuario seed: {email} | password: {password}")
