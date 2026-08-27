from datetime import datetime
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from service.models import User
from service.notification_email_outbox import _attachment_for_delivery


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
@override_settings(
    SECURE_SSL_REDIRECT=False,
    # Backend no-inline: el encolado deja la fila en la cola y no dispara el
    # render del PDF, así se puede inspeccionar el adjunto declarado.
    EMAIL_BACKEND="service.email_backend.EmailBackend",
    EMAIL_DELIVERY_ASYNC=True,
)
class QuoteMgApprovedNotificationTest(TestCase):
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
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    cod_empresa TEXT NULL,
                    razon_social TEXT NOT NULL
                )
                """
            )
            cur.execute("CREATE TABLE IF NOT EXISTS marcas (id BIGSERIAL PRIMARY KEY, nombre TEXT)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS models (
                    id BIGSERIAL PRIMARY KEY,
                    marca_id INTEGER NULL,
                    nombre TEXT,
                    tipo_equipo TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NULL,
                    marca_id INTEGER NULL,
                    model_id INTEGER NULL,
                    numero_serie TEXT NULL,
                    numero_interno TEXT NULL,
                    n_de_control TEXT NULL,
                    mg_estado TEXT NOT NULL DEFAULT 'activo',
                    mg_venta_fecha TIMESTAMPTZ NULL,
                    mg_venta_factura_numero TEXT NULL,
                    mg_venta_remito_numero TEXT NULL,
                    mg_venta_customer_id INTEGER NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NULL,
                    estado TEXT,
                    motivo TEXT NULL,
                    presupuesto_estado TEXT,
                    permite_reparacion BOOLEAN DEFAULT TRUE,
                    asignado_a INTEGER NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id BIGSERIAL PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL,
                    version_num INTEGER NOT NULL DEFAULT 1,
                    origen_quote_id INTEGER NULL,
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
                    fecha_emitido TIMESTAMPTZ NULL,
                    fecha_aprobado TIMESTAMPTZ NULL,
                    fecha_rechazado TIMESTAMPTZ NULL,
                    rechazo_comentario TEXT NULL,
                    pdf_url TEXT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quote_items (
                    id BIGSERIAL PRIMARY KEY,
                    quote_id INTEGER NOT NULL,
                    tipo TEXT NOT NULL,
                    descripcion TEXT NOT NULL,
                    qty NUMERIC(12,2) NOT NULL DEFAULT 0,
                    precio_u NUMERIC(12,2) NOT NULL DEFAULT 0,
                    repuesto_id INTEGER NULL,
                    repuesto_codigo TEXT NULL,
                    costo_u_neto NUMERIC(12,2) NULL
                )
                """
            )
            # A propósito no se crean catalogo_repuestos ni repuestos_movimientos:
            # los presupuestos de este módulo no llevan ítems de repuesto, y
            # crearlas más flacas acá rompería a los módulos que sí las usan.

            # Las tablas base pueden venir de otro módulo de tests: los CREATE de
            # arriba son no-op y las columnas que faltan se agregan acá.
            # Ojo: este módulo aplica el esquema de notificaciones, así que el
            # aviso al técnico deja de cortarse por falta de tablas y sus
            # columnas (`equipo_variante`, `variante`) pasan a ser obligatorias
            # para cualquier test de aprobación que corra después en la misma base.
            for statement in (
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code TEXT NULL",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT NULL",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS n_de_control TEXT NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_estado TEXT NOT NULL DEFAULT 'activo'",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_fecha TIMESTAMPTZ NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_factura_numero TEXT NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_remito_numero TEXT NULL",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_customer_id INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS permite_reparacion BOOLEAN DEFAULT TRUE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
            ):
                cur.execute(statement)

        call_command("apply_notifications_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def _last_id(cls, cur):
        cur.execute("SELECT LASTVAL()")
        return int(cur.fetchone()[0])

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            for table in (
                "notification_email_outbox",
                "notifications",
                "notification_user_preferences",
                "notification_email_addresses",
                "quote_items",
                "quotes",
                "ingresos",
                "devices",
                "models",
                "marcas",
                "customers",
            ):
                cur.execute(f"DELETE FROM {table}")

        User.objects.all().delete()
        self.cobranzas_user = User.objects.create(
            nombre="Cobranzas",
            email="cobranzas-mg@example.com",
            hash_pw="",
            rol="cobranzas",
            activo=True,
        )
        self.jefe_user = User.objects.create(
            nombre="Jefe Taller",
            email="jefe-mg@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe_user)

    # ------------------------------------------------------------------ utils

    def _seed(
        self,
        *,
        razon_social="Cliente Test SRL",
        numero_interno="",
        numero_serie="NS-0001",
        mg_estado="activo",
        mg_venta_factura=None,
    ):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (cod_empresa, razon_social) VALUES (%s,%s)",
                ["CLI", razon_social],
            )
            customer_id = self._last_id(cur)
            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["Philips"])
            marca_id = self._last_id(cur)
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [marca_id, "DreamStation", "CPAP"],
            )
            model_id = self._last_id(cur)
            cur.execute(
                """
                INSERT INTO devices
                  (customer_id, marca_id, model_id, numero_serie, numero_interno,
                   mg_estado, mg_venta_factura_numero)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    customer_id,
                    marca_id,
                    model_id,
                    numero_serie,
                    numero_interno,
                    mg_estado,
                    mg_venta_factura,
                ],
            )
            device_id = self._last_id(cur)
            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, motivo, presupuesto_estado)
                VALUES (%s,'presupuestado','Reparación','presupuestado')
                """,
                [device_id],
            )
            ingreso_id = self._last_id(cur)
            cur.execute(
                """
                INSERT INTO quotes
                  (ingreso_id, version_num, estado, moneda, subtotal, iva_21, total, fecha_emitido)
                VALUES (%s,1,'presupuestado','ARS',%s,%s,%s,%s)
                """,
                [ingreso_id, Decimal("100.00"), Decimal("21.00"), Decimal("121.00"), now],
            )
            quote_id = self._last_id(cur)
            cur.execute(
                """
                INSERT INTO quote_items (quote_id, tipo, descripcion, qty, precio_u)
                VALUES (%s,'mano_obra','Service general',%s,%s)
                """,
                [quote_id, Decimal("2"), Decimal("50.00")],
            )
        return ingreso_id, quote_id

    def _aprobar(self, ingreso_id):
        # El guardado en QUOTES_SAVE_DIR no es parte de lo que se prueba acá.
        with patch(
            "service.views.quotes_views.render_quote_pdf",
            return_value=("quote.pdf", b"%PDF-1.4"),
        ):
            return self.client.post(f"/api/quotes/{ingreso_id}/aprobar/", {}, format="json")

    def _outbox(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT recipient, subject, body, attachment_kind, attachment_reference
                  FROM notification_email_outbox
                 WHERE notification_key='presupuesto_mg_aprobado'
                 ORDER BY id
                """
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------ tests

    def test_mg_propio_encola_el_pdf_para_cobranzas(self):
        ingreso_id, quote_id = self._seed(numero_interno="MG 3877")

        resp = self._aprobar(ingreso_id)
        self.assertEqual(resp.status_code, 200)

        rows = self._outbox()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["recipient"], "cobranzas-mg@example.com")
        self.assertEqual(row["attachment_kind"], "quote_pdf")
        self.assertEqual(row["attachment_reference"], f"{ingreso_id}:{quote_id}")
        self.assertIn("MG 3877", row["subject"])
        self.assertIn(f"OS {str(ingreso_id).zfill(6)}", row["subject"])
        # El total sale recalculado desde los ítems (2 x 50 + IVA), no de quotes.subtotal.
        self.assertIn("$ 100,00", row["body"])
        self.assertIn("$ 121,00", row["body"])

    def test_mg_historico_no_avisa(self):
        ingreso_id, _ = self._seed(numero_interno="MG 3877", mg_estado="inactivo_venta")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        self.assertEqual(self._outbox(), [])

    def test_mg_con_rastro_de_venta_no_avisa(self):
        # mg_estado quedó 'activo' pero hay factura de venta cargada: sigue siendo
        # un MG que salió del patrimonio.
        ingreso_id, _ = self._seed(numero_interno="MG 3877", mg_venta_factura="A 0001-00000123")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        self.assertEqual(self._outbox(), [])

    def test_equipo_de_cliente_no_avisa(self):
        ingreso_id, _ = self._seed(numero_interno="", numero_serie="S12BEA05054")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        self.assertEqual(self._outbox(), [])

    def test_cliente_mg_bio_sin_codigo_interno_avisa(self):
        ingreso_id, _ = self._seed(razon_social="MG BIO S.A.", numero_interno="")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        self.assertEqual(len(self._outbox()), 1)

    def test_cobranzas_con_mail_apagado_no_recibe(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences (user_id, notification_key, email_enabled)
                VALUES (%s,'presupuesto_mg_aprobado',FALSE)
                """,
                [self.cobranzas_user.id],
            )
        ingreso_id, _ = self._seed(numero_interno="MG 3877")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        self.assertEqual(self._outbox(), [])

    def test_jefe_no_recibe_por_defecto(self):
        # El rol está permitido pero apagado por defecto: sólo cobranzas.
        ingreso_id, _ = self._seed(numero_interno="MG 3877")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        recipients = {row["recipient"] for row in self._outbox()}
        self.assertEqual(recipients, {"cobranzas-mg@example.com"})

    def test_reaprobar_no_duplica(self):
        ingreso_id, _ = self._seed(numero_interno="MG 3877")

        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)
        self.assertEqual(self._aprobar(ingreso_id).status_code, 200)

        self.assertEqual(len(self._outbox()), 1)


