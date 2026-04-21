from decimal import Decimal

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


class RepuestosCostCurrencyAPITest(TestCase):
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
        subrubros_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_subrubros (
                codigo VARCHAR(8) PRIMARY KEY,
                nombre TEXT NOT NULL,
                activo {bool_type} DEFAULT 1,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """
        catalogo_sql = f"""
            CREATE TABLE IF NOT EXISTS catalogo_repuestos (
                id {auto_inc},
                codigo VARCHAR(64) UNIQUE,
                nombre TEXT,
                costo_neto NUMERIC(12,2) NOT NULL DEFAULT 0,
                costo_usd NUMERIC(12,2) NULL,
                costo_moneda VARCHAR(3) DEFAULT 'USD',
                precio_venta NUMERIC(12,2) NULL,
                multiplicador NUMERIC(10,4) NULL,
                stock_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0,
                stock_min NUMERIC(12,2) NOT NULL DEFAULT 0,
                tipo_articulo TEXT NULL,
                categoria TEXT NULL,
                unidad_medida TEXT NULL,
                marca_fabricante TEXT NULL,
                nro_parte TEXT NULL,
                ubicacion_deposito TEXT NULL,
                estado TEXT NULL,
                notas TEXT NULL,
                fecha_ultima_compra DATE NULL,
                fecha_ultimo_conteo DATE NULL,
                fecha_vencimiento DATE NULL,
                activo {bool_type} DEFAULT 1,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """
        proveedores_sql = f"""
            CREATE TABLE IF NOT EXISTS proveedores_externos (
                id {auto_inc},
                nombre VARCHAR(255) UNIQUE
            ){engine_suffix}
        """
        repuestos_proveedores_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_proveedores (
                id {auto_inc},
                repuesto_id INT NOT NULL,
                proveedor_id INT NOT NULL,
                sku_proveedor TEXT NULL,
                lead_time_dias INT NULL,
                prioridad INT NULL,
                ultima_compra DATE NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """
        stock_permisos_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_stock_permisos (
                id {auto_inc},
                tecnico_id INT NOT NULL,
                enabled_by INT NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                expires_at {datetime_type} NULL,
                revoked_at {datetime_type} NULL,
                revoked_by INT NULL,
                nota TEXT NULL
            ){engine_suffix}
        """
        repuestos_config_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_config (
                id {auto_inc},
                dolar_ars NUMERIC(12,4) NOT NULL DEFAULT 0,
                multiplicador_general NUMERIC(10,4) NOT NULL DEFAULT 1
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(subrubros_sql)
            cur.execute(catalogo_sql)
            cur.execute(proveedores_sql)
            cur.execute(repuestos_proveedores_sql)
            cur.execute(stock_permisos_sql)
            cur.execute(repuestos_config_sql)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM repuestos_proveedores")
            cur.execute("DELETE FROM repuestos_stock_permisos")
            cur.execute("DELETE FROM proveedores_externos")
            cur.execute("DELETE FROM catalogo_repuestos")
            cur.execute("DELETE FROM repuestos_subrubros")
            cur.execute("DELETE FROM repuestos_config")
            cur.execute(
                """
                INSERT INTO repuestos_subrubros (codigo, nombre, activo)
                VALUES (%s,%s,%s)
                """,
                ["1517", "Repuesto generico", 1],
            )
            cur.execute(
                """
                INSERT INTO repuestos_config (dolar_ars, multiplicador_general)
                VALUES (%s,%s)
                """,
                [Decimal("1000.00"), Decimal("2.0000")],
            )

        User.objects.all().delete()
        cls.jefe_user = User.objects.create(
            nombre="Jefe Repuestos",
            email="jefe-repuestos-cost@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe_user)
        with connection.cursor() as cur:
            cur.execute("DELETE FROM repuestos_proveedores")
            cur.execute("DELETE FROM proveedores_externos")
            cur.execute("DELETE FROM repuestos_stock_permisos")
            cur.execute("DELETE FROM catalogo_repuestos")

    def _insert_repuesto(self, **kwargs):
        payload = {
            "codigo": kwargs.get("codigo", "1517001"),
            "nombre": kwargs.get("nombre", "Repuesto test"),
            "costo_neto": kwargs.get("costo_neto", Decimal("0.00")),
            "costo_usd": kwargs.get("costo_usd"),
            "costo_moneda": kwargs.get("costo_moneda", "USD"),
            "multiplicador": kwargs.get("multiplicador"),
            "stock_on_hand": kwargs.get("stock_on_hand", Decimal("0.00")),
            "stock_min": kwargs.get("stock_min", Decimal("0.00")),
            "estado": kwargs.get("estado"),
            "activo": 1,
        }
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalogo_repuestos
                  (codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador, stock_on_hand, stock_min, estado, activo)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    payload["codigo"],
                    payload["nombre"],
                    payload["costo_neto"],
                    payload["costo_usd"],
                    payload["costo_moneda"],
                    payload["multiplicador"],
                    payload["stock_on_hand"],
                    payload["stock_min"],
                    payload["estado"],
                    payload["activo"],
                ],
            )
            return _last_insert_id(cur)

    def test_post_repuesto_ars_persiste_costo_en_pesos(self):
        resp = self.client.post(
            "/api/repuestos/",
            {
                "subrubro_codigo": "1517",
                "nombre": "Filtro ARS",
                "stock_on_hand": 1,
                "costo_moneda": "ARS",
                "costo_valor": "123.45",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data.get("costo_moneda"), "ARS")
        self.assertEqual(Decimal(str(resp.data.get("costo_valor"))), Decimal("123.45"))
        self.assertEqual(Decimal(str(resp.data.get("costo_ars"))), Decimal("123.45"))
        self.assertEqual(Decimal(str(resp.data.get("precio_venta"))), Decimal("246.90"))

        with connection.cursor() as cur:
            cur.execute(
                "SELECT costo_neto, costo_usd, costo_moneda FROM catalogo_repuestos WHERE id=%s",
                [resp.data["id"]],
            )
            costo_neto, costo_usd, costo_moneda = cur.fetchone()
            self.assertEqual(Decimal(str(costo_neto)), Decimal("123.45"))
            self.assertIsNone(costo_usd)
            self.assertEqual(costo_moneda, "ARS")

    def test_post_repuesto_fecha_vencimiento_opcional(self):
        resp = self.client.post(
            "/api/repuestos/",
            {
                "subrubro_codigo": "1517",
                "nombre": "Filtro con vencimiento",
                "stock_on_hand": 1,
                "fecha_vencimiento": "2026-12-31",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(str(resp.data.get("fecha_vencimiento")), "2026-12-31")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT fecha_vencimiento FROM catalogo_repuestos WHERE id=%s",
                [resp.data["id"]],
            )
            (fecha_vencimiento,) = cur.fetchone()
            self.assertEqual(str(fecha_vencimiento), "2026-12-31")

    def test_patch_repuesto_acepta_legacy_usd_y_permite_cambiar_a_ars(self):
        repuesto_id = self._insert_repuesto(codigo="1517002", nombre="Legacy")

        resp_usd = self.client.patch(
            f"/api/repuestos/{repuesto_id}/",
            {"costo_usd": "10.00"},
            format="json",
        )
        self.assertEqual(resp_usd.status_code, 200)
        self.assertEqual(resp_usd.data.get("costo_moneda"), "USD")
        self.assertEqual(Decimal(str(resp_usd.data.get("costo_valor"))), Decimal("10.00"))
        self.assertEqual(Decimal(str(resp_usd.data.get("costo_ars"))), Decimal("10000.00"))
        self.assertEqual(Decimal(str(resp_usd.data.get("precio_venta"))), Decimal("20000.00"))

        resp_ars = self.client.patch(
            f"/api/repuestos/{repuesto_id}/",
            {"costo_moneda": "ARS", "costo_valor": "500.00"},
            format="json",
        )
        self.assertEqual(resp_ars.status_code, 200)
        self.assertEqual(resp_ars.data.get("costo_moneda"), "ARS")
        self.assertEqual(Decimal(str(resp_ars.data.get("costo_valor"))), Decimal("500.00"))
        self.assertEqual(Decimal(str(resp_ars.data.get("precio_venta"))), Decimal("1000.00"))

        with connection.cursor() as cur:
            cur.execute(
                "SELECT costo_neto, costo_usd, costo_moneda FROM catalogo_repuestos WHERE id=%s",
                [repuesto_id],
            )
            costo_neto, costo_usd, costo_moneda = cur.fetchone()
            self.assertEqual(Decimal(str(costo_neto)), Decimal("500.00"))
            self.assertIsNone(costo_usd)
            self.assertEqual(costo_moneda, "ARS")

    def test_catalogo_equipo_hint_prioriza_repuestos_asociados(self):
        self._insert_repuesto(
            codigo="1517999",
            nombre="No asociado",
            estado="Longfian | JAY-5",
        )
        self._insert_repuesto(
            codigo="1517001",
            nombre="Asociado PB 560",
            estado="Longfian | PB 560\nPB 560",
        )

        without_hint = self.client.get("/api/catalogos/repuestos/?limit=2")
        self.assertEqual(without_hint.status_code, 200)
        self.assertEqual(without_hint.data[0]["codigo"], "1517999")

        with_hint = self.client.get(
            "/api/catalogos/repuestos/?limit=2&equipo_hint=Longfian%20%7C%20PB%20560&equipo_hint=PB%20560"
        )
        self.assertEqual(with_hint.status_code, 200)
        self.assertEqual(with_hint.data[0]["codigo"], "1517001")


class QuoteCostCurrencyAPITest(TestCase):
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
        ingresos_sql = f"""
            CREATE TABLE IF NOT EXISTS ingresos (
                id {auto_inc},
                estado TEXT NULL,
                presupuesto_estado TEXT NULL
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
        catalogo_sql = f"""
            CREATE TABLE IF NOT EXISTS catalogo_repuestos (
                id {auto_inc},
                codigo VARCHAR(64) UNIQUE,
                nombre TEXT,
                costo_neto NUMERIC(12,2) NOT NULL DEFAULT 0,
                costo_usd NUMERIC(12,2) NULL,
                costo_moneda VARCHAR(3) DEFAULT 'USD',
                precio_venta NUMERIC(12,2) NULL,
                multiplicador NUMERIC(10,4) NULL,
                activo {bool_type} DEFAULT 1
            ){engine_suffix}
        """
        repuestos_config_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_config (
                id {auto_inc},
                dolar_ars NUMERIC(12,4) NOT NULL DEFAULT 0,
                multiplicador_general NUMERIC(10,4) NOT NULL DEFAULT 1
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(ingresos_sql)
            cur.execute(quotes_sql)
            cur.execute(quote_items_sql)
            cur.execute(catalogo_sql)
            cur.execute(repuestos_config_sql)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM quote_items")
            cur.execute("DELETE FROM quotes")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM catalogo_repuestos")
            cur.execute("DELETE FROM repuestos_config")
            cur.execute(
                """
                INSERT INTO repuestos_config (dolar_ars, multiplicador_general)
                VALUES (%s,%s)
                """,
                [Decimal("1000.00"), Decimal("2.0000")],
            )
        User.objects.filter(email="jefe-quotes-cost@example.com").delete()
        cls.jefe_user = User.objects.create(
            nombre="Jefe Quotes",
            email="jefe-quotes-cost@example.com",
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
            cur.execute("DELETE FROM catalogo_repuestos")

    def _seed_ingreso(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO ingresos (estado, presupuesto_estado) VALUES (%s,%s)",
                ["diagnosticado", "pendiente"],
            )
            return _last_insert_id(cur)

    def _seed_repuesto(self, *, codigo, nombre, costo_moneda, costo_usd=None, costo_neto=Decimal("0.00")):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalogo_repuestos
                  (codigo, nombre, costo_neto, costo_usd, costo_moneda, activo)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [codigo, nombre, costo_neto, costo_usd, costo_moneda, 1],
            )
            return _last_insert_id(cur)

    def test_quote_item_ars_cost_normaliza_y_autocompleta_precio(self):
        ingreso_id = self._seed_ingreso()
        repuesto_id = self._seed_repuesto(
            codigo="R-ARS",
            nombre="Repuesto ARS",
            costo_moneda="ARS",
            costo_neto=Decimal("500.00"),
        )

        resp = self.client.post(
            f"/api/quotes/{ingreso_id}/items/",
            {"tipo": "repuesto", "repuesto_id": repuesto_id, "descripcion": "tmp", "qty": 2},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        item = next(it for it in resp.data["items"] if int(it["repuesto_id"]) == repuesto_id)
        self.assertEqual(Decimal(str(item["costo_u_neto"])), Decimal("500.00"))
        self.assertEqual(Decimal(str(item["precio_u"])), Decimal("1000.00"))

        with connection.cursor() as cur:
            cur.execute(
                "SELECT costo_u_neto, precio_u FROM quote_items WHERE repuesto_id=%s",
                [repuesto_id],
            )
            costo_u_neto, precio_u = cur.fetchone()
            self.assertEqual(Decimal(str(costo_u_neto)), Decimal("500.00"))
            self.assertEqual(Decimal(str(precio_u)), Decimal("1000.00"))

    def test_quote_item_usd_cost_convierte_a_ars(self):
        ingreso_id = self._seed_ingreso()
        repuesto_id = self._seed_repuesto(
            codigo="R-USD",
            nombre="Repuesto USD",
            costo_moneda="USD",
            costo_usd=Decimal("10.00"),
            costo_neto=Decimal("0.00"),
        )

        resp = self.client.post(
            f"/api/quotes/{ingreso_id}/items/",
            {"tipo": "repuesto", "repuesto_id": repuesto_id, "descripcion": "tmp", "qty": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        item = next(it for it in resp.data["items"] if int(it["repuesto_id"]) == repuesto_id)
        self.assertEqual(Decimal(str(item["costo_u_neto"])), Decimal("10000.00"))
        self.assertEqual(Decimal(str(item["precio_u"])), Decimal("20000.00"))

        with connection.cursor() as cur:
            cur.execute(
                "SELECT costo_u_neto, precio_u FROM quote_items WHERE repuesto_id=%s",
                [repuesto_id],
            )
            costo_u_neto, precio_u = cur.fetchone()
            self.assertEqual(Decimal(str(costo_u_neto)), Decimal("10000.00"))
            self.assertEqual(Decimal(str(precio_u)), Decimal("20000.00"))
