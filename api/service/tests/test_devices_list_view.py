from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DevicesListViewAPITest(TestCase):
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
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    updated_by INTEGER NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT,
                    razon_social TEXT NOT NULL,
                    telefono TEXT,
                    telefono_2 TEXT,
                    email TEXT
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
                    tipo_equipo TEXT,
                    variante TEXT
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
                    marca_id INTEGER NULL REFERENCES marcas(id),
                    model_id INTEGER NULL REFERENCES models(id),
                    numero_serie TEXT,
                    numero_interno TEXT,
                    tipo_equipo TEXT,
                    variante TEXT,
                    garantia_vence DATE,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    propietario TEXT,
                    propietario_nombre TEXT,
                    propietario_contacto TEXT,
                    propietario_doc TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT,
                    motivo TEXT,
                    fecha_ingreso TIMESTAMPTZ NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ubicacion_id INTEGER NULL REFERENCES locations(id)
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
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM locations")
            cur.execute("DELETE FROM customers")
            cur.execute("DELETE FROM user_permission_overrides")
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
                ["PART", "Particular", "111"],
            )
            cls.customer_particular_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI001", "Clinica Demo", "222"],
            )
            cls.customer_cli_id = cls._last_id(cur)
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["BMC"])
            cls.marca_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s)",
                [cls.marca_id, "CPAP G3", "CPAP", "Elite"],
            )
            cls.model_id = cls._last_id(cur)
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["Longfian"])
            cls.marca_oxi_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s)",
                [cls.marca_oxi_id, "JAY-5", "Concentrador", "5L"],
            )
            cls.model_oxi_id = cls._last_id(cur)
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Taller"])
            cls.location_id = cls._last_id(cur)
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Depósito"])
            cls.location_deposito_id = cls._last_id(cur)
            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno,
                    tipo_equipo, variante, ubicacion_id,
                    propietario_nombre, propietario_contacto, propietario_doc,
                    alquilado, alquiler_a
                ) VALUES (%s,%s,%s,%s,%s,NULL,NULL,%s,%s,%s,%s,false,'')
                """,
                [
                    cls.customer_particular_id,
                    cls.marca_id,
                    cls.model_id,
                    "SER-CPAP-001",
                    "",
                    cls.location_id,
                    "Juan Perez",
                    "11-5555-1234",
                    "20-12345678-9",
                ],
            )
            cls.device_id = cls._last_id(cur)
            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno,
                    tipo_equipo, variante, ubicacion_id,
                    propietario_nombre, propietario_contacto, propietario_doc,
                    alquilado, alquiler_a
                ) VALUES (%s,%s,%s,%s,%s,NULL,NULL,%s,'','','',false,'')
                """,
                [
                    cls.customer_cli_id,
                    cls.marca_id,
                    cls.model_id,
                    "SER-MG-003",
                    "MG 0003",
                    cls.location_id,
                ],
            )
            cls.device_mg_id = cls._last_id(cur)
            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno,
                    tipo_equipo, variante, ubicacion_id,
                    propietario_nombre, propietario_contacto, propietario_doc,
                    alquilado, alquiler_a
                ) VALUES (%s,%s,%s,%s,%s,NULL,NULL,%s,'','','',true,%s)
                """,
                [
                    cls.customer_cli_id,
                    cls.marca_oxi_id,
                    cls.model_oxi_id,
                    "SER-OXI-002",
                    "",
                    cls.location_deposito_id,
                    "Clinica Demo",
                ],
            )
            cls.device_oxi_id = cls._last_id(cur)
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, motivo, fecha_ingreso, ubicacion_id)
                VALUES (%s,%s,%s, CURRENT_TIMESTAMP, %s)
                """,
                [cls.device_id, "ingresado", "reparacion", cls.location_id],
            )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _list_ids(self, **params):
        query = {"page_size": 20, **params}
        resp = self.client.get("/api/equipos/", query)
        self.assertEqual(resp.status_code, 200, resp.data)
        return {int(item.get("id") or 0) for item in (resp.data.get("items") or [])}

    def test_devices_list_uses_model_type_and_variant_when_device_is_blank(self):
        resp = self.client.get("/api/equipos/?page_size=20")
        self.assertEqual(resp.status_code, 200)
        items = resp.data.get("items") or []
        row = next((item for item in items if int(item.get("id") or 0) == self.device_id), None)
        self.assertIsNotNone(row)
        self.assertEqual(row.get("tipo_equipo"), "CPAP")
        self.assertEqual(row.get("variante"), "Elite")

    def test_device_identificadores_uses_model_type_and_variant_when_device_is_blank(self):
        resp = self.client.get(f"/api/devices/{self.device_id}/identificadores/")
        self.assertEqual(resp.status_code, 200)
        device = resp.data.get("device") or {}
        self.assertEqual(device.get("tipo_equipo"), "CPAP")
        self.assertEqual(device.get("variante"), "Elite")

    def test_admin_can_create_direct_device_with_default_permissions(self):
        resp = self.client.post(
            "/api/devices/alta-directa/",
            {
                "customer_id": self.customer_cli_id,
                "marca_id": self.marca_id,
                "model_id": self.model_id,
                "numero_serie": "SER-CPAP-ADM-002",
                "numero_interno": "",
                "tipo_equipo": "CPAP",
                "variante": "Elite",
                "ubicacion_id": self.location_id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        device = resp.data.get("device") or {}
        self.assertEqual(device.get("customer_id"), self.customer_cli_id)
        self.assertEqual(device.get("numero_serie"), "SER-CPAP-ADM-002")
        self.assertEqual(device.get("tipo_equipo"), "CPAP")

    def test_devices_list_search_matches_owner_name_and_variant(self):
        resp_owner = self.client.get("/api/equipos/?page_size=20&q=Juan Perez")
        self.assertEqual(resp_owner.status_code, 200)
        owner_ids = {int(item.get("id") or 0) for item in (resp_owner.data.get("items") or [])}
        self.assertIn(self.device_id, owner_ids)

        resp_variant = self.client.get("/api/equipos/?page_size=20&q=Elite")
        self.assertEqual(resp_variant.status_code, 200)
        variant_ids = {int(item.get("id") or 0) for item in (resp_variant.data.get("items") or [])}
        self.assertIn(self.device_id, variant_ids)

    def test_devices_list_filters_by_catalog_fields(self):
        cpap_ids = self._list_ids(tipo_equipo="CPAP")
        self.assertIn(self.device_id, cpap_ids)
        self.assertIn(self.device_mg_id, cpap_ids)
        self.assertNotIn(self.device_oxi_id, cpap_ids)

        marca_ids = self._list_ids(marca_id=self.marca_oxi_id)
        self.assertEqual(marca_ids, {self.device_oxi_id})

        modelo_ids = self._list_ids(modelo="JAY")
        self.assertEqual(modelo_ids, {self.device_oxi_id})

        variante_ids = self._list_ids(modelo="Elite")
        self.assertIn(self.device_id, variante_ids)
        self.assertIn(self.device_mg_id, variante_ids)
        self.assertNotIn(self.device_oxi_id, variante_ids)

        ubicacion_ids = self._list_ids(ubicacion_id=self.location_deposito_id)
        self.assertEqual(ubicacion_ids, {self.device_oxi_id})

        alquilado_ids = self._list_ids(alquilado="1")
        self.assertEqual(alquilado_ids, {self.device_oxi_id})

    def test_devices_list_filters_by_property(self):
        mg_ids = self._list_ids(propiedad="mg")
        self.assertIn(self.device_mg_id, mg_ids)
        self.assertNotIn(self.device_id, mg_ids)
        self.assertNotIn(self.device_oxi_id, mg_ids)

        cliente_ids = self._list_ids(propiedad="cliente")
        self.assertIn(self.device_id, cliente_ids)
        self.assertIn(self.device_oxi_id, cliente_ids)
        self.assertNotIn(self.device_mg_id, cliente_ids)
