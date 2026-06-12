from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.auth import issue_token
from service.delivery_orders import (
    DeliveryOrderError,
    create_delivery_order,
    list_delivery_orders,
    mark_delivered,
    mark_invoiced,
)
from service.models import User
from service.notifications import (
    emit_notification,
    notify_estado_patrimonial,
    notify_ingreso_asignado,
    notify_ingreso_liberado,
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
            for statement in (
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS telefono TEXT NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT NULL",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INTEGER NULL",
            ):
                cur.execute(statement)

        call_command("apply_notifications_schema", verbosity=0)
        call_command("apply_delivery_orders_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.filter(email__endswith="@notif.test").delete()

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
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM notification_user_preferences")

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
            [(self.recepcion.id, "Nueva orden de entrega OE-NOTIF-001", "/administracion/ordenes-entrega")],
        )

    def test_remito_cargado_notifica_cobranzas(self):
        order = create_delivery_order(
            {
                "id": "do-notif-remito",
                "orderNumber": "OE-NOTIF-002",
                "customerId": self.customer_id,
                "deliveryType": "sale",
                "sellerName": "Ventas",
                "operationCompanyLabel": "RESPIFLOW",
                "items": [{"description": "Equipo de prueba", "quantity": 1}],
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
            [(self.cobranzas.id, "Entrega lista para facturar OE-NOTIF-002", "/cobranzas/facturacion")],
        )

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

    def test_ordenes_entrega_buscan_por_articulos_y_exponen_origen(self):
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
