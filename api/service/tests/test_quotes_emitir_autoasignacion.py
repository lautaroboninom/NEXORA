from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


def _schema_bits():
    vendor = connection.vendor
    if vendor == "sqlite":
        auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
        datetime_type = "DATETIME"
    elif vendor == "postgresql":
        auto_inc = "BIGSERIAL PRIMARY KEY"
        datetime_type = "TIMESTAMPTZ"
    else:
        auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
        datetime_type = "DATETIME"
    bool_type = "BOOLEAN" if vendor != "sqlite" else "INTEGER"
    engine_suffix = " ENGINE=InnoDB" if vendor == "mysql" else ""
    return auto_inc, datetime_type, bool_type, engine_suffix


def _last_insert_id(cur):
    if connection.vendor == "sqlite":
        cur.execute("SELECT last_insert_rowid()")
    elif connection.vendor == "postgresql":
        cur.execute("SELECT LASTVAL()")
    else:
        cur.execute("SELECT LAST_INSERT_ID()")
    return int(cur.fetchone()[0])


class QuoteEmitirAutoasignacionAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        auto_inc, datetime_type, bool_type, engine_suffix = _schema_bits()

        users_sql = f"""
            CREATE TABLE IF NOT EXISTS users (
                id {auto_inc},
                nombre TEXT,
                email VARCHAR(320) UNIQUE,
                hash_pw TEXT,
                rol TEXT,
                activo {bool_type} DEFAULT 1
            ){engine_suffix}
        """
        marcas_sql = f"""
            CREATE TABLE IF NOT EXISTS marcas (
                id {auto_inc},
                nombre TEXT
            ){engine_suffix}
        """
        models_sql = f"""
            CREATE TABLE IF NOT EXISTS models (
                id {auto_inc},
                marca_id INT NULL,
                nombre TEXT,
                variante TEXT NULL
            ){engine_suffix}
        """
        devices_sql = f"""
            CREATE TABLE IF NOT EXISTS devices (
                id {auto_inc},
                marca_id INT NULL,
                model_id INT NULL,
                variante TEXT NULL
            ){engine_suffix}
        """
        ingresos_sql = f"""
            CREATE TABLE IF NOT EXISTS ingresos (
                id {auto_inc},
                device_id INT NULL,
                estado TEXT,
                presupuesto_estado TEXT,
                equipo_variante TEXT NULL
            ){engine_suffix}
        """
        quotes_sql = f"""
            CREATE TABLE IF NOT EXISTS quotes (
                id {auto_inc},
                ingreso_id INT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'pendiente',
                moneda TEXT NOT NULL DEFAULT 'ARS',
                subtotal NUMERIC(12,2) NOT NULL DEFAULT 0,
                iva_21 NUMERIC(12,2) NOT NULL DEFAULT 0,
                total NUMERIC(12,2) NOT NULL DEFAULT 0,
                autorizado_por TEXT NULL,
                forma_pago TEXT NULL,
                plazo_entrega_txt TEXT NULL,
                garantia_txt TEXT NULL,
                mant_oferta_txt TEXT NULL,
                fecha_emitido {datetime_type} NULL,
                fecha_aprobado {datetime_type} NULL,
                pdf_url TEXT NULL
            ){engine_suffix}
        """
        quote_items_sql = f"""
            CREATE TABLE IF NOT EXISTS quote_items (
                id {auto_inc},
                quote_id INT NOT NULL,
                tipo TEXT NOT NULL,
                descripcion TEXT NOT NULL,
                qty NUMERIC(12,2) NOT NULL DEFAULT 1,
                precio_u NUMERIC(12,2) NOT NULL DEFAULT 0,
                repuesto_id INT NULL,
                repuesto_codigo TEXT NULL,
                costo_u_neto NUMERIC(12,2) NULL
            ){engine_suffix}
        """
        catalogo_repuestos_sql = f"""
            CREATE TABLE IF NOT EXISTS catalogo_repuestos (
                id {auto_inc},
                codigo TEXT NULL,
                nombre TEXT NULL,
                estado TEXT NULL,
                activo {bool_type} DEFAULT 1
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(marcas_sql)
            cur.execute(models_sql)
            cur.execute(devices_sql)
            cur.execute(ingresos_sql)
            cur.execute(quotes_sql)
            cur.execute(quote_items_sql)
            cur.execute(catalogo_repuestos_sql)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.filter(email="jefe-emitir-autoasig@example.com").delete()
        cls.jefe_user = User.objects.create(
            nombre="Jefe Emitir",
            email="jefe-emitir-autoasig@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe_user)
        with connection.cursor() as cur:
            cur.execute("DELETE FROM quote_items")
            cur.execute("DELETE FROM quotes")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM catalogo_repuestos")

    def _seed_marca(self, nombre: str) -> int:
        with connection.cursor() as cur:
            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", [nombre])
            return _last_insert_id(cur)

    def _seed_modelo(self, marca_id: int | None, nombre: str, variante: str | None = None) -> int:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO models (marca_id, nombre, variante) VALUES (%s,%s,%s)",
                [marca_id, nombre, variante],
            )
            return _last_insert_id(cur)

    def _seed_device(self, marca_id: int | None, modelo_id: int | None, variante: str | None = None) -> int:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO devices (marca_id, model_id, variante) VALUES (%s,%s,%s)",
                [marca_id, modelo_id, variante],
            )
            return _last_insert_id(cur)

    def _seed_ingreso(self, device_id: int, equipo_variante: str | None = None) -> int:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, presupuesto_estado, equipo_variante)
                VALUES (%s,%s,%s,%s)
                """,
                [device_id, "diagnosticado", "pendiente", equipo_variante],
            )
            return _last_insert_id(cur)

    def _seed_quote(self, ingreso_id: int) -> int:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quotes (ingreso_id, estado, moneda, subtotal, iva_21, total)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [ingreso_id, "pendiente", "ARS", 0, 0, 0],
            )
            return _last_insert_id(cur)

    def _seed_repuesto(self, codigo: str, estado: str | None = None, activo: int = 1) -> int:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalogo_repuestos (codigo, nombre, estado, activo)
                VALUES (%s,%s,%s,%s)
                """,
                [codigo, f"Repuesto {codigo}", estado, activo],
            )
            return _last_insert_id(cur)

    def _seed_quote_item(
        self,
        quote_id: int,
        repuesto_id: int | None = None,
        repuesto_codigo: str | None = None,
    ) -> int:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quote_items
                  (quote_id, tipo, descripcion, qty, precio_u, repuesto_id, repuesto_codigo, costo_u_neto)
                VALUES (%s,'repuesto','Repuesto',1,100,%s,%s,50)
                """,
                [quote_id, repuesto_id, repuesto_codigo],
            )
            return _last_insert_id(cur)

    def _get_repuesto_estado(self, repuesto_id: int) -> str:
        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(estado, '') FROM catalogo_repuestos WHERE id=%s", [repuesto_id])
            row = cur.fetchone()
            return str(row[0] or "")

    def _emitir(self, ingreso_id: int):
        with patch("service.views.quotes_views.render_quote_pdf", return_value=("OS_TEST.pdf", b"%PDF-1.4")):
            return self.client.post(f"/api/quotes/{ingreso_id}/emitir/", {}, format="json")

    def test_emitir_asocia_repuesto_con_marca_modelo_variante(self):
        marca_id = self._seed_marca("BMC")
        modelo_id = self._seed_modelo(marca_id, "CPAP G2", variante="VAR_MODELO")
        device_id = self._seed_device(marca_id, modelo_id, variante="VAR_DEVICE")
        ingreso_id = self._seed_ingreso(device_id, equipo_variante="PRO")
        quote_id = self._seed_quote(ingreso_id)
        repuesto_id = self._seed_repuesto("1501001")
        self._seed_quote_item(quote_id, repuesto_id=repuesto_id)

        resp = self._emitir(ingreso_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_repuesto_estado(repuesto_id), "BMC | CPAP G2 | PRO")

    def test_emitir_asocia_repuesto_sin_variante(self):
        marca_id = self._seed_marca("Longfian")
        modelo_id = self._seed_modelo(marca_id, "JAY-5")
        device_id = self._seed_device(marca_id, modelo_id)
        ingreso_id = self._seed_ingreso(device_id)
        quote_id = self._seed_quote(ingreso_id)
        repuesto_id = self._seed_repuesto("1504004")
        self._seed_quote_item(quote_id, repuesto_id=repuesto_id)

        resp = self._emitir(ingreso_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_repuesto_estado(repuesto_id), "Longfian | JAY-5")

    def test_emitir_asocia_tambien_cuando_item_tiene_solo_repuesto_codigo(self):
        marca_id = self._seed_marca("Inogen")
        modelo_id = self._seed_modelo(marca_id, "ONE")
        device_id = self._seed_device(marca_id, modelo_id, variante="G2")
        ingreso_id = self._seed_ingreso(device_id)
        quote_id = self._seed_quote(ingreso_id)
        repuesto_id = self._seed_repuesto("1504002")
        self._seed_quote_item(quote_id, repuesto_id=None, repuesto_codigo="1504002")

        resp = self._emitir(ingreso_id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(repuesto_id > 0)
        self.assertEqual(self._get_repuesto_estado(repuesto_id), "Inogen | ONE | G2")

    def test_emitir_es_idempotente_y_no_duplica_linea(self):
        marca_id = self._seed_marca("BMC")
        modelo_id = self._seed_modelo(marca_id, "PolyWatch YH-600B")
        device_id = self._seed_device(marca_id, modelo_id, variante="PRO")
        ingreso_id = self._seed_ingreso(device_id)
        quote_id = self._seed_quote(ingreso_id)
        repuesto_id = self._seed_repuesto("1508001")
        self._seed_quote_item(quote_id, repuesto_id=repuesto_id)

        resp_1 = self._emitir(ingreso_id)
        resp_2 = self._emitir(ingreso_id)
        self.assertEqual(resp_1.status_code, 200)
        self.assertEqual(resp_2.status_code, 200)

        estado = self._get_repuesto_estado(repuesto_id)
        lines = [line.strip() for line in estado.splitlines() if line.strip()]
        self.assertEqual(lines.count("BMC | PolyWatch YH-600B | PRO"), 1)
        self.assertEqual(len(lines), 1)

    def test_emitir_no_asocia_si_falta_marca_o_modelo(self):
        device_id = self._seed_device(None, None, variante="X")
        ingreso_id = self._seed_ingreso(device_id, equipo_variante="Y")
        quote_id = self._seed_quote(ingreso_id)
        repuesto_id = self._seed_repuesto("1517001", estado="Equipo previo")
        self._seed_quote_item(quote_id, repuesto_id=repuesto_id)

        resp = self._emitir(ingreso_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_repuesto_estado(repuesto_id), "Equipo previo")
