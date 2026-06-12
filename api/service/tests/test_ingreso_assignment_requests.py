from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
@override_settings(
    ASSIGNMENT_REQUEST_RECIPIENTS=[],
    COMPANY_FOOTER_EMAIL=None,
    COMPANY_FOOTER_EMAIL_2=None,
)
class IngresoAssignmentRequestAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo BOOLEAN DEFAULT TRUE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    razon_social TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS marcas (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                    id BIGSERIAL PRIMARY KEY,
                    marca_id INTEGER NULL REFERENCES marcas(id),
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER NULL REFERENCES marcas(id),
                    model_id INTEGER NULL REFERENCES models(id),
                    numero_serie TEXT,
                    numero_interno TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT,
                    asignado_a INTEGER NULL REFERENCES users(id),
                    equipo_variante TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    updated_by INTEGER NULL
                )
                """
            )
            for statement in (
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_serie TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
            ):
                cur.execute(statement)

        call_command("apply_assignment_requests_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.filter(email__endswith="@assignment.test").delete()
        cls.tecnico = User.objects.create(
            nombre="Técnico Asignación",
            email="tecnico@assignment.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.token = issue_token(cls.tecnico)
        with connection.cursor() as cur:
            cur.execute("INSERT INTO customers (razon_social) VALUES (%s) RETURNING id", ["Cliente Test"])
            customer_id = cur.fetchone()[0]
            cur.execute("INSERT INTO marcas (nombre) VALUES (%s) RETURNING id", ["Marca Test"])
            marca_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s, %s, %s) RETURNING id",
                [marca_id, "Modelo Test", "Equipo"],
            )
            model_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                [customer_id, marca_id, model_id, "SER-ASIG-1", ""],
            )
            device_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, asignado_a) VALUES (%s, %s, NULL) RETURNING id",
                [device_id, "diagnosticado"],
            )
            cls.ingreso_id = cur.fetchone()[0]

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_assignment_requests WHERE ingreso_id = %s", [self.ingreso_id])

    def _url(self):
        return f"/api/ingresos/{self.ingreso_id}/solicitar-asignacion/"

    def test_solicitud_asignacion_es_idempotente_por_tecnico(self):
        first = self.client.post(self._url(), {}, format="json")
        second = self.client.post(self._url(), {}, format="json")

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.data.get("ok"))
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data.get("ok"))
        self.assertTrue(second.data.get("already_pending"))

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingreso_assignment_requests
                 WHERE ingreso_id = %s
                   AND usuario_id = %s
                   AND accepted_at IS NULL
                   AND canceled_at IS NULL
                """,
                [self.ingreso_id, self.tecnico.id],
            )
            count = int(cur.fetchone()[0])

        self.assertEqual(count, 1)
