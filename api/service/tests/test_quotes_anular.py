from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.models import User


class QuoteAnularAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
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
                device_id INT NULL,
                estado TEXT,
                presupuesto_estado TEXT,
                asignado_a INT NULL
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
                qty NUMERIC(12,2) NOT NULL DEFAULT 0,
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
                costo_neto NUMERIC(12,2) NOT NULL DEFAULT 0,
                costo_usd NUMERIC(12,2) NULL,
                costo_moneda TEXT NULL,
                stock_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0,
                stock_min NUMERIC(12,2) NOT NULL DEFAULT 0,
                ubicacion_deposito TEXT NULL,
                activo {bool_type} DEFAULT 1,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """

        repuestos_movimientos_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_movimientos (
                id {auto_inc},
                repuesto_id INT NOT NULL,
                tipo TEXT NOT NULL,
                qty NUMERIC(12,2) NOT NULL,
                stock_prev NUMERIC(12,2) NULL,
                stock_new NUMERIC(12,2) NULL,
                ref_tipo TEXT NULL,
                ref_id INT NULL,
                nota TEXT NULL,
                fecha_compra DATE NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                created_by INT NULL
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(ingresos_sql)
            cur.execute(quotes_sql)
            cur.execute(quote_items_sql)
            cur.execute(catalogo_repuestos_sql)
            cur.execute(repuestos_movimientos_sql)
            try:
                cur.execute("ALTER TABLE ingresos ADD COLUMN presupuesto_estado TEXT")
            except Exception:
                pass
        super().setUpClass()

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
            cur.execute("DELETE FROM quote_items")
            cur.execute("DELETE FROM quotes")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM repuestos_movimientos")
            cur.execute("DELETE FROM catalogo_repuestos")
        User.objects.filter(email="jefe-anular-quotes@example.com").delete()
        cls.jefe_user = User.objects.create(
            nombre="Jefe Presupuesto",
            email="jefe-anular-quotes@example.com",
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
            cur.execute("DELETE FROM repuestos_movimientos")
            cur.execute("DELETE FROM catalogo_repuestos")

    def _url_anular(self, ingreso_id: int) -> str:
        return f"/api/quotes/{ingreso_id}/anular/"

    def _seed_case(self, ingreso_estado: str, presupuesto_estado: str, quote_estado: str, stock: int = 3, qty: int = 2):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO ingresos (estado, presupuesto_estado, asignado_a) VALUES (%s,%s,%s)",
                [ingreso_estado, presupuesto_estado, self.jefe_user.id],
            )
            ingreso_id = self._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO quotes (
                    ingreso_id, estado, moneda, subtotal, iva_21, total,
                    autorizado_por, forma_pago, plazo_entrega_txt, garantia_txt, mant_oferta_txt,
                    fecha_emitido, fecha_aprobado, pdf_url
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    ingreso_id,
                    quote_estado,
                    "ARS",
                    Decimal("100.00"),
                    Decimal("21.00"),
                    Decimal("121.00"),
                    "Cliente",
                    "Contado",
                    "5 dias",
                    "90 dias",
                    "7 dias",
                    now,
                    now if quote_estado == "aprobado" else None,
                    "/api/quotes/x/pdf/",
                ],
            )
            quote_id = self._last_insert_id(cur)

            cur.execute(
                "INSERT INTO catalogo_repuestos (codigo, nombre, stock_on_hand, stock_min, activo, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                ["R-001", "Repuesto test", Decimal(str(stock)), Decimal("0"), 1, now],
            )
            repuesto_id = self._last_insert_id(cur)

            cur.execute(
                """
                INSERT INTO quote_items
                  (quote_id, tipo, descripcion, qty, precio_u, repuesto_id, repuesto_codigo, costo_u_neto)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [quote_id, "repuesto", "Repuesto test", Decimal(str(qty)), Decimal("50.00"), repuesto_id, "R-001", Decimal("25.00")],
            )

        return ingreso_id, quote_id, repuesto_id

    def test_anular_aprobado_revierte_stock_y_vuelve_a_diagnosticado(self):
        ingreso_id, quote_id, repuesto_id = self._seed_case(
            ingreso_estado="reparar",
            presupuesto_estado="aprobado",
            quote_estado="aprobado",
            stock=3,
            qty=2,
        )

        resp = self.client.post(self._url_anular(ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "pendiente")

        with connection.cursor() as cur:
            cur.execute("SELECT estado, presupuesto_estado FROM ingresos WHERE id=%s", [ingreso_id])
            ingreso_estado, ingreso_presu = cur.fetchone()
            self.assertEqual(ingreso_estado, "diagnosticado")
            self.assertEqual(ingreso_presu, "pendiente")

            cur.execute("SELECT estado, fecha_emitido, fecha_aprobado, pdf_url FROM quotes WHERE id=%s", [quote_id])
            q_estado, q_emit, q_aprob, q_pdf = cur.fetchone()
            self.assertEqual(q_estado, "pendiente")
            self.assertIsNone(q_emit)
            self.assertIsNone(q_aprob)
            self.assertIsNone(q_pdf)

            cur.execute("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s", [repuesto_id])
            stock = Decimal(str(cur.fetchone()[0]))
            self.assertEqual(stock, Decimal("5"))

            cur.execute(
                """
                SELECT tipo, qty, stock_prev, stock_new, ref_tipo, ref_id, created_by
                FROM repuestos_movimientos
                WHERE repuesto_id=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                [repuesto_id],
            )
            mov = cur.fetchone()
            self.assertIsNotNone(mov)
            self.assertEqual(mov[0], "ingreso_anulacion_aprobado")
            self.assertEqual(Decimal(str(mov[1])), Decimal("2"))
            self.assertEqual(Decimal(str(mov[2])), Decimal("3"))
            self.assertEqual(Decimal(str(mov[3])), Decimal("5"))
            self.assertEqual(mov[4], "quote")
            self.assertEqual(int(mov[5]), quote_id)
            self.assertEqual(int(mov[6]), self.jefe_user.id)

    def test_anular_aprobado_bloquea_entregado_alquilado_y_baja(self):
        for estado_final in ("entregado", "alquilado", "baja"):
            with self.subTest(estado_final=estado_final):
                with connection.cursor() as cur:
                    cur.execute("DELETE FROM quote_items")
                    cur.execute("DELETE FROM quotes")
                    cur.execute("DELETE FROM ingresos")
                    cur.execute("DELETE FROM repuestos_movimientos")
                    cur.execute("DELETE FROM catalogo_repuestos")

                ingreso_id, quote_id, repuesto_id = self._seed_case(
                    ingreso_estado=estado_final,
                    presupuesto_estado="aprobado",
                    quote_estado="aprobado",
                    stock=4,
                    qty=2,
                )

                resp = self.client.post(self._url_anular(ingreso_id), {}, format="json")
                self.assertEqual(resp.status_code, 400)

                with connection.cursor() as cur:
                    cur.execute("SELECT estado, presupuesto_estado FROM ingresos WHERE id=%s", [ingreso_id])
                    ingreso_estado, ingreso_presu = cur.fetchone()
                    self.assertEqual(ingreso_estado, estado_final)
                    self.assertEqual(ingreso_presu, "aprobado")

                    cur.execute("SELECT estado FROM quotes WHERE id=%s", [quote_id])
                    self.assertEqual(cur.fetchone()[0], "aprobado")

                    cur.execute("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s", [repuesto_id])
                    self.assertEqual(Decimal(str(cur.fetchone()[0])), Decimal("4"))

                    cur.execute("SELECT COUNT(*) FROM repuestos_movimientos WHERE repuesto_id=%s", [repuesto_id])
                    self.assertEqual(int(cur.fetchone()[0]), 0)

    def test_anular_presupuestado_no_revierte_stock(self):
        ingreso_id, quote_id, repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="presupuestado",
            quote_estado="presupuestado",
            stock=10,
            qty=2,
        )

        resp = self.client.post(self._url_anular(ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "pendiente")

        with connection.cursor() as cur:
            cur.execute("SELECT estado, presupuesto_estado FROM ingresos WHERE id=%s", [ingreso_id])
            ingreso_estado, ingreso_presu = cur.fetchone()
            self.assertEqual(ingreso_estado, "diagnosticado")
            self.assertEqual(ingreso_presu, "pendiente")

            cur.execute("SELECT estado, fecha_emitido, fecha_aprobado, pdf_url FROM quotes WHERE id=%s", [quote_id])
            q_estado, q_emit, q_aprob, q_pdf = cur.fetchone()
            self.assertEqual(q_estado, "pendiente")
            self.assertIsNone(q_emit)
            self.assertIsNone(q_aprob)
            self.assertIsNone(q_pdf)

            cur.execute("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s", [repuesto_id])
            self.assertEqual(Decimal(str(cur.fetchone()[0])), Decimal("10"))

            cur.execute("SELECT COUNT(*) FROM repuestos_movimientos WHERE repuesto_id=%s", [repuesto_id])
            self.assertEqual(int(cur.fetchone()[0]), 0)
