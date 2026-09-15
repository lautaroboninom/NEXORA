import os
import tempfile
from decimal import Decimal

from django.db import connection
from django.test import TestCase

from service.tests.schema_helpers import create_fixture_device
from service.repuestos import corregir_codigos_repuestos, load_maestro_codigos_from_excel


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


class CorregirCodigosRepuestosTests(TestCase):
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
                CREATE TABLE IF NOT EXISTS quote_items (
                    id {auto_inc},
                    quote_id INT NOT NULL DEFAULT 1,
                    tipo TEXT NOT NULL DEFAULT 'repuesto',
                    descripcion TEXT NOT NULL DEFAULT '',
                    repuesto_codigo TEXT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS repuestos_cambios (
                    id {auto_inc},
                    repuesto_id INT NULL,
                    codigo TEXT NULL,
                    accion TEXT NOT NULL,
                    nombre_prev TEXT NULL,
                    nombre_new TEXT NULL,
                    nota TEXT NULL,
                    created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    created_by INT NULL
                ){engine_suffix}
                """
            )
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM catalogo_repuestos")
            cur.execute("DELETE FROM quote_items")
            cur.execute("DELETE FROM repuestos_cambios")

    def _insert_repuesto(self, codigo, nombre="x"):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO catalogo_repuestos (codigo, nombre, activo) VALUES (%s,%s,TRUE)",
                [codigo, nombre],
            )

    def _insert_quote_item(self, repuesto_codigo):
        device_id = create_fixture_device()
        with connection.cursor() as cur:
            cur.execute("INSERT INTO ingresos(device_id, motivo) VALUES (%s, 'reparación') RETURNING id", [device_id])
            ingreso_id = cur.fetchone()[0]
            cur.execute("INSERT INTO quotes(ingreso_id) VALUES (%s) RETURNING id", [ingreso_id])
            quote_id = cur.fetchone()[0]
            cur.execute("INSERT INTO quote_items(quote_id, tipo, descripcion, precio_u, repuesto_codigo) VALUES (%s, 'repuesto', 'Repuesto de prueba', 0, %s)", [quote_id, repuesto_codigo])

    def test_renombra_codigo_viejo_a_nuevo_y_actualiza_quote_items(self):
        self._insert_repuesto("AE-001", "Caja de embalaje C-500")
        self._insert_quote_item("AE-001")

        items = [{
            "codigo_viejo": "AE-001", "codigo_nuevo": "1401001",
            "descripcion": "CAJA DE EMBALAJE C-500", "cod_prov": "", "proveedor": "TURANO", "equipo": "C-500",
        }]
        res = corregir_codigos_repuestos(items, dry_run=False)

        self.assertEqual(res["renombrados"], [("AE-001", "1401001")])
        with connection.cursor() as cur:
            cur.execute("SELECT codigo FROM catalogo_repuestos")
            self.assertEqual(cur.fetchone()[0], "1401001")
            cur.execute("SELECT repuesto_codigo FROM quote_items")
            self.assertEqual(cur.fetchone()[0], "1401001")
            cur.execute("SELECT accion, codigo FROM repuestos_cambios")
            accion, codigo = cur.fetchone()
            self.assertEqual(accion, "recodificacion")
            self.assertEqual(codigo, "1401001")

    def test_dry_run_no_escribe_nada(self):
        self._insert_repuesto("AE-001", "Caja de embalaje C-500")
        items = [{"codigo_viejo": "AE-001", "codigo_nuevo": "1401001", "descripcion": "", "cod_prov": "", "proveedor": "", "equipo": ""}]

        res = corregir_codigos_repuestos(items, dry_run=True)

        self.assertEqual(res["renombrados"], [("AE-001", "1401001")])
        with connection.cursor() as cur:
            cur.execute("SELECT codigo FROM catalogo_repuestos")
            self.assertEqual(cur.fetchone()[0], "AE-001")

    def test_no_toca_nada_si_codigo_nuevo_ya_existe_como_otra_fila(self):
        self._insert_repuesto("AE-001", "Caja vieja")
        self._insert_repuesto("1401001", "Ya existe con codigo nuevo")
        items = [{"codigo_viejo": "AE-001", "codigo_nuevo": "1401001", "descripcion": "", "cod_prov": "", "proveedor": "", "equipo": ""}]

        res = corregir_codigos_repuestos(items, dry_run=False)

        self.assertEqual(res["renombrados"], [])
        self.assertEqual(res["colisiones"], [{"codigo_viejo": "AE-001", "codigo_nuevo": "1401001"}])
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM catalogo_repuestos WHERE codigo='AE-001'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_sin_cambio_cuando_bejerman_es_igual_al_viejo(self):
        self._insert_repuesto("BA-303", "algo")
        items = [{"codigo_viejo": "BA-303", "codigo_nuevo": "BA-303", "descripcion": "", "cod_prov": "", "proveedor": "", "equipo": ""}]

        res = corregir_codigos_repuestos(items, dry_run=False)

        self.assertEqual(res["renombrados"], [])
        self.assertEqual(res["sin_cambio"], ["BA-303"])

    def test_pendiente_cuando_no_hay_codigo_bejerman_asignado(self):
        self._insert_repuesto("CE-016", "algo")
        items = [{"codigo_viejo": "CE-016", "codigo_nuevo": "", "descripcion": "", "cod_prov": "", "proveedor": "", "equipo": ""}]

        res = corregir_codigos_repuestos(items, dry_run=False)

        self.assertEqual(res["renombrados"], [])
        self.assertEqual(res["pendientes"], ["CE-016"])

    def test_no_encontrado_cuando_codigo_viejo_no_existe_en_catalogo(self):
        items = [{"codigo_viejo": "ZZ-999", "codigo_nuevo": "1999999", "descripcion": "", "cod_prov": "", "proveedor": "", "equipo": ""}]

        res = corregir_codigos_repuestos(items, dry_run=False)

        self.assertEqual(res["renombrados"], [])
        self.assertEqual(res["no_encontrados"], ["ZZ-999"])

    def test_load_maestro_codigos_from_excel_detecta_header_y_filas(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maestro.xlsx")
            wb = Workbook()
            ws = wb.active
            ws.title = "CODIGOS"
            ws.append([None, None, None, None, None, None, None])
            ws.append([None, "MAESTRO DE CODIGOS", None, None, None, None, None])
            ws.append([None, None, None, None, None, None, None])
            ws.append([None, "CODIGO", "CODIGO BEJERMAN", "DESCRIPCION", "COD PROV.", "PROVEEDOR", "EQUIPO"])
            ws.append([None, "AE-001", 1401001, "CAJA DE EMBALAJE C-500", None, "TURANO", "C-500"])
            ws.append([None, "CE-016", None, "CIRCUITO INTEGRADO LM3914N", None, None, None])
            wb.save(path)

            items = load_maestro_codigos_from_excel(path, sheet="CODIGOS")

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["codigo_viejo"], "AE-001")
        self.assertEqual(items[0]["codigo_nuevo"], "1401001")
        self.assertEqual(items[0]["proveedor"], "TURANO")
        self.assertEqual(items[1]["codigo_viejo"], "CE-016")
        self.assertEqual(items[1]["codigo_nuevo"], "")
