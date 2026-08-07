import os
import tempfile
from decimal import Decimal

from django.db import connection
from django.test import TestCase

from service.repuestos import actualizar_costos_masivos, load_costos_masivos_from_excel


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


class ActualizarCostosMasivosTests(TestCase):
    @classmethod
    def setUpClass(cls):
        auto_inc, datetime_type, bool_type, engine_suffix = _schema_bits()
        with connection.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS catalogo_repuestos (
                    id {auto_inc},
                    codigo VARCHAR(64) UNIQUE,
                    nombre TEXT,
                    costo_neto NUMERIC(12,2) NOT NULL DEFAULT 0,
                    costo_usd NUMERIC(12,2) NULL,
                    costo_moneda VARCHAR(3) DEFAULT 'USD',
                    activo {bool_type} DEFAULT TRUE,
                    updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
                """
            )
            if connection.vendor == "postgresql":
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS costo_neto NUMERIC(12,2) NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE catalogo_repuestos ADD COLUMN IF NOT EXISTS costo_moneda VARCHAR(3) DEFAULT 'USD'")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS repuestos_config (
                    id {auto_inc},
                    dolar_ars NUMERIC(12,4) NOT NULL DEFAULT 0,
                    multiplicador_general NUMERIC(10,4) NOT NULL DEFAULT 1
                ){engine_suffix}
                """
            )
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM catalogo_repuestos")
            cur.execute("DELETE FROM repuestos_config")
            cur.execute(
                "INSERT INTO repuestos_config (dolar_ars, multiplicador_general) VALUES (%s,%s)",
                [Decimal("1500.00"), Decimal("2.0000")],
            )

    def _insert_repuesto(self, codigo, nombre, costo_usd=None):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO catalogo_repuestos (codigo, nombre, costo_usd, costo_moneda, activo) "
                "VALUES (%s,%s,%s,'USD',TRUE)",
                [codigo, nombre, costo_usd],
            )

    def test_actualiza_costo_de_repuesto_existente_sin_tocar_nombre(self):
        self._insert_repuesto("1101002", "Nombre viejo en DB", costo_usd=Decimal("100.00"))

        items = [{"codigo": "1101002", "nombre": "Nombre nuevo del excel", "costo_usd": Decimal("280.00")}]
        res = actualizar_costos_masivos(items, insertar_faltantes=True)

        self.assertEqual(res["actualizados"], ["1101002"])
        self.assertEqual(res["insertados"], [])

        with connection.cursor() as cur:
            cur.execute(
                "SELECT nombre, costo_usd, costo_neto, costo_moneda FROM catalogo_repuestos WHERE codigo=%s",
                ["1101002"],
            )
            nombre, costo_usd, costo_neto, costo_moneda = cur.fetchone()
            self.assertEqual(nombre, "Nombre viejo en DB")
            self.assertEqual(Decimal(str(costo_usd)), Decimal("280.00"))
            self.assertEqual(Decimal(str(costo_neto)), Decimal("420000.00"))
            self.assertEqual(costo_moneda, "USD")

    def test_inserta_codigo_nuevo_cuando_insertar_faltantes(self):
        items = [{"codigo": "1230030", "nombre": "Aspirador Movi-Vac 550", "costo_usd": Decimal("450.00")}]
        res = actualizar_costos_masivos(items, insertar_faltantes=True)

        self.assertEqual(res["insertados"], ["1230030"])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT nombre, costo_usd, activo FROM catalogo_repuestos WHERE codigo=%s",
                ["1230030"],
            )
            nombre, costo_usd, activo = cur.fetchone()
            self.assertEqual(nombre, "Aspirador Movi-Vac 550")
            self.assertEqual(Decimal(str(costo_usd)), Decimal("450.00"))
            self.assertTrue(activo)

    def test_omite_codigo_nuevo_cuando_no_insertar_faltantes(self):
        items = [{"codigo": "9999999", "nombre": "No existe", "costo_usd": Decimal("10.00")}]
        res = actualizar_costos_masivos(items, insertar_faltantes=False)

        self.assertEqual(res["insertados"], [])
        self.assertEqual(res["omitidos"], ["9999999"])
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM catalogo_repuestos WHERE codigo=%s", ["9999999"])
            self.assertEqual(cur.fetchone()[0], 0)

    def test_load_costos_masivos_from_excel_lee_hoja1(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "costos.xlsx")
            wb = Workbook()
            ws = wb.active
            ws.title = "Hoja1"
            ws.append(["Cod. Intern.", "Descripción", "Lista en USD NETO"])
            ws.append([1101002, "Aspirador portátil Konsung 9E-B", 280])
            ws.append([1230030, "Aspirador portátil Movi-Vac 550", 450])
            wb.save(path)

            items = load_costos_masivos_from_excel(path, sheet="Hoja1")

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["codigo"], "1101002")
        self.assertEqual(items[0]["costo_usd"], Decimal("280.00"))
        self.assertEqual(items[1]["codigo"], "1230030")
        self.assertEqual(items[1]["costo_usd"], Decimal("450.00"))