class QuotePdfAttachmentTest(TestCase):
    """El adjunto quote_pdf se materializa en el worker de la cola."""

    @patch("service.pdf.LOGO_PATH", "")
    @patch("service.pdf._logo_path_for_company", return_value="")
    @patch("service.pdf._get_empresa_facturar", return_value="")
    @patch("service.pdf._q", return_value=[])
    @patch("service.pdf._get_data")
    def test_devuelve_bytes_tipo_y_nombre(
        self,
        get_data_mock,
        _q_mock,
        _empresa_mock,
        _logo_mock,
    ):
        get_data_mock.return_value = (
            {
                "ingreso_id": 29397,
                "quote_version_num": 2,
                "quote_id": 815,
                "cliente": "MG BIO S.A.",
                "cliente_razon_social": "MG BIO S.A.",
                "cliente_alias_interno": "MGBIO",
                "cliente_cuit": "30715975978",
                "cliente_contacto": "",
                "cliente_telefono": "",
                "cliente_telefono_2": "",
                "cliente_email": "",
                "propietario_nombre": "",
                "propietario_doc": "",
                "propietario_contacto": "",
                "fecha_emitido": datetime(2026, 8, 19, 9, 0, 0),
                "marca": "Philips",
                "modelo": "DreamStation",
                "equipo": "CPAP",
                "equipo_variante": "",
                "numero_serie": "NS-0001",
                "numero_interno": "MG 3877",
                "remito_ingreso": "",
                "etiq_ok": True,
                "motivo": "",
                "informe_preliminar": "",
                "accesorios": "",
                "descripcion_problema": "",
                "trabajos_realizados": "",
                "subtotal": Decimal("100.00"),
                "iva_21": Decimal("21.00"),
                "total": Decimal("121.00"),
                "forma_pago": "30 F.F.",
                "plazo_entrega_txt": "< 5 DÍAS HÁBILES",
                "garantia_txt": "90 DÍAS",
                "mant_oferta_txt": "7 DÍAS",
            },
            [],
        )

        result = _attachment_for_delivery(
            {
                "id": 1,
                "attachment_kind": "quote_pdf",
                "attachment_reference": "29397:815",
                "attachment_context": "{}",
            }
        )

        self.assertIsNotNone(result)
        pdf_bytes, content_type, filename = result
        # El orden importa: render_quote_pdf devuelve (filename, pdf).
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(filename, "OS 29397-2 MGBIO.pdf")

    def test_referencia_sin_quote_id_usa_la_version_vigente(self):
        with patch("service.pdf.render_quote_pdf") as render_mock:
            render_mock.return_value = ("OS 000123-1 Cliente.pdf", b"%PDF-1.4")
            result = _attachment_for_delivery(
                {
                    "id": 2,
                    "attachment_kind": "quote_pdf",
                    "attachment_reference": "123",
                    "attachment_context": "{}",
                }
            )

        render_mock.assert_called_once_with(123, quote_id=None)
        self.assertEqual(result[2], "OS 000123-1 Cliente.pdf")
