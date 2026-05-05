from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DeviceIdentificadoresApiTest(TestCase):
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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    user_id INTEGER NULL,
                    role TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    ip TEXT,
                    user_agent TEXT,
                    status_code INTEGER NOT NULL,
                    body JSONB
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT,
                    razon_social TEXT NOT NULL,
                    telefono TEXT
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
                    marca_id INTEGER REFERENCES marcas(id),
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    marca_id INTEGER REFERENCES marcas(id),
                    model_id INTEGER REFERENCES models(id),
                    numero_serie TEXT,
                    numero_interno TEXT,
                    tipo_equipo TEXT,
                    variante TEXT,
                    ubicacion_id INTEGER REFERENCES locations(id),
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT
                )
                """
            )
        super().setUpClass()

    @classmethod
    def _last_id(cls, cur):
        cur.execute("SELECT LASTVAL()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM locations")
            cur.execute("DELETE FROM customers")
        User.objects.all().delete()

        cls.user = User.objects.create(
            nombre="Admin Equipos",
            email="admin-equipos@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.token = issue_token(cls.user)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI001", "Clinica Uno", "123"],
            )
            cls.customer_1_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI002", "Clinica Dos", "456"],
            )
            cls.customer_2_id = cls._last_id(cur)

            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["Marca A"])
            cls.marca_1_id = cls._last_id(cur)
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["Marca B"])
            cls.marca_2_id = cls._last_id(cur)

            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [cls.marca_1_id, "Modelo A1", "Monitor"],
            )
            cls.model_1_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [cls.marca_2_id, "Modelo B1", "Desfibrilador"],
            )
            cls.model_2_id = cls._last_id(cur)

            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Taller"])
            cls.loc_1_id = cls._last_id(cur)
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Depósito"])
            cls.loc_2_id = cls._last_id(cur)

            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno,
                    tipo_equipo, variante, ubicacion_id, alquilado, alquiler_a
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,false,'')
                """,
                [
                    cls.customer_1_id,
                    cls.marca_1_id,
                    cls.model_1_id,
                    "NS-BASE-1",
                    "MG 1001",
                    "Monitor",
                    "V1",
                    cls.loc_1_id,
                ],
            )
            cls.device_1_id = cls._last_id(cur)

            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno,
                    tipo_equipo, variante, ubicacion_id, alquilado, alquiler_a
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,false,'')
                """,
                [
                    cls.customer_1_id,
                    cls.marca_1_id,
                    cls.model_1_id,
                    "NS-DUP-2",
                    "MG 7777",
                    "Monitor",
                    "V2",
                    cls.loc_1_id,
                ],
            )
            cls.device_2_id = cls._last_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _url(self, device_id):
        return f"/api/devices/{device_id}/identificadores/"

    def test_get_retornar_payload_completo(self):
        resp = self.client.get(self._url(self.device_1_id), format="json")
        self.assertEqual(resp.status_code, 200)
        device = resp.data.get("device") or {}
        self.assertEqual(int(device.get("id") or 0), self.device_1_id)
        self.assertEqual(int(device.get("customer_id") or 0), self.customer_1_id)
        self.assertEqual(int(device.get("marca_id") or 0), self.marca_1_id)
        self.assertEqual(int(device.get("model_id") or 0), self.model_1_id)
        self.assertEqual(device.get("numero_serie"), "NS-BASE-1")
        self.assertEqual(device.get("numero_interno"), "MG 1001")

    def test_patch_parcial_identificadores_sigue_funcionando(self):
        resp = self.client.patch(
            self._url(self.device_1_id),
            {"numero_serie": "NS-PARCIAL-1", "numero_interno": "1234"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(numero_serie,''), COALESCE(numero_interno,'') FROM devices WHERE id=%s",
                [self.device_1_id],
            )
            numero_serie, numero_interno = cur.fetchone()
        self.assertEqual(numero_serie, "NS-PARCIAL-1")
        self.assertEqual(numero_interno, "MG 1234")

    def test_patch_completo_actualiza_todos_los_campos(self):
        payload = {
            "customer_id": self.customer_2_id,
            "tipo_equipo": "Desfibrilador",
            "marca_id": self.marca_2_id,
            "model_id": self.model_2_id,
            "variante": "Serie X",
            "numero_serie": "NS-EDIT-100",
            "numero_interno": "2001",
            "ubicacion_id": self.loc_2_id,
            "alquilado": True,
            "alquiler_a": "Clinica Dos",
        }
        resp = self.client.patch(self._url(self.device_1_id), payload, format="json")
        self.assertEqual(resp.status_code, 200)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT
                  customer_id,
                  marca_id,
                  model_id,
                  COALESCE(tipo_equipo,''),
                  COALESCE(variante,''),
                  COALESCE(numero_serie,''),
                  COALESCE(numero_interno,''),
                  ubicacion_id,
                  COALESCE(alquilado,false),
                  COALESCE(alquiler_a,'')
                FROM devices
                WHERE id=%s
                """,
                [self.device_1_id],
            )
            row = cur.fetchone()

        self.assertEqual(int(row[0]), self.customer_2_id)
        self.assertEqual(int(row[1]), self.marca_2_id)
        self.assertEqual(int(row[2]), self.model_2_id)
        self.assertEqual(row[3], "Desfibrilador")
        self.assertEqual(row[4], "Serie X")
        self.assertEqual(row[5], "NS-EDIT-100")
        self.assertEqual(row[6], "MG 2001")
        self.assertEqual(int(row[7]), self.loc_2_id)
        self.assertTrue(bool(row[8]))
        self.assertEqual(row[9], "Clinica Dos")

    def test_patch_rechaza_numero_serie_duplicado(self):
        resp = self.client.patch(
            self._url(self.device_1_id),
            {"numero_serie": "NS-DUP-2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "NS_DUPLICATE")

    def test_patch_rechaza_numero_interno_duplicado(self):
        resp = self.client.patch(
            self._url(self.device_1_id),
            {"numero_interno": "7777"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "MG_DUPLICATE")
