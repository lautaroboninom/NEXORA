import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from service.auth import issue_token
from service.bejerman_delivery import (
    _current_multi_remito_result,
    generate_bejerman_remito,
    list_rental_available_equipment,
    register_delivery_order_remitos,
    reissue_corrected_bejerman_remito_review,
    verify_bejerman_remito_review,
)
from service.delivery_orders import (
    DeliveryOrderError,
    cancel_order,
    create_delivery_order,
    delivery_order_billing_totals,
    ensure_service_release_order_for_ingreso,
    get_delivery_order,
    list_delivery_orders,
    mark_delivered,
    mark_invoiced,
    mark_not_billable,
    mark_prepared,
    notify_delivery_order_created,
    postpone_delivery_order_billing,
    process_due_delivery_order_billing_postponements,
    reactivate_delivery_order_billing,
    list_remito_history,
    update_delivery_order,
    update_item_partidas,
)
from service.models import User
from service.notifications import (
    emit_notification,
    notify_estado_patrimonial,
    notify_ingreso_asignado,
    notify_ingreso_liberado,
)
from service.remito_pdf_notifications import notify_bejerman_remito_pdf_issued
from service.bejerman_sdk import BejermanSdkResponseError
from service.repair_ready_notifications import notify_repair_ready_for_remito
from service.service_order_billing import (
    list_service_orders_to_bill,
    notify_service_order_ready_to_bill,
    register_service_order_invoice,
)


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class NotificationsAPITest(TestCase):
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
                    razon_social TEXT NOT NULL,
                    telefono TEXT NULL,
                    email TEXT NULL
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
                    marca_id INTEGER NULL REFERENCES marcas(id),
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
                    variante TEXT
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
                    presupuesto_estado TEXT,
                    equipo_variante TEXT,
                    asignado_a INTEGER NULL REFERENCES users(id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_article_mappings (
                    id BIGSERIAL PRIMARY KEY,
                    model_id INTEGER NULL,
                    variante_norm TEXT NOT NULL DEFAULT '',
                    article_code TEXT NULL,
                    article_description TEXT NULL,
                    match_source TEXT NOT NULL DEFAULT 'auto',
                    confirmed_at TIMESTAMPTZ NULL,
                    updated_at TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER,
                    a_estado TEXT,
                    usuario_id INTEGER NULL,
                    comentario TEXT NULL,
                    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for statement in (
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS telefono TEXT NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT NULL",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS marca_id INTEGER NULL",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_servicio TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_salida TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS factura_numero TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquilado BOOLEAN DEFAULT FALSE",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_a TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_remito TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_fecha TIMESTAMPTZ NULL",
            ):
                cur.execute(statement)

        call_command("apply_notifications_schema", verbosity=0)
        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_bejerman_pdf_requests_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM notification_email_addresses")
            cur.execute("DELETE FROM notification_push_subscriptions")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.filter(email__endswith="@notif.test").delete()

        cls.admin = User.objects.create(
            nombre="Admin Notificaciones",
            email="admin@notif.test",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.ventas = User.objects.create(
            nombre="Ventas Notificaciones",
            email="ventas@notif.test",
            hash_pw="",
            rol="ventas",
            activo=True,
            bejerman_seller_code="EZE",
            bejerman_seller_code_confirmed_at=timezone.now(),
        )
        cls.supervisor = User.objects.create(
            nombre="Supervisor Notificaciones",
            email="supervisor@notif.test",
            hash_pw="",
            rol="supervisor",
            activo=True,
        )
        cls.jefe = User.objects.create(
            nombre="Jefe Notificaciones",
            email="jefe@notif.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Técnico Notificaciones",
            email="tecnico@notif.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.otro = User.objects.create(
            nombre="Técnico Sin Liberado",
            email="otro@notif.test",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.recepcion = User.objects.create(
            nombre="Recepcion Notificaciones",
            email="recepcion@notif.test",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        cls.cobranzas = User.objects.create(
            nombre="Cobranzas Notificaciones",
            email="cobranzas@notif.test",
            hash_pw="",
            rol="cobranzas",
            activo=True,
        )
        cls.jefe_token = issue_token(cls.jefe)
        cls.tecnico_token = issue_token(cls.tecnico)
        cls.otro_token = issue_token(cls.otro)

        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono, email) VALUES (%s,%s,%s,%s) RETURNING id",
                ["CLI-NOTIF", "Clínica Notificaciones", "123", "ops@notif.test"],
            )
            customer_id = int(cur.fetchone()[0])
            cls.customer_id = customer_id
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["ResMed"])
            marca_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id",
                [marca_id, "AirSense 10", "CPAP", ""],
            )
            model_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                [customer_id, marca_id, model_id, "NS-NOTIF-001", "MG 9001", ""],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    presupuesto_estado, equipo_variante, asignado_a
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    device_id,
                    "liberado",
                    "reparación",
                    timezone.now(),
                    timezone.now(),
                    "no_aplica",
                    "",
                    cls.tecnico.id,
                ],
            )
            cls.ingreso_id = int(cur.fetchone()[0])

    def setUp(self):
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM delivery_order_events")
            cur.execute("DELETE FROM delivery_order_item_partidas")
            cur.execute("DELETE FROM delivery_order_items")
            cur.execute("DELETE FROM delivery_orders")
            cur.execute("DELETE FROM bejerman_integration_attempts")
            cur.execute("DELETE FROM bejerman_remito_groups")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM notification_email_addresses")
            cur.execute("DELETE FROM notification_push_subscriptions")

    def _auth_as_jefe(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jefe_token}")

    def _auth_as_tecnico(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tecnico_token}")

    def _enable_notification(self, user, notification_key, enabled=True):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences(user_id, notification_key, enabled)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, notification_key) DO UPDATE SET enabled=EXCLUDED.enabled
                """,
                [user.id, notification_key, enabled],
            )

    def _enable_liberado_for_tecnico(self):
        self._enable_notification(self.tecnico, "ingreso_liberado", True)

    def _create_commercial_correction_order(
        self,
        order_id: str = "do-commercial-correction",
        *,
        status: str = "entregado_pendiente_facturacion",
        remito_number: str = "RT R 00004-00012345",
        invoice_number: str | None = None,
        attach_bejerman_group: bool = True,
        billing_required: bool = True,
    ) -> dict:
        order = create_delivery_order(
            {
                "id": order_id,
                "orderNumber": f"OE-{order_id}",
                "customerId": self.customer_id,
                "bejermanCustomerCode": "CLI-NOTIF",
                "customerName": "Clínica Notificaciones",
                "deliveryType": "sale",
                "companyKey": "SEPID",
                "sellerName": "Ventas",
                "commercialDeadline": "15 días",
                "status": status,
                "remitoNumber": remito_number,
                "invoiceNumber": invoice_number or "",
                "items": [
                    {
                        "articleCode": "ART-001",
                        "description": "Equipo de prueba",
                        "quantity": 2,
                        "unitPrice": "1000.00",
                        "priceCurrency": "ARS",
                        "discountPercent": "0",
                    }
                ],
            },
            self.jefe.id,
        )
        if remito_number and attach_bejerman_group:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bejerman_remito_groups(
                        id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                        comprobante_numero, remito_number, customer_code, customer_name, seller_code,
                        payment_term_code, operation_code, deposit_code, status, order_ids, response_summary,
                        generated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, CURRENT_TIMESTAMP)
                    """,
                    [
                        f"brg-{order_id}",
                        "SEPID",
                        "RT",
                        "R",
                        "00004",
                        "00012345",
                        remito_number,
                        "CLI-NOTIF",
                        "Clínica Notificaciones",
                        "ADM",
                        "30",
                        "MC",
                        "VAL",
                        "generated",
                        json.dumps([order_id]),
                        json.dumps({"billingRequired": billing_required}),
                    ],
                )
                cur.execute(
                    "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = %s",
                    [f"brg-{order_id}", order_id],
                )
        if invoice_number:
            with connection.cursor() as cur:
                cur.execute("UPDATE delivery_orders SET invoice_number = %s WHERE id = %s", [invoice_number, order_id])
        return get_delivery_order(order_id)

    def _insert_push_subscription(self, user, endpoint="https://push.notif.test/sub-1"):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_push_subscriptions(user_id, endpoint, p256dh, auth)
                VALUES (%s, %s, %s, %s)
                """,
                [user.id, endpoint, "p256dh-test", "auth-test"],
            )

    def test_endpoint_factura_pdf_de_orden_devuelve_pdf_bejerman(self):
        self.client.force_authenticate(user=self.recepcion)

        with patch(
            "service.views.delivery_orders_views.get_delivery_order_invoice_pdf",
            return_value=(b"%PDF-1.4\nfactura", "application/pdf", "factura-FC-A-0001-00000012.pdf"),
        ) as pdf_mock:
            resp = self.client.get("/api/ordenes-entrega/do-invoice/factura/pdf/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn('filename="factura-FC-A-0001-00000012.pdf"', resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"%PDF-"))
        pdf_mock.assert_called_once_with("do-invoice", actor_user_id=self.recepcion.id)

    @patch("service.views.ingresos_views.require_roles", return_value=None)
    @patch("service.views.ingresos_views.ingreso_is_internal_equipment", return_value=False)
    @patch("service.views.ingresos_views.ingreso_is_demo_return", return_value=False)
    @patch("service.views.ingresos_views.notify_repair_ready_for_remito")
    def test_marcar_reparado_con_presupuesto_aprobado_dispara_aviso_de_remito(
        self,
        mock_notify,
        _mock_demo_return,
        _mock_internal_equipment,
        _mock_roles,
    ):
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado='reparar',
                       presupuesto_estado='aprobado',
                       asignado_a=%s
                 WHERE id=%s
                """,
                [self.tecnico.id, self.ingreso_id],
            )

        self.client.force_authenticate(user=self.tecnico)
        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/reparado/", {}, format="json")

        self.assertEqual(resp.status_code, 200)
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        self.assertEqual(args[0], self.ingreso_id)
        self.assertIn("request", kwargs)

    @override_settings(
        REPAIR_READY_RECIPIENTS=[],
        ASSIGNMENT_REQUEST_RECIPIENTS=[],
        COMPANY_FOOTER_EMAIL=None,
        COMPANY_FOOTER_EMAIL_2=None,
    )
    def test_aviso_reparacion_lista_notifica_recepcion_por_defecto(self):
        result = notify_repair_ready_for_remito(self.ingreso_id, actor_name="Tecnico")

        self.assertEqual(result["notifications"], 1)
        self.assertEqual(result["recipients"], [])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, title, href
                  FROM notifications
                 WHERE notification_key = 'reparacion_lista_remito'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [
                (
                    self.recepcion.id,
                    f"Reparación lista para remito - OS {str(self.ingreso_id).zfill(5)}",
                    f"/ingresos/{self.ingreso_id}?tab=principal",
                )
            ],
        )

    @override_settings(
        REPAIR_READY_RECIPIENTS=[],
        ASSIGNMENT_REQUEST_RECIPIENTS=[],
        COMPANY_FOOTER_EMAIL=None,
        COMPANY_FOOTER_EMAIL_2=None,
    )
    def test_aviso_reparacion_lista_respeta_baja_de_recepcion(self):
        self._enable_notification(self.recepcion, "reparacion_lista_remito", False)

        result = notify_repair_ready_for_remito(self.ingreso_id, actor_name="Tecnico")

        self.assertEqual(result["notifications"], 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'reparacion_lista_remito'
                """
            )
            total = int(cur.fetchone()[0])
        self.assertEqual(total, 0)

    def _create_service_release_order(self, order_id: str) -> dict:
        return create_delivery_order(
            {
                "id": order_id,
                "orderNumber": f"OES-{order_id}",
                "customerId": self.customer_id,
                "deliveryType": "service_release",
                "sourceSystem": "nexora",
                "sourceExternalId": str(self.ingreso_id),
                "sourceReference": f"OS {self.ingreso_id}",
                "ingresoId": self.ingreso_id,
                "equipmentModel": "CPAP | ResMed | AirSense 10",
                "equipmentSerial": "NS-NOTIF-001",
                "equipmentInternalNumber": "MG 9001",
                "sellerName": "Servicio técnico",
                "status": "armado_pendiente_entrega",
                "items": [
                    {
                        "articleCode": "1115003",
                        "articleName": "CPAP | ResMed | AirSense 10 - N° serie NS-NOTIF-001 - N° interno (MG) MG 9001",
                        "description": "CPAP | ResMed | AirSense 10 - N° serie NS-NOTIF-001 - N° interno (MG) MG 9001",
                        "quantity": 1,
                        "partida": "NS-NOTIF-001",
                    }
                ],
            },
            self.jefe.id,
        )

    def _create_service_release_ingreso(
        self,
        serial: str,
        internal: str,
        *,
        customer_id: int | None = None,
        resolucion: str = "reparado",
    ) -> tuple[int, int]:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.marca_id, d.model_id
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE t.id = %s
                """,
                [self.ingreso_id],
            )
            marca_id, model_id = cur.fetchone()
            cur.execute(
                "INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                [customer_id or self.customer_id, marca_id, model_id, serial, internal, ""],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    presupuesto_estado, equipo_variante, asignado_a, resolucion,
                    remito_salida, factura_numero, fecha_entrega
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL)
                RETURNING id
                """,
                [
                    device_id,
                    "liberado",
                    "reparación",
                    timezone.now(),
                    timezone.now(),
                    "no_aplica",
                    "",
                    self.tecnico.id,
                    resolucion,
                ],
            )
            return int(cur.fetchone()[0]), device_id

    def test_liberaciones_rss_compatibles_se_agrupan_en_una_orden(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       factura_numero = NULL,
                       fecha_entrega = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        second_ingreso_id, _second_device_id = self._create_service_release_ingreso("NS-RSS-GRP-002", "MG 9002")

        first_order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
        grouped_order = ensure_service_release_order_for_ingreso(second_ingreso_id, self.jefe.id)

        self.assertIsNotNone(first_order)
        self.assertEqual(grouped_order["id"], first_order["id"])
        self.assertEqual(grouped_order["serviceReleaseCount"], 2)
        self.assertEqual(grouped_order["orderNumber"], f"OS-{self.ingreso_id:05d} + 1 OS")
        self.assertEqual(
            {item["ingresoId"] for item in grouped_order["items"]},
            {self.ingreso_id, second_ingreso_id},
        )
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM delivery_orders WHERE delivery_type = 'service_release'")
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute(
                "SELECT ingreso_id FROM delivery_order_items WHERE order_id = %s ORDER BY sort_order",
                [grouped_order["id"]],
            )
            self.assertEqual({row[0] for row in cur.fetchall()}, {self.ingreso_id, second_ingreso_id})

    def test_liberacion_rss_incompatible_por_motivo_o_cliente_abre_otra_orden(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET estado = 'liberado', resolucion = 'reparado' WHERE id = %s", [self.ingreso_id])
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono, email) VALUES (%s,%s,%s,%s) RETURNING id",
                ["CLI-OTRO", "Cliente Otro", "", ""],
            )
            other_customer_id = int(cur.fetchone()[0])
        different_reason_id, _ = self._create_service_release_ingreso("NS-RSS-MOT-003", "MG 9003", resolucion="sin_reparacion")
        different_customer_id, _ = self._create_service_release_ingreso(
            "NS-RSS-CLI-004",
            "MG 9004",
            customer_id=other_customer_id,
        )

        first_order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
        reason_order = ensure_service_release_order_for_ingreso(different_reason_id, self.jefe.id)
        customer_order = ensure_service_release_order_for_ingreso(different_customer_id, self.jefe.id)

        self.assertNotEqual(reason_order["id"], first_order["id"])
        self.assertNotEqual(customer_order["id"], first_order["id"])
        self.assertNotEqual(customer_order["id"], reason_order["id"])
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM delivery_orders WHERE delivery_type = 'service_release'")
            self.assertEqual(int(cur.fetchone()[0]), 3)

    def test_rss_emitido_completa_entrega_de_todas_las_os_del_grupo(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        second_ingreso_id, _ = self._create_service_release_ingreso("NS-RSS-EMI-002", "MG 9012")
        order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
        ensure_service_release_order_for_ingreso(second_ingreso_id, self.jefe.id)

        with patch("service.bejerman_delivery.BejermanSDKClient", self._fake_bejerman_remito_client()):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["remitoNumber"], "RSS R 00004-00001234")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, estado, remito_salida, fecha_entrega
                  FROM ingresos
                 WHERE id = ANY(%s)
                 ORDER BY id
                """,
                [[self.ingreso_id, second_ingreso_id]],
            )
            rows = cur.fetchall()
        self.assertEqual({row[0] for row in rows}, {self.ingreso_id, second_ingreso_id})
        self.assertEqual({row[1] for row in rows}, {"entregado"})
        self.assertEqual({row[2] for row in rows}, {"RSS R 00004-00001234"})
        self.assertTrue(all(row[3] is not None for row in rows))

    def test_rss_partida_rechazada_cae_a_ent_mas_documento_n(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)

        sent_comprobantes = []
        stock_comprobantes = []
        present_partidas = set()

        class PartidaRejectedRssClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def obtener_stock_partida(self, requested_partida):
                if requested_partida not in present_partidas:
                    return {"DatosJSON": []}
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1111003",
                            "Art_CodDeposito": "STC",
                            "Art_Partida": requested_partida,
                            "Art_RealUM1": 1,
                            "Art_DispUM1": 1,
                        }
                    ]
                }

            def ingresar_lista_comprobantes_stock_json(self, comprobantes, *, numera_flex="N"):
                stock_comprobantes.append(list(comprobantes or []))
                for item in comprobantes or []:
                    present_partidas.add(item.get("Comprobante_ArtPartida"))
                return {"Resultado": "OK", "DatosJSON": json.dumps(comprobantes or [])}

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                if comprobante.get("Comprobante_ActualizaStock") == "S":
                    raise BejermanSdkResponseError("El comprobante no se importó. Partida inexistente.")
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RSS",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00001234",
                        }
                    ),
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", PartidaRejectedRssClient):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["remitoNumber"], "RSS R 00004-00001234")
        self.assertEqual(len(sent_comprobantes), 2)
        self.assertEqual(sent_comprobantes[0]["Comprobante_ActualizaStock"], "S")
        self.assertEqual(sent_comprobantes[1]["Comprobante_ActualizaStock"], "N")
        self.assertEqual(len(stock_comprobantes), 1)
        rejected_article_lines = [item for item in sent_comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        recovered_article_lines = [item for item in sent_comprobantes[1]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        partida = rejected_article_lines[0]["Item_Partida"]
        self.assertTrue(partida)
        self.assertEqual(stock_comprobantes[0][0]["Comprobante_ArtPartida"], partida)
        self.assertEqual(stock_comprobantes[0][0]["Comprobante_ArtDeposito"], "STC")
        self.assertEqual(recovered_article_lines[0]["Item_Partida"], partida)
        self.assertGreater(recovered_article_lines[0]["Item_CantidadUM1"], 0)
        with connection.cursor() as cur:
            cur.execute("SELECT status FROM bejerman_remito_groups WHERE id = %s", [result["groupId"]])
            self.assertEqual(cur.fetchone()[0], "generated")

    def test_rss_corrige_articulo_por_stock_de_partida_antes_de_emitir(self):
        self._ensure_rental_article_mapping("1103004")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, partida, article_code
                  FROM delivery_order_items
                 WHERE order_id = %s
                 ORDER BY sort_order, id
                 LIMIT 1
                """,
                [order["id"]],
            )
            item_id, partida, original_article_code = cur.fetchone()
        self.assertEqual(original_article_code, "1103004")

        sent_comprobantes = []

        class StockAlignedRssClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def obtener_stock_partida(self, requested_partida):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1111003",
                            "Articulo_Descripcion": "Articulo correcto por stock",
                            "Art_CodDeposito": "STC",
                            "Art_Partida": requested_partida,
                            "Art_RealUM1": 1,
                            "Art_DispUM1": 1,
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RSS",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00001234",
                        }
                    ),
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", StockAlignedRssClient):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["remitoNumber"], "RSS R 00004-00001234")
        article_lines = [item for item in sent_comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CodigoArticulo"], "1111003")
        self.assertEqual(article_lines[0]["Item_Deposito"], "STC")
        self.assertEqual(article_lines[0]["Item_Partida"], partida)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_code, stock_deposit_code, stock_available_quantity
                  FROM delivery_order_items
                 WHERE id = %s
                """,
                [item_id],
            )
            updated = cur.fetchone()
        self.assertEqual(updated[0], "1111003")
        self.assertEqual(updated[1], "STC")
        self.assertEqual(float(updated[2]), 1.0)

    def test_rss_grande_se_divide_por_limite_de_lineas_impresas(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )

        ingreso_ids = [self.ingreso_id]
        for index in range(12):
            ingreso_id, _ = self._create_service_release_ingreso(
                f"NS-RSS-SPLIT-{index + 2:03d}",
                f"MG-SPLIT-{index + 2:03d}",
            )
            ingreso_ids.append(ingreso_id)

        with override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=99, BEJERMAN_RSS_PRINT_LINES_PER_ITEM=1):
            order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
            for ingreso_id in ingreso_ids[1:]:
                order = ensure_service_release_order_for_ingreso(ingreso_id, self.jefe.id)

        self.assertEqual(order["serviceReleaseCount"], 13)
        sent_comprobantes = []

        class SplitRssClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                number = 1234 + len(sent_comprobantes) - 1
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RSS",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": f"{number:08d}",
                        }
                    ),
                }

        with override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=8):
            with patch("service.bejerman_delivery.BejermanSDKClient", SplitRssClient):
                result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertTrue(result["multiple"])
        self.assertEqual(result["remitoNumbers"], [
            "RSS R 00004-00001234",
            "RSS R 00004-00001235",
            "RSS R 00004-00001236",
            "RSS R 00004-00001237",
            "RSS R 00004-00001238",
            "RSS R 00004-00001239",
            "RSS R 00004-00001240",
        ])
        self.assertEqual(len(sent_comprobantes), 7)
        article_counts = [
            len([item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"])
            for comprobante in sent_comprobantes
        ]
        line_counts = [len(comprobante["Comprobante_Items"]) for comprobante in sent_comprobantes]
        self.assertEqual(article_counts, [2, 2, 2, 2, 2, 2, 1])
        self.assertTrue(all(count <= 8 for count in line_counts))
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT remito_salida, COUNT(*)
                  FROM ingresos
                 WHERE id = ANY(%s)
                 GROUP BY remito_salida
                 ORDER BY remito_salida
                """,
                [ingreso_ids],
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [
            ("RSS R 00004-00001234", 2),
            ("RSS R 00004-00001235", 2),
            ("RSS R 00004-00001236", 2),
            ("RSS R 00004-00001237", 2),
            ("RSS R 00004-00001238", 2),
            ("RSS R 00004-00001239", 2),
            ("RSS R 00004-00001240", 1),
        ])

    def test_rss_cuarenta_y_ocho_equipos_genera_siete_remitos(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )

        ingreso_ids = [self.ingreso_id]
        for index in range(47):
            ingreso_id, _ = self._create_service_release_ingreso(
                f"NS-RSS-48-{index + 2:03d}",
                f"MG-RSS-48-{index + 2:03d}",
            )
            ingreso_ids.append(ingreso_id)

        with override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=22, BEJERMAN_RSS_PRINT_LINES_PER_ITEM=3):
            order_ids = []
            for ingreso_id in ingreso_ids:
                order = ensure_service_release_order_for_ingreso(ingreso_id, self.jefe.id)
                if order["id"] not in order_ids:
                    order_ids.append(order["id"])

        self.assertEqual(len(order_ids), 7)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, COUNT(doi.id)
                  FROM delivery_orders o
                  JOIN delivery_order_items doi ON doi.order_id = o.id
                 WHERE o.id = ANY(%s)
                 GROUP BY o.id
                 ORDER BY array_position(%s::text[], o.id)
                """,
                [order_ids, order_ids],
            )
            order_counts = [int(row[1]) for row in cur.fetchall()]
        self.assertEqual(order_counts, [7, 7, 7, 7, 7, 7, 6])

        sent_comprobantes = []

        class Split48RssClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                number = 2234 + len(sent_comprobantes) - 1
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RSS",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": f"{number:08d}",
                        }
                    ),
                }

        with override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=22, BEJERMAN_RSS_PRINT_LINES_PER_ITEM=3):
            with patch("service.bejerman_delivery.BejermanSDKClient", Split48RssClient):
                result = generate_bejerman_remito(order_ids, self.jefe.id, {"issueDate": "2026-07-02"})

        self.assertTrue(result["multiple"])
        self.assertEqual(len(result["remitoNumbers"]), 7)
        self.assertEqual(len(sent_comprobantes), 7)
        article_counts = [
            len([item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"])
            for comprobante in sent_comprobantes
        ]
        line_counts = [len(comprobante["Comprobante_Items"]) for comprobante in sent_comprobantes]
        self.assertEqual(article_counts, [7, 7, 7, 7, 7, 7, 6])
        self.assertTrue(all(count <= 22 for count in line_counts))
        self.assertEqual(line_counts, [21, 21, 21, 21, 21, 21, 18])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT remito_salida, COUNT(*)
                  FROM ingresos
                 WHERE id = ANY(%s)
                 GROUP BY remito_salida
                 ORDER BY remito_salida
                """,
                [ingreso_ids],
            )
            rows = cur.fetchall()
        self.assertEqual([int(row[1]) for row in rows], [7, 7, 7, 7, 7, 7, 6])

    def test_rss_grande_en_revision_se_reinicia_dividido_sin_reemitir_a_ciegas(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )

        ingreso_ids = [self.ingreso_id]
        for index in range(12):
            ingreso_id, _ = self._create_service_release_ingreso(
                f"NS-RSS-REVIEW-{index + 2:03d}",
                f"MG-REVIEW-{index + 2:03d}",
            )
            ingreso_ids.append(ingreso_id)

        with override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=99, BEJERMAN_RSS_PRINT_LINES_PER_ITEM=1):
            order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
            for ingreso_id in ingreso_ids[1:]:
                order = ensure_service_release_order_for_ingreso(ingreso_id, self.jefe.id)

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status, order_ids, response_summary, last_error
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                [
                    "brg-rss-review-split",
                    "SEPID",
                    "RSS",
                    "R",
                    "00004",
                    "CLI-NOTIF",
                    "Clinica Notificaciones",
                    "ADM",
                    "30",
                    "REP",
                    "STC",
                    "failed_review",
                    json.dumps([order["id"]]),
                    json.dumps({"reviewRequired": True}),
                    "Bejerman devolvió un error después del envío.",
                ],
            )
            cur.execute(
                "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = %s",
                ["brg-rss-review-split", order["id"]],
            )

        sent_comprobantes = []
        lookup_calls = []

        class ReviewSplitRssClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_comprobantes_ventas(self, filters):
                lookup_calls.append(filters)
                return {"DatosJSON": []}

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                number = 2234 + len(sent_comprobantes) - 1
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RSS",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": f"{number:08d}",
                        }
                    ),
                }

        with override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=8):
            with patch("service.bejerman_delivery.BejermanSDKClient", ReviewSplitRssClient):
                result = generate_bejerman_remito(
                    [order["id"]],
                    self.jefe.id,
                    {"forcePrintSplitReissue": True},
                )

        self.assertTrue(lookup_calls)
        self.assertTrue(result["multiple"])
        self.assertEqual(len(sent_comprobantes), 7)
        self.assertTrue(all(len(comprobante["Comprobante_Items"]) <= 8 for comprobante in sent_comprobantes))
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT response_summary->>'supersededForPrintSplit'
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                ["brg-rss-review-split"],
            )
            superseded = cur.fetchone()[0]
            cur.execute(
                """
                SELECT remito_salida, COUNT(*)
                  FROM ingresos
                 WHERE id = ANY(%s)
                 GROUP BY remito_salida
                 ORDER BY remito_salida
                """,
                [ingreso_ids],
            )
            rows = cur.fetchall()
        self.assertEqual(superseded, "true")
        self.assertEqual(rows, [
            ("RSS R 00004-00002234", 2),
            ("RSS R 00004-00002235", 2),
            ("RSS R 00004-00002236", 2),
            ("RSS R 00004-00002237", 2),
            ("RSS R 00004-00002238", 2),
            ("RSS R 00004-00002239", 2),
            ("RSS R 00004-00002240", 1),
        ])

    def test_cobranzas_factura_sincroniza_todas_las_os_del_grupo(self):
        self._ensure_rental_article_mapping("1111003")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = NULL,
                       factura_numero = NULL,
                       fecha_entrega = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        second_ingreso_id, _ = self._create_service_release_ingreso("NS-RSS-FAC-002", "MG 9022")
        order = ensure_service_release_order_for_ingreso(self.ingreso_id, self.jefe.id)
        ensure_service_release_order_for_ingreso(second_ingreso_id, self.jefe.id)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'entregado',
                       remito_salida = 'RSS R 00004-00001234',
                       fecha_entrega = CURRENT_TIMESTAMP,
                       factura_numero = NULL
                 WHERE id = ANY(%s)
                """,
                [[self.ingreso_id, second_ingreso_id]],
            )
            cur.execute(
                """
                UPDATE delivery_orders
                   SET remito_number = 'RSS R 00004-00001234',
                       status = 'entregado_pendiente_facturacion',
                       remito_location = 'recepcion'
                 WHERE id = %s
                """,
                [order["id"]],
            )

        billed = register_service_order_invoice(second_ingreso_id, "FC A 0001-00016666", self.cobranzas.id)

        self.assertEqual(billed["facturaNumero"], "FC A 0001-00016666")
        self.assertEqual(billed["serviceOrderId"], order["id"])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT factura_numero FROM ingresos WHERE id = ANY(%s) ORDER BY id",
                [[self.ingreso_id, second_ingreso_id]],
            )
            self.assertEqual([row[0] for row in cur.fetchall()], ["FC A 0001-00016666", "FC A 0001-00016666"])
            cur.execute("SELECT invoice_number FROM delivery_orders WHERE id = %s", [order["id"]])
            self.assertEqual(cur.fetchone()[0], "FC A 0001-00016666")

    def _fake_bejerman_remito_client(self):
        class FakeBejermanRemitoClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RSS",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00001234",
                        }
                    ),
                }

        return FakeBejermanRemitoClient

    def _ensure_rental_location_ids(self):
        with connection.cursor() as cur:
            def ensure_location_id(nombre):
                cur.execute("SELECT id FROM locations WHERE nombre = %s ORDER BY id ASC LIMIT 1", [nombre])
                row = cur.fetchone()
                if row:
                    return int(row[0])
                cur.execute("INSERT INTO locations(nombre) VALUES (%s) RETURNING id", [nombre])
                return int(cur.fetchone()[0])

            rental_id = ensure_location_id("Estantería de Alquiler")
            dash_id = ensure_location_id("-")
        return rental_id, dash_id

    def _ensure_rental_article_mapping(self, article_code="ART-ALQ"):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.model_id
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE t.id = %s
                """,
                [self.ingreso_id],
            )
            model_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO bejerman_article_mappings(model_id, variante_norm, article_code, article_description, match_source, confirmed_at, updated_at)
                VALUES (%s, '', %s, %s, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                [model_id, article_code, "Equipo alquiler"],
            )
        return model_id

    def _create_rental_ingreso(self, serial: str, internal: str, *, existing: bool = False) -> tuple[int, int]:
        rental_id, _dash_id = self._ensure_rental_location_ids()
        if existing:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingresos
                       SET estado = 'disponible',
                           ubicacion_id = %s,
                           alquilado = FALSE,
                           alquiler_a = NULL,
                           alquiler_remito = NULL,
                           alquiler_fecha = NULL
                     WHERE id = %s
                    """,
                    [rental_id, self.ingreso_id],
                )
                cur.execute("SELECT device_id FROM ingresos WHERE id = %s", [self.ingreso_id])
                return self.ingreso_id, int(cur.fetchone()[0])

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.marca_id, d.model_id
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE t.id = %s
                """,
                [self.ingreso_id],
            )
            marca_id, model_id = cur.fetchone()
            cur.execute(
                "INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                [self.customer_id, marca_id, model_id, serial, internal, ""],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, fecha_creacion,
                    presupuesto_estado, equipo_variante, asignado_a, ubicacion_id, alquilado
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
                RETURNING id
                """,
                [
                    device_id,
                    "disponible",
                    "alquiler",
                    timezone.now(),
                    timezone.now(),
                    "no_aplica",
                    "",
                    self.tecnico.id,
                    rental_id,
                ],
            )
            return int(cur.fetchone()[0]), device_id

    def _rental_item(self, ingreso_id: int, device_id: int, serial: str, article_code="ART-ALQ"):
        return {
            "ingresoId": ingreso_id,
            "deviceId": device_id,
            "articleCode": article_code,
            "articleName": "Equipo alquiler",
            "description": f"Equipo alquiler - N° serie {serial}",
            "quantity": 1,
            "partida": serial,
            "stockDepositCode": "STL",
            "stockAvailableQuantity": 1,
            "stockCheckedAt": timezone.now().isoformat(),
        }

    def _fake_rta_client(self, sent_comprobantes, stock_rows):
        class FakeRtaClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def obtener_stock_deposito(self, deposit_code):
                return {"DatosJSON": stock_rows}

            def obtener_partidas(self):
                return {
                    "DatosJSON": [
                        {
                            "Partida_Codigo": row["Art_Partida"],
                            "Deposito_Codigo": row["Art_CodDeposito"],
                            "Partida_FechaVtoIngreso": "2030-12-31",
                        }
                        for row in stock_rows
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RTA",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00004567",
                        }
                    ),
                }

        return FakeRtaClient

    def _delivery_partida_stock_patch(self, lots):
        def fake_lots(article_code, deposit_code, company_key, actor_user_id, cache):
            return [
                lot
                for lot in lots
                if lot.get("articleCode") == article_code and lot.get("depositCode", deposit_code) == deposit_code
            ]

        return patch("service.delivery_orders._delivery_partida_stock_lots", side_effect=fake_lots)

    def _create_rt_split_order(
        self,
        order_id: str,
        item_count: int,
        *,
        partida_prefix: str,
        article_code: str = "ART-SPLIT",
        discount_percent: str = "0",
    ) -> dict:
        lots = [
            {
                "articleCode": article_code,
                "depositCode": "VAL",
                "partida": f"{partida_prefix}-{index:03d}",
                "availableQuantity": 1,
            }
            for index in range(item_count)
        ]
        with self._delivery_partida_stock_patch(lots):
            return create_delivery_order(
                {
                    "id": order_id,
                    "orderNumber": f"OE-{order_id.upper()}",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "MAX",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": article_code,
                            "articleName": "Artículo divisible",
                            "articleRequiresPartida": True,
                            "description": "Artículo divisible",
                            "quantity": 1,
                            "unitPrice": "10",
                            "discountPercent": discount_percent,
                            "partida": f"{partida_prefix}-{index:03d}",
                        }
                        for index in range(item_count)
                    ],
                },
                self.jefe.id,
            )

    def _create_rt_multipartida_split_order(
        self,
        order_id: str,
        partida_count: int,
        *,
        partida_prefix: str,
        article_code: str = "ART-MULTIPARTIDA",
    ) -> dict:
        lots = [
            {
                "articleCode": article_code,
                "depositCode": "VAL",
                "partida": f"{partida_prefix}-{index:03d}",
                "availableQuantity": 1,
            }
            for index in range(partida_count)
        ]
        with self._delivery_partida_stock_patch(lots):
            return create_delivery_order(
                {
                    "id": order_id,
                    "orderNumber": f"OE-{order_id.upper()}",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "MAX",
                    "status": "armado_pendiente_entrega",
                    "priceCurrency": "ARS",
                    "items": [
                        {
                            "articleCode": article_code,
                            "articleName": "Artículo multipartida",
                            "articleRequiresPartida": True,
                            "description": "Artículo multipartida",
                            "quantity": partida_count,
                            "unitPrice": "10",
                            "priceCurrency": "ARS",
                            "discountPercent": "0",
                            "partidas": [
                                {
                                    "partida": f"{partida_prefix}-{index:03d}",
                                    "assignedQuantity": 1,
                                    "stockDepositCode": "VAL",
                                }
                                for index in range(partida_count)
                            ],
                        }
                    ],
                },
                self.jefe.id,
            )

    def _fake_rt_split_client(self, sent_comprobantes, article_codes=None, first_number=3234):
        known_article_codes = list(article_codes or ["ART-SPLIT"])

        class SplitSaleClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": article_code,
                            "Articulo_Descripcion": "Artículo divisible",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                        for article_code in known_article_codes
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                number = first_number + len(sent_comprobantes) - 1
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": f"{number:08d}",
                        }
                    ),
                }

        return SplitSaleClient

    @staticmethod
    def _rt_article_counts(sent_comprobantes):
        return [
            len([item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"])
            for comprobante in sent_comprobantes
        ]

    def _attach_failed_rt_review_group(self, order_id: str, group_id: str, *, issue_date: str) -> None:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status, order_ids, response_summary, last_error
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                [
                    group_id,
                    "SEPID",
                    "RT",
                    "R",
                    "00004",
                    "CLI-NOTIF",
                    "Clinica Notificaciones",
                    "MAX",
                    "30",
                    "MC",
                    "VAL",
                    "failed_review",
                    json.dumps([order_id]),
                    json.dumps(
                        {
                            "reviewRequired": True,
                            "reconciliationRequired": True,
                            "reviewState": "requires_verification",
                            "failureCertainty": "confirmed_not_imported",
                            "bridgeRequest": {"issueDate": issue_date},
                        }
                    ),
                    "El comprobante no se importó en Bejerman.",
                ],
            )
            cur.execute(
                "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = %s",
                [group_id, order_id],
            )

    def _create_sale_order_ready_for_manual_remito(self, order_id: str, company_key: str = "SEPID") -> dict:
        partida = f"P-{order_id}"
        with self._delivery_partida_stock_patch(
            [{"articleCode": "ART-MANUAL", "depositCode": "VAL", "partida": partida, "availableQuantity": 1}]
        ):
            return create_delivery_order(
                {
                    "id": order_id,
                    "orderNumber": f"OE-{order_id.upper()}",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": company_key,
                    "sellerName": "Ventas",
                    "sellerCode": "MAX",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "ART-MANUAL",
                            "articleName": "Artículo manual",
                            "articleRequiresPartida": True,
                            "description": "Artículo manual",
                            "quantity": 1,
                            "unitPrice": "0",
                            "partida": partida,
                        }
                    ],
                },
                self.jefe.id,
            )

    def _create_sale_order_without_partidas_for_manual_remito(
        self,
        order_id: str,
        company_key: str = "MGBIO",
    ) -> dict:
        return create_delivery_order(
            {
                "id": order_id,
                "orderNumber": f"OE-{order_id.upper()}",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "companyKey": company_key,
                "sellerName": "Ventas",
                "sellerCode": "MAX",
                "status": "pendiente_armado",
                "items": [
                    {
                        "articleCode": "1218002",
                        "articleName": "Prolongador de tubbing",
                        "articleRequiresPartida": True,
                        "description": "Prolongador de tubbing",
                        "quantity": 100,
                        "unitPrice": "0",
                    }
                ],
            },
            self.jefe.id,
        )

    def test_cargar_remito_bejerman_directo_no_registra_movimiento_stock(self):
        cases = [
            ("do-manual-digital-sep", "SEPID", "00004"),
            ("do-manual-digital-mg", "MGBIO", "00007"),
        ]

        with patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls:
            for order_id, company_key, point in cases:
                order = self._create_sale_order_ready_for_manual_remito(order_id, company_key)
                prepared = mark_prepared(order["id"], self.recepcion.id, f"RT R {point}-00012345")

                self.assertEqual(prepared["remitoNumber"], f"RT R {point}-00012345")
                self.assertTrue(prepared["bejermanRemitoGroupId"])
                with connection.cursor() as cur:
                    cur.execute(
                        """
                        SELECT g.company_key, g.comprobante_tipo, g.comprobante_pto_venta,
                               g.comprobante_numero, g.status, g.response_summary
                          FROM delivery_orders o
                          JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                         WHERE o.id = %s
                        """,
                        [order["id"]],
                    )
                    row = cur.fetchone()
                summary = row[5] if isinstance(row[5], dict) else json.loads(row[5])
                self.assertEqual(row[:5], (company_key, "RT", point, "00012345", "reconciled"))
                self.assertEqual(summary["documentMode"], "existing")
                self.assertTrue(summary["existingBejermanRemito"])
                self.assertFalse(summary["stockMovementGenerated"])
                self.assertEqual(summary["stockMovementSource"], "bejerman_direct")
                self.assertNotIn("manualRemitoNumber", summary)

        sdk_cls.assert_not_called()

    def test_cargar_remito_0007_existente_no_exige_partidas(self):
        order = self._create_sale_order_without_partidas_for_manual_remito("do-manual-0007-sin-partidas")

        with patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls:
            prepared = mark_prepared(order["id"], self.recepcion.id, "0007-00000656")

        self.assertEqual(prepared["remitoNumber"], "RT R 00007-00000656")
        self.assertTrue(prepared["bejermanRemitoGroupId"])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT g.company_key, g.comprobante_tipo, g.comprobante_pto_venta,
                       g.comprobante_numero, g.status, g.response_summary
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
        summary = row[5] if isinstance(row[5], dict) else json.loads(row[5])
        self.assertEqual(row[:5], ("MGBIO", "RT", "00007", "00000656", "reconciled"))
        self.assertEqual(summary["documentMode"], "existing")
        self.assertTrue(summary["existingBejermanRemito"])
        self.assertFalse(summary["stockMovementGenerated"])
        sdk_cls.assert_not_called()

    def test_cargar_remito_pv1_sigue_exigiendo_partidas(self):
        order = self._create_sale_order_without_partidas_for_manual_remito("do-manual-pv1-sin-partidas", "SEPID")

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_prepared(order["id"], self.recepcion.id, "0001-00000656")

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDAS_REQUIRED")

    def test_cargar_remito_pv1_registra_movimiento_stock_en_bejerman(self):
        order = self._create_sale_order_ready_for_manual_remito("do-manual-pv1")
        sent_comprobantes = []
        sent_kwargs = []
        init_kwargs = []

        class FakeManualRemitoClient:
            def __init__(self, *args, **kwargs):
                init_kwargs.append(kwargs)

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "ART-MANUAL",
                            "Articulo_Descripcion": "Artículo manual",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                sent_kwargs.append(kwargs)
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00001",
                            "Comprobante_Numero": "00022345",
                        }
                    ),
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", FakeManualRemitoClient):
            prepared = mark_prepared(order["id"], self.recepcion.id, "RT R 00001-00022345")

        self.assertEqual(prepared["remitoNumber"], "RT R 00001-00022345")
        self.assertEqual(init_kwargs[0]["company_key"], "SEPID")
        self.assertEqual(sent_kwargs[0]["numera_flex"], "N")
        self.assertEqual(sent_kwargs[0]["emite_reg"], "R")
        self.assertEqual(sent_comprobantes[0]["Comprobante_PtoVenta"], "00001")
        self.assertEqual(sent_comprobantes[0]["Comprobante_Numero"], "00022345")
        self.assertEqual(sent_comprobantes[0]["Comprobante_ActualizaStock"], "S")
        article_lines = [item for item in sent_comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_Deposito"], "VAL")
        self.assertEqual(article_lines[0]["Item_Partida"], "P-do-manual-pv1")
        self.assertEqual(article_lines[0]["Item_TipoIVA"], "1")
        self.assertEqual(article_lines[0]["Item_TasaIVAInscrip"], 21)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT g.comprobante_pto_venta, g.comprobante_numero, g.status, g.response_summary
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
        summary = row[3] if isinstance(row[3], dict) else json.loads(row[3])
        self.assertEqual(row[:3], ("00001", "00022345", "generated"))
        self.assertEqual(summary["documentMode"], "register")
        self.assertEqual(summary["manualRemitoNumber"], "RT R 00001-00022345")
        self.assertTrue(summary["stockMovementGenerated"])
        self.assertEqual(summary["stockMovementSource"], "nexora_manual_pv1")

    def test_una_orden_asocia_dos_remitos_existentes_sin_dividirse(self):
        order = self._create_rt_split_order(
            "do-multi-remito-existente",
            11,
            partida_prefix="P-MULTI-EXISTENTE",
        )

        with patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls:
            result = register_delivery_order_remitos(
                order["id"],
                self.recepcion.id,
                {
                    "remitos": [
                        {"remitoNumber": "RT R 00004-00088001"},
                        {"remitoNumber": "RT R 00004-00088002"},
                    ]
                },
            )

        sdk_cls.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["remitoSetStatus"], "complete")
        self.assertEqual(result["remitoNumbers"], ["RT R 00004-00088001", "RT R 00004-00088002"])
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM delivery_orders WHERE id = %s", [order["id"]])
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute(
                """
                SELECT l.sequence, l.expected_count, l.allocation_status, COUNT(a.id)
                  FROM delivery_order_remito_links l
                  LEFT JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
                 WHERE l.order_id = %s
                 GROUP BY l.id, l.sequence, l.expected_count, l.allocation_status
                 ORDER BY l.sequence
                """,
                [order["id"]],
            )
            links = cur.fetchall()
        self.assertEqual(links, [(1, 2, "external_unknown", 0), (2, 2, "external_unknown", 0)])

    def test_reintento_manual_salta_el_remito_00001_ya_exitoso(self):
        order = self._create_rt_split_order(
            "do-multi-remito-registrado",
            2,
            partida_prefix="P-MULTI-REGISTRADO",
        )
        item_ids = [item["id"] for item in order["items"]]
        payload = {
            "remitos": [
                {
                    "remitoNumber": "RT R 00001-00088101",
                    "allocations": [{"itemId": item_ids[0], "quantity": 1}],
                },
                {
                    "remitoNumber": "RT R 00001-00088102",
                    "allocations": [{"itemId": item_ids[1], "quantity": 1}],
                },
            ]
        }
        first_sent = []
        BaseClient = self._fake_rt_split_client(first_sent, first_number=88101)

        class FailSecondClient(BaseClient):
            attempts = 0

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                type(self).attempts += 1
                if type(self).attempts == 2:
                    raise BejermanSdkResponseError("Falla controlada del segundo remito")
                return super().ingresar_comprobante_ventas_json(comprobante, **kwargs)

        with patch("service.bejerman_delivery.BejermanSDKClient", FailSecondClient):
            partial = register_delivery_order_remitos(order["id"], self.recepcion.id, payload)

        self.assertFalse(partial["success"])
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["remitoSetStatus"], "partial")
        self.assertEqual(partial["remitoNumbers"], ["RT R 00001-00088101"])

        changed_payload = {
            "remitos": [
                {
                    "remitoNumber": "RT R 00001-00088101",
                    "allocations": [{"itemId": item_ids[1], "quantity": 1}],
                },
                {
                    "remitoNumber": "RT R 00001-00088102",
                    "allocations": [{"itemId": item_ids[0], "quantity": 1}],
                },
            ]
        }
        with patch("service.bejerman_delivery.BejermanSDKClient") as retry_sdk_cls:
            with self.assertRaises(DeliveryOrderError) as mismatch:
                register_delivery_order_remitos(order["id"], self.recepcion.id, changed_payload)
        self.assertEqual(mismatch.exception.code, "DELIVERY_REMITO_ALLOCATION_RETRY_MISMATCH")
        retry_sdk_cls.assert_not_called()

        retry_sent = []
        with patch(
            "service.bejerman_delivery.BejermanSDKClient",
            self._fake_rt_split_client(retry_sent, first_number=88102),
        ):
            completed = register_delivery_order_remitos(order["id"], self.recepcion.id, payload)

        self.assertTrue(completed["success"])
        self.assertEqual(completed["remitoSetStatus"], "complete")
        self.assertEqual(len(first_sent), 1)
        self.assertEqual(len(retry_sent), 1)
        self.assertEqual(
            completed["remitoNumbers"],
            ["RT R 00001-00088101", "RT R 00001-00088102"],
        )

    def test_varios_remitos_00001_rechazan_distribuciones_invalidas_y_modos_mezclados(self):
        order = self._create_rt_split_order(
            "do-multi-remito-validaciones",
            2,
            partida_prefix="P-MULTI-VALIDACIONES",
        )
        first_item_id, second_item_id = [item["id"] for item in order["items"]]
        cases = [
            (
                "faltante",
                {
                    "remitos": [
                        {
                            "remitoNumber": "RT R 00001-00088301",
                            "allocations": [{"itemId": first_item_id, "quantity": 0.5}],
                        },
                        {
                            "remitoNumber": "RT R 00001-00088302",
                            "allocations": [{"itemId": second_item_id, "quantity": 1}],
                        },
                    ]
                },
                "DELIVERY_REMITO_ALLOCATIONS_INCOMPLETE",
            ),
            (
                "exceso",
                {
                    "remitos": [
                        {
                            "remitoNumber": "RT R 00001-00088303",
                            "allocations": [{"itemId": first_item_id, "quantity": 2}],
                        },
                        {
                            "remitoNumber": "RT R 00001-00088304",
                            "allocations": [{"itemId": second_item_id, "quantity": 1}],
                        },
                    ]
                },
                "DELIVERY_REMITO_ALLOCATIONS_INCOMPLETE",
            ),
            (
                "solapamiento",
                {
                    "remitos": [
                        {
                            "remitoNumber": "RT R 00001-00088305",
                            "allocations": [{"itemId": first_item_id, "quantity": 0.5}],
                        },
                        {
                            "remitoNumber": "RT R 00001-00088306",
                            "allocations": [
                                {"itemId": first_item_id, "quantity": 0.5},
                                {"itemId": second_item_id, "quantity": 1},
                            ],
                        },
                    ]
                },
                "DELIVERY_REMITO_ALLOCATION_OVERLAP",
            ),
            (
                "número duplicado",
                {
                    "remitos": [
                        {"remitoNumber": "RT R 00001-00088307"},
                        {"remitoNumber": "RT R 00001-00088307"},
                    ]
                },
                "DELIVERY_REMITO_DUPLICATED",
            ),
            (
                "modos mezclados",
                {
                    "remitos": [
                        {"remitoNumber": "RT R 00001-00088308"},
                        {"remitoNumber": "RT R 00004-00088309"},
                    ]
                },
                "DELIVERY_REMITO_MIXED_MODES",
            ),
            (
                "empresas mezcladas",
                {
                    "remitos": [
                        {"remitoNumber": "RT R 00004-00088310"},
                        {"remitoNumber": "RT R 00007-00088311"},
                    ]
                },
                "DELIVERY_REMITO_MIXED_COMPANIES",
            ),
        ]

        with patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls:
            for label, payload, expected_code in cases:
                with self.subTest(label=label):
                    with self.assertRaises(DeliveryOrderError) as raised:
                        register_delivery_order_remitos(order["id"], self.recepcion.id, payload)
                    self.assertEqual(raised.exception.code, expected_code)
        sdk_cls.assert_not_called()

    def test_remito_existente_reconcilia_grupo_incierto_sin_nuevo_movimiento(self):
        order = self._create_sale_order_ready_for_manual_remito("do-reconcile-existing")
        group_id = "brg-reconcile-existing"
        self._attach_failed_rt_review_group(order["id"], group_id, issue_date="2026-08-04")

        with patch("service.bejerman_delivery.BejermanSDKClient") as sdk_cls:
            result = register_delivery_order_remitos(
                order["id"],
                self.recepcion.id,
                {"remitoNumber": "RT R 00004-00088201"},
            )

        sdk_cls.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["remitoSetStatus"], "complete")
        self.assertEqual(result["remitos"][0]["groupId"], group_id)
        self.assertEqual(result["remitos"][0]["allocationStatus"], "external_unknown")
        with connection.cursor() as cur:
            cur.execute("SELECT status, remito_number FROM bejerman_remito_groups WHERE id = %s", [group_id])
            self.assertEqual(cur.fetchone(), ("reconciled", "RT R 00004-00088201"))

    def test_ingreso_liberado_respeta_preferencia_y_no_duplica(self):
        self._enable_liberado_for_tecnico()

        inserted_1 = notify_ingreso_liberado(self.ingreso_id)
        inserted_2 = notify_ingreso_liberado(self.ingreso_id)

        self.assertEqual(inserted_1, 1)
        self.assertEqual(inserted_2, 0)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'ingreso_liberado'
                 GROUP BY user_id
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.tecnico.id, 1)])

    def test_override_activado_no_agrega_destinatarios_ajenos(self):
        self._enable_notification(self.otro, "ingreso_asignado", True)

        inserted = emit_notification(
            "ingreso_asignado",
            user_ids=[self.tecnico.id],
            title="Asignación directa",
            body="Solo el destinatario concreto debe recibirla.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:asignacion:directa",
            payload={"ingreso_id": self.ingreso_id},
        )

        self.assertEqual(inserted, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                  FROM notifications
                 WHERE notification_key = 'ingreso_asignado'
                   AND dedupe_key = 'test:asignacion:directa'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.tecnico.id,)])

    def test_ingreso_asignado_solo_notifica_al_tecnico_destinatario(self):
        self._enable_notification(self.tecnico, "ingreso_asignado", True)
        self._enable_notification(self.otro, "ingreso_asignado", True)

        inserted = notify_ingreso_asignado(self.ingreso_id, self.tecnico.id)

        self.assertEqual(inserted, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                  FROM notifications
                 WHERE notification_key = 'ingreso_asignado'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.tecnico.id,)])

    def test_roles_no_crean_audiencia_interna(self):
        self._enable_notification(self.tecnico, "ingreso_asignado", True)

        inserted = emit_notification(
            "ingreso_asignado",
            roles=["tecnico"],
            title="Rol ignorado",
            body="El rol no debe crear audiencia interna.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:rol:ignorado",
        )

        self.assertEqual(inserted, 0)

    def test_panel_no_marca_leida_y_click_mantiene_historial_reciente(self):
        self._enable_liberado_for_tecnico()
        notify_ingreso_liberado(self.ingreso_id)
        self._auth_as_tecnico()

        resp = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["unread_count"], 1)
        notification_id = resp.data["items"][0]["id"]

        with connection.cursor() as cur:
            cur.execute("SELECT read_at, clicked_at FROM notifications WHERE id=%s", [notification_id])
            self.assertEqual(cur.fetchone(), (None, None))

        click_resp = self.client.post(f"/api/notificaciones/{notification_id}/click/")
        self.assertEqual(click_resp.status_code, 200)
        self.assertTrue(click_resp.data["href"])

        resp_after = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp_after.status_code, 200)
        self.assertEqual(resp_after.data["unread_count"], 0)
        self.assertEqual(len(resp_after.data["items"]), 1)
        self.assertEqual(resp_after.data["items"][0]["id"], notification_id)
        self.assertIsNotNone(resp_after.data["items"][0]["read_at"])
        with connection.cursor() as cur:
            cur.execute("SELECT read_at, clicked_at FROM notifications WHERE id=%s", [notification_id])
            read_at, clicked_at = cur.fetchone()
        self.assertIsNotNone(read_at)
        self.assertIsNotNone(clicked_at)

    def test_panel_marca_todas_como_leidas(self):
        self._enable_notification(self.tecnico, "ingreso_asignado", True)
        self._enable_liberado_for_tecnico()
        notify_ingreso_liberado(self.ingreso_id)
        notify_ingreso_asignado(self.ingreso_id, self.tecnico.id)
        self._auth_as_tecnico()

        resp = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["unread_count"], 2)

        read_all_resp = self.client.post("/api/notificaciones/read-all/")
        self.assertEqual(read_all_resp.status_code, 200)
        self.assertEqual(read_all_resp.data["updated"], 2)

        resp_after = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp_after.status_code, 200)
        self.assertEqual(resp_after.data["unread_count"], 0)
        self.assertEqual(len(resp_after.data["items"]), 2)
        self.assertTrue(all(item["read_at"] for item in resp_after.data["items"]))

    def test_panel_filtra_por_usuario_y_no_permite_click_ajeno(self):
        emit_notification(
            "ingreso_asignado",
            user_ids=[self.tecnico.id],
            title="Notificación del técnico",
            body="Visible solo para el técnico.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:panel:tecnico",
        )
        emit_notification(
            "solicitud_baja",
            user_ids=[self.jefe.id],
            title="Notificación del jefe",
            body="Visible solo para el jefe.",
            href=f"/ingresos/{self.ingreso_id}?tab=principal",
            entity_type="ingreso",
            entity_id=self.ingreso_id,
            dedupe_key="test:panel:jefe",
        )
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM notifications WHERE dedupe_key = 'test:panel:tecnico'")
            tecnico_notification_id = cur.fetchone()[0]
            cur.execute("SELECT id FROM notifications WHERE dedupe_key = 'test:panel:jefe'")
            jefe_notification_id = cur.fetchone()[0]

        self._auth_as_tecnico()
        resp = self.client.get("/api/notificaciones/?limit=20")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["unread_count"], 1)
        self.assertEqual([item["id"] for item in resp.data["items"]], [tecnico_notification_id])

        click_resp = self.client.post(f"/api/notificaciones/{jefe_notification_id}/click/")
        self.assertEqual(click_resp.status_code, 404)

    def test_flujo_por_email_crea_notificacion_solo_para_email_resuelto(self):
        inserted = notify_estado_patrimonial(self.ingreso_id, "baja", emails=[self.jefe.email])

        self.assertEqual(inserted, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                  FROM notifications
                 WHERE notification_key = 'baja_patrimonial'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.jefe.id,)])

    def test_preferencias_heredan_defaults_y_guardan_overrides(self):
        self._auth_as_jefe()

        resp = self.client.get(f"/api/usuarios/{self.tecnico.id}/notificaciones/")
        self.assertEqual(resp.status_code, 200)
        items = {item["key"]: item for item in resp.data["items"]}
        self.assertFalse(items["ingreso_liberado"]["default_enabled"])
        self.assertFalse(items["ingreso_liberado"]["effective_enabled"])
        self.assertTrue(items["ingreso_asignado"]["default_enabled"])
        self.assertTrue(items["ingreso_asignado"]["effective_enabled"])

        put_resp = self.client.put(
            f"/api/usuarios/{self.tecnico.id}/notificaciones/",
            {"preferences": {"ingreso_liberado": True, "ingreso_asignado": None}},
            format="json",
        )
        self.assertEqual(put_resp.status_code, 200)
        items_after = {item["key"]: item for item in put_resp.data["items"]}
        self.assertTrue(items_after["ingreso_liberado"]["override_enabled"])
        self.assertTrue(items_after["ingreso_liberado"]["effective_enabled"])
        self.assertIsNone(items_after["ingreso_asignado"]["override_enabled"])
        self.assertTrue(items_after["ingreso_asignado"]["effective_enabled"])

        reset_resp = self.client.put(
            f"/api/usuarios/{self.tecnico.id}/notificaciones/",
            {"preferences": {"ingreso_liberado": None}},
            format="json",
        )
        self.assertEqual(reset_resp.status_code, 200)
        reset_items = {item["key"]: item for item in reset_resp.data["items"]}
        self.assertIsNone(reset_items["ingreso_liberado"]["override_enabled"])
        self.assertFalse(reset_items["ingreso_liberado"]["effective_enabled"])

    def test_configuracion_publica_guarda_canales_y_emails_extra(self):
        self.client.force_authenticate(user=self.cobranzas)

        resp = self.client.get("/api/notificaciones/configuracion/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["primary_email"], "cobranzas@notif.test")
        self.assertTrue(resp.data["capabilities"]["canManageExtraEmails"])
        items = {item["key"]: item for item in resp.data["items"]}
        self.assertTrue(items["sales_order_remito_ready"]["effective_channels"]["email"])

        put_resp = self.client.put(
            "/api/notificaciones/configuracion/",
            {
                "preferences": {
                    "sales_order_remito_ready": {
                        "bell": False,
                        "email": True,
                        "push": False,
                    }
                }
            },
            format="json",
        )
        self.assertEqual(put_resp.status_code, 200)
        updated = {item["key"]: item for item in put_resp.data["items"]}
        channels = updated["sales_order_remito_ready"]["effective_channels"]
        self.assertFalse(channels["bell"])
        self.assertTrue(channels["email"])
        self.assertFalse(channels["push"])

        email_resp = self.client.post(
            "/api/notificaciones/configuracion/emails/",
            {"email": "cobranzas.extra@notif.test"},
            format="json",
        )
        self.assertEqual(email_resp.status_code, 201)
        config_resp = self.client.get("/api/notificaciones/configuracion/")
        self.assertEqual(
            [row["email"] for row in config_resp.data["extra_emails"]],
            ["cobranzas.extra@notif.test"],
        )

    def test_configuracion_tecnico_excluye_claves_protegidas_y_rechaza_guardado(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences(
                  user_id, notification_key, enabled, bell_enabled, email_enabled, push_enabled
                )
                VALUES (%s, 'remito_pdf_rt', TRUE, TRUE, TRUE, TRUE)
                ON CONFLICT (user_id, notification_key) DO UPDATE SET
                  enabled = EXCLUDED.enabled,
                  bell_enabled = EXCLUDED.bell_enabled,
                  email_enabled = EXCLUDED.email_enabled,
                  push_enabled = EXCLUDED.push_enabled
                """,
                [self.tecnico.id],
            )

        self.client.force_authenticate(user=self.tecnico)
        resp = self.client.get("/api/notificaciones/configuracion/")
        self.assertEqual(resp.status_code, 200)
        keys = {item["key"] for item in resp.data["items"]}
        self.assertIn("ingreso_asignado", keys)
        self.assertNotIn("sales_order_remito_ready", keys)
        self.assertNotIn("billing_pending_summary", keys)
        self.assertFalse(any(key.startswith("remito_pdf_") for key in keys))

        put_resp = self.client.put(
            "/api/notificaciones/configuracion/",
            {"preferences": {"remito_pdf_rt": {"email": True}}},
            format="json",
        )
        self.assertEqual(put_resp.status_code, 400)

    def test_configuracion_cobranzas_solo_muestra_cobranzas_y_todos_los_pdf_remitos(self):
        self.client.force_authenticate(user=self.cobranzas)

        resp = self.client.get("/api/notificaciones/configuracion/")
        self.assertEqual(resp.status_code, 200)
        items = {item["key"]: item for item in resp.data["items"]}
        expected_keys = {
            "sales_order_remito_ready",
            "billing_pending_summary",
            "billing_remito_postponement_due",
            "service_order_ready_to_bill",
            "sales_order_commercial_terms_updated",
            "remito_pdf_rt",
            "remito_pdf_rd",
            "remito_pdf_rta",
            "remito_pdf_rtn",
            "remito_pdf_rss",
            "remito_pdf_ris",
            "remito_pdf_rda",
            "remito_pdf_rdn",
        }
        self.assertEqual(set(items), expected_keys)
        for key in ("sales_order_remito_ready", "billing_pending_summary", "service_order_ready_to_bill"):
            self.assertEqual(items[key]["group"], "Cobranzas")
            self.assertEqual(items[key]["allowed_channels"], ["bell", "email", "push"])
        self.assertEqual(
            {key for key in items if key.startswith("remito_pdf_")},
            {
                "remito_pdf_rt",
                "remito_pdf_rd",
                "remito_pdf_rta",
                "remito_pdf_rtn",
                "remito_pdf_rss",
                "remito_pdf_ris",
                "remito_pdf_rda",
                "remito_pdf_rdn",
            },
        )
        for key in (
            "remito_pdf_rt",
            "remito_pdf_rd",
            "remito_pdf_rta",
            "remito_pdf_rtn",
            "remito_pdf_rss",
            "remito_pdf_ris",
            "remito_pdf_rda",
            "remito_pdf_rdn",
        ):
            self.assertEqual(items[key]["allowed_channels"], ["email"])
            self.assertFalse(items[key]["effective_channels"]["bell"])
            self.assertTrue(items[key]["effective_channels"]["email"])
            self.assertFalse(items[key]["effective_channels"]["push"])

    def test_emails_extra_solo_admin_y_cobranzas(self):
        self.client.force_authenticate(user=self.tecnico)
        resp = self.client.post(
            "/api/notificaciones/configuracion/emails/",
            {"email": "tecnico.extra@notif.test"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(
        WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key",
        WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key",
        WEB_PUSH_VAPID_SUBJECT="mailto:notificaciones@sepid.com.ar",
    )
    def test_push_respeta_canal_push_independiente_de_campana(self):
        self.client.force_authenticate(user=self.recepcion)
        self.client.put(
            "/api/notificaciones/configuracion/",
            {
                "preferences": {
                    "sales_order_created": {
                        "bell": True,
                        "email": True,
                        "push": False,
                    }
                }
            },
            format="json",
        )
        self._insert_push_subscription(self.recepcion, endpoint="https://push.notif.test/channel-off")

        with patch("service.notifications.webpush", return_value=None) as webpush_mock:
            create_delivery_order(
                {
                    "id": "do-push-channel-off",
                    "orderNumber": "OE-PUSH-CH-001",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "operationCompanyLabel": "RESPIFLOW",
                    "items": [{"description": "Equipo de prueba", "quantity": 1}],
                },
                self.jefe.id,
            )

        webpush_mock.assert_not_called()
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'sales_order_created'
                   AND user_id = %s
                   AND COALESCE(bell_enabled, TRUE) = TRUE
                """,
                [self.recepcion.id],
            )
            self.assertEqual(cur.fetchone()[0], 1)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_remito_pdf_rss_respeta_optout_de_cobranzas_y_emails_extra(self):
        mail.outbox = []
        self.client.force_authenticate(user=self.cobranzas)
        self.client.post(
            "/api/notificaciones/configuracion/emails/",
            {"email": "cobranzas.extra@notif.test"},
            format="json",
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RSS R 00004-00000001",
                document_type="RSS",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )

        self.assertEqual(result["emails"], 4)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            set(mail.outbox[0].to),
            {
                "admin@notif.test",
                "cobranzas@notif.test",
                "cobranzas.extra@notif.test",
                "recepcion@notif.test",
            },
        )
        self.assertEqual(result["notificationKey"], "remito_pdf_rss")

        mail.outbox = []
        self.client.put(
            "/api/notificaciones/configuracion/",
            {"preferences": {"remito_pdf_rss": {"email": False}}},
            format="json",
        )
        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RSS R 00004-00000002",
                document_type="RSS",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )

        self.assertEqual(result["emails"], 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(set(mail.outbox[0].to), {"admin@notif.test", "recepcion@notif.test"})

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_remito_pdf_rd_usa_politica_de_entrega(self):
        mail.outbox = []

        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RD R 00004-00000001",
                document_type="RD",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )

        self.assertEqual(result["notificationKey"], "remito_pdf_rd")
        self.assertEqual(result["emails"], 3)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            set(mail.outbox[0].to),
            {"admin@notif.test", "cobranzas@notif.test", "recepcion@notif.test"},
        )

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_remito_pdf_rda_incluye_cobranzas_y_excluye_tecnico_aunque_tenga_preferencia(self):
        mail.outbox = []
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences(
                  user_id, notification_key, enabled, bell_enabled, email_enabled, push_enabled
                )
                VALUES (%s, 'remito_pdf_rda', TRUE, TRUE, TRUE, TRUE)
                ON CONFLICT (user_id, notification_key) DO UPDATE SET
                  enabled = EXCLUDED.enabled,
                  bell_enabled = EXCLUDED.bell_enabled,
                  email_enabled = EXCLUDED.email_enabled,
                  push_enabled = EXCLUDED.push_enabled
                """,
                [self.tecnico.id],
            )

        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RDA R 00004-00000001",
                document_type="RDA",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )

        self.assertEqual(result["notificationKey"], "remito_pdf_rda")
        self.assertEqual(result["emails"], 3)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(set(mail.outbox[0].to), {"admin@notif.test", "cobranzas@notif.test", "recepcion@notif.test"})

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_remito_pdf_rda_admin_recepcion_y_cobranzas_pueden_desactivar_por_codigo(self):
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RDA R 00004-00000010",
                document_type="RDA",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )
        self.assertEqual(result["emails"], 3)
        self.assertEqual(set(mail.outbox[0].to), {"admin@notif.test", "cobranzas@notif.test", "recepcion@notif.test"})

        self.client.force_authenticate(user=self.admin)
        self.client.put(
            "/api/notificaciones/configuracion/",
            {"preferences": {"remito_pdf_rda": {"email": False}}},
            format="json",
        )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RDA R 00004-00000011",
                document_type="RDA",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )
        self.assertEqual(result["emails"], 2)
        self.assertEqual(set(mail.outbox[0].to), {"cobranzas@notif.test", "recepcion@notif.test"})

        self.client.force_authenticate(user=self.recepcion)
        self.client.put(
            "/api/notificaciones/configuracion/",
            {"preferences": {"remito_pdf_rda": {"email": False}}},
            format="json",
        )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RDA R 00004-00000012",
                document_type="RDA",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )
        self.assertEqual(result["emails"], 1)
        self.assertEqual(set(mail.outbox[0].to), {"cobranzas@notif.test"})

        self.client.force_authenticate(user=self.cobranzas)
        self.client.put(
            "/api/notificaciones/configuracion/",
            {"preferences": {"remito_pdf_rda": {"email": False}}},
            format="json",
        )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            result = notify_bejerman_remito_pdf_issued(
                remito_number="RDA R 00004-00000013",
                document_type="RDA",
                company_key="MGBIO",
                customer_name="Cliente Notificaciones",
                source="test",
                pdf_loader=lambda: (b"%PDF-1.4\nremito", "remito.pdf"),
            )
        self.assertEqual(result["emails"], 0)
        self.assertEqual(mail.outbox, [])

    def test_orden_entrega_creada_notifica_recepcion(self):
        order = create_delivery_order(
            {
                "id": "do-notif-created",
                "orderNumber": "OE-NOTIF-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        self.assertEqual(order["id"], "do-notif-created")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, title, href
                  FROM notifications
                 WHERE notification_key = 'sales_order_created'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [
                (
                    self.recepcion.id,
                    "Nueva orden de entrega OE-NOTIF-001",
                    "/administracion/ordenes-entrega?orderId=do-notif-created",
                )
            ],
        )

    @override_settings(
        WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key",
        WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key",
        WEB_PUSH_VAPID_SUBJECT="mailto:notificaciones@sepid.com.ar",
    )
    def test_push_subscription_api_crea_actualiza_y_elimina(self):
        self.client.force_authenticate(user=self.recepcion)
        subscription = {
            "endpoint": "https://push.notif.test/api-sub",
            "keys": {"p256dh": "p256dh-api", "auth": "auth-api"},
        }

        with patch("service.notifications.webpush", object()):
            config_resp = self.client.get("/api/notificaciones/push/config/")
            self.assertEqual(config_resp.status_code, 200)
            self.assertTrue(config_resp.data["available"])
            self.assertEqual(config_resp.data["publicKey"], "public-test-key")
            self.assertFalse(config_resp.data["active"])

            post_resp = self.client.post(
                "/api/notificaciones/push/subscription/",
                subscription,
                format="json",
                HTTP_USER_AGENT="NEXORA Test Browser",
            )
            self.assertEqual(post_resp.status_code, 200)
            self.assertTrue(post_resp.data["active"])

            config_after = self.client.get("/api/notificaciones/push/config/")
            self.assertTrue(config_after.data["active"])

            delete_resp = self.client.delete(
                "/api/notificaciones/push/subscription/",
                {"endpoint": subscription["endpoint"]},
                format="json",
            )
            self.assertEqual(delete_resp.status_code, 200)
            self.assertEqual(delete_resp.data["deleted"], 1)

    @override_settings(
        WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key",
        WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key",
        WEB_PUSH_VAPID_SUBJECT="mailto:notificaciones@sepid.com.ar",
    )
    def test_orden_entrega_creada_envia_push_a_recepcion(self):
        self._insert_push_subscription(self.recepcion, endpoint="https://push.notif.test/oe-created")

        with patch("service.notifications.webpush", return_value=None) as webpush_mock:
            create_delivery_order(
                {
                    "id": "do-push-created",
                    "orderNumber": "OE-PUSH-001",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "operationCompanyLabel": "RESPIFLOW",
                    "items": [{"description": "Equipo de prueba", "quantity": 1}],
                },
                self.jefe.id,
            )

        self.assertEqual(webpush_mock.call_count, 1)
        kwargs = webpush_mock.call_args.kwargs
        self.assertEqual(kwargs["subscription_info"]["endpoint"], "https://push.notif.test/oe-created")
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["title"], "Nueva orden de entrega OE-PUSH-001")
        self.assertEqual(payload["href"], "/administracion/ordenes-entrega?orderId=do-push-created")
        self.assertEqual(payload["notificationKey"], "sales_order_created")
        self.assertEqual(payload["payload"]["orderId"], "do-push-created")

    @override_settings(
        WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key",
        WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key",
        WEB_PUSH_VAPID_SUBJECT="mailto:notificaciones@sepid.com.ar",
    )
    def test_orden_entrega_creada_no_envia_push_si_recepcion_desactivo_notificacion(self):
        self._enable_notification(self.recepcion, "sales_order_created", False)
        self._insert_push_subscription(self.recepcion, endpoint="https://push.notif.test/oe-disabled")

        with patch("service.notifications.webpush", return_value=None) as webpush_mock:
            create_delivery_order(
                {
                    "id": "do-push-disabled",
                    "orderNumber": "OE-PUSH-002",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "operationCompanyLabel": "RESPIFLOW",
                    "items": [{"description": "Equipo de prueba", "quantity": 1}],
                },
                self.jefe.id,
            )

        webpush_mock.assert_not_called()

    @override_settings(
        WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key",
        WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key",
        WEB_PUSH_VAPID_SUBJECT="mailto:notificaciones@sepid.com.ar",
    )
    def test_orden_entrega_creada_no_duplica_push_por_dedupe(self):
        self._insert_push_subscription(self.recepcion, endpoint="https://push.notif.test/oe-dedupe")
        order = {
            "id": "do-push-dedupe",
            "orderNumber": "OE-PUSH-003",
            "customerId": self.customer_id,
            "deliveryType": "sale",
            "sellerName": "Ventas",
            "operationCompanyLabel": "RESPIFLOW",
            "items": [{"description": "Equipo de prueba", "quantity": 1}],
        }

        with patch("service.notifications.webpush", return_value=None) as webpush_mock:
            created = create_delivery_order(order, self.jefe.id)
            notify_delivery_order_created(created["id"], self.jefe.id)

        self.assertEqual(webpush_mock.call_count, 1)

    @override_settings(
        WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key",
        WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key",
        WEB_PUSH_VAPID_SUBJECT="mailto:notificaciones@sepid.com.ar",
    )
    def test_push_muerto_se_desactiva_en_404_410(self):
        endpoint = "https://push.notif.test/dead"
        self._insert_push_subscription(self.recepcion, endpoint=endpoint)

        class FakeWebPushException(Exception):
            def __init__(self):
                super().__init__("gone")
                self.response = type("Response", (), {"status_code": 410})()

        with patch("service.notifications.WebPushException", FakeWebPushException), patch(
            "service.notifications.webpush",
            side_effect=FakeWebPushException(),
        ):
            create_delivery_order(
                {
                    "id": "do-push-dead",
                    "orderNumber": "OE-PUSH-004",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "operationCompanyLabel": "RESPIFLOW",
                    "items": [{"description": "Equipo de prueba", "quantity": 1}],
                },
                self.jefe.id,
            )

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT disabled_at IS NOT NULL, failure_count
                  FROM notification_push_subscriptions
                 WHERE endpoint = %s
                """,
                [endpoint],
            )
            disabled, failure_count = cur.fetchone()
        self.assertTrue(disabled)
        self.assertEqual(failure_count, 1)

    def test_orden_entrega_auto_numera_venta_oe_y_liberacion_os(self):
        first = create_delivery_order(
            {
                "id": "do-auto-number-1",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )
        second = create_delivery_order(
            {
                "id": "do-auto-number-2",
                "customerId": self.customer_id,
                "deliveryType": "service_release",
                "sourceSystem": "nexora",
                "sourceExternalId": str(self.ingreso_id),
                "sourceReference": f"OS {self.ingreso_id}",
                "ingresoId": self.ingreso_id,
                "sellerName": "Servicio técnico",
                "items": [{"description": "Servicio técnico", "quantity": 1, "partida": "SN-1"}],
            },
            self.jefe.id,
        )

        self.assertEqual(first["orderNumber"], "OE-00001")
        self.assertEqual(second["orderNumber"], f"OS-{str(self.ingreso_id).zfill(5)}")
        self.assertEqual(second["sourceReference"], f"OS-{str(self.ingreso_id).zfill(5)}")

    def test_orden_entrega_guarda_y_edita_empresa_bejerman(self):
        order = create_delivery_order(
            {
                "id": "do-company-key",
                "orderNumber": "OE-COMPANY-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "companyKey": "MGBIO",
                "sellerName": "Ventas",
                "items": [{"description": "Artículo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        self.assertEqual(order["companyKey"], "MGBIO")
        updated = update_delivery_order(
            order["id"],
            {
                "customerId": self.customer_id,
                "deliveryType": "demo",
                "companyKey": "SEPID",
                "sellerName": "ADM",
                "items": [
                    {
                        "id": order["items"][0]["id"],
                        "description": "Artículo de prueba editado",
                        "quantity": 1,
                    }
                ],
            },
            self.jefe.id,
        )

        self.assertEqual(updated["companyKey"], "SEPID")
        with connection.cursor() as cur:
            cur.execute("SELECT company_key FROM delivery_orders WHERE id = %s", [order["id"]])
            self.assertEqual(cur.fetchone()[0], "SEPID")

    def test_orden_entrega_precarga_codigo_vendedor_del_usuario_ventas(self):
        order = create_delivery_order(
            {
                "id": "do-seller-user-code",
                "orderNumber": "OE-SELLER-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "items": [{"description": "Artículo de prueba", "quantity": 1}],
            },
            self.ventas.id,
        )

        self.assertEqual(order["sellerCode"], "EZE")
        self.assertEqual(order["sellerName"], "EZE Ezequiel Merino")

    def test_orden_entrega_preserva_codigo_vendedor_editado(self):
        order = create_delivery_order(
            {
                "id": "do-seller-manual-code",
                "orderNumber": "OE-SELLER-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerCode": "tom",
                "items": [{"description": "Artículo de prueba", "quantity": 1}],
            },
            self.ventas.id,
        )

        self.assertEqual(order["sellerCode"], "TOM")
        self.assertEqual(order["sellerName"], "TOM Tomas Perez Avila")

    def test_orden_entrega_moneda_precio_default_ars(self):
        order = create_delivery_order(
            {
                "id": "do-currency-default",
                "orderNumber": "OE-CUR-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [{"description": "Artículo de prueba", "quantity": 1, "unitPrice": "1000"}],
            },
            self.jefe.id,
        )

        self.assertEqual(order["priceCurrency"], "ARS")
        self.assertEqual(delivery_order_billing_totals(order)["currency"], "ARS")
        with connection.cursor() as cur:
            cur.execute("SELECT price_currency FROM delivery_orders WHERE id = %s", [order["id"]])
            self.assertEqual(cur.fetchone()[0], "ARS")

    def test_orden_entrega_moneda_precio_usd_y_preserva_al_editar(self):
        order = create_delivery_order(
            {
                "id": "do-currency-usd",
                "orderNumber": "OE-CUR-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "priceCurrency": "USD",
                "commercialExchangeRate": "1200",
                "sellerName": "Ventas",
                "items": [{"description": "Artículo de prueba", "quantity": 2, "unitPrice": "100"}],
            },
            self.jefe.id,
        )

        self.assertEqual(order["priceCurrency"], "USD")
        self.assertEqual(order["items"][0]["priceCurrency"], "USD")
        self.assertEqual(delivery_order_billing_totals(order)["currency"], "USD")
        updated = update_delivery_order(
            order["id"],
            {
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "id": order["items"][0]["id"],
                        "description": "Artículo de prueba editado",
                        "quantity": 2,
                        "unitPrice": "100",
                    }
                ],
            },
            self.jefe.id,
        )

        self.assertEqual(updated["priceCurrency"], "USD")
        self.assertEqual(updated["items"][0]["priceCurrency"], "USD")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.price_currency, i.price_currency
                  FROM delivery_orders o
                  JOIN delivery_order_items i ON i.order_id = o.id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            self.assertEqual(cur.fetchone(), ("USD", "USD"))

    def test_orden_entrega_moneda_precio_por_item(self):
        order = create_delivery_order(
            {
                "id": "do-currency-item",
                "orderNumber": "OE-CUR-ITEM",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "priceCurrency": "ARS",
                "sellerName": "Ventas",
                "items": [
                    {"description": "Artículo en pesos", "quantity": 1, "unitPrice": "1000", "priceCurrency": "ARS"},
                    {"description": "Artículo en dólares", "quantity": 1, "unitPrice": "100", "priceCurrency": "USD"},
                ],
            },
            self.jefe.id,
        )

        self.assertEqual([item["priceCurrency"] for item in order["items"]], ["ARS", "USD"])
        totals = delivery_order_billing_totals(order)
        self.assertEqual(totals["currency"], "MIXED")
        self.assertTrue(totals["mixedCurrency"])
        self.assertEqual(totals["totalsByCurrency"]["ARS"]["total"], Decimal("1000"))
        self.assertEqual(totals["totalsByCurrency"]["USD"]["total"], Decimal("100"))

    def test_orden_entrega_rechaza_moneda_precio_invalida(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            create_delivery_order(
                {
                    "id": "do-currency-invalid",
                    "orderNumber": "OE-CUR-003",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "priceCurrency": "EUR",
                    "sellerName": "Ventas",
                    "items": [{"description": "Artículo de prueba", "quantity": 1}],
                },
                self.jefe.id,
            )

        self.assertEqual(ctx.exception.code, "INVALID_PRICE_CURRENCY")

        with self.assertRaises(DeliveryOrderError) as item_ctx:
            create_delivery_order(
                {
                    "id": "do-currency-item-invalid",
                    "orderNumber": "OE-CUR-004",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "items": [{"description": "Artículo de prueba", "quantity": 1, "priceCurrency": "EUR"}],
                },
                self.jefe.id,
            )

        self.assertEqual(item_ctx.exception.code, "INVALID_PRICE_CURRENCY")

    def test_admin_puede_cargar_descuento_por_item_en_orden_entrega(self):
        order = create_delivery_order(
            {
                "id": "do-discount-admin",
                "orderNumber": "OE-DESC-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "description": "Artículo de prueba",
                        "quantity": 2,
                        "unitPrice": "1000",
                        "discountPercent": "10",
                    }
                ],
            },
            self.admin.id,
        )

        item = order["items"][0]
        self.assertEqual(item["discountPercent"], 10.0)
        self.assertEqual(item["grossSubtotal"], 2000.0)
        self.assertEqual(item["discountAmount"], 200.0)
        self.assertEqual(item["netSubtotal"], 1800.0)
        totals = delivery_order_billing_totals(order)
        self.assertEqual(totals["grossTotal"], Decimal("2000"))
        self.assertEqual(totals["discountTotal"], Decimal("200"))
        self.assertEqual(totals["total"], Decimal("1800"))
        with connection.cursor() as cur:
            cur.execute("SELECT discount_percent FROM delivery_order_items WHERE id = %s", [item["id"]])
            self.assertEqual(cur.fetchone()[0], Decimal("10.00"))

    def test_supervisor_puede_cargar_descuento_por_item_en_orden_entrega(self):
        order = create_delivery_order(
            {
                "id": "do-discount-supervisor",
                "orderNumber": "OE-DESC-SUP",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "description": "Artículo de prueba",
                        "quantity": 1,
                        "unitPrice": "1000",
                        "discountPercent": "5",
                    }
                ],
            },
            self.supervisor.id,
        )

        item = order["items"][0]
        self.assertEqual(item["discountPercent"], 5.0)
        self.assertEqual(item["discountAmount"], 50.0)
        self.assertEqual(item["netSubtotal"], 950.0)

    def test_jefe_puede_cargar_descuento_por_item_en_orden_entrega(self):
        order = create_delivery_order(
            {
                "id": "do-discount-jefe",
                "orderNumber": "OE-DESC-JEFE",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "description": "Artículo de prueba",
                        "quantity": 1,
                        "unitPrice": "1000",
                        "discountPercent": "5",
                    }
                ],
            },
            self.jefe.id,
        )

        item = order["items"][0]
        self.assertEqual(item["discountPercent"], 5.0)
        self.assertEqual(item["discountAmount"], 50.0)
        self.assertEqual(item["netSubtotal"], 950.0)

    def test_no_admin_no_puede_crear_descuento_por_item(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            create_delivery_order(
                {
                    "id": "do-discount-denied-create",
                    "orderNumber": "OE-DESC-002",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "items": [
                        {
                            "description": "Artículo de prueba",
                            "quantity": 1,
                            "unitPrice": "1000",
                            "discountPercent": "5",
                        }
                    ],
                },
                self.tecnico.id,
            )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_DISCOUNT_ADMIN_REQUIRED")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_no_admin_conserva_descuento_existente_si_no_lo_envia(self):
        order = create_delivery_order(
            {
                "id": "do-discount-preserve",
                "orderNumber": "OE-DESC-003",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "description": "Artículo de prueba",
                        "quantity": 2,
                        "unitPrice": "1000",
                        "discountPercent": "12.5",
                    }
                ],
            },
            self.admin.id,
        )

        updated = update_delivery_order(
            order["id"],
            {
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "id": order["items"][0]["id"],
                        "description": "Artículo de prueba editado",
                        "quantity": 2,
                        "unitPrice": "1000",
                    }
                ],
            },
            self.tecnico.id,
        )
        self.assertEqual(updated["items"][0]["discountPercent"], 12.5)
        self.assertEqual(updated["items"][0]["netSubtotal"], 1750.0)

        with self.assertRaises(DeliveryOrderError) as ctx:
            update_delivery_order(
                order["id"],
                {
                    "items": [
                        {
                            "id": order["items"][0]["id"],
                            "description": "Artículo de prueba editado",
                            "quantity": 2,
                            "unitPrice": "1000",
                            "discountPercent": "0",
                        }
                    ],
                },
                self.tecnico.id,
            )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_DISCOUNT_ADMIN_REQUIRED")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_orden_entrega_rechaza_empresa_bejerman_invalida(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            create_delivery_order(
                {
                    "id": "do-company-invalid",
                    "orderNumber": "OE-COMPANY-INVALID",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "TEST",
                    "sellerName": "Ventas",
                    "items": [{"description": "Artículo de prueba", "quantity": 1}],
                },
                self.jefe.id,
            )

        self.assertEqual(ctx.exception.code, "INVALID_COMPANY_KEY")

    def test_orden_entrega_pendiente_stock_se_lista_y_se_libera_manual(self):
        order = create_delivery_order(
            {
                "id": "do-stock-pending",
                "orderNumber": "OE-STOCK-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "status": "pendiente_stock",
                "sellerName": "Ventas",
                "items": [{"id": "doi-stock-pending", "description": "Equipo por ingresar", "quantity": 1}],
            },
            self.jefe.id,
        )

        self.assertEqual(order["status"], "pendiente_stock")
        listed = list_delivery_orders({"status": "pendiente_stock", "limit": 10})
        self.assertEqual([item["id"] for item in listed["items"]], [order["id"]])
        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_STOCK_PENDING")
        updated = update_delivery_order(
            order["id"],
            {
                "status": "pendiente_armado",
                "items": order["items"],
            },
            self.jefe.id,
        )
        self.assertEqual(updated["status"], "pendiente_armado")

    def test_orden_entrega_pendiente_stock_no_emite_remito(self):
        order = create_delivery_order(
            {
                "id": "do-stock-remito",
                "orderNumber": "OE-STOCK-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "status": "pendiente_stock",
                "sellerName": "Ventas",
                "items": [{"id": "doi-stock-remito", "description": "Equipo por ingresar", "quantity": 1}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            generate_bejerman_remito([order["id"]], self.jefe.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_STOCK_PENDING")

    def test_orden_alquiler_planificada_permite_articulo_sin_ns(self):
        order = create_delivery_order(
            {
                "id": "do-rental-future-row",
                "orderNumber": "OEA-FUT-001",
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "ADM",
                "items": [
                    {
                        "id": "doi-rental-future-row",
                        "articleCode": "ART-ALQ",
                        "articleName": "Equipo alquiler",
                        "description": "Equipo alquiler futuro",
                        "quantity": 2,
                        "stockDepositCode": "STL",
                    }
                ],
            },
            self.jefe.id,
        )

        self.assertEqual(order["status"], "pendiente_armado")
        self.assertEqual(order["items"][0]["quantity"], 2.0)
        self.assertIsNone(order["items"][0]["ingresoId"])
        self.assertIsNone(order["items"][0]["deviceId"])
        self.assertEqual(order["items"][0]["partida"], "")
        self.assertEqual(order["items"][0]["partidas"], [])
        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDAS_REQUIRED")

    def test_orden_alquiler_carga_ns_resuelve_os_y_bloquea_reserva_duplicada(self):
        ingreso_1, device_1 = self._create_rental_ingreso("NS-NOTIF-001", "MG 9001", existing=True)
        ingreso_2, device_2 = self._create_rental_ingreso("NS-NOTIF-002", "MG 9002")
        order = create_delivery_order(
            {
                "id": "do-rental-future-ns",
                "orderNumber": "OEA-FUT-002",
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "ADM",
                "items": [
                    {
                        "id": "doi-rental-future-ns",
                        "articleCode": "ART-ALQ",
                        "articleName": "Equipo alquiler",
                        "description": "Equipo alquiler futuro",
                        "quantity": 2,
                    }
                ],
            },
            self.jefe.id,
        )
        lots = [
            {"articleCode": "ART-ALQ", "depositCode": "STL", "partida": "NS-NOTIF-001", "availableQuantity": 1},
            {"articleCode": "ART-ALQ", "depositCode": "STL", "partida": "NS-NOTIF-002", "availableQuantity": 1},
        ]

        with self._delivery_partida_stock_patch(lots):
            updated = update_item_partidas(
                order["id"],
                "doi-rental-future-ns",
                self.recepcion.id,
                [
                    {"partida": "NS-NOTIF-001", "assignedQuantity": 1},
                    {"partida": "NS-NOTIF-002", "assignedQuantity": 1},
                ],
            )
            prepared = mark_prepared(order["id"], self.recepcion.id)

        partidas = updated["items"][0]["partidas"]
        self.assertEqual([row["ingresoId"] for row in partidas], [ingreso_1, ingreso_2])
        self.assertEqual([row["deviceId"] for row in partidas], [device_1, device_2])
        self.assertEqual([row["stockDepositCode"] for row in partidas], ["STL", "STL"])
        self.assertEqual(prepared["status"], "armado_pendiente_entrega")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT ingreso_id, device_id
                  FROM delivery_order_item_partidas
                 WHERE order_item_id = %s
                 ORDER BY sort_order
                """,
                ["doi-rental-future-ns"],
            )
            self.assertEqual(cur.fetchall(), [(ingreso_1, device_1), (ingreso_2, device_2)])

        duplicate = create_delivery_order(
            {
                "id": "do-rental-future-duplicate",
                "orderNumber": "OEA-FUT-003",
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "ADM",
                "items": [
                    {
                        "id": "doi-rental-future-duplicate",
                        "articleCode": "ART-ALQ",
                        "description": "Equipo alquiler duplicado",
                        "quantity": 1,
                    }
                ],
            },
            self.jefe.id,
        )
        with self._delivery_partida_stock_patch(lots), self.assertRaises(DeliveryOrderError) as ctx:
            update_item_partidas(
                duplicate["id"],
                "doi-rental-future-duplicate",
                self.recepcion.id,
                [{"partida": "NS-NOTIF-001", "assignedQuantity": 1}],
            )

        self.assertEqual(ctx.exception.code, "RENTAL_EQUIPMENT_RESERVED")

    def test_orden_alquiler_guarda_equipo_vinculado_y_bloquea_reserva_duplicada(self):
        self._ensure_rental_article_mapping()
        ingreso_id, device_id = self._create_rental_ingreso("NS-NOTIF-001", "MG 9001", existing=True)
        item = self._rental_item(ingreso_id, device_id, "NS-NOTIF-001")

        order = create_delivery_order(
            {
                "id": "do-rental-linked",
                "orderNumber": "OEA-LINK-001",
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "ADM",
                "sellerCode": "ADM",
                "items": [item],
            },
            self.jefe.id,
        )

        self.assertEqual(order["items"][0]["ingresoId"], ingreso_id)
        self.assertEqual(order["items"][0]["deviceId"], device_id)
        self.assertEqual(order["items"][0]["stockDepositCode"], "STL")
        with connection.cursor() as cur:
            cur.execute("SELECT ingreso_id, device_id FROM delivery_order_items WHERE order_id = %s", [order["id"]])
            self.assertEqual(cur.fetchone(), (ingreso_id, device_id))

        with self.assertRaises(DeliveryOrderError) as ctx:
            create_delivery_order(
                {
                    "id": "do-rental-duplicate",
                    "orderNumber": "OEA-LINK-002",
                    "customerId": self.customer_id,
                    "deliveryType": "rental",
                    "companyKey": "SEPID",
                    "sellerName": "ADM",
                    "sellerCode": "ADM",
                    "items": [item],
                },
                self.jefe.id,
            )
        self.assertEqual(ctx.exception.code, "RENTAL_EQUIPMENT_RESERVED")

    def test_orden_alquiler_fuerza_codigo_vendedor_adm_al_crear_y_editar(self):
        self._ensure_rental_article_mapping()
        ingreso_id, device_id = self._create_rental_ingreso("NS-NOTIF-003", "MG 9003")
        item = self._rental_item(ingreso_id, device_id, "NS-NOTIF-003")

        order = create_delivery_order(
            {
                "id": "do-rental-seller-adm",
                "orderNumber": "OEA-ADM-001",
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "Ventas",
                "sellerCode": "EZE",
                "items": [item],
            },
            self.ventas.id,
        )

        self.assertEqual(order["sellerCode"], "ADM")
        self.assertEqual(order["sellerName"], "ADM Administración")
        updated = update_delivery_order(
            order["id"],
            {
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "Otro",
                "sellerCode": "TOM",
                "items": [
                    {
                        "id": order["items"][0]["id"],
                        **item,
                    }
                ],
            },
            self.ventas.id,
        )

        self.assertEqual(updated["sellerCode"], "ADM")
        self.assertEqual(updated["sellerName"], "ADM Administración")

    def test_selector_alquiler_devuelve_solo_equipos_con_stock_stl(self):
        self._ensure_rental_article_mapping()
        ingreso_1, _device_1 = self._create_rental_ingreso("NS-NOTIF-001", "MG 9001", existing=True)
        ingreso_2, _device_2 = self._create_rental_ingreso("NS-NOTIF-002", "MG 9002")
        stock_rows = [
            {"Articulo_Codigo": "ART-ALQ", "Art_CodDeposito": "STL", "Art_Partida": "NS-NOTIF-001", "Art_DispUM1": 1, "Art_RealUM1": 1},
            {"Articulo_Codigo": "ART-ALQ", "Art_CodDeposito": "STL", "Art_Partida": "NS-NOTIF-002", "Art_DispUM1": 0, "Art_RealUM1": 1},
        ]

        with patch("service.bejerman_delivery.BejermanSDKClient", self._fake_rta_client([], stock_rows)):
            data = list_rental_available_equipment(limit=20, actor_user_id=self.jefe.id, company_key="SEPID")

        ingreso_ids = [item["ingresoId"] for item in data["items"]]
        self.assertIn(ingreso_1, ingreso_ids)
        self.assertNotIn(ingreso_2, ingreso_ids)
        selected = next(item for item in data["items"] if item["ingresoId"] == ingreso_1)
        self.assertEqual(selected["sourceReference"], f"OS-{str(ingreso_1).zfill(5)}")
        self.assertEqual(selected["stockDepositCode"], "STL")
        self.assertEqual(selected["partida"], "NS-NOTIF-001")
        self.assertEqual(selected["stockAvailableQuantity"], 1.0)

    def test_selector_alquiler_usa_articulo_del_stock_stl_si_falta_mapeo(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_article_mappings")
        ingreso_id, _device_id = self._create_rental_ingreso("NS-NOTIF-001", "MG 9001", existing=True)
        stock_rows = [
            {"Articulo_Codigo": "ART-STL-DIRECTO", "Art_CodDeposito": "STL", "Art_Partida": "NS-NOTIF-001", "Art_DispUM1": 1, "Art_RealUM1": 1},
        ]

        with patch("service.bejerman_delivery.BejermanSDKClient", self._fake_rta_client([], stock_rows)):
            data = list_rental_available_equipment(search="NS-NOTIF-001", limit=20, actor_user_id=self.jefe.id, company_key="SEPID")

        self.assertEqual(data["warning"], None)
        selected = next(item for item in data["items"] if item["ingresoId"] == ingreso_id)
        self.assertEqual(selected["articleCode"], "ART-STL-DIRECTO")
        self.assertEqual(selected["partida"], "NS-NOTIF-001")
        self.assertEqual(selected["stockDepositCode"], "STL")
        self.assertEqual(selected["stockAvailableQuantity"], 1.0)

    def test_rta_emitido_marca_varios_ingresos_como_alquilados(self):
        ingreso_1, _device_1 = self._create_rental_ingreso("NS-NOTIF-001", "MG 9001", existing=True)
        ingreso_2, _device_2 = self._create_rental_ingreso("NS-NOTIF-002", "MG 9002")
        order = create_delivery_order(
            {
                "id": "do-rental-rta-sync",
                "orderNumber": "OEA-RTA-001",
                "customerId": self.customer_id,
                "deliveryType": "rental",
                "companyKey": "SEPID",
                "sellerName": "Ventas",
                "sellerCode": "EZE",
                "items": [
                    {
                        "id": "doi-rental-rta-sync",
                        "articleCode": "ART-ALQ",
                        "articleName": "Equipo alquiler",
                        "description": "Equipo alquiler futuro",
                        "quantity": 2,
                    },
                ],
            },
            self.jefe.id,
        )
        self.assertEqual(order["sellerCode"], "ADM")
        sent_comprobantes = []
        stock_rows = [
            {"Articulo_Codigo": "ART-ALQ", "Art_CodDeposito": "STL", "Art_Partida": "NS-NOTIF-001", "Art_DispUM1": 1, "Art_RealUM1": 1},
            {"Articulo_Codigo": "ART-ALQ", "Art_CodDeposito": "STL", "Art_Partida": "NS-NOTIF-002", "Art_DispUM1": 1, "Art_RealUM1": 1},
        ]
        lots = [
            {"articleCode": "ART-ALQ", "depositCode": "STL", "partida": "NS-NOTIF-001", "availableQuantity": 1},
            {"articleCode": "ART-ALQ", "depositCode": "STL", "partida": "NS-NOTIF-002", "availableQuantity": 1},
        ]

        with self._delivery_partida_stock_patch(lots):
            update_item_partidas(
                order["id"],
                "doi-rental-rta-sync",
                self.recepcion.id,
                [
                    {"partida": "NS-NOTIF-001", "assignedQuantity": 1},
                    {"partida": "NS-NOTIF-002", "assignedQuantity": 1},
                ],
            )
            prepared = mark_prepared(order["id"], self.recepcion.id)

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", self._fake_rta_client(sent_comprobantes, stock_rows)),
            self._delivery_partida_stock_patch(lots),
        ):
            result = generate_bejerman_remito(
                [prepared["id"]],
                self.jefe.id,
                {"issueDate": "2026-06-19", "sellerCode": "TOM"},
            )

        self.assertEqual(result["remitoNumber"], "RTA R 00004-00004567")
        self.assertFalse(result["billingRequired"])
        self.assertEqual(result["orders"][0]["status"], "armado_pendiente_entrega")
        self.assertIsNone(result["orders"][0]["deliveredAt"])
        self.assertEqual(sent_comprobantes[0]["Comprobante_Tipo"], "RTA")
        self.assertEqual(sent_comprobantes[0]["Comprobante_TipoOperacion"], "ALQ")
        self.assertEqual(sent_comprobantes[0]["Vendedor_Codigo"], "ADM")
        article_lines = [item for item in sent_comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([line["Item_CantidadUM1"] for line in article_lines], [1, 1])
        self.assertEqual([line["Item_Deposito"] for line in article_lines], ["STL", "STL"])
        self.assertEqual([line["Item_Partida"] for line in article_lines], ["NS-NOTIF-001", "NS-NOTIF-002"])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT t.id, t.estado, t.alquilado, t.alquiler_a, t.alquiler_remito, t.alquiler_fecha, loc.nombre
                  FROM ingresos t
                  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                 WHERE t.id = ANY(%s)
                 ORDER BY t.id
                """,
                [[ingreso_1, ingreso_2]],
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM ingreso_events
                 WHERE ticket_id = ANY(%s)
                   AND a_estado = 'alquilado'
                   AND comentario = 'Alquiler registrado por RTA Bejerman'
                """,
                [[ingreso_1, ingreso_2]],
            )
            event_count = int(cur.fetchone()[0])
            cur.execute("SELECT seller_code FROM bejerman_remito_groups WHERE id = %s", [result["groupId"]])
            group_seller_code = cur.fetchone()[0]
        self.assertEqual([row[1] for row in rows], ["alquilado", "alquilado"])
        self.assertEqual([row[2] for row in rows], [True, True])
        self.assertEqual([row[3] for row in rows], ["Clínica Notificaciones", "Clínica Notificaciones"])
        self.assertEqual([row[4] for row in rows], ["RTA R 00004-00004567", "RTA R 00004-00004567"])
        self.assertTrue(all(row[5] is not None for row in rows))
        self.assertEqual([row[6] for row in rows], ["-", "-"])
        self.assertEqual(event_count, 2)
        self.assertEqual(group_seller_code, "ADM")

    def test_remito_bejerman_usa_empresa_explicitada_en_orden(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "ART-1", "depositCode": "VAL", "partida": "P1", "availableQuantity": 1}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-company",
                    "orderNumber": "OE-COMPANY-REMITO",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "MGBIO",
                    "sellerName": "Ventas",
                    "sellerCode": "MAX",
                    "status": "armado_pendiente_entrega",
                    "items": [{"articleCode": "ART-1", "description": "Artículo de prueba", "quantity": 1, "partida": "P1"}],
                },
                self.jefe.id,
            )
        init_kwargs = []
        sent_comprobantes = []

        class FakeRemitoClient:
            def __init__(self, *args, **kwargs):
                init_kwargs.append(kwargs)

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "ART-1",
                            "Articulo_Descripcion": "Artículo de prueba",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00007",
                            "Comprobante_Numero": "00001235",
                        }
                    ),
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", FakeRemitoClient):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["companyKey"], "MGBIO")
        self.assertTrue(result["billingRequired"])
        self.assertEqual(result["orders"][0]["status"], "armado_pendiente_entrega")
        self.assertIsNone(result["orders"][0]["deliveredAt"])
        pending_billing = list_delivery_orders({"pendingBilling": True, "limit": 10})
        self.assertIn(order["id"], [item["id"] for item in pending_billing["items"]])
        self.assertEqual(init_kwargs[0]["company_key"], "MGBIO")
        self.assertEqual(sent_comprobantes[0]["Cliente_SitIVA"], "1")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT company_key, seller_code, status, attempt_count,
                       response_summary ->> 'orchestrationStatus'
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [result["groupId"]],
            )
            self.assertEqual(cur.fetchone(), ("MGBIO", "MAX", "generated", 1, "generated"))
            cur.execute(
                """
                SELECT phase, status
                  FROM bejerman_integration_attempts
                 WHERE group_id = %s
                 ORDER BY id
                """,
                [result["groupId"]],
            )
            attempts = cur.fetchall()
        self.assertIn(("reserve", "reserved"), attempts)
        self.assertIn(("sending", "sdk_request"), attempts)
        self.assertIn(("sdk_response", "received"), attempts)
        self.assertIn(("generated", "finalized"), attempts)

    def test_remito_bejerman_emite_articulo_sin_partida_en_blanco(self):
        order = create_delivery_order(
            {
                "id": "do-remito-no-partida",
                "orderNumber": "OE-NO-PARTIDA",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "companyKey": "SEPID",
                "sellerName": "Ventas",
                "sellerCode": "MAX",
                "status": "armado_pendiente_entrega",
                "items": [
                    {
                        "articleCode": "1208010",
                        "articleName": "Filtro de aire",
                        "articleRequiresPartida": False,
                        "description": "Filtro de aire",
                        "quantity": 10,
                    }
                ],
            },
            self.jefe.id,
        )
        sent_comprobantes = []

        class FakeRemitoClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1208010",
                            "Articulo_Descripcion": "Filtro de aire",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00001236",
                        }
                    ),
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", FakeRemitoClient):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["remitoNumber"], "RT R 00004-00001236")
        article_lines = [item for item in sent_comprobantes[0]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CodigoArticulo"], "1208010")
        self.assertEqual(article_lines[0]["Item_Partida"], " ")
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 10)

    def test_venta_grande_se_divide_en_varios_remitos_por_limite_de_lineas(self):
        # Caso OE-00116: una venta con más partidas de las que entran en la
        # hoja del RT debe emitirse como varios remitos automáticamente.
        order = self._create_rt_split_order(
            "do-remito-venta-split",
            25,
            partida_prefix="P-VENTA-SPLIT",
        )
        sent_comprobantes = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_comprobantes),
            ):
                result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertTrue(result["multiple"])
        self.assertEqual(
            result["remitoNumbers"],
            ["RT R 00004-00003234", "RT R 00004-00003235", "RT R 00004-00003236"],
        )
        self.assertEqual(len(sent_comprobantes), 3)
        line_counts = [len(comprobante["Comprobante_Items"]) for comprobante in sent_comprobantes]
        self.assertEqual(self._rt_article_counts(sent_comprobantes), [10, 10, 5])
        self.assertTrue(all(count <= 22 for count in line_counts))
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT order_number, remito_number,
                       (SELECT COUNT(*) FROM delivery_order_items i WHERE i.order_id = d.id) AS item_count
                  FROM delivery_orders d
                 WHERE id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT l.sequence, l.expected_count, COUNT(a.id)
                  FROM delivery_order_remito_links l
                  JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
                 WHERE l.order_id = %s
                 GROUP BY l.id, l.sequence, l.expected_count
                 ORDER BY l.sequence
                """,
                [order["id"]],
            )
            link_rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FROM delivery_orders WHERE source_external_id LIKE %s",
                [f"{order['id']}:%"],
            )
            fragment_count = int(cur.fetchone()[0])
        self.assertEqual(row, ("OE-DO-REMITO-VENTA-SPLIT", " / ".join(result["remitoNumbers"]), 25))
        self.assertEqual(link_rows, [(1, 3, 10), (2, 3, 10), (3, 3, 5)])
        self.assertEqual(fragment_count, 0)

    def test_rt_respeta_los_limites_de_diez_y_once_articulos(self):
        order_10 = self._create_rt_split_order(
            "do-remito-venta-diez",
            10,
            partida_prefix="P-DIEZ",
        )
        order_11 = self._create_rt_split_order(
            "do-remito-venta-once",
            11,
            partida_prefix="P-ONCE",
        )
        sent_10 = []
        sent_11 = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_10, first_number=3300),
            ):
                result_10 = generate_bejerman_remito([order_10["id"]], self.jefe.id, {})
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_11, first_number=3310),
            ):
                result_11 = generate_bejerman_remito([order_11["id"]], self.jefe.id, {})

        self.assertFalse(result_10.get("multiple", False))
        self.assertEqual(self._rt_article_counts(sent_10), [10])
        self.assertTrue(result_11["multiple"])
        self.assertEqual(result_11["remitoNumbers"], ["RT R 00004-00003310", "RT R 00004-00003311"])
        self.assertEqual(self._rt_article_counts(sent_11), [10, 1])
        self.assertTrue(all(len(comprobante["Comprobante_Items"]) <= 22 for comprobante in sent_10 + sent_11))

    def test_rt_reserva_el_conjunto_y_reintenta_solo_el_remito_faltante(self):
        order = self._create_rt_split_order(
            "do-remito-parcial-retry",
            11,
            partida_prefix="P-PARCIAL-RETRY",
        )
        first_sent = []
        reservation_snapshots = []
        BaseClient = self._fake_rt_split_client(first_sent, first_number=3330)

        class FailSecondClient(BaseClient):
            attempts = 0

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                type(self).attempts += 1
                if type(self).attempts == 1:
                    with connection.cursor() as cur:
                        cur.execute(
                            """
                            SELECT g.status
                              FROM delivery_order_remito_links l
                              JOIN bejerman_remito_groups g ON g.id = l.remito_group_id
                             WHERE l.order_id = %s
                             ORDER BY l.sequence
                            """,
                            [order["id"]],
                        )
                        reservation_snapshots.append([row[0] for row in cur.fetchall()])
                if type(self).attempts == 2:
                    raise BejermanSdkResponseError("El comprobante no se importó. Rechazo controlado.")
                return super().ingresar_comprobante_ventas_json(comprobante, **kwargs)

        with patch("service.bejerman_delivery.BejermanSDKClient", FailSecondClient):
            partial = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertFalse(partial["success"])
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["remitoSetStatus"], "partial")
        self.assertEqual(reservation_snapshots, [["sending", "reserved"]])
        self.assertEqual(len(first_sent), 1)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT sequence, expected_count FROM delivery_order_remito_links WHERE order_id = %s",
                [order["id"]],
            )
            self.assertEqual(cur.fetchall(), [(1, 2)])

        retry_sent = []
        with patch(
            "service.bejerman_delivery.BejermanSDKClient",
            self._fake_rt_split_client(retry_sent, first_number=3331),
        ):
            completed = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertTrue(completed["success"])
        self.assertEqual(completed["remitoSetStatus"], "complete")
        self.assertEqual(len(retry_sent), 1)
        self.assertEqual(completed["remitoNumbers"], ["RT R 00004-00003330", "RT R 00004-00003331"])

    def test_rt_agrupa_ordenes_sin_superar_diez_articulos(self):
        orders_5 = [
            self._create_rt_split_order(
                f"do-remito-cinco-{index}",
                5,
                partida_prefix=f"P-CINCO-{index}",
            )
            for index in range(2)
        ]
        orders_6 = [
            self._create_rt_split_order(
                f"do-remito-seis-{index}",
                6,
                partida_prefix=f"P-SEIS-{index}",
            )
            for index in range(2)
        ]
        sent_5 = []
        sent_6 = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_5, first_number=3320),
            ):
                result_5 = generate_bejerman_remito(
                    [order["id"] for order in orders_5],
                    self.jefe.id,
                    {},
                )
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_6, first_number=3330),
            ):
                result_6 = generate_bejerman_remito(
                    [order["id"] for order in orders_6],
                    self.jefe.id,
                    {},
                )

        self.assertFalse(result_5.get("multiple", False))
        self.assertEqual(self._rt_article_counts(sent_5), [10])
        grouped_result = _current_multi_remito_result([order["id"] for order in orders_5])
        self.assertEqual(len(grouped_result["remitos"]), 1)
        self.assertEqual(len(grouped_result["groupIds"]), 1)
        self.assertEqual(len(grouped_result["orders"]), 2)
        self.assertTrue(result_6["multiple"])
        self.assertEqual(result_6["remitoNumbers"], ["RT R 00004-00003330", "RT R 00004-00003331"])
        self.assertEqual(self._rt_article_counts(sent_6), [6, 6])
        self.assertTrue(all(len(comprobante["Comprobante_Items"]) <= 22 for comprobante in sent_5 + sent_6))

    def test_rt_diez_articulos_con_descuento_caben_en_veintidos_renglones(self):
        order = self._create_rt_split_order(
            "do-remito-diez-descuento",
            10,
            partida_prefix="P-DESCUENTO",
            discount_percent="5",
        )
        sent_comprobantes = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_comprobantes, first_number=3340),
            ):
                result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertFalse(result.get("multiple", False))
        self.assertEqual(self._rt_article_counts(sent_comprobantes), [10])
        self.assertEqual(len(sent_comprobantes[0]["Comprobante_Items"]), 22)

    def test_rt_divide_un_item_con_trece_partidas_sin_perder_datos(self):
        order = self._create_rt_multipartida_split_order(
            "do-remito-multipartida-trece",
            13,
            partida_prefix="P-MULTI",
        )
        sent_comprobantes = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(
                    sent_comprobantes,
                    article_codes=["ART-MULTIPARTIDA"],
                    first_number=3350,
                ),
            ):
                result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertTrue(result["multiple"])
        self.assertEqual(result["remitoNumbers"], ["RT R 00004-00003350", "RT R 00004-00003351"])
        self.assertEqual(self._rt_article_counts(sent_comprobantes), [10, 3])
        article_lines = [
            item
            for comprobante in sent_comprobantes
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "A"
        ]
        expected_partidas = [f"P-MULTI-{index:03d}" for index in range(13)]
        self.assertEqual([item["Item_Partida"] for item in article_lines], expected_partidas)
        self.assertEqual(
            sum(abs(Decimal(str(item["Item_CantidadUM1"]))) for item in article_lines),
            Decimal("13"),
        )
        self.assertEqual(
            sum(Decimal(str(comprobante["Comprobante_ImporteTotal"])) for comprobante in sent_comprobantes),
            Decimal("157.3"),
        )

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.order_number, i.quantity, i.unit_price, i.price_currency, i.discount_percent,
                       COUNT(p.id), COALESCE(SUM(p.assigned_quantity), 0)
                  FROM delivery_orders d
                  JOIN delivery_order_items i ON i.order_id = d.id
                  LEFT JOIN delivery_order_item_partidas p ON p.order_item_id = i.id
                 WHERE d.id = %s OR d.source_external_id LIKE %s
                 GROUP BY d.order_number, i.id, i.quantity, i.unit_price, i.price_currency, i.discount_percent
                 ORDER BY d.order_number
                """,
                [order["id"], f"{order['id']}:%"],
            )
            item_rows = cur.fetchall()
            cur.execute(
                """
                SELECT l.sequence, COUNT(a.id), COALESCE(SUM(a.assigned_quantity), 0)
                  FROM delivery_order_remito_links l
                  JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
                 WHERE l.order_id = %s
                 GROUP BY l.id, l.sequence
                 ORDER BY l.sequence
                """,
                [order["id"]],
            )
            allocation_rows = cur.fetchall()
            cur.execute(
                """
                SELECT p.partida, p.stock_deposit_code, p.stock_available_quantity
                  FROM delivery_order_item_partidas p
                  JOIN delivery_order_items i ON i.id = p.order_item_id
                  JOIN delivery_orders d ON d.id = i.order_id
                 WHERE d.id = %s OR d.source_external_id LIKE %s
                 ORDER BY p.partida
                """,
                [order["id"], f"{order['id']}:%"],
            )
            partida_rows = cur.fetchall()

        self.assertEqual(len(item_rows), 1)
        self.assertEqual([int(row[1]) for row in item_rows], [13])
        self.assertTrue(all(Decimal(row[2]) == Decimal("10") for row in item_rows))
        self.assertEqual({row[3] for row in item_rows}, {"ARS"})
        self.assertTrue(all(Decimal(row[4]) == Decimal("0") for row in item_rows))
        self.assertEqual([int(row[5]) for row in item_rows], [13])
        self.assertEqual([int(row[6]) for row in item_rows], [13])
        self.assertEqual([(row[0], int(row[1]), int(row[2])) for row in allocation_rows], [(1, 10, 10), (2, 3, 3)])
        self.assertEqual([row[0] for row in partida_rows], expected_partidas)
        self.assertEqual({row[1] for row in partida_rows}, {"VAL"})
        self.assertTrue(all(Decimal(row[2]) == Decimal("1") for row in partida_rows))

    def test_rt_divide_un_item_con_veintiseis_partidas_sin_duplicarlas(self):
        order = self._create_rt_multipartida_split_order(
            "do-remito-multipartida-veintiseis",
            26,
            partida_prefix="P-MULTI-26",
        )
        sent_comprobantes = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(
                    sent_comprobantes,
                    article_codes=["ART-MULTIPARTIDA"],
                    first_number=3380,
                ),
            ):
                result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertTrue(result["multiple"])
        self.assertEqual(
            result["remitoNumbers"],
            ["RT R 00004-00003380", "RT R 00004-00003381", "RT R 00004-00003382"],
        )
        self.assertEqual(self._rt_article_counts(sent_comprobantes), [10, 10, 6])
        article_lines = [
            item
            for comprobante in sent_comprobantes
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "A"
        ]
        expected_partidas = [f"P-MULTI-26-{index:03d}" for index in range(26)]
        self.assertEqual([item["Item_Partida"] for item in article_lines], expected_partidas)
        self.assertEqual(
            sum(abs(Decimal(str(item["Item_CantidadUM1"]))) for item in article_lines),
            Decimal("26"),
        )

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.order_number, i.quantity, COUNT(p.id), COALESCE(SUM(p.assigned_quantity), 0)
                  FROM delivery_orders d
                  JOIN delivery_order_items i ON i.order_id = d.id
                  LEFT JOIN delivery_order_item_partidas p ON p.order_item_id = i.id
                 WHERE d.id = %s OR d.source_external_id LIKE %s
                 GROUP BY d.order_number, i.id, i.quantity
                 ORDER BY d.order_number
                """,
                [order["id"], f"{order['id']}:%"],
            )
            item_rows = cur.fetchall()
            cur.execute(
                """
                SELECT l.sequence, COUNT(a.id), COALESCE(SUM(a.assigned_quantity), 0)
                  FROM delivery_order_remito_links l
                  JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
                 WHERE l.order_id = %s
                 GROUP BY l.id, l.sequence
                 ORDER BY l.sequence
                """,
                [order["id"]],
            )
            allocation_rows = cur.fetchall()
            cur.execute(
                """
                SELECT p.partida, p.assigned_quantity
                  FROM delivery_order_item_partidas p
                  JOIN delivery_order_items i ON i.id = p.order_item_id
                  JOIN delivery_orders d ON d.id = i.order_id
                 WHERE d.id = %s OR d.source_external_id LIKE %s
                 ORDER BY p.partida
                """,
                [order["id"], f"{order['id']}:%"],
            )
            partida_rows = cur.fetchall()

        self.assertEqual(len(item_rows), 1)
        self.assertEqual([int(row[1]) for row in item_rows], [26])
        self.assertEqual([int(row[2]) for row in item_rows], [26])
        self.assertEqual([int(row[3]) for row in item_rows], [26])
        self.assertEqual(
            [(row[0], int(row[1]), int(row[2])) for row in allocation_rows],
            [(1, 10, 10), (2, 10, 10), (3, 6, 6)],
        )
        self.assertEqual([row[0] for row in partida_rows], expected_partidas)
        self.assertEqual(len({row[0] for row in partida_rows}), 26)
        self.assertTrue(all(Decimal(row[1]) == Decimal("1") for row in partida_rows))

    def test_rt_ignora_skip_print_split_recibido_en_el_payload(self):
        order = self._create_rt_split_order(
            "do-remito-skip-publico",
            11,
            partida_prefix="P-SKIP-PUBLICO",
        )
        sent_comprobantes = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch(
                "service.bejerman_delivery.BejermanSDKClient",
                self._fake_rt_split_client(sent_comprobantes, first_number=3360),
            ):
                result = generate_bejerman_remito(
                    [order["id"]],
                    self.jefe.id,
                    {"_skipPrintSplit": True},
                )

        self.assertTrue(result["multiple"])
        self.assertEqual(result["remitoNumbers"], ["RT R 00004-00003360", "RT R 00004-00003361"])
        self.assertEqual(self._rt_article_counts(sent_comprobantes), [10, 1])

    def test_rt_defensa_final_bloquea_exceso_antes_de_enviar_al_sdk(self):
        order = self._create_rt_split_order(
            "do-remito-guard-final",
            11,
            partida_prefix="P-GUARD-FINAL",
        )
        sent_comprobantes = []

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with (
                patch(
                    "service.bejerman_delivery._split_remito_orders_for_printing",
                    side_effect=lambda orders, *args, **kwargs: orders,
                ),
                patch(
                    "service.bejerman_delivery._partition_remito_orders_for_printing",
                    side_effect=lambda orders, *args, **kwargs: [orders],
                ),
                patch(
                    "service.bejerman_delivery.BejermanSDKClient",
                    self._fake_rt_split_client(sent_comprobantes, first_number=3370),
                ),
                self.assertRaises(DeliveryOrderError) as ctx,
            ):
                generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(ctx.exception.code, "BEJERMAN_RT_PRINT_LIMIT_EXCEEDED")
        self.assertEqual(sent_comprobantes, [])
        self.assertFalse(get_delivery_order(order["id"], include_events=False)["bejermanRemitoGroupId"])

    def test_revision_rt_grande_confirmado_no_importado_reemite_diez_mas_uno(self):
        order = self._create_rt_split_order(
            "do-rt-review-split-once",
            11,
            partida_prefix="P-RT-REVIEW",
        )
        old_group_id = "brg-rt-review-split-once"
        self._attach_failed_rt_review_group(order["id"], old_group_id, issue_date="2026-07-21")
        sent_comprobantes = []
        lookup_calls = []
        BaseReviewClient = self._fake_rt_split_client(sent_comprobantes, first_number=3400)

        class ReviewSplitRtClient(BaseReviewClient):
            def list_comprobantes_ventas(self, filters):
                lookup_calls.append(filters)
                return {"DatosJSON": []}

        with override_settings(
            BEJERMAN_SALE_MAX_ARTICLE_LINES=10,
            BEJERMAN_SALE_MAX_PRINT_LINES=22,
        ):
            with patch("service.bejerman_delivery.BejermanSDKClient", ReviewSplitRtClient):
                verify_result = verify_bejerman_remito_review(old_group_id, self.jefe.id)
                self.assertEqual(verify_result["reviewState"], "ready_for_correction")
                self.assertEqual(verify_result["failureCertainty"], "confirmed_not_imported")
                self.assertFalse(get_delivery_order(order["id"], include_events=False)["bejermanRemitoGroupId"])

                result = reissue_corrected_bejerman_remito_review(
                    old_group_id,
                    self.jefe.id,
                    {"confirm": True, "issueDate": "2026-07-21"},
                )

        self.assertEqual(len(lookup_calls), 2)
        self.assertTrue(result["multiple"])
        self.assertEqual(result["previousGroupId"], old_group_id)
        self.assertEqual(
            result["remitoNumbers"],
            ["RT R 00004-00003400", "RT R 00004-00003401"],
        )
        self.assertEqual(len(result["groupIds"]), 2)
        self.assertEqual(self._rt_article_counts(sent_comprobantes), [10, 1])
        self.assertTrue(all(len(item["Comprobante_Items"]) <= 22 for item in sent_comprobantes))
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT status,
                       response_summary ->> 'reviewState',
                       response_summary ->> 'replacementGroupId',
                       response_summary ->> 'replacementGroupIds',
                       response_summary ->> 'replacementRemitoNumbers',
                       response_summary ->> 'reissueInProgress'
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [old_group_id],
            )
            old_group = cur.fetchone()
        self.assertEqual(old_group[0], "failed_review")
        self.assertEqual(old_group[1], "superseded")
        self.assertEqual(old_group[2], result["groupIds"][0])
        self.assertEqual(json.loads(old_group[3]), result["groupIds"])
        self.assertEqual(json.loads(old_group[4]), result["remitoNumbers"])
        self.assertEqual(old_group[5], "false")

    def test_revision_rt_grande_bloquea_lookup_fallido_o_ambiguo_sin_emitir(self):
        for index, (lookup_status, expected_code) in enumerate(
            (
                ("lookup_failed", "BEJERMAN_REMITO_LOOKUP_UNAVAILABLE"),
                ("ambiguous", "BEJERMAN_REMITO_LOOKUP_AMBIGUOUS"),
            ),
            start=1,
        ):
            with self.subTest(lookup_status=lookup_status):
                order = self._create_rt_split_order(
                    f"do-rt-review-{lookup_status}",
                    11,
                    partida_prefix=f"P-RT-REVIEW-{index}",
                )
                old_group_id = f"brg-rt-review-{lookup_status}"
                self._attach_failed_rt_review_group(order["id"], old_group_id, issue_date="2026-07-21")
                sent_comprobantes = []

                def unresolved_lookup(*args, lookup_outcome=None, **kwargs):
                    if lookup_outcome is not None:
                        lookup_outcome.update({"status": lookup_status})
                        if lookup_status == "ambiguous":
                            lookup_outcome.update({"candidateCount": 2, "matchCount": 2})
                    return None

                with (
                    patch(
                        "service.bejerman_delivery.BejermanSDKClient",
                        self._fake_rt_split_client(sent_comprobantes, first_number=3410 + index),
                    ),
                    patch(
                        "service.bejerman_delivery._recover_delivery_remito_after_emit_error",
                        side_effect=unresolved_lookup,
                    ),
                    self.assertRaises(DeliveryOrderError) as ctx,
                ):
                    verify_bejerman_remito_review(old_group_id, self.jefe.id)

                self.assertEqual(ctx.exception.code, expected_code)
                self.assertEqual(sent_comprobantes, [])
                with connection.cursor() as cur:
                    cur.execute(
                        """
                        SELECT o.remito_number,
                               o.bejerman_remito_group_id,
                               g.status,
                               g.response_summary ->> 'reviewState',
                               g.response_summary ->> 'failureCertainty',
                               g.response_summary ->> 'diagnosticCode',
                               g.response_summary ->> 'candidateCount',
                               g.response_summary ->> 'matchCount'
                          FROM delivery_orders o
                          JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                         WHERE o.id = %s
                        """,
                        [order["id"]],
                    )
                    state = cur.fetchone()
                    cur.execute(
                        """
                        SELECT COUNT(*)
                          FROM bejerman_integration_attempts
                         WHERE group_id = %s
                           AND phase IN ('sending', 'review_reissue')
                        """,
                        [old_group_id],
                    )
                    emission_attempts = int(cur.fetchone()[0])
                self.assertEqual(state[:6], (None, old_group_id, "failed_review", "blocked", "unknown", expected_code))
                if lookup_status == "ambiguous":
                    self.assertEqual(state[6:], ("2", "2"))
                else:
                    self.assertEqual(state[6:], (None, None))
                self.assertEqual(emission_attempts, 0)

    def test_remito_bejerman_recupera_rt_creado_tras_error_falso_de_stock(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-recovery-stock",
                    "orderNumber": "OE-00069",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "commercialExchangeRate": "1500",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Mascara buconasal BMC-F5A talle medium",
                            "articleRequiresPartida": True,
                            "description": "Mascara buconasal BMC-F5A talle medium",
                            "quantity": 20,
                            "unitPrice": "29",
                            "partidas": [
                                {
                                    "partida": "D2507112H",
                                    "assignedQuantity": 20,
                                    "stockDepositCode": "VAL",
                                }
                            ],
                        }
                    ],
                },
                self.jefe.id,
            )

        class FalseStockErrorClient:
            def __init__(self, *args, **kwargs):
                self.calls = []

            def list_clientes(self):
                self.calls.append(("list_clientes", None))
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                self.calls.append(("list_articulos", filters))
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Mascara buconasal BMC-F5A talle medium",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                self.calls.append(("ingresar_comprobante_ventas_json", comprobante))
                raise BejermanSdkResponseError("Stock insuficiente en deposito VAL")

            def list_comprobantes_ventas(self, filters):
                self.calls.append(("list_comprobantes_ventas", filters))
                return {
                    "DatosJSON": [
                        {
                            "Comprobante_ID": "4739",
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00004739",
                            "Comprobante_FechaEmision": "2026-07-01",
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Comprobante_TipoOperacion": "MC",
                        }
                    ]
                }

            def detalle_comprobante_ventas(self, comprobante_id):
                self.calls.append(("detalle_comprobante_ventas", comprobante_id))
                return {
                    "DatosJSON": {
                        "Comprobante_Tipo": "RT",
                        "Comprobante_Letra": "R",
                        "Comprobante_PtoVenta": "00004",
                        "Comprobante_Numero": "00004739",
                        "Comprobante_FechaEmision": "2026-07-01",
                        "Cliente_Codigo": "CLI-NOTIF",
                        "Cliente_RazonSocial": "Clinica Notificaciones",
                        "Comprobante_TipoOperacion": "MC",
                        "Comprobante_Mensaje": "NEXORA pendiente",
                        "Comprobante_Items": [
                            {"Item_Tipo": "L", "Item_DescripArticulo": "Orden OE-00069"},
                            {
                                "Item_Tipo": "A",
                                "Item_CodigoArticulo": "1202014",
                                "Item_DescripArticulo": "Mascara buconasal BMC-F5A talle medium",
                                "Item_CantidadUM1": 20,
                                "Item_Deposito": "VAL",
                                "Item_Partida": "D2507112H",
                            },
                        ],
                    }
                }

        fake_client = FalseStockErrorClient()
        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client),
            patch("service.bejerman_delivery.notify_bejerman_remito_pdf_issued"),
        ):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-01"})

        self.assertEqual(result["remitoNumber"], "RT R 00004-00004739")
        self.assertEqual(result["orders"][0]["remitoNumber"], "RT R 00004-00004739")
        self.assertIn("list_comprobantes_ventas", [call[0] for call in fake_client.calls])
        self.assertIn("detalle_comprobante_ventas", [call[0] for call in fake_client.calls])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.remito_number, g.status, g.comprobante_numero,
                       g.response_summary ->> 'recoveredAfterError',
                       g.response_summary ->> 'originalEmitError'
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM delivery_order_events
                 WHERE order_id = %s
                   AND event_type = 'bejerman_remito_failed'
                """,
                [order["id"]],
            )
            failed_events = int(cur.fetchone()[0])
        self.assertEqual(row[0], "RT R 00004-00004739")
        self.assertEqual(row[1], "reconciled")
        self.assertEqual(row[2], "00004739")
        self.assertEqual(row[3], "true")
        self.assertIn("Stock insuficiente", row[4])
        self.assertEqual(failed_events, 0)

    def test_remito_bejerman_error_post_envio_sin_match_queda_en_revision_y_reintento_no_reemite(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-review-no-match",
                    "orderNumber": "OE-00070",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Mascara buconasal",
                            "articleRequiresPartida": True,
                            "description": "Mascara buconasal",
                            "quantity": 20,
                            "unitPrice": "29",
                            "partidas": [{"partida": "D2507112H", "assignedQuantity": 20, "stockDepositCode": "VAL"}],
                        }
                    ],
                },
                self.jefe.id,
            )

        class NoMatchAfterSendClient:
            def __init__(self):
                self.ingresar_count = 0
                self.lookup_count = 0

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Mascara buconasal",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                self.ingresar_count += 1
                raise BejermanSdkResponseError("Stock insuficiente en deposito VAL")

            def list_comprobantes_ventas(self, filters):
                self.lookup_count += 1
                return {"DatosJSON": []}

        fake_client = NoMatchAfterSendClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client):
            with self.assertRaises(DeliveryOrderError) as ctx:
                generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-01"})

        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REQUIRES_REVIEW")
        self.assertIn("Stock insuficiente en deposito VAL", str(ctx.exception))
        self.assertEqual(fake_client.ingresar_count, 1)
        self.assertEqual(fake_client.lookup_count, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.remito_number, o.bejerman_remito_group_id, g.status,
                       g.response_summary ->> 'reviewRequired'
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
            group_id = row[1]
            cur.execute(
                """
                SELECT phase, status
                  FROM bejerman_integration_attempts
                 WHERE group_id = %s
                 ORDER BY id
                """,
                [group_id],
            )
            attempts = cur.fetchall()
        self.assertEqual(row[0], None)
        self.assertTrue(group_id)
        self.assertEqual(row[2], "failed_review")
        self.assertEqual(row[3], "true")
        self.assertIn(("sending", "sdk_request"), attempts)
        self.assertIn(("sdk_error", "sent_unknown"), attempts)
        self.assertIn(("failed_review", "requires_review"), attempts)

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client):
            with self.assertRaises(DeliveryOrderError) as retry_ctx:
                generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-01"})

        self.assertEqual(retry_ctx.exception.code, "BEJERMAN_REMITO_REQUIRES_REVIEW")
        self.assertEqual(fake_client.ingresar_count, 1)
        self.assertEqual(fake_client.lookup_count, 2)

    def test_revision_verifica_sin_emitir_corrige_y_reconcilia_de_nuevo_antes_de_reemitir(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-safe-review",
                    "orderNumber": "OE-00105",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "MGBIO",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Mascara buconasal",
                            "articleRequiresPartida": True,
                            "description": "Mascara buconasal",
                            "quantity": 1,
                            "unitPrice": "29",
                            "partidas": [{"partida": "D2507112H", "assignedQuantity": 1, "stockDepositCode": "VAL"}],
                        }
                    ],
                },
                self.jefe.id,
            )

        class SafeReviewClient:
            def __init__(self):
                self.ingresar_count = 0
                self.lookup_count = 0

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "6",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Mascara buconasal",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def list_comprobantes_ventas(self, filters):
                self.lookup_count += 1
                return {"DatosJSON": []}

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                self.ingresar_count += 1
                if self.ingresar_count == 1:
                    raise BejermanSdkResponseError(
                        "El comprobante no se importó. Se ha superado el Máximo para Ajustar diferencias permitido."
                    )
                return {
                    "Resultado": "OK",
                    "DatosJSON": {
                        "Comprobante_Tipo": "RT",
                        "Comprobante_Letra": "R",
                        "Comprobante_PtoVenta": "00007",
                        "Comprobante_Numero": "00000106",
                    },
                }

        fake_client = SafeReviewClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client):
            with self.assertRaises(DeliveryOrderError):
                generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-13"})
            with connection.cursor() as cur:
                cur.execute("SELECT bejerman_remito_group_id FROM delivery_orders WHERE id = %s", [order["id"]])
                old_group_id = cur.fetchone()[0]

            verify_result = verify_bejerman_remito_review(old_group_id, self.jefe.id)
            self.assertEqual(verify_result["reviewState"], "ready_for_correction")
            self.assertEqual(fake_client.ingresar_count, 1)

            with self.assertRaises(DeliveryOrderError) as unchanged_ctx:
                reissue_corrected_bejerman_remito_review(
                    old_group_id,
                    self.jefe.id,
                    {"confirm": True, "issueDate": "2026-07-13"},
                )
            self.assertEqual(unchanged_ctx.exception.code, "BEJERMAN_REMITO_PAYLOAD_UNCHANGED")
            self.assertEqual(fake_client.ingresar_count, 1)

            editable = get_delivery_order(order["id"], include_events=False)
            edited_items = [{**item, "unitPrice": "30"} for item in editable["items"]]
            with self._delivery_partida_stock_patch(
                [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
            ):
                update_delivery_order(order["id"], {**editable, "items": edited_items}, self.jefe.id)

            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_remito_groups
                       SET response_summary = response_summary || %s::jsonb
                     WHERE id = %s
                    """,
                    [
                        json.dumps(
                            {
                                "reissueInProgress": True,
                                "reissueClaimedAt": timezone.now().isoformat(),
                            }
                        ),
                        old_group_id,
                    ],
                )
            with self.assertRaises(DeliveryOrderError) as concurrent_ctx:
                reissue_corrected_bejerman_remito_review(
                    old_group_id,
                    self.jefe.id,
                    {"confirm": True, "issueDate": "2026-07-13"},
                )
            self.assertEqual(concurrent_ctx.exception.code, "BEJERMAN_REMITO_REISSUE_CONFLICT")
            self.assertEqual(fake_client.ingresar_count, 1)
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_remito_groups
                       SET response_summary = response_summary || %s::jsonb
                     WHERE id = %s
                    """,
                    [
                        json.dumps({"reissueClaimedAt": (timezone.now() - timedelta(minutes=16)).isoformat()}),
                        old_group_id,
                    ],
                )

            result = reissue_corrected_bejerman_remito_review(
                old_group_id,
                self.jefe.id,
                {"confirm": True, "issueDate": "2026-07-13"},
            )

        self.assertEqual(fake_client.lookup_count, 4)
        self.assertEqual(fake_client.ingresar_count, 2)
        self.assertEqual(result["remitoNumber"], "RT R 00007-00000106")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT response_summary ->> 'reviewState',
                       response_summary ->> 'replacementGroupId',
                       response_summary ->> 'replacementRemitoNumber',
                       response_summary ->> 'reissueInProgress'
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [old_group_id],
            )
            old_group = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM delivery_order_events
                 WHERE order_id = %s
                   AND event_type IN ('bejerman_remito_correction_saved', 'bejerman_remito_reissued_after_review')
                """,
                [order["id"]],
            )
            audit_events = int(cur.fetchone()[0])
        self.assertEqual(old_group[0], "superseded")
        self.assertTrue(old_group[1])
        self.assertEqual(old_group[2], "RT R 00007-00000106")
        self.assertEqual(old_group[3], "false")
        self.assertEqual(audit_events, 2)

    def test_revision_acepta_fallback_sql_exitoso_sin_candidatos_y_habilita_correccion(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-review-sql-empty",
                    "orderNumber": "OE-REVIEW-SQL-EMPTY",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "MGBIO",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Máscara buconasal",
                            "articleRequiresPartida": True,
                            "description": "Máscara buconasal",
                            "quantity": 1,
                            "unitPrice": "29",
                            "partidas": [
                                {
                                    "partida": "D2507112H",
                                    "assignedQuantity": 1,
                                    "stockDepositCode": "VAL",
                                }
                            ],
                        }
                    ],
                },
                self.jefe.id,
            )

        class BrokenSdkLookupClient:
            def __init__(self):
                self.ingresar_count = 0

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clínica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "6",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Máscara buconasal",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                self.ingresar_count += 1
                raise BejermanSdkResponseError(
                    "El comprobante no se importó. Se ha superado el Máximo para Ajustar diferencias permitido."
                )

            def list_comprobantes_ventas(self, filters):
                raise BejermanSdkResponseError("Invalid column name 'cvetop_Cod'.")

        def successful_empty_remote_lookup(*args, lookup_outcome=None, **kwargs):
            if lookup_outcome is not None:
                lookup_outcome.update({"status": "success", "rowCount": 0})
            return []

        fake_client = BrokenSdkLookupClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client):
            with patch(
                "service.bejerman_delivery._remote_sales_recovery_records",
                side_effect=successful_empty_remote_lookup,
            ):
                with self.assertRaises(DeliveryOrderError):
                    generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-13"})
                with connection.cursor() as cur:
                    cur.execute("SELECT bejerman_remito_group_id FROM delivery_orders WHERE id = %s", [order["id"]])
                    group_id = cur.fetchone()[0]

                result = verify_bejerman_remito_review(group_id, self.jefe.id)

        self.assertEqual(result["reviewState"], "ready_for_correction")
        self.assertEqual(result["failureCertainty"], "confirmed_not_imported")
        self.assertEqual(fake_client.ingresar_count, 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.bejerman_remito_group_id,
                       g.status,
                       g.response_summary->>'reviewState',
                       g.response_summary->>'failureCertainty'
                  FROM delivery_orders o
                  CROSS JOIN bejerman_remito_groups g
                 WHERE o.id = %s
                   AND g.id = %s
                """,
                [order["id"], group_id],
            )
            state = cur.fetchone()
        self.assertEqual(state, (None, "failed_review", "ready_for_correction", "confirmed_not_imported"))

    def test_remito_bejerman_revision_bloquea_reemision_forzada_sin_perder_auditoria(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-review-force",
                    "orderNumber": "OE-00072",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Mascara buconasal",
                            "articleRequiresPartida": True,
                            "description": "Mascara buconasal",
                            "quantity": 20,
                            "unitPrice": "29",
                            "partidas": [{"partida": "D2507112H", "assignedQuantity": 20, "stockDepositCode": "VAL"}],
                        }
                    ],
                },
                self.jefe.id,
            )

        class ForceReissueClient:
            def __init__(self):
                self.ingresar_count = 0
                self.lookup_count = 0

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Mascara buconasal",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                self.ingresar_count += 1
                raise BejermanSdkResponseError("Stock insuficiente en deposito VAL")

            def list_comprobantes_ventas(self, filters):
                self.lookup_count += 1
                return {"DatosJSON": []}

        fake_client = ForceReissueClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client):
            with self.assertRaises(DeliveryOrderError):
                generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-01"})

            with connection.cursor() as cur:
                cur.execute("SELECT bejerman_remito_group_id FROM delivery_orders WHERE id = %s", [order["id"]])
                old_group_id = cur.fetchone()[0]

            with self.assertRaises(DeliveryOrderError) as force_ctx:
                generate_bejerman_remito(
                    [order["id"]],
                    self.jefe.id,
                    {"issueDate": "2026-07-01", "forceReviewedReissue": True},
                )

        self.assertEqual(force_ctx.exception.code, "BEJERMAN_REMITO_REQUIRES_REVIEW")
        self.assertEqual(fake_client.ingresar_count, 1)
        self.assertEqual(fake_client.lookup_count, 2)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT g.status, g.response_summary ->> 'reviewRequired'
                  FROM bejerman_remito_groups g
                 WHERE g.id = %s
                """,
                [old_group_id],
            )
            old_group = cur.fetchone()
            cur.execute(
                """
                SELECT o.remito_number, o.bejerman_remito_group_id, g.status
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            current = cur.fetchone()
            cur.execute(
                """
                SELECT phase, status
                  FROM bejerman_integration_attempts
                 WHERE group_id = %s
                   AND phase = 'review_reissue_blocked'
                """,
                [old_group_id],
            )
            blocked_attempt = cur.fetchone()
        self.assertEqual(old_group, ("failed_review", "true"))
        self.assertEqual(current[0], None)
        self.assertEqual(current[1], old_group_id)
        self.assertEqual(current[2], "failed_review")
        self.assertEqual(blocked_attempt, ("review_reissue_blocked", "manual_review_required"))

    def test_remito_bejerman_no_adopta_candidatos_ambiguos(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-review-ambiguous",
                    "orderNumber": "OE-00071",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Mascara buconasal",
                            "articleRequiresPartida": True,
                            "description": "Mascara buconasal",
                            "quantity": 20,
                            "unitPrice": "29",
                            "partidas": [{"partida": "D2507112H", "assignedQuantity": 20, "stockDepositCode": "VAL"}],
                        }
                    ],
                },
                self.jefe.id,
            )

        class AmbiguousAfterSendClient:
            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Mascara buconasal",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                raise BejermanSdkResponseError("Timeout SOAP")

            def list_comprobantes_ventas(self, filters):
                return {
                    "DatosJSON": [
                        {
                            "Comprobante_ID": "4739",
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00004739",
                            "Comprobante_FechaEmision": "2026-07-01",
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Comprobante_TipoOperacion": "MC",
                        },
                        {
                            "Comprobante_ID": "4740",
                            "Comprobante_Tipo": "RT",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00004740",
                            "Comprobante_FechaEmision": "2026-07-01",
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Comprobante_TipoOperacion": "MC",
                        },
                    ]
                }

            def detalle_comprobante_ventas(self, comprobante_id):
                return {
                    "DatosJSON": {
                        "Comprobante_Tipo": "RT",
                        "Comprobante_Letra": "R",
                        "Comprobante_PtoVenta": "00004",
                        "Comprobante_Numero": "0000" + str(comprobante_id),
                        "Comprobante_FechaEmision": "2026-07-01",
                        "Cliente_Codigo": "CLI-NOTIF",
                        "Comprobante_TipoOperacion": "MC",
                        "Comprobante_Items": [
                            {"Item_Tipo": "L", "Item_DescripArticulo": "Orden OE-00071"},
                            {
                                "Item_Tipo": "A",
                                "Item_CodigoArticulo": "1202014",
                                "Item_CantidadUM1": 20,
                                "Item_Deposito": "VAL",
                                "Item_Partida": "D2507112H",
                            },
                        ],
                    }
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=AmbiguousAfterSendClient()):
            with self.assertRaises(DeliveryOrderError) as ctx:
                generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-01"})

        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REQUIRES_REVIEW")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.remito_number, g.status
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM bejerman_integration_attempts a
                  JOIN delivery_orders o ON o.bejerman_remito_group_id = a.group_id
                 WHERE o.id = %s
                   AND a.phase = 'recovery_lookup'
                   AND a.status = 'ambiguous'
                """,
                [order["id"]],
            )
            ambiguous_attempts = int(cur.fetchone()[0])
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "failed_review")
        self.assertEqual(ambiguous_attempts, 1)

    def test_remito_bejerman_fallback_sql_no_adopta_duplicado_nexora_exacto(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "1202014", "depositCode": "VAL", "partida": "D2507112H", "availableQuantity": 20}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-remito-sql-fallback",
                    "orderNumber": "OE-SQL-FALLBACK",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "companyKey": "SEPID",
                    "sellerName": "Ventas",
                    "sellerCode": "EZE",
                    "status": "armado_pendiente_entrega",
                    "items": [
                        {
                            "articleCode": "1202014",
                            "articleName": "Mascara buconasal",
                            "articleRequiresPartida": True,
                            "description": "Mascara buconasal",
                            "quantity": 20,
                            "unitPrice": "29",
                            "partidas": [{"partida": "D2507112H", "assignedQuantity": 20, "stockDepositCode": "VAL"}],
                        }
                    ],
                },
                self.jefe.id,
            )

        class ListadoRotoClient:
            def __init__(self):
                self.calls = []

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def list_articulos(self, filters=None, limit=20):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1202014",
                            "Articulo_Descripcion": "Mascara buconasal",
                            "Art_TipoTasaVentas": "1",
                            "Art_CodTasaIVAVentas": "01",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                self.calls.append(("ingresar_comprobante_ventas_json", comprobante))
                raise BejermanSdkResponseError("Timeout SOAP")

            def list_comprobantes_ventas(self, filters):
                self.calls.append(("list_comprobantes_ventas", filters))
                raise BejermanSdkResponseError("Invalid column name 'cvetop_Cod'.")

        remote_records = [
            {
                "Comprobante_ID": "17958",
                "Comprobante_Tipo": "RT",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00004",
                "Comprobante_Numero": "00004740",
                "Comprobante_FechaEmision": "2026-07-01",
                "Cliente_Codigo": "CLI-NOTIF",
                "Comprobante_TipoOperacion": "MC",
                "Comprobante_Mensaje": "NEXORA brg-anterior Ordenes OE-SQL-FALLBACK",
                "Item_CodigoArticulo": "1202014",
                "Item_Partida": "D2507112H",
                "Recovery_Text": "NEXORA OE-SQL-FALLBACK 1202014 D2507112H",
            },
            {
                "Comprobante_ID": "17959",
                "Comprobante_Tipo": "RT",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00004",
                "Comprobante_Numero": "00004741",
                "Comprobante_FechaEmision": "2026-07-01",
                "Cliente_Codigo": "CLI-NOTIF",
                "Comprobante_TipoOperacion": "MC",
                "Comprobante_Mensaje": "NEXORA brg-reintento Ordenes OE-SQL-FALLBACK",
                "Item_CodigoArticulo": "1202014",
                "Item_Partida": "D2507112H",
                "Recovery_Text": "NEXORA OE-SQL-FALLBACK 1202014 D2507112H",
            },
        ]
        fake_client = ListadoRotoClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=fake_client):
            with patch("service.bejerman_delivery._remote_sales_recovery_records", return_value=remote_records) as remote_lookup:
                with self.assertRaises(DeliveryOrderError) as ctx:
                    generate_bejerman_remito([order["id"]], self.jefe.id, {"issueDate": "2026-07-01"})

        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REQUIRES_REVIEW")
        self.assertEqual([call[0] for call in fake_client.calls].count("ingresar_comprobante_ventas_json"), 1)
        remote_lookup.assert_called_once()
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.remito_number, g.status, g.response_summary->>'recoverySource'
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM bejerman_integration_attempts a
                  JOIN delivery_orders o ON o.bejerman_remito_group_id = a.group_id
                 WHERE o.id = %s
                   AND a.phase = 'recovery_lookup'
                   AND a.status = 'ambiguous'
                """,
                [order["id"]],
            )
            ambiguous_attempts = int(cur.fetchone()[0])
        self.assertEqual(row, (None, "failed_review", None))
        self.assertEqual(ambiguous_attempts, 1)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_remito_rtn_emitido_queda_no_facturable_sin_notificar_cobranzas(self):
        mail.outbox = []
        with self._delivery_partida_stock_patch(
            [{"articleCode": "ART-DEMO", "depositCode": "VAL", "partida": "P-DEMO", "availableQuantity": 1}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-rtn-no-billing",
                    "orderNumber": "OE-RTN-NO-BILLING",
                    "customerId": self.customer_id,
                    "deliveryType": "demo",
                    "companyKey": "SEPID",
                    "sellerName": "Demo",
                    "status": "armado_pendiente_entrega",
                    "items": [{"articleCode": "ART-DEMO", "description": "Equipo demo", "quantity": 1, "partida": "P-DEMO"}],
                },
                self.jefe.id,
            )

        sent_comprobantes = []

        class FakeRtnClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_clientes(self):
                return {
                    "DatosJSON": [
                        {
                            "Cliente_Codigo": "CLI-NOTIF",
                            "Cliente_RazonSocial": "Clinica Notificaciones",
                            "Cliente_NroDocumento": "30700000000",
                            "Cliente_Provincia": "02",
                            "Cliente_TipoDocumento": "1",
                            "Cliente_SitIVA": "1",
                        }
                    ]
                }

            def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
                sent_comprobantes.append(comprobante)
                return {
                    "Resultado": "OK",
                    "DatosJSON": json.dumps(
                        {
                            "Comprobante_Tipo": "RTN",
                            "Comprobante_Letra": "R",
                            "Comprobante_PtoVenta": "00004",
                            "Comprobante_Numero": "00004567",
                        }
                    ),
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", FakeRtnClient):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(sent_comprobantes[0]["Comprobante_Tipo"], "RTN")
        self.assertFalse(result["billingRequired"])
        self.assertEqual(result["orders"][0]["status"], "armado_pendiente_entrega")
        self.assertIsNone(result["orders"][0]["deliveredAt"])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.status, g.response_summary ->> 'billingRequired'
                  FROM delivery_orders o
                  JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.id = %s
                """,
                [order["id"]],
            )
            self.assertEqual(cur.fetchone(), ("armado_pendiente_entrega", "false"))
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'sales_order_remito_ready'
                   AND title LIKE %s
                """,
                ["%OE-RTN-NO-BILLING%"],
            )
            self.assertEqual(cur.fetchone()[0], 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_remito_cargado_notifica_cobranzas(self):
        mail.outbox = []
        order = create_delivery_order(
            {
                "id": "do-notif-remito",
                "orderNumber": "OE-NOTIF-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "rawPedido": "Detalle completo de entrega para facturar.",
                "items": [
                    {"articleCode": "ART-001", "description": "Equipo de prueba", "quantity": 2, "unitPrice": "1000"},
                    {"articleCode": "ART-002", "description": "Accesorio sin precio", "quantity": 1},
                ],
            },
            self.jefe.id,
        )

        mark_delivered(order["id"], self.jefe.id, "RT 0002-00012345")

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, title, href
                  FROM notifications
                 WHERE notification_key = 'sales_order_remito_ready'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [(self.cobranzas.id, "Entrega lista para facturar OE-NOTIF-002", "/cobranzas/facturacion?orderId=do-notif-remito")],
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["cobranzas@notif.test"])
        self.assertIn("RT 0002-00012345", message.body)
        self.assertIn("OE-NOTIF-002", message.body)
        self.assertIn("ART-001", message.body)
        self.assertIn("$ 2.000,00", message.body)
        self.assertIn("ítem(s) sin precio", message.body)
        self.assertIn("https://nexora.test/cobranzas/facturacion?orderId=do-notif-remito", message.body)

        mark_delivered(order["id"], self.jefe.id, "RT 0002-00012345")
        self.assertEqual(len(mail.outbox), 1)

    def test_entrega_con_numero_sin_prefijo_respeta_facturacion_del_grupo_bejerman(self):
        order = self._create_commercial_correction_order(
            order_id="do-remito-grupo-facturable",
            status="armado_pendiente_entrega",
            remito_number="0001-00012345",
            billing_required=True,
        )
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE delivery_orders SET status = 'armado_pendiente_entrega' WHERE id = %s",
                [order["id"]],
            )

        delivered = mark_delivered(order["id"], self.jefe.id, "0001-00012345")

        self.assertEqual(delivered["status"], "entregado_pendiente_facturacion")
        pending = list_delivery_orders({"pendingBilling": True, "limit": 10})
        self.assertIn(order["id"], [item["id"] for item in pending["items"]])

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_supervisor_corrige_precio_plazo_solo_en_nexora_y_notifica_cobranzas(self):
        order = self._create_commercial_correction_order()
        mail.outbox = []
        item = order["items"][0]
        self.client.force_authenticate(user=self.supervisor)

        with patch(
            "service.delivery_orders.perform_bejerman_remito_price_fix",
            create=True,
            side_effect=AssertionError("La corrección local no debe consultar ni modificar Bejerman"),
        ) as bejerman_price_fix:
            resp = self.client.patch(
                f"/api/ordenes-entrega/{order['id']}/correccion-comercial/",
                {
                    "commercialDeadline": "30 días",
                    "items": [
                        {
                            "id": item["id"],
                            "unitPrice": "1500.00",
                            "priceCurrency": "ARS",
                            "discountPercent": "10",
                        }
                    ],
                },
                format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        bejerman_price_fix.assert_not_called()
        self.assertEqual(resp.data["commercialDeadline"], "30 días")
        self.assertEqual(Decimal(str(resp.data["items"][0]["unitPrice"])), Decimal("1500.00"))
        self.assertEqual(Decimal(str(resp.data["items"][0]["discountPercent"])), Decimal("10.00"))
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT commercial_deadline, price_currency
                  FROM delivery_orders
                 WHERE id = %s
                """,
                [order["id"]],
            )
            self.assertEqual(cur.fetchone(), ("30 días", "ARS"))
            cur.execute(
                """
                SELECT unit_price, discount_percent, price_currency
                  FROM delivery_order_items
                 WHERE id = %s
                """,
                [item["id"]],
            )
            unit_price, discount_percent, currency = cur.fetchone()
            self.assertEqual(unit_price, Decimal("1500.00"))
            self.assertEqual(discount_percent, Decimal("10.00"))
            self.assertEqual(currency, "ARS")
            cur.execute(
                """
                SELECT actor_user_id, metadata
                  FROM delivery_order_events
                 WHERE order_id = %s
                   AND event_type = 'delivery_order_commercial_correction'
                """,
                [order["id"]],
            )
            event_actor_user_id, metadata = cur.fetchone()
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            cur.execute(
                """
                SELECT user_id, title, href
                  FROM notifications
                 WHERE notification_key = 'sales_order_commercial_terms_updated'
                """,
            )
            notification_rows = cur.fetchall()
        self.assertEqual(metadata["previousDeadline"], "15 días")
        self.assertEqual(metadata["newDeadline"], "30 días")
        self.assertEqual(event_actor_user_id, self.supervisor.id)
        self.assertEqual(metadata["remitoNumber"], order["remitoNumber"])
        self.assertEqual(metadata["correctionScope"], "nexora_only")
        self.assertFalse(metadata["bejermanRemitoModified"])
        self.assertEqual(Decimal(metadata["items"][0]["previousUnitPrice"]), Decimal("1000.00"))
        self.assertEqual(Decimal(metadata["items"][0]["newUnitPrice"]), Decimal("1500.00"))
        self.assertEqual(Decimal(metadata["items"][0]["previousDiscountPercent"]), Decimal("0"))
        self.assertEqual(Decimal(metadata["items"][0]["newDiscountPercent"]), Decimal("10"))
        self.assertEqual(
            notification_rows,
            [(self.cobranzas.id, f"Corrección comercial en {order['orderNumber']}", f"/cobranzas/facturacion?orderId={order['id']}")],
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["cobranzas@notif.test"])
        self.assertIn(order["remitoNumber"], message.body)
        self.assertIn("30 días", message.body)
        self.assertIn("el RT emitido en Bejerman no fue modificado", message.body)
        self.assertIn("https://nexora.test/cobranzas/facturacion?orderId=do-commercial-correction", message.body)

    def test_correccion_comercial_local_permite_remito_pendiente_sin_grupo_bejerman(self):
        order = self._create_commercial_correction_order(
            order_id="do-commercial-without-bejerman-group",
            status="armado_pendiente_entrega",
            attach_bejerman_group=False,
        )
        item = order["items"][0]
        self.assertFalse(order["bejermanRemitoGroupId"])
        self.client.force_authenticate(user=self.supervisor)

        with patch(
            "service.delivery_orders.perform_bejerman_remito_price_fix",
            create=True,
            side_effect=AssertionError("La corrección local no debe consultar ni modificar Bejerman"),
        ) as bejerman_price_fix:
            resp = self.client.patch(
                f"/api/ordenes-entrega/{order['id']}/correccion-comercial/",
                {
                    "commercialDeadline": "45 días",
                    "items": [
                        {
                            "id": item["id"],
                            "unitPrice": "1750.00",
                            "priceCurrency": "ARS",
                            "discountPercent": "0",
                        }
                    ],
                },
                format="json",
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        bejerman_price_fix.assert_not_called()
        self.assertEqual(resp.data["commercialDeadline"], "45 días")
        self.assertEqual(Decimal(str(resp.data["items"][0]["unitPrice"])), Decimal("1750.00"))

    def test_correccion_comercial_local_permite_rt_con_billing_required_false(self):
        order = self._create_commercial_correction_order(
            order_id="do-commercial-rt-billing-false",
            status="armado_pendiente_entrega",
            billing_required=False,
        )
        item = order["items"][0]
        self.assertFalse(order["bejermanRemitoGroup"]["responseSummary"]["billingRequired"])
        self.client.force_authenticate(user=self.supervisor)

        resp = self.client.patch(
            f"/api/ordenes-entrega/{order['id']}/correccion-comercial/",
            {
                "commercialDeadline": "30 días",
                "items": [
                    {
                        "id": item["id"],
                        "unitPrice": "1495.50",
                        "priceCurrency": "ARS",
                        "discountPercent": "0",
                    }
                ],
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["commercialDeadline"], "30 días")
        self.assertEqual(Decimal(str(resp.data["items"][0]["unitPrice"])), Decimal("1495.50"))

    def test_correccion_comercial_rechaza_roles_no_supervisor(self):
        roles = [self.admin, self.jefe, self.ventas, self.recepcion, self.cobranzas]
        for user in roles:
            with self.subTest(role=user.rol):
                order = self._create_commercial_correction_order(order_id=f"do-commercial-role-{user.rol}")
                item = order["items"][0]
                self.client.force_authenticate(user=user)
                resp = self.client.patch(
                    f"/api/ordenes-entrega/{order['id']}/correccion-comercial/",
                    {
                        "commercialDeadline": "30 días",
                        "items": [
                            {
                                "id": item["id"],
                                "unitPrice": "1500.00",
                                "priceCurrency": "ARS",
                                "discountPercent": "0",
                            }
                        ],
                    },
                    format="json",
                )
                self.assertEqual(resp.status_code, 403, resp.data)

    def test_correccion_comercial_rechaza_orden_facturada_o_sin_remito(self):
        cases = [
            ("do-commercial-invoiced", "entregado_pendiente_facturacion", "RT R 00004-00012345", "FC A 00004-00000001"),
            ("do-commercial-no-remito", "entregado_pendiente_facturacion", "", None),
            ("do-commercial-cancelled", "cancelado", "RT R 00004-00012345", None),
        ]
        self.client.force_authenticate(user=self.supervisor)
        for order_id, status, remito_number, invoice_number in cases:
            with self.subTest(order_id=order_id):
                order = self._create_commercial_correction_order(
                    order_id=order_id,
                    status=status,
                    remito_number=remito_number,
                    invoice_number=invoice_number,
                )
                item = order["items"][0]
                resp = self.client.patch(
                    f"/api/ordenes-entrega/{order['id']}/correccion-comercial/",
                    {
                        "commercialDeadline": "30 días",
                        "items": [
                            {
                                "id": item["id"],
                                "unitPrice": "1500.00",
                                "priceCurrency": "ARS",
                                "discountPercent": "0",
                            }
                        ],
                    },
                    format="json",
                )
                self.assertEqual(resp.status_code, 409, resp.data)

    def test_correccion_comercial_rechaza_cambios_fuera_de_precio_plazo(self):
        order = self._create_commercial_correction_order()
        item = order["items"][0]
        self.client.force_authenticate(user=self.supervisor)
        resp = self.client.patch(
            f"/api/ordenes-entrega/{order['id']}/correccion-comercial/",
            {
                "commercialDeadline": "30 días",
                "items": [
                    {
                        "id": item["id"],
                        "unitPrice": "1500.00",
                        "priceCurrency": "ARS",
                        "discountPercent": "0",
                        "quantity": "99",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409, resp.data)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_remito_rss_cargado_queda_no_facturable_sin_notificar_cobranzas(self):
        mail.outbox = []
        order = self._create_service_release_order("do-rss-manual-no-billing")

        delivered = mark_delivered(order["id"], self.jefe.id, "RSS R 00004-00001234")

        self.assertEqual(delivered["status"], "entregado_no_facturable")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'sales_order_remito_ready'
                   AND title LIKE %s
                """,
                ["%do-rss-manual-no-billing%"],
            )
            self.assertEqual(cur.fetchone()[0], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_entrega_rechaza_remito_bejerman_en_revision(self):
        order = create_delivery_order(
            {
                "id": "do-remito-review-deliver",
                "orderNumber": "OE-REVIEW-DELIVER",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "status": "armado_pendiente_entrega",
                "remitoNumber": "RT R 00004-00004741",
                "items": [{"articleCode": "1202014", "description": "Artículo", "quantity": 1}],
            },
            self.jefe.id,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                    remito_number, customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, order_ids, status, response_summary
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
                """,
                [
                    "brg-review-deliver",
                    "RT",
                    "R",
                    "00004",
                    "00004741",
                    "RT R 00004-00004741",
                    "CLI-NOTIF",
                    "Clínica Notificaciones",
                    "ADM",
                    "30",
                    "MC",
                    "VAL",
                    json.dumps([order["id"]]),
                    "failed_review",
                    json.dumps({"reviewRequired": True}),
                ],
            )
            cur.execute(
                "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = %s",
                ["brg-review-deliver", order["id"]],
            )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_delivered(order["id"], self.jefe.id, None)

        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REQUIRES_REVIEW")

    def test_historial_remitos_filtra_por_cliente_numero_y_orden(self):
        home_order = create_delivery_order(
            {
                "id": "do-history-home-air",
                "orderNumber": "OE-00072",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "operationCompanyLabel": "MGBIO",
                "status": "armado_pendiente_entrega",
                "items": [{"articleCode": "1202014", "description": "Artículo HOME", "quantity": 1}],
            },
            self.jefe.id,
        )
        service_order = create_delivery_order(
            {
                "id": "do-history-os-29342",
                "orderNumber": "OS-29342",
                "customerId": self.customer_id,
                "deliveryType": "service_release",
                "operationCompanyLabel": "SEPID",
                "status": "armado_pendiente_entrega",
                "items": [{"articleCode": "1118002", "description": "Artículo servicio", "quantity": 1}],
            },
            self.jefe.id,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    comprobante_numero, remito_number, customer_code, customer_name, seller_code,
                    payment_term_code, operation_code, deposit_code, order_ids, status, response_summary, generated_at
                )
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, CURRENT_TIMESTAMP),
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, CURRENT_TIMESTAMP - INTERVAL '1 minute')
                """,
                [
                    "brg-history-home-air",
                    "MGBIO",
                    "RT",
                    "R",
                    "00007",
                    "00000665",
                    "RT R 00007-00000665",
                    "HAIR",
                    "HOME AIR S.R.L.",
                    "ADM",
                    "30",
                    "MC",
                    "VAL",
                    json.dumps([home_order["id"]]),
                    "generated",
                    json.dumps({}),
                    "brg-history-service",
                    "SEPID",
                    "RSS",
                    "R",
                    "00004",
                    "00004743",
                    "RSS R 00004-00004743",
                    "NOV",
                    "NOVAMED S.A.",
                    "ADM",
                    "30",
                    "REP",
                    "STC",
                    json.dumps([service_order["id"]]),
                    "reconciled",
                    json.dumps({"bridgeRequest": {"orders": [{"orderNumber": "OS-29342"}]}}),
                ],
            )

        by_customer = list_remito_history({"search": "HOME AIR", "pageSize": "10"})
        by_number = list_remito_history({"search": "00000665", "pageSize": "10"})
        by_order = list_remito_history({"search": "OS-29342", "pageSize": "10"})

        self.assertEqual([item["id"] for item in by_customer], ["brg-history-home-air"])
        self.assertEqual([item["id"] for item in by_number], ["brg-history-home-air"])
        self.assertEqual([item["id"] for item in by_order], ["brg-history-service"])

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_os_lista_facturar_notifica_cobranzas_y_envia_pdf(self):
        mail.outbox = []
        self._create_service_release_order("do-os-bill-notice")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = 'RSS R 00004-00001234',
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )

        with self.captureOnCommitCallbacks(execute=True):
            result = notify_service_order_ready_to_bill(self.ingreso_id, actor_name="Recepcion")

        self.assertEqual(result["notifications"], 1)
        self.assertEqual(result["emails"], 1)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, title, href
                  FROM notifications
                 WHERE notification_key = 'service_order_ready_to_bill'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(
            rows,
            [
                (
                    self.cobranzas.id,
                    f"OS lista para facturar - OS {str(self.ingreso_id).zfill(5)}",
                    f"/cobranzas/facturacion?serviceOrderId={self.ingreso_id}",
                )
            ],
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["cobranzas@notif.test"])
        self.assertIn(f"OS lista para facturar - OS {str(self.ingreso_id).zfill(5)}", message.subject)
        self.assertIn("Facturar en Bejerman como Concepto de servicio al 21%", message.body)
        self.assertIn("No facturar desde la RSS", message.body)
        self.assertIn("1115003", message.body)
        self.assertIn(f"https://nexora.test/cobranzas/facturacion?serviceOrderId={self.ingreso_id}", message.body)
        self.assertEqual(len(message.attachments), 1)
        self.assertTrue(message.attachments[0][1].startswith(b"%PDF-"))

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_os_lista_facturar_deduplica_mail_y_notificacion(self):
        mail.outbox = []
        self._create_service_release_order("do-os-bill-dedupe")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='liberado', resolucion='reparado', factura_numero=NULL WHERE id=%s",
                [self.ingreso_id],
            )

        with self.captureOnCommitCallbacks(execute=True):
            notify_service_order_ready_to_bill(self.ingreso_id, actor_name="Recepcion")
        with self.captureOnCommitCallbacks(execute=True):
            result = notify_service_order_ready_to_bill(self.ingreso_id, actor_name="Recepcion")

        self.assertEqual(result["notifications"], 0)
        self.assertEqual(result["emails"], 0)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM notifications WHERE notification_key = 'service_order_ready_to_bill'"
            )
            self.assertEqual(cur.fetchone()[0], 1)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_os_lista_facturar_envia_mail_si_falla_pdf(self):
        mail.outbox = []
        self._create_service_release_order("do-os-bill-pdf-fails")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='liberado', resolucion='reparado', factura_numero=NULL WHERE id=%s",
                [self.ingreso_id],
            )

        with patch("service.service_order_billing.logger"):
            with patch("service.service_order_billing.render_service_order_billing_pdf", side_effect=RuntimeError("boom")):
                with self.captureOnCommitCallbacks(execute=True):
                    result = notify_service_order_ready_to_bill(self.ingreso_id, actor_name="Recepcion")

        self.assertEqual(result["notifications"], 1)
        self.assertEqual(result["emails"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments, [])

    def test_listado_os_a_facturar_y_registro_factura_os(self):
        self._create_service_release_order("do-os-bill-queue")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       resolucion = 'reparado',
                       remito_salida = 'RSS R 00004-00001234',
                       fecha_entrega = CURRENT_TIMESTAMP,
                       factura_numero = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
            cur.execute("SELECT remito_salida, fecha_entrega FROM ingresos WHERE id=%s", [self.ingreso_id])
            before_remito, before_fecha_entrega = cur.fetchone()

        listed = list_service_orders_to_bill({})
        self.assertIn(self.ingreso_id, [item["ingresoId"] for item in listed["items"]])
        item = next(item for item in listed["items"] if item["ingresoId"] == self.ingreso_id)
        self.assertEqual(item["conceptCode"], "1115003")
        self.assertEqual(item["rss"], "RSS R 00004-00001234")

        self.client.force_authenticate(user=self.cobranzas)
        list_resp = self.client.get("/api/cobranzas/os-a-facturar/")
        self.assertEqual(list_resp.status_code, 200)
        self.assertIn(self.ingreso_id, [row["ingresoId"] for row in list_resp.data["items"]])
        pdf_resp = self.client.get(f"/api/cobranzas/os-a-facturar/{self.ingreso_id}/pdf/")
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp["Content-Type"], "application/pdf")
        self.assertTrue(pdf_resp.content.startswith(b"%PDF-"))

        invoice_resp = self.client.post(
            f"/api/cobranzas/os-a-facturar/{self.ingreso_id}/factura/",
            {"facturaNumero": "FC A 0001-00001234"},
            format="json",
        )
        self.assertEqual(invoice_resp.status_code, 200)
        self.assertEqual(invoice_resp.data["facturaNumero"], "FC A 0001-00001234")
        idempotent_resp = self.client.post(
            f"/api/cobranzas/os-a-facturar/{self.ingreso_id}/factura/",
            {"facturaNumero": "FC A 0001-00001234"},
            format="json",
        )
        self.assertEqual(idempotent_resp.status_code, 200)
        conflict_resp = self.client.post(
            f"/api/cobranzas/os-a-facturar/{self.ingreso_id}/factura/",
            {"facturaNumero": "FC A 0001-00009999"},
            format="json",
        )
        self.assertEqual(conflict_resp.status_code, 409)

        with connection.cursor() as cur:
            cur.execute("SELECT remito_salida, fecha_entrega, factura_numero FROM ingresos WHERE id=%s", [self.ingreso_id])
            after_remito, after_fecha_entrega, after_factura = cur.fetchone()
        self.assertEqual(after_remito, before_remito)
        self.assertEqual(after_fecha_entrega, before_fecha_entrega)
        self.assertEqual(after_factura, "FC A 0001-00001234")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT invoice_number, invoiced_by_user_id, invoiced_at
                  FROM delivery_orders
                 WHERE id = %s
                """,
                ["do-os-bill-queue"],
            )
            synced_order = cur.fetchone()
        self.assertEqual(synced_order[0], "FC A 0001-00001234")
        self.assertEqual(synced_order[1], self.cobranzas.id)
        self.assertIsNotNone(synced_order[2])
        listed_after = list_service_orders_to_bill({})
        self.assertNotIn(self.ingreso_id, [item["ingresoId"] for item in listed_after["items"]])

    @patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4\nOS", "remito-os.pdf"))
    @patch("service.views.reportes_views.notify_service_order_ready_to_bill")
    @patch("service.views.reportes_views.ensure_service_release_order_for_ingreso")
    @patch("service.views.reportes_views.notify_ingreso_liberado")
    @patch("service.views.reportes_views.enqueue_client_ready_transfer_for_ingreso")
    @patch("service.views.reportes_views.ingreso_is_demo_return", return_value=False)
    @patch("service.views.reportes_views.ingreso_is_internal_equipment", return_value=False)
    def test_remito_salida_liberacion_dispara_aviso_os_a_facturar(
        self,
        _mock_internal,
        _mock_demo,
        _mock_transfer,
        _mock_liberado,
        mock_ensure_release,
        mock_notify_billing,
        _mock_pdf,
    ):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='reparado', resolucion='reparado', factura_numero=NULL WHERE id=%s",
                [self.ingreso_id],
            )

        self.client.force_authenticate(user=self.jefe)
        resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/remito/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        mock_ensure_release.assert_called_once_with(self.ingreso_id, self.jefe.id)
        mock_notify_billing.assert_called_once()
        args, kwargs = mock_notify_billing.call_args
        self.assertEqual(args[0], self.ingreso_id)
        self.assertIn("request", kwargs)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        PUBLIC_WEB_URL="https://nexora.test",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_resumen_diario_cobranzas_envia_pendientes_y_deduplica(self):
        create_delivery_order(
            {
                "id": "do-summary-1",
                "orderNumber": "OE-SUM-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "status": "entregado_pendiente_facturacion",
                "remitoNumber": "RT 0002-00022222",
                "items": [{"articleCode": "ART-SUM", "description": "Equipo resumen", "quantity": 1, "unitPrice": "1500"}],
            },
            self.jefe.id,
        )
        create_delivery_order(
            {
                "id": "do-summary-2",
                "orderNumber": "OE-SUM-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "status": "facturado",
                "remitoNumber": "RT 0002-00033333",
                "invoiceNumber": "FC A 0001-00000001",
                "items": [{"description": "Equipo facturado", "quantity": 1, "unitPrice": "999"}],
            },
            self.jefe.id,
        )
        mail.outbox = []

        out = StringIO()
        call_command("send_billing_pending_notifications", stdout=out)

        self.assertIn("encolados=1", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Hay 1 remito(s) pendiente(s) de facturación.", body)
        self.assertIn("RT 0002-00022222", body)
        self.assertIn("OE-SUM-001", body)
        self.assertIn("$ 1.500,00", body)
        self.assertIn("https://nexora.test/cobranzas/facturacion", body)
        self.assertNotIn("RT 0002-00033333", body)
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notifications WHERE notification_key = 'billing_pending_summary'")
            self.assertEqual(cur.fetchone()[0], 1)

        out = StringIO()
        call_command("send_billing_pending_notifications", stdout=out)
        self.assertIn("omitidos=1", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)

    def test_orden_entrega_preparada_permite_editar_items_y_partidas(self):
        order = create_delivery_order(
            {
                "id": "do-edit-prepared",
                "orderNumber": "OE-EDIT-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "status": "armado_pendiente_entrega",
                "items": [
                    {"id": "doi-edit-keep", "articleCode": "ART-OLD", "description": "Artículo viejo", "quantity": 1},
                    {"id": "doi-edit-delete", "description": "Artículo a quitar", "quantity": 1},
                ],
            },
            self.jefe.id,
        )

        with self._delivery_partida_stock_patch(
            [
                {"articleCode": "ART-NEW", "depositCode": "VAL", "partida": "P-EDIT-1", "availableQuantity": 1, "expirationDate": "2030-12-31"},
                {"articleCode": "ART-NEW", "depositCode": "VAL", "partida": "P-EDIT-2", "availableQuantity": 1, "expirationDate": "2030-12-31"},
            ]
        ):
            updated = update_delivery_order(
                order["id"],
                {
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas editadas",
                    "operationCompanyLabel": "SEPID",
                    "rawPedido": "Pedido editado",
                    "items": [
                        {
                            "id": "doi-edit-keep",
                            "articleCode": "ART-NEW",
                            "articleName": "Artículo editado",
                            "description": "Artículo editado",
                            "quantity": 2,
                            "partidas": [
                                {"partida": "P-EDIT-1", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                {"partida": "P-EDIT-2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                            ],
                        },
                        {
                            "articleCode": "ART-ADD",
                            "articleName": "Artículo agregado",
                            "description": "Artículo agregado",
                            "quantity": 1,
                        },
                    ],
                },
                self.jefe.id,
            )

        self.assertEqual(updated["status"], "armado_pendiente_entrega")
        self.assertEqual(updated["sellerName"], "Ventas editadas")
        self.assertEqual(updated["rawPedido"], "Pedido editado")
        self.assertEqual([item["articleCode"] for item in updated["items"]], ["ART-NEW", "ART-ADD"])
        self.assertEqual(updated["items"][0]["partida"], "")
        self.assertEqual([row["partida"] for row in updated["items"][0]["partidas"]], ["P-EDIT-1", "P-EDIT-2"])
        self.assertEqual(updated["items"][0]["partidas"][0]["stockAvailableQuantity"], 1.0)

    def test_orden_entrega_rechaza_partidas_con_suma_inconsistente(self):
        order = create_delivery_order(
            {
                "id": "do-edit-mismatch",
                "orderNumber": "OE-EDIT-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "items": [{"id": "doi-mismatch", "articleCode": "ART-1", "description": "Artículo", "quantity": 2}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            update_delivery_order(
                order["id"],
                {
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "operationCompanyLabel": "SEPID",
                    "items": [
                        {
                            "id": "doi-mismatch",
                            "articleCode": "ART-1",
                            "description": "Artículo",
                            "quantity": 2,
                            "partidas": [{"partida": "P-UNICA", "assignedQuantity": 1}],
                        }
                    ],
                },
                self.jefe.id,
            )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDAS_QUANTITY_MISMATCH")

    def test_orden_entrega_puede_crearse_sin_partidas_pero_no_prepararse(self):
        order = create_delivery_order(
            {
                "id": "do-prepare-missing-partidas",
                "orderNumber": "OE-PREP-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [{"id": "doi-prepare-missing", "description": "Artículo", "quantity": 1}],
            },
            self.jefe.id,
        )

        self.assertEqual(order["status"], "pendiente_armado")
        self.assertEqual(order["items"][0]["partidas"], [])
        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDAS_REQUIRED")
        self.assertEqual(ctx.exception.status_code, 409)
        with connection.cursor() as cur:
            cur.execute("SELECT status FROM delivery_orders WHERE id = %s", [order["id"]])
            self.assertEqual(cur.fetchone()[0], "pendiente_armado")

    def test_orden_entrega_preparada_permite_articulo_sin_partida_en_bejerman(self):
        order = create_delivery_order(
            {
                "id": "do-prepare-no-partida-article",
                "orderNumber": "OE-PREP-NO-PARTIDA",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "id": "doi-no-partida-article",
                        "articleCode": "1208010",
                        "articleName": "Filtro de aire",
                        "articleRequiresPartida": False,
                        "description": "Filtro de aire",
                        "quantity": 10,
                    }
                ],
            },
            self.jefe.id,
        )

        self.assertIs(order["items"][0]["articleRequiresPartida"], False)
        with patch("service.delivery_orders._delivery_article_available_quantity", return_value=100):
            prepared = mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(prepared["status"], "armado_pendiente_entrega")
        self.assertEqual(prepared["items"][0]["partidas"], [])
        self.assertIs(prepared["items"][0]["articleRequiresPartida"], False)

    def test_orden_entrega_preparada_bloquea_articulo_sin_partida_con_stock_insuficiente(self):
        order = create_delivery_order(
            {
                "id": "do-prepare-no-partida-sin-stock",
                "orderNumber": "OE-PREP-NO-PARTIDA-STOCK",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "id": "doi-no-partida-sin-stock",
                        "articleCode": "1208010",
                        "articleName": "Filtro de aire",
                        "articleRequiresPartida": False,
                        "description": "Filtro de aire",
                        "quantity": 10,
                    }
                ],
            },
            self.jefe.id,
        )

        with patch("service.delivery_orders._delivery_article_available_quantity", return_value=3):
            with self.assertRaises(DeliveryOrderError) as ctx:
                mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_ARTICLE_STOCK_INSUFFICIENT")
        with connection.cursor() as cur:
            cur.execute("SELECT status FROM delivery_orders WHERE id = %s", [order["id"]])
            self.assertEqual(cur.fetchone()[0], "pendiente_armado")

    def test_orden_entrega_preparada_bloquea_articulo_seriado_sin_partida(self):
        order = create_delivery_order(
            {
                "id": "do-prepare-requires-partida",
                "orderNumber": "OE-PREP-REQUIRES-PARTIDA",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [
                    {
                        "id": "doi-requires-partida",
                        "articleCode": "ART-PARTIDA",
                        "articleRequiresPartida": True,
                        "description": "Artículo con partida",
                        "quantity": 1,
                    }
                ],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDAS_REQUIRED")

    def test_orden_entrega_preparada_exige_partidas_completas(self):
        order = create_delivery_order(
            {
                "id": "do-prepare-complete-partidas",
                "orderNumber": "OE-PREP-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [{"id": "doi-prepare-complete", "articleCode": "ART-PREP", "description": "Artículo", "quantity": 2}],
            },
            self.jefe.id,
        )

        with self._delivery_partida_stock_patch(
            [
                {"articleCode": "ART-PREP", "depositCode": "VAL", "partida": "P-001", "availableQuantity": 1, "expirationDate": "2030-12-31"},
                {"articleCode": "ART-PREP", "depositCode": "VAL", "partida": "P-002", "availableQuantity": 1, "expirationDate": "2030-12-31"},
            ]
        ):
            update_item_partidas(
                order["id"],
                "doi-prepare-complete",
                self.recepcion.id,
                [
                    {"partida": "P-001", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                    {"partida": "P-002", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                ],
            )
        prepared = mark_prepared(order["id"], self.recepcion.id)

        self.assertEqual(prepared["status"], "armado_pendiente_entrega")
        self.assertEqual([row["partida"] for row in prepared["items"][0]["partidas"]], ["P-001", "P-002"])

    def test_orden_entrega_rechaza_partida_que_no_figura_en_stock(self):
        order = create_delivery_order(
            {
                "id": "do-partida-missing-stock",
                "orderNumber": "OE-PREP-004",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [{"id": "doi-partida-missing-stock", "articleCode": "ART-STOCK", "description": "Artículo", "quantity": 1}],
            },
            self.jefe.id,
        )

        with self._delivery_partida_stock_patch(
            [{"articleCode": "ART-STOCK", "depositCode": "VAL", "partida": "P-OK", "availableQuantity": 1}]
        ):
            with self.assertRaises(DeliveryOrderError) as ctx:
                update_item_partidas(
                    order["id"],
                    "doi-partida-missing-stock",
                    self.recepcion.id,
                    [{"partida": "P-MISSING", "assignedQuantity": 1, "stockDepositCode": "VAL"}],
                )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDA_NOT_FOUND")

    def test_orden_entrega_rechaza_partidas_duplicadas(self):
        order = create_delivery_order(
            {
                "id": "do-duplicate-partidas",
                "orderNumber": "OE-PREP-003",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "items": [{"id": "doi-duplicate-partidas", "description": "Artículo", "quantity": 2}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            update_item_partidas(
                order["id"],
                "doi-duplicate-partidas",
                self.recepcion.id,
                [
                    {"partida": "P-001", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                    {"partida": "P-001", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                ],
            )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDAS_DUPLICATED")

    def test_orden_entrega_con_remito_o_emision_bejerman_no_se_edita(self):
        with_remito = create_delivery_order(
            {
                "id": "do-edit-remito",
                "orderNumber": "OE-EDIT-003",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "remitoNumber": "RT 0002-00012349",
                "items": [{"description": "Artículo", "quantity": 1}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as remito_ctx:
            update_delivery_order(with_remito["id"], {"items": [{"description": "Editado", "quantity": 1}]}, self.jefe.id)
        self.assertEqual(remito_ctx.exception.code, "DELIVERY_ORDER_LOCKED")

        in_progress = create_delivery_order(
            {
                "id": "do-edit-group",
                "orderNumber": "OE-EDIT-004",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "items": [{"description": "Artículo", "quantity": 1}],
            },
            self.jefe.id,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, comprobante_tipo, customer_code, customer_name, seller_code,
                    payment_term_code, operation_code, deposit_code, order_ids, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    "brg-edit-lock",
                    "RT",
                    "CLI-NOTIF",
                    "Clínica Notificaciones",
                    "ADM",
                    "30",
                    "MC",
                    "VAL",
                    json.dumps([in_progress["id"]]),
                    "pending",
                ],
            )
            cur.execute(
                "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = %s",
                ["brg-edit-lock", in_progress["id"]],
            )

        with self.assertRaises(DeliveryOrderError) as group_ctx:
            update_delivery_order(in_progress["id"], {"items": [{"description": "Editado", "quantity": 1}]}, self.jefe.id)
        self.assertEqual(group_ctx.exception.code, "DELIVERY_ORDER_LOCKED")

    def test_cancelacion_rechaza_orden_entregada(self):
        order = create_delivery_order(
            {
                "id": "do-cancel-delivered",
                "orderNumber": "OE-CAN-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )
        mark_delivered(order["id"], self.jefe.id, "RT 0002-00012348")

        with self.assertRaises(DeliveryOrderError) as ctx:
            cancel_order(order["id"], self.jefe.id)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_DELIVERED")
        with connection.cursor() as cur:
            cur.execute("SELECT status FROM delivery_orders WHERE id = %s", [order["id"]])
            status = cur.fetchone()[0]
        self.assertEqual(status, "entregado_pendiente_facturacion")

    def test_ordenes_entrega_ocultan_canceladas_por_defecto(self):
        active = create_delivery_order(
            {
                "id": "do-list-active",
                "orderNumber": "OE-LST-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo activo", "quantity": 1}],
            },
            self.jefe.id,
        )
        cancelled = create_delivery_order(
            {
                "id": "do-list-cancelled",
                "orderNumber": "OE-LST-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo cancelado", "quantity": 1}],
            },
            self.jefe.id,
        )
        cancel_order(cancelled["id"], self.jefe.id)

        default_result = list_delivery_orders({"limit": 10})
        cancelled_result = list_delivery_orders({"status": "cancelado", "limit": 10})

        self.assertEqual([item["id"] for item in default_result["items"]], [active["id"]])
        self.assertEqual(default_result["total"], 1)
        self.assertEqual([item["id"] for item in cancelled_result["items"]], [cancelled["id"]])

    def test_ordenes_entrega_ordenan_por_creacion_no_por_fecha_de_origen(self):
        sale = create_delivery_order(
            {
                "id": "do-sort-sale",
                "orderNumber": "OE-SRT-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "orderDate": "2026-06-23",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Venta creada antes", "quantity": 1}],
            },
            self.jefe.id,
        )
        release = create_delivery_order(
            {
                "id": "do-sort-release",
                "orderNumber": "OE-SRT-002",
                "customerId": self.customer_id,
                "deliveryType": "service_release",
                "orderDate": "2026-05-29",
                "sourceSystem": "nexora",
                "sourceExternalId": str(self.ingreso_id),
                "sourceReference": f"OS {self.ingreso_id}",
                "ingresoId": self.ingreso_id,
                "equipmentModel": "CPAP | ResMed | AirSense 10",
                "equipmentSerial": "NS-NOTIF-001",
                "equipmentInternalNumber": "MG 9001",
                "sellerName": "Servicio técnico",
                "status": "armado_pendiente_entrega",
                "items": [
                    {
                        "articleCode": "1115003",
                        "articleName": "CPAP | ResMed | AirSense 10",
                        "description": "CPAP | ResMed | AirSense 10",
                        "quantity": 1,
                        "partida": "NS-NOTIF-001",
                    }
                ],
            },
            self.jefe.id,
        )
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE delivery_orders SET created_at = %s::timestamptz WHERE id = %s",
                ["2026-06-23 10:00:00-03", sale["id"]],
            )
            cur.execute(
                "UPDATE delivery_orders SET created_at = %s::timestamptz WHERE id = %s",
                ["2026-06-23 15:28:00-03", release["id"]],
            )

        result = list_delivery_orders({"limit": 10})

        self.assertEqual([item["id"] for item in result["items"][:2]], [release["id"], sale["id"]])

    def test_remitos_pendientes_ordenan_por_emision_y_excluyen_facturados(self):
        imported = create_delivery_order(
            {
                "id": "do-pending-imported",
                "orderNumber": "OE-PEND-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "orderDate": "2026-05-05",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "remitoNumber": "RT 0002-00000001",
                "items": [{"description": "Remito importado", "quantity": 1}],
            },
            self.jefe.id,
        )
        older_generated = create_delivery_order(
            {
                "id": "do-pending-generated-old",
                "orderNumber": "OE-PEND-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "orderDate": "2026-06-20",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "remitoNumber": "RT 0002-00000002",
                "items": [{"description": "Remito generado viejo", "quantity": 1}],
            },
            self.jefe.id,
        )
        newer_generated = create_delivery_order(
            {
                "id": "do-pending-generated-new",
                "orderNumber": "OE-PEND-003",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "orderDate": "2026-04-01",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "remitoNumber": "RT 0002-00000003",
                "items": [{"description": "Remito generado nuevo", "quantity": 1}],
            },
            self.jefe.id,
        )
        already_invoiced = create_delivery_order(
            {
                "id": "do-pending-with-invoice",
                "orderNumber": "OE-PEND-004",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "orderDate": "2026-04-01",
                "sellerName": "Ventas",
                "operationCompanyLabel": "SEPID",
                "remitoNumber": "RT 0002-00000004",
                "invoiceNumber": "FC A 0001-00001234",
                "items": [{"description": "Remito ya facturado", "quantity": 1}],
            },
            self.jefe.id,
        )
        with connection.cursor() as cur:
            for group_id, order, generated_at in (
                ("brg-pending-old", older_generated, "2026-05-10 09:00:00-03"),
                ("brg-pending-new", newer_generated, "2026-06-10 09:00:00-03"),
            ):
                cur.execute(
                    """
                    INSERT INTO bejerman_remito_groups(
                        id, company_key, comprobante_tipo, customer_code, customer_name,
                        seller_code, payment_term_code, operation_code, deposit_code,
                        order_ids, status, generated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
                    """,
                    [
                        group_id,
                        "SEPID",
                        "RT",
                        "CLI-NOTIF",
                        "Clínica Notificaciones",
                        "ADM",
                        "30",
                        "MC",
                        "VAL",
                        json.dumps([order["id"]]),
                        "generated",
                        generated_at,
                    ],
                )
                cur.execute(
                    "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = %s",
                    [group_id, order["id"]],
                )
            cur.execute(
                """
                UPDATE delivery_orders
                   SET delivered_at = %s::timestamptz, created_at = %s::timestamptz
                 WHERE id = %s
                """,
                ["2026-06-20 10:00:00-03", "2026-06-20 10:00:00-03", imported["id"]],
            )

        result = list_delivery_orders({"pendingBilling": True, "limit": 10})

        self.assertEqual(result["total"], 3)
        self.assertEqual(
            [item["id"] for item in result["items"]],
            [imported["id"], older_generated["id"], newer_generated["id"]],
        )
        self.assertNotIn(already_invoiced["id"], [item["id"] for item in result["items"]])

    def test_facturacion_requiere_numero_de_factura(self):
        order = create_delivery_order(
            {
                "id": "do-invoice-required",
                "orderNumber": "OE-INV-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "remitoNumber": "RT 0002-00012346",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced(order["id"], self.cobranzas.id, "")

        self.assertEqual(ctx.exception.code, "INVOICE_REQUIRED")

    def test_postergar_remito_lo_oculta_hasta_reactivarlo(self):
        order = self._create_commercial_correction_order("do-billing-postponed")
        return_date = (timezone.localdate() + timedelta(days=2)).isoformat()

        postponed = postpone_delivery_order_billing(order["id"], self.cobranzas.id, return_date)

        self.assertEqual(postponed["billingPostponedAt"][:10], timezone.localdate().isoformat())
        due_at = datetime.fromisoformat(postponed["billingPostponedUntil"])
        self.assertEqual(timezone.localtime(due_at).strftime("%H:%M"), "09:00")
        self.assertNotIn(order["id"], [item["id"] for item in list_delivery_orders({"pendingBilling": True, "limit": 10})["items"]])
        self.assertIn(order["id"], [item["id"] for item in list_delivery_orders({"postponedBilling": True, "limit": 10})["items"]])

        reactivated = reactivate_delivery_order_billing(order["id"], self.cobranzas.id)

        self.assertIsNone(reactivated["billingPostponedUntil"])
        self.assertIn(order["id"], [item["id"] for item in list_delivery_orders({"pendingBilling": True, "limit": 10})["items"]])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT event_type FROM delivery_order_events WHERE order_id = %s ORDER BY id",
                [order["id"]],
            )
            event_types = [row[0] for row in cur.fetchall()]
        self.assertIn("delivery_order_billing_postponed", event_types)
        self.assertIn("delivery_order_billing_reactivated", event_types)

    def test_postergar_remito_requiere_fecha_futura(self):
        order = self._create_commercial_correction_order("do-billing-postponed-date")

        with self.assertRaises(DeliveryOrderError) as ctx:
            postpone_delivery_order_billing(order["id"], self.cobranzas.id, timezone.localdate().isoformat())

        self.assertEqual(ctx.exception.code, "POSTPONEMENT_DATE_MUST_BE_FUTURE")

    def test_postergar_y_reactivar_remito_usan_permiso_de_facturacion(self):
        order = self._create_commercial_correction_order("do-billing-postponed-api")
        self.client.force_authenticate(user=self.cobranzas)
        return_date = (timezone.localdate() + timedelta(days=1)).isoformat()

        postponed = self.client.post(
            f"/api/ordenes-entrega/{order['id']}/posponer-facturacion/",
            {"returnDate": return_date},
            format="json",
        )
        self.assertEqual(postponed.status_code, 200, postponed.data)
        self.assertTrue(postponed.data["billingPostponedUntil"])

        reactivated = self.client.post(
            f"/api/ordenes-entrega/{order['id']}/reactivar-facturacion/",
            {},
            format="json",
        )
        self.assertEqual(reactivated.status_code, 200, reactivated.data)
        self.assertIsNone(reactivated.data["billingPostponedUntil"])

    def test_vencimiento_de_remito_pospuesto_notifica_una_sola_vez_sin_email(self):
        order = self._create_commercial_correction_order("do-billing-postponed-notification")
        return_date = (timezone.localdate() + timedelta(days=1)).isoformat()
        postpone_delivery_order_billing(order["id"], self.cobranzas.id, return_date)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET billing_postponed_until = CURRENT_TIMESTAMP - INTERVAL '1 minute'
                 WHERE id = %s
                """,
                [order["id"]],
            )

        with patch("service.notification_email_outbox.EmailMessage") as email_message_mock:
            first = process_due_delivery_order_billing_postponements()
            second = process_due_delivery_order_billing_postponements()

        self.assertEqual(first["due"], 1)
        self.assertEqual(second["due"], 0)
        self.assertEqual(first["notifications"], 1)
        email_message_mock.assert_not_called()
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT notification_key, href
                  FROM notifications
                 WHERE user_id = %s
                   AND notification_key = 'billing_remito_postponement_due'
                 ORDER BY id
                """,
                [self.cobranzas.id],
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [("billing_remito_postponement_due", f"/cobranzas/facturacion?orderId={order['id']}")])

    def test_facturacion_solo_acepta_remitos_pendientes(self):
        order = create_delivery_order(
            {
                "id": "do-invoice-state",
                "orderNumber": "OE-INV-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced(order["id"], self.cobranzas.id, "FC A 0001-00001234")

        self.assertEqual(ctx.exception.code, "INVALID_INVOICE_STATE")

    def test_facturacion_registra_numero_y_cierra_orden(self):
        order = create_delivery_order(
            {
                "id": "do-invoice-ok",
                "orderNumber": "OE-INV-003",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "remitoNumber": "RT 0002-00012347",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        updated = mark_invoiced(order["id"], self.cobranzas.id, "FC A 0001-00001234")

        self.assertEqual(updated["status"], "facturado")
        self.assertEqual(updated["invoiceNumber"], "FC A 0001-00001234")
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, invoice_number, invoiced_by_user_id, invoiced_at
                  FROM delivery_orders
                 WHERE id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
        self.assertEqual(row[0], "facturado")
        self.assertEqual(row[1], "FC A 0001-00001234")
        self.assertEqual(row[2], self.cobranzas.id)
        self.assertIsNotNone(row[3])

    def test_facturacion_orden_reparacion_sincroniza_factura_en_hoja(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET factura_numero = NULL WHERE id = %s", [self.ingreso_id])
        order = self._create_service_release_order("do-service-invoice-sync")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET remito_number = %s,
                       status = 'entregado_pendiente_facturacion',
                       remito_location = 'recepcion'
                 WHERE id = %s
                """,
                ["RT 0002-00015555", order["id"]],
            )

        updated = mark_invoiced(order["id"], self.cobranzas.id, "FC A 0001-00015555")

        self.assertEqual(updated["status"], "facturado")
        self.assertEqual(updated["invoiceNumber"], "FC A 0001-00015555")
        with connection.cursor() as cur:
            cur.execute("SELECT factura_numero FROM ingresos WHERE id = %s", [self.ingreso_id])
            factura_numero = cur.fetchone()[0]
        self.assertEqual(factura_numero, "FC A 0001-00015555")

    def test_facturacion_de_remito_emitido_no_entrega_orden(self):
        order = create_delivery_order(
            {
                "id": "do-invoice-before-delivery",
                "orderNumber": "OE-INV-004",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET remito_number = %s,
                       remito_location = 'recepcion',
                       prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP)
                 WHERE id = %s
                """,
                ["RT 0002-00022347", order["id"]],
            )

        pending = list_delivery_orders({"pendingBilling": True, "limit": 10})
        self.assertIn(order["id"], [item["id"] for item in pending["items"]])

        updated = mark_invoiced(order["id"], self.cobranzas.id, "FC A 0001-00002234")

        self.assertEqual(updated["status"], "armado_pendiente_entrega")
        self.assertEqual(updated["invoiceNumber"], "FC A 0001-00002234")
        self.assertIsNone(updated["deliveredAt"])
        pending_after_invoice = list_delivery_orders({"pendingBilling": True, "limit": 10})
        self.assertNotIn(order["id"], [item["id"] for item in pending_after_invoice["items"]])

        delivered = mark_delivered(order["id"], self.recepcion.id, None)

        self.assertEqual(delivered["status"], "facturado")
        self.assertIsNotNone(delivered["deliveredAt"])

    def test_no_se_factura_cierra_remito_pendiente_como_no_facturable(self):
        order = create_delivery_order(
            {
                "id": "do-not-billable-ok",
                "orderNumber": "OE-NOBILL-001",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "remitoNumber": "RT 0002-00012348",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        updated = mark_not_billable(order["id"], self.cobranzas.id, "No corresponde facturar")

        self.assertEqual(updated["status"], "entregado_no_facturable")
        self.assertEqual(updated["invoiceNumber"], "")
        pending = list_delivery_orders({"pendingBilling": True, "limit": 10})
        self.assertNotIn(order["id"], [item["id"] for item in pending["items"]])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT status, invoice_number
                  FROM delivery_orders
                 WHERE id = %s
                """,
                [order["id"]],
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT event_type, note
                  FROM delivery_order_events
                 WHERE order_id = %s
                 ORDER BY created_at DESC, id DESC
                 LIMIT 1
                """,
                [order["id"]],
            )
            event = cur.fetchone()
        self.assertEqual(row[0], "entregado_no_facturable")
        self.assertIsNone(row[1])
        self.assertEqual(event, ("delivery_order_not_billable", "No corresponde facturar"))

    def test_no_se_factura_solo_acepta_remitos_pendientes(self):
        order = create_delivery_order(
            {
                "id": "do-not-billable-state",
                "orderNumber": "OE-NOBILL-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
            },
            self.jefe.id,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_not_billable(order["id"], self.cobranzas.id, "No corresponde facturar")

        self.assertEqual(ctx.exception.code, "INVALID_NOT_BILLABLE_STATE")

    def test_ordenes_entrega_buscan_por_articulos_y_exponen_origen(self):
        with self._delivery_partida_stock_patch(
            [{"articleCode": "ART-1504005", "depositCode": "VAL", "partida": "L-2026", "availableQuantity": 2}]
        ):
            order = create_delivery_order(
                {
                    "id": "do-article-search",
                    "orderNumber": "OE-ART-001",
                    "customerId": self.customer_id,
                    "deliveryType": "sale",
                    "sellerName": "Ventas",
                    "operationCompanyLabel": "SEPID",
                    "sourceSystem": "portal",
                    "sourceExternalId": "portal-order-001",
                    "sourceReference": "Pedido Drive 15",
                    "sourceSheet": "Ventas Junio",
                    "sourceRow": 15,
                    "sourceColor": "#fff2cc",
                    "rawPedido": "Pedido de artículos generales",
                    "commercialCondition": "Contado",
                    "items": [
                        {
                            "articleCode": "ART-1504005",
                            "articleName": "Filtro bacteriológico",
                            "description": "Set universal para alquiler",
                            "quantity": 2,
                            "partida": "L-2026",
                        }
                    ],
                },
                self.jefe.id,
            )

        self.assertEqual(order["sourceSheet"], "Ventas Junio")
        self.assertEqual(order["sourceRow"], 15)
        self.assertEqual(order["sourceColor"], "#fff2cc")

        for query in ("ART-1504005", "bacteriológico", "Set universal", "Ventas Junio", "Contado"):
            result = list_delivery_orders({"q": query, "limit": 10})
            self.assertEqual([item["id"] for item in result["items"]], ["do-article-search"])

    def test_rss_emitido_completa_entrega_del_ingreso(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       alquilado = FALSE,
                       alquiler_a = NULL,
                       alquiler_remito = NULL,
                       alquiler_fecha = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        order = self._create_service_release_order("do-rss-sync-ok")

        with patch("service.bejerman_delivery.BejermanSDKClient", self._fake_bejerman_remito_client()):
            result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["remitoNumber"], "RSS R 00004-00001234")
        self.assertFalse(result["billingRequired"])
        self.assertEqual(result["orders"][0]["status"], "armado_pendiente_entrega")
        self.assertIsNone(result["orders"][0]["deliveredAt"])
        with connection.cursor() as cur:
            cur.execute(
                "SELECT estado, remito_salida, fecha_entrega FROM ingresos WHERE id = %s",
                [self.ingreso_id],
            )
            row = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM notifications
                 WHERE notification_key = 'sales_order_remito_ready'
                   AND title LIKE %s
                """,
                [f"%{order['orderNumber']}%"],
            )
            billing_notifications = cur.fetchone()[0]
        self.assertEqual(row[0], "entregado")
        self.assertEqual(row[1], "RSS R 00004-00001234")
        self.assertIsNotNone(row[2])
        self.assertEqual(billing_notifications, 0)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@nexora.test",
    )
    def test_rss_emitido_envia_pdf_por_mail_a_roles_obligatorios(self):
        mail.outbox = []
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'liberado',
                       remito_salida = NULL,
                       fecha_entrega = NULL
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        order = self._create_service_release_order("do-rss-mandatory-pdf-mail")

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", self._fake_bejerman_remito_client()),
            patch(
                "service.bejerman_delivery.get_remito_group_pdf",
                return_value=(b"%PDF-1.4\nRSS mail", "application/pdf", "remito-RSS.pdf"),
            ) as pdf_mock,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                result = generate_bejerman_remito([order["id"]], self.jefe.id, {})

        self.assertEqual(result["remitoNumber"], "RSS R 00004-00001234")
        self.assertFalse(result["billingRequired"])
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(
            {message.to[0] for message in mail.outbox},
            {"admin@notif.test", "cobranzas@notif.test", "recepcion@notif.test"},
        )
        for message in mail.outbox:
            self.assertIn("RSS R 00004-00001234", message.subject)
            self.assertIn("Se emitió un remito en Bejerman.", message.body)
            self.assertEqual(len(message.attachments), 1)
            self.assertTrue(message.attachments[0][1].startswith(b"%PDF-"))
        pdf_mock.assert_called_once_with(result["groupId"], actor_user_id=self.jefe.id)

    def test_rss_emitido_marca_vendido_pendiente_como_vendido_entregado(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingresos
                   SET estado = 'vendido_pendiente_entrega',
                       remito_salida = NULL,
                       fecha_entrega = NULL,
                       alquilado = TRUE,
                       alquiler_a = 'Cliente anterior',
                       alquiler_remito = 'ALQ-001',
                       alquiler_fecha = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
        order = self._create_service_release_order("do-rss-sync-sale-ok")

        with patch("service.bejerman_delivery.BejermanSDKClient", self._fake_bejerman_remito_client()):
            generate_bejerman_remito([order["id"]], self.jefe.id, {})

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, remito_salida, fecha_entrega, alquilado, alquiler_a, alquiler_remito, alquiler_fecha
                  FROM ingresos
                 WHERE id = %s
                """,
                [self.ingreso_id],
            )
            row = cur.fetchone()
        self.assertEqual(row[0], "vendido_entregado")
        self.assertEqual(row[1], "RSS R 00004-00001234")
        self.assertIsNotNone(row[2])
        self.assertFalse(row[3])
        self.assertIsNone(row[4])
        self.assertIsNone(row[5])
        self.assertIsNone(row[6])
