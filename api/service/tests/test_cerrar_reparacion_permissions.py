from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


class CerrarReparacionPermissionsAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            engine_suffix = ""
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            engine_suffix = ""
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            engine_suffix = " ENGINE=InnoDB"

        with connection.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id {auto_inc},
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo {bool_type} DEFAULT 1
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    motivo TEXT,
                    resolucion TEXT,
                    asignado_a INT NULL
                ){engine_suffix}
                """
            )

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "sqlite":
            cur.execute("SELECT last_insert_rowid()")
        elif connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        else:
            cur.execute("SELECT LAST_INSERT_ID()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingresos")
        User.objects.all().delete()

        cls.jefe = User.objects.create(
            nombre="Jefe Resolucion",
            email="jefe-resolucion@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Tecnico Resolucion",
            email="tecnico-resolucion@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.admin = User.objects.create(
            nombre="Admin Resolucion",
            email="admin-resolucion@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.jefe_veedor = User.objects.create(
            nombre="Jefe Veedor Resolucion",
            email="jefe-veedor-resolucion@example.com",
            hash_pw="",
            rol="jefe_veedor",
            activo=True,
        )

        cls.tokens = {
            "jefe": issue_token(cls.jefe),
            "tecnico": issue_token(cls.tecnico),
            "admin": issue_token(cls.admin),
            "jefe_veedor": issue_token(cls.jefe_veedor),
        }

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingresos")
            cur.execute(
                "INSERT INTO ingresos (motivo, resolucion, asignado_a) VALUES (%s, %s, %s)",
                ["reparacion", None, self.tecnico.id],
            )
            self.ingreso_id = self._last_insert_id(cur)

    def _url(self):
        return f"/api/ingresos/{self.ingreso_id}/cerrar/"

    def _post_as(self, role, payload):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens[role]}")
        return self.client.post(self._url(), payload, format="json")

    def _resolucion_actual(self):
        with connection.cursor() as cur:
            cur.execute("SELECT resolucion FROM ingresos WHERE id=%s", [self.ingreso_id])
            row = cur.fetchone()
        return row[0] if row else None

    def test_jefe_puede_guardar_resolucion(self):
        resp = self._post_as("jefe", {"resolucion": "reparado"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._resolucion_actual(), "reparado")

    def test_tecnico_admin_y_jefe_veedor_no_pueden_guardar_resolucion(self):
        for role in ("tecnico", "admin", "jefe_veedor"):
            with self.subTest(role=role):
                resp = self._post_as(role, {"resolucion": "reparado"})
                self.assertEqual(resp.status_code, 403)
                self.assertIsNone(self._resolucion_actual())
