import hashlib
from datetime import timedelta

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient


PORTAL_TOKEN = "portal-test-token"
PORTAL_TOKEN_HASH = hashlib.sha256(PORTAL_TOKEN.encode("utf-8")).hexdigest()


@override_settings(
    PORTAL_INTEGRATION_TOKEN_SHA256=PORTAL_TOKEN_HASH,
    PORTAL_INTEGRATION_TOKEN_SHA256_FALLBACKS=[],
    PORTAL_INTEGRATION_ALLOWED_IPS=[],
)
class PortalIntegrationAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        dt_type = "TIMESTAMPTZ" if connection.vendor == "postgresql" else "DATETIME"
        bool_type = "BOOLEAN" if connection.vendor != "sqlite" else "INTEGER"

        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY,
                    razon_social TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS marcas (
                    id INTEGER PRIMARY KEY,
                    nombre TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    marca_id INTEGER,
                    nombre TEXT,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY,
                    nombre TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    nombre TEXT,
                    email TEXT,
                    hash_pw TEXT,
                    rol TEXT,
                    activo BOOLEAN
                )
                """.replace("BOOLEAN", bool_type)
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER,
                    marca_id INTEGER,
                    model_id INTEGER,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id INTEGER PRIMARY KEY,
                    device_id INTEGER,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    motivo TEXT,
                    fecha_ingreso {dt_type},
                    fecha_creacion {dt_type},
                    fecha_servicio {dt_type},
                    ubicacion_id INTEGER,
                    asignado_a INTEGER,
                    equipo_variante TEXT
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY,
                    ingreso_id INTEGER,
                    estado TEXT,
                    total NUMERIC,
                    moneda TEXT,
                    fecha_emitido {dt_type}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipos_derivados (
                    id INTEGER PRIMARY KEY,
                    ingreso_id INTEGER,
                    estado TEXT,
                    fecha_deriv {dt_type}
                )
                """
            )

    @classmethod
    def setUpTestData(cls):
        cls._seed()

    @classmethod
    def _seed(cls):
        now = timezone.now()
        with connection.cursor() as cur:
            for table in (
                "equipos_derivados",
                "quotes",
                "ingresos",
                "devices",
                "users",
                "locations",
                "models",
                "marcas",
                "customers",
            ):
                cur.execute(f"DELETE FROM {table}")

            cur.execute("INSERT INTO customers (id, razon_social) VALUES (%s, %s), (%s, %s)", [1, "Cliente A", 2, "Cliente B"])
            cur.execute("INSERT INTO marcas (id, nombre) VALUES (%s, %s)", [1, "Marca"])
            cur.execute(
                "INSERT INTO models (id, marca_id, nombre, tipo_equipo, variante) VALUES (%s, %s, %s, %s, %s)",
                [1, 1, "Modelo X", "Monitor", ""],
            )
            cur.execute("INSERT INTO locations (id, nombre) VALUES (%s, %s), (%s, %s)", [1, "Taller", 2, "Deposito"])
            cur.execute("INSERT INTO users (id, nombre, email, hash_pw, rol, activo) VALUES (%s, %s, %s, %s, %s, %s)", [7, "Tecnico Uno", "t@example.com", "", "tecnico", True])
            cur.execute(
                """
                INSERT INTO devices (id, customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s),
                  (%s, %s, %s, %s, %s, %s, %s),
                  (%s, %s, %s, %s, %s, %s, %s),
                  (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    10, 1, 1, 1, "SER-A", "MG-A", "",
                    11, 1, 1, 1, "SER-B", "MG-B", "",
                    12, 2, 1, 1, "SER-C", "MG-C", "",
                    13, 1, 1, 1, "SER-D", "MG-D", "",
                ],
            )
            rows = [
                (100, 10, "diagnostico", "pendiente", "reparacion", now - timedelta(days=2), now - timedelta(days=2), None, 1, None, ""),
                (101, 11, "entregado", "emitido", "reparacion", now - timedelta(days=4), now - timedelta(days=4), None, 1, None, ""),
                (102, 12, "diagnostico", "pendiente", "reparacion", now - timedelta(days=18), now - timedelta(days=18), None, 1, None, ""),
                (103, 13, "diagnostico", "pendiente", "reparacion", now - timedelta(days=1), now - timedelta(days=1), None, 2, None, ""),
                (104, 10, "reparacion", "emitido", "urgente control", now - timedelta(days=8), now - timedelta(days=8), None, 1, 7, ""),
            ]
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO ingresos (
                        id, device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion,
                        fecha_servicio, ubicacion_id, asignado_a, equipo_variante
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    row,
                )
            cur.execute(
                "INSERT INTO quotes (id, ingreso_id, estado, total, moneda, fecha_emitido) VALUES (%s, %s, %s, %s, %s, %s)",
                [1, 104, "emitido", 100, "ARS", now - timedelta(days=1)],
            )

    def setUp(self):
        self.client = APIClient()

    def _auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {PORTAL_TOKEN}")

    def test_missing_or_invalid_token_returns_401(self):
        response = self.client.get("/api/integrations/portal/clientes/1/general/")
        self.assertEqual(response.status_code, 401)

        self.client.credentials(HTTP_AUTHORIZATION="Bearer wrong")
        response = self.client.get("/api/integrations/portal/clientes/1/general/")
        self.assertEqual(response.status_code, 401)

    def test_client_general_uses_nexora_general_rules(self):
        self._auth()
        response = self.client.get("/api/integrations/portal/clientes/1/general/")

        self.assertEqual(response.status_code, 200)
        ids = {item["nexoraIngresoId"] for item in response.data["items"]}
        self.assertEqual(ids, {100, 104})
        self.assertTrue(all(item["companyName"] == "Cliente A" for item in response.data["items"]))

    def test_client_summary_does_not_cross_customer_boundary(self):
        self._auth()
        response = self.client.get("/api/integrations/portal/clientes/2/ingresos/100/summary/")

        self.assertEqual(response.status_code, 404)

    def test_internal_queue_respects_workshop_and_terminal_filters(self):
        self._auth()
        response = self.client.get(
            "/api/integrations/portal/internal/work-queue/?companyId=1&assignedUserId=unassigned&page=1&pageSize=10"
        )

        self.assertEqual(response.status_code, 200)
        ids = [item["nexoraIngresoId"] for item in response.data["items"]]
        self.assertEqual(ids, [100])
        self.assertEqual(response.data["pagination"]["totalItems"], 1)

