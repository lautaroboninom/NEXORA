from decimal import Decimal
from unittest.mock import patch

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
                activo {bool_type} DEFAULT TRUE
            ){engine_suffix}
        """

        ingresos_sql = f"""
            CREATE TABLE IF NOT EXISTS ingresos (
                id {auto_inc},
                device_id INT NULL,
                estado TEXT,
                presupuesto_estado TEXT,
                presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL,
                presupuesto_rechazado_quote_id INT NULL,
                motivo TEXT NULL,
                permite_reparacion {bool_type} DEFAULT TRUE,
                asignado_a INT NULL
            ){engine_suffix}
        """
        customers_sql = f"""
            CREATE TABLE IF NOT EXISTS customers (
                id {auto_inc},
                razon_social TEXT NULL
            ){engine_suffix}
        """
        marcas_sql = f"""
            CREATE TABLE IF NOT EXISTS marcas (
                id {auto_inc},
                nombre TEXT NULL
            ){engine_suffix}
        """
        models_sql = f"""
            CREATE TABLE IF NOT EXISTS models (
                id {auto_inc},
                nombre TEXT NULL,
                tipo_equipo TEXT NULL
            ){engine_suffix}
        """
        devices_sql = f"""
            CREATE TABLE IF NOT EXISTS devices (
                id {auto_inc},
                customer_id INT NULL,
                marca_id INT NULL,
                model_id INT NULL,
                numero_serie TEXT NULL,
                numero_interno TEXT NULL
            ){engine_suffix}
        """

        quotes_sql = f"""
            CREATE TABLE IF NOT EXISTS quotes (
                id {auto_inc},
                ingreso_id INT NOT NULL,
                version_num INT NOT NULL DEFAULT 1,
                origen_quote_id INT NULL,
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
                fecha_rechazado {datetime_type} NULL,
                rechazo_comentario TEXT NULL,
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
                activo {bool_type} DEFAULT TRUE,
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

        audit_log_sql = f"""
            CREATE TABLE IF NOT EXISTS audit_log (
                id {auto_inc},
                ts {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                user_id INT NULL,
                role TEXT NULL,
                method TEXT NULL,
                path TEXT NULL,
                ip TEXT NULL,
                user_agent TEXT NULL,
                status_code INT NULL,
                body TEXT NULL
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(ingresos_sql)
            cur.execute(customers_sql)
            cur.execute(marcas_sql)
            cur.execute(models_sql)
            cur.execute(devices_sql)
            cur.execute(quotes_sql)
            cur.execute(quote_items_sql)
            cur.execute(catalogo_repuestos_sql)
            cur.execute(repuestos_movimientos_sql)
            cur.execute(audit_log_sql)
            try:
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_cobro_neto NUMERIC(12,2) NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_quote_id INT NULL")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT NULL")
                cur.execute(f"ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS permite_reparacion {bool_type} DEFAULT TRUE")
            except Exception:
                connection.rollback()
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
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
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
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
            cur.execute("DELETE FROM repuestos_movimientos")
            cur.execute("DELETE FROM catalogo_repuestos")

    def _url_anular(self, ingreso_id: int) -> str:
        return f"/api/quotes/{ingreso_id}/anular/"

    def _url_rechazar(self, ingreso_id: int) -> str:
        return f"/api/quotes/{ingreso_id}/rechazar/"

    def _url_aprobar(self, ingreso_id: int) -> str:
        return f"/api/quotes/{ingreso_id}/aprobar/"

    def _url_versiones(self, ingreso_id: int) -> str:
        return f"/api/quotes/{ingreso_id}/versiones/"

    def _url_detail(self, ingreso_id: int, quote_id: int | None = None) -> str:
        if quote_id is None:
            return f"/api/quotes/{ingreso_id}/"
        return f"/api/quotes/{ingreso_id}/?quote_id={quote_id}"

    def _seed_case(self, ingreso_estado: str, presupuesto_estado: str, quote_estado: str, stock: int = 3, qty: int = 2):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute("INSERT INTO customers (razon_social) VALUES (%s)", ["Cliente test"])
            customer_id = self._last_insert_id(cur)
            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["Marca test"])
            marca_id = self._last_insert_id(cur)
            cur.execute("INSERT INTO models (nombre, tipo_equipo) VALUES (%s,%s)", ["Modelo test", "Equipo"])
            model_id = self._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno)
                VALUES (%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_id, model_id, "NS-QA-001", "MG-QA-001"],
            )
            device_id = self._last_insert_id(cur)
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, presupuesto_estado, asignado_a) VALUES (%s,%s,%s,%s)",
                [device_id, ingreso_estado, presupuesto_estado, self.jefe_user.id],
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
                ["R-001", "Repuesto test", Decimal(str(stock)), Decimal("0"), True, now],
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

    def test_detalle_emitido_legacy_muestra_acciones_de_emitido(self):
        ingreso_id, _quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="pendiente",
            quote_estado="emitido",
        )

        resp = self.client.get(self._url_detail(ingreso_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "presupuestado")
        self.assertTrue(resp.data.get("is_editable"))
        self.assertTrue(resp.data.get("can_reject"))
        self.assertEqual(resp.data.get("versions")[0].get("estado"), "presupuestado")

    def test_rechazar_emitido_legacy_guarda_fecha_y_comentario(self):
        ingreso_id, quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="pendiente",
            quote_estado="emitido",
        )

        resp = self.client.post(
            self._url_rechazar(ingreso_id),
            {
                "rechazo_comentario": "Cliente pide descuento",
                "presupuesto_rechazado_cobro_neto": "12800",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "rechazado")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT estado, fecha_rechazado, rechazo_comentario FROM quotes WHERE id=%s",
                [quote_id],
            )
            q_estado, q_fecha_rech, q_comentario = cur.fetchone()
            cur.execute(
                """
                SELECT presupuesto_estado, presupuesto_rechazado_cobro_neto, presupuesto_rechazado_quote_id
                FROM ingresos
                WHERE id=%s
                """,
                [ingreso_id],
            )
            ingreso_presu, cobro_neto, linked_quote_id = cur.fetchone()

        self.assertEqual(q_estado, "rechazado")
        self.assertIsNotNone(q_fecha_rech)
        self.assertEqual(q_comentario, "Cliente pide descuento")
        self.assertEqual(ingreso_presu, "rechazado")
        self.assertEqual(Decimal(str(cobro_neto)), Decimal("12800"))
        self.assertEqual(int(linked_quote_id), quote_id)

    def test_aprobar_emitido_legacy_descuenta_stock(self):
        ingreso_id, quote_id, repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="pendiente",
            quote_estado="emitido",
            stock=10,
            qty=2,
        )

        with patch("service.views.quotes_views.render_quote_pdf", return_value=("quote.pdf", b"%PDF-1.4")):
            resp = self.client.post(self._url_aprobar(ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "aprobado")

        with connection.cursor() as cur:
            cur.execute("SELECT estado, presupuesto_estado FROM ingresos WHERE id=%s", [ingreso_id])
            ingreso_estado, ingreso_presu = cur.fetchone()
            cur.execute("SELECT estado, fecha_aprobado FROM quotes WHERE id=%s", [quote_id])
            q_estado, q_fecha_aprob = cur.fetchone()
            cur.execute("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s", [repuesto_id])
            stock = Decimal(str(cur.fetchone()[0]))

        self.assertEqual(ingreso_estado, "reparar")
        self.assertEqual(ingreso_presu, "aprobado")
        self.assertEqual(q_estado, "aprobado")
        self.assertIsNotNone(q_fecha_aprob)
        self.assertEqual(stock, Decimal("8"))

    @patch("service.views.quotes_views.notify_repair_ready_for_remito")
    def test_aprobar_reparado_dispara_aviso_de_remito(self, mock_notify):
        ingreso_id, _quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="reparado",
            presupuesto_estado="presupuestado",
            quote_estado="presupuestado",
        )

        with patch("service.views.quotes_views.render_quote_pdf", return_value=("quote.pdf", b"%PDF-1.4")):
            resp = self.client.post(self._url_aprobar(ingreso_id), {}, format="json")

        self.assertEqual(resp.status_code, 200)
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        self.assertEqual(args[0], ingreso_id)
        self.assertIn("request", kwargs)

    @patch("service.views.quotes_views.notify_repair_ready_for_remito")
    def test_aprobar_presupuesto_ya_aprobado_no_repite_aviso_de_remito(self, mock_notify):
        ingreso_id, _quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="reparado",
            presupuesto_estado="aprobado",
            quote_estado="aprobado",
        )

        resp = self.client.post(self._url_aprobar(ingreso_id), {}, format="json")

        self.assertEqual(resp.status_code, 200)
        mock_notify.assert_not_called()

    def test_rechazar_presupuestado_guarda_fecha_y_comentario(self):
        ingreso_id, quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="presupuestado",
            quote_estado="presupuestado",
        )

        resp = self.client.post(
            self._url_rechazar(ingreso_id),
            {
                "rechazo_comentario": "Cliente pide descuento",
                "presupuesto_rechazado_cobro_neto": "12800",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "rechazado")
        self.assertTrue(resp.data.get("can_create_new_version"))

        with connection.cursor() as cur:
            cur.execute(
                "SELECT estado, fecha_rechazado, rechazo_comentario FROM quotes WHERE id=%s",
                [quote_id],
            )
            q_estado, q_fecha_rech, q_comentario = cur.fetchone()
            cur.execute(
                """
                SELECT presupuesto_estado, presupuesto_rechazado_cobro_neto, presupuesto_rechazado_quote_id
                FROM ingresos
                WHERE id=%s
                """,
                [ingreso_id],
            )
            ingreso_presu, cobro_neto, linked_quote_id = cur.fetchone()

        self.assertEqual(q_estado, "rechazado")
        self.assertIsNotNone(q_fecha_rech)
        self.assertEqual(q_comentario, "Cliente pide descuento")
        self.assertEqual(ingreso_presu, "rechazado")
        self.assertEqual(Decimal(str(cobro_neto)), Decimal("12800"))
        self.assertEqual(int(linked_quote_id), quote_id)

    def test_rechazar_presupuesto_exige_cobro_neto(self):
        ingreso_id, quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="presupuestado",
            quote_estado="presupuestado",
        )

        resp = self.client.post(
            self._url_rechazar(ingreso_id),
            {"rechazo_comentario": "Cliente no autoriza"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("presupuesto_rechazado_cobro_neto", str(resp.data).lower())

        with connection.cursor() as cur:
            cur.execute(
                "SELECT estado, fecha_rechazado, rechazo_comentario FROM quotes WHERE id=%s",
                [quote_id],
            )
            q_estado, q_fecha_rech, q_comentario = cur.fetchone()
            cur.execute(
                """
                SELECT presupuesto_estado, presupuesto_rechazado_cobro_neto, presupuesto_rechazado_quote_id
                FROM ingresos
                WHERE id=%s
                """,
                [ingreso_id],
            )
            ingreso_presu, cobro_neto, linked_quote_id = cur.fetchone()

        self.assertEqual(q_estado, "presupuestado")
        self.assertIsNone(q_fecha_rech)
        self.assertIsNone(q_comentario)
        self.assertEqual(ingreso_presu, "presupuestado")
        self.assertIsNone(cobro_neto)
        self.assertIsNone(linked_quote_id)

    def test_crear_nueva_version_copia_items_y_historica_queda_solo_lectura(self):
        ingreso_id, quote_id, _repuesto_id = self._seed_case(
            ingreso_estado="diagnosticado",
            presupuesto_estado="presupuestado",
            quote_estado="presupuestado",
        )
        reject_resp = self.client.post(
            self._url_rechazar(ingreso_id),
            {
                "rechazo_comentario": "Falta autorización",
                "presupuesto_rechazado_cobro_neto": "12800",
            },
            format="json",
        )
        self.assertEqual(reject_resp.status_code, 200)

        with connection.cursor() as cur:
            cur.execute("SELECT id FROM quote_items WHERE quote_id=%s ORDER BY id LIMIT 1", [quote_id])
            old_item_id = int(cur.fetchone()[0])

        resp = self.client.post(self._url_versiones(ingreso_id), {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("estado"), "pendiente")
        self.assertEqual(int(resp.data.get("version_num") or 0), 2)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, version_num, origen_quote_id, estado, fecha_emitido, fecha_aprobado, fecha_rechazado,
                       rechazo_comentario, pdf_url
                FROM quotes
                WHERE ingreso_id=%s
                ORDER BY version_num
                """,
                [ingreso_id],
            )
            rows = cur.fetchall()
            self.assertEqual(len(rows), 2)
            new_quote = rows[1]
            self.assertEqual(int(new_quote[1]), 2)
            self.assertEqual(int(new_quote[2]), quote_id)
            self.assertEqual(new_quote[3], "pendiente")
            self.assertIsNone(new_quote[4])
            self.assertIsNone(new_quote[5])
            self.assertIsNone(new_quote[6])
            self.assertIsNone(new_quote[7])
            self.assertIsNone(new_quote[8])

            cur.execute("SELECT COUNT(*) FROM quote_items WHERE quote_id=%s", [quote_id])
            old_items = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM quote_items WHERE quote_id=%s", [int(new_quote[0])])
            new_items = int(cur.fetchone()[0])

        self.assertEqual(old_items, new_items)

        detail_resp = self.client.get(self._url_detail(ingreso_id, quote_id=quote_id))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertFalse(detail_resp.data.get("is_current"))
        self.assertFalse(detail_resp.data.get("is_editable"))

        patch_resp = self.client.patch(
            f"/api/quotes/{ingreso_id}/items/{old_item_id}/",
            {"qty": 3},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, 400)

