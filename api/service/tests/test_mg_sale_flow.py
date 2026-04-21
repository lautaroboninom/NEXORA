from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class MgSaleFlowAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE
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
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    informe_preliminar TEXT,
                    accesorios TEXT,
                    comentarios TEXT,
                    garantia_reparacion BOOLEAN,
                    garantia_fabrica BOOLEAN,
                    remito_ingreso TEXT
                )
                """
            )
        call_command("apply_mg_sale_schema")

    @classmethod
    def _last_id(cls, cur):
        cur.execute("SELECT LASTVAL()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM device_mg_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.all().delete()

        cls.user = User.objects.create(
            nombre="Admin MG",
            email="admin-mg@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.token = issue_token(cls.user)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["MGBIO", "MG BIO", ""],
            )
            cls.customer_mg_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s)",
                ["CLI001", "Clinica Demo", "123"],
            )
            cls.customer_cli_id = cls._last_id(cur)

            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["ResMed"])
            cls.marca_id = cls._last_id(cur)
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [cls.marca_id, "AirSense 10", "CPAP"],
            )
            cls.model_id = cls._last_id(cur)
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", ["Taller"])
            cls.taller_id = cls._last_id(cur)

            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado, mg_estado
                ) VALUES (%s,%s,%s,%s,%s,false,'activo')
                """,
                [cls.customer_cli_id, cls.marca_id, cls.model_id, "NS-MG-9001", "MG 9001"],
            )
            cls.device_id = cls._last_id(cur)

            cur.execute(
                """
                INSERT INTO devices(
                    customer_id, marca_id, model_id, numero_serie, numero_interno, alquilado, mg_estado
                ) VALUES (%s,%s,%s,%s,%s,false,'activo')
                """,
                [cls.customer_cli_id, cls.marca_id, cls.model_id, "NS-MG-9002", "MG 9002"],
            )
            cls.device_id_2 = cls._last_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _url_venta(self, device_id):
        return f"/api/equipos/{device_id}/mg/venta/"

    def _url_reactivar(self, device_id):
        return f"/api/equipos/{device_id}/mg/reactivar/"

    def _url_nuevo_ingreso(self):
        return "/api/ingresos/nuevo/"

    def _base_nuevo_payload(self, *, numero_serie, numero_interno):
        return {
            "cliente": {"id": self.customer_cli_id},
            "equipo": {
                "marca_id": self.marca_id,
                "modelo_id": self.model_id,
                "numero_serie": numero_serie,
                "numero_interno": numero_interno,
                "garantia": False,
            },
            "motivo": "reparacion",
            "informe_preliminar": "Test",
            "comentarios": "",
            "garantia_reparacion": False,
            "accesorios_items": [],
            "propietario": {"nombre": "", "contacto": "", "doc": ""},
            "fecha_ingreso": "2026-03-10",
            "remito_ingreso": "",
            "ubicacion_id": self.taller_id,
        }

    def test_mg_venta_acepta_solo_factura(self):
        resp = self.client.post(
            self._url_venta(self.device_id),
            {"factura_numero": "FAC-001", "source": "equipos"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["device"]["mg_estado"], "inactivo_venta")
        self.assertTrue(resp.data["device"]["mg_inactivo_venta"])

    def test_mg_venta_acepta_solo_remito(self):
        resp = self.client.post(
            self._url_venta(self.device_id_2),
            {"remito_numero": "REM-001", "source": "equipos"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["device"]["mg_estado"], "inactivo_venta")

    def test_mg_venta_rechaza_sin_comprobante(self):
        resp = self.client.post(
            self._url_venta(self.device_id),
            {"source": "equipos"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "MG_VENTA_COMPROBANTE_REQUERIDO")

    def test_mg_venta_rechaza_doble_venta(self):
        first = self.client.post(
            self._url_venta(self.device_id),
            {"factura_numero": "FAC-111", "source": "equipos"},
            format="json",
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            self._url_venta(self.device_id),
            {"factura_numero": "FAC-112", "source": "equipos"},
            format="json",
        )
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data.get("conflict_type"), "MG_YA_INACTIVO_VENTA")

    def test_mg_reactivacion_registra_evento(self):
        sold = self.client.post(
            self._url_venta(self.device_id),
            {"factura_numero": "FAC-200", "source": "equipos"},
            format="json",
        )
        self.assertEqual(sold.status_code, 200)
        reactivated = self.client.post(
            self._url_reactivar(self.device_id),
            {"observaciones": "Corrección", "source": "equipos"},
            format="json",
        )
        self.assertEqual(reactivated.status_code, 200)
        self.assertEqual(reactivated.data["device"]["mg_estado"], "activo")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM device_mg_events WHERE device_id=%s AND accion='reactivacion'",
                [self.device_id],
            )
            count = int(cur.fetchone()[0] or 0)
        self.assertGreaterEqual(count, 1)

    def test_nuevo_ingreso_rechaza_mg_inactivo(self):
        sold = self.client.post(
            self._url_venta(self.device_id),
            {"factura_numero": "FAC-300", "source": "equipos"},
            format="json",
        )
        self.assertEqual(sold.status_code, 200)

        payload = self._base_nuevo_payload(numero_serie="NS-MG-9001", numero_interno="MG 9001")
        resp = self.client.post(self._url_nuevo_ingreso(), payload, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("conflict_type"), "MG_INACTIVO_VENTA")

    def test_nuevo_ingreso_por_ns_sigue_funcionando(self):
        sold = self.client.post(
            self._url_venta(self.device_id),
            {"remito_numero": "REM-301", "source": "equipos"},
            format="json",
        )
        self.assertEqual(sold.status_code, 200)

        payload = self._base_nuevo_payload(numero_serie="NS-MG-9001", numero_interno="")
        resp = self.client.post(self._url_nuevo_ingreso(), payload, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(bool(resp.data.get("ingreso_id")))
