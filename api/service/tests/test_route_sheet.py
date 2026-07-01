import json

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.notifications import list_notifications_for_user


class RouteSheetAPITest(TestCase):
    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo BOOLEAN DEFAULT TRUE,
                    bejerman_seller_code TEXT NULL,
                    bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    razon_social TEXT NOT NULL DEFAULT '',
                    cod_empresa TEXT NULL,
                    alias_interno TEXT NULL,
                    bejerman_cod_empresa TEXT NULL,
                    bejerman_domicilio TEXT NULL,
                    bejerman_localidad TEXT NULL,
                    bejerman_provincia TEXT NULL,
                    bejerman_codigo_postal TEXT NULL
                )
                """
            )
            for column in (
                "cod_empresa TEXT NULL",
                "alias_interno TEXT NULL",
                "bejerman_cod_empresa TEXT NULL",
                "bejerman_domicilio TEXT NULL",
                "bejerman_localidad TEXT NULL",
                "bejerman_provincia TEXT NULL",
                "bejerman_codigo_postal TEXT NULL",
            ):
                cur.execute(f"ALTER TABLE customers ADD COLUMN IF NOT EXISTS {column}")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY
                )
                """
            )
        call_command("apply_user_permissions_schema", verbosity=0)
        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_notifications_schema", verbosity=0)
        call_command("apply_route_sheet_schema", verbosity=0)

        cls.supervisor = User.objects.create(
            nombre="Supervisor Ruta",
            email="supervisor-ruta@example.com",
            hash_pw="",
            rol="supervisor",
            activo=True,
        )
        cls.logistica = User.objects.create(
            nombre="Logistica Ruta",
            email="logistica-ruta@example.com",
            hash_pw="",
            rol="logistica",
            activo=True,
        )

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM notifications")
            cur.execute("DELETE FROM route_stop_events")
            cur.execute("DELETE FROM route_stops")
            cur.execute("DELETE FROM route_locations")
            cur.execute("DELETE FROM delivery_order_events")
            cur.execute("DELETE FROM delivery_order_item_partidas")
            cur.execute("DELETE FROM delivery_order_items")
            cur.execute("DELETE FROM delivery_orders")
            cur.execute(
                """
                INSERT INTO delivery_orders (
                  id, order_number, customer_name, delivery_type, status, remito_number,
                  seller_name, order_date, raw_pedido, created_at
                )
                VALUES (
                  'do-ruta-1', 'OE-RUTA-001', 'Cliente Ruta', 'sale', 'armado_pendiente_entrega',
                  'RT 0001-00000001', 'Ventas', '2026-06-29', 'Pedido de prueba', '2026-06-29 09:00:00-03'
                )
                """
            )

    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(user)}")
        return client

    def _create_stop(self):
        response = self._client(self.supervisor).post(
            "/api/hoja-ruta/",
            {
                "routeDate": "2026-06-29",
                "requestedDate": "2026-06-29",
                "requesterName": "Ventas",
                "placeName": "Cliente Ruta",
                "address": "Calle Falsa 123",
                "task": "Entregar pedido",
                "deliveryOrderId": "do-ruta-1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_logistica_no_lista_ordenes_ni_gestiona_paradas(self):
        client = self._client(self.logistica)

        self.assertEqual(client.get("/api/ordenes-entrega/").status_code, 403)
        create_response = client.post(
            "/api/hoja-ruta/",
            {"routeDate": "2026-06-29", "placeName": "Cliente", "task": "Tarea"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 403)

    def test_logistica_no_gestiona_direcciones_frecuentes(self):
        client = self._client(self.logistica)
        location = self._client(self.supervisor).post(
            "/api/hoja-ruta/lugares/",
            {"name": "Librería Pepito", "address": "Av. Test 123"},
            format="json",
        )
        self.assertEqual(location.status_code, 201, location.data)

        create_response = client.post(
            "/api/hoja-ruta/lugares/",
            {"name": "Librería Pepito", "address": "Av. Test 123"},
            format="json",
        )
        patch_response = client.patch(
            f"/api/hoja-ruta/lugares/{location.data['id']}/",
            {"address": "Av. Test 456"},
            format="json",
        )

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(patch_response.status_code, 403)

    def test_lugares_permiten_mismo_nombre_con_distinta_direccion(self):
        client = self._client(self.supervisor)

        first = client.post(
            "/api/hoja-ruta/lugares/",
            {"name": "Librería Pepito", "address": "Av. Test 123"},
            format="json",
        )
        second = client.post(
            "/api/hoja-ruta/lugares/",
            {"name": "Librería Pepito", "address": "San Martín 456"},
            format="json",
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(first.data["id"], second.data["id"])
        response = client.get("/api/hoja-ruta/lugares/?q=Librería%20Pepito")
        self.assertEqual(response.status_code, 200, response.data)
        addresses = {item["address"] for item in response.data["items"] if item.get("id")}
        self.assertIn("Av. Test 123", addresses)
        self.assertIn("San Martín 456", addresses)

    def test_parada_con_lugar_libre_guarda_direccion_reutilizable(self):
        client = self._client(self.supervisor)
        response = client.post(
            "/api/hoja-ruta/",
            {
                "routeDate": "2026-06-29",
                "placeName": "Librería Pepito",
                "address": "Av. Test 123",
                "task": "Retirar lapiceras",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["placeName"], "Librería Pepito")
        locations = client.get("/api/hoja-ruta/lugares/?q=Librería%20Pepito")
        self.assertEqual(locations.status_code, 200, locations.data)
        reusable = [item for item in locations.data["items"] if item.get("id") and item["name"] == "Librería Pepito"]
        self.assertEqual(len(reusable), 1)
        self.assertEqual(reusable[0]["address"], "Av. Test 123")
        self.assertGreaterEqual(reusable[0]["usageCount"], 1)

    def test_parada_desde_cliente_vincula_direccion_operativa(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (
                  razon_social, cod_empresa, bejerman_domicilio, bejerman_localidad
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                ["Cliente Bejerman Ruta", "CBR", "Av. Cliente 100", "CABA"],
            )
            customer_id = cur.fetchone()[0]

        response = self._client(self.supervisor).post(
            "/api/hoja-ruta/",
            {
                "routeDate": "2026-06-29",
                "customerId": customer_id,
                "placeName": "Cliente Bejerman Ruta",
                "address": "Depósito operativo 200",
                "task": "Entrega programada",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT customer_id, name, address FROM route_locations WHERE id = %s",
                [response.data["locationId"]],
            )
            row = cur.fetchone()
        self.assertEqual(row, (customer_id, "Cliente Bejerman Ruta", "Depósito operativo 200"))

    def test_catalogo_clientes_incluye_domicilio_bejerman_y_direcciones_de_ruta(self):
        client = self._client(self.supervisor)
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (
                  razon_social, cod_empresa, bejerman_domicilio, bejerman_localidad
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                ["Cliente Con Direcciones", "CCD", "Av. Fiscal 10", "CABA"],
            )
            customer_id = cur.fetchone()[0]

        location_response = client.post(
            "/api/hoja-ruta/lugares/",
            {
                "customerId": customer_id,
                "name": "Cliente Con Direcciones",
                "address": "Depósito 20",
            },
            format="json",
        )
        self.assertEqual(location_response.status_code, 201, location_response.data)

        response = client.get("/api/catalogos/clientes/?include_route_locations=1")

        self.assertEqual(response.status_code, 200, response.data)
        customer = next(row for row in response.data if row["id"] == customer_id)
        sources = {item["sourceType"]: item["address"] for item in customer["routeLocations"]}
        self.assertEqual(sources["bejerman"], "Av. Fiscal 10, CABA")
        self.assertEqual(sources["route_location"], "Depósito 20")

    def test_logistica_solo_ve_notificaciones_de_paradas_nuevas(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notifications(user_id, notification_key, dedupe_key, title, body, href)
                VALUES
                  (%s, 'sales_order_created', 'old-sales-for-logistica', 'Nueva orden de entrega OE-OLD', '', ''),
                  (%s, 'reparacion_lista_remito', 'old-repair-for-logistica', 'Reparación lista para remito - OS 00001', '', '')
                """,
                [self.logistica.id, self.logistica.id],
            )

        stop = self._create_stop()

        data = list_notifications_for_user(self.logistica.id, limit=10)
        self.assertEqual(data["unread_count"], 1)
        self.assertEqual([item["notification_key"] for item in data["items"]], ["route_stop_created"])
        self.assertEqual(data["items"][0]["href"], "/hoja-de-ruta?date=2026-06-29")
        payload = data["items"][0]["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertEqual(payload["stopId"], stop["id"])

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, title
                  FROM notifications
                 WHERE notification_key = 'route_stop_created'
                 ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(self.logistica.id, "Nueva parada de Hoja de ruta - Cliente Ruta")])

    def test_logistica_completa_parada_y_entrega_orden_vinculada(self):
        stop = self._create_stop()
        client = self._client(self.logistica)

        response = client.post(f"/api/hoja-ruta/{stop['id']}/completar/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "completado")
        with connection.cursor() as cur:
            cur.execute("SELECT status, delivered_by_user_id FROM delivery_orders WHERE id = 'do-ruta-1'")
            status, delivered_by = cur.fetchone()
        self.assertEqual(status, "entregado_pendiente_facturacion")
        self.assertEqual(delivered_by, self.logistica.id)

    def test_cancelar_parada_la_oculta_del_listado_diario(self):
        stop = self._create_stop()
        client = self._client(self.supervisor)

        response = client.post(f"/api/hoja-ruta/{stop['id']}/cancelar/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "cancelado")
        listed = client.get("/api/hoja-ruta/?date=2026-06-29")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertNotIn(stop["id"], [item["id"] for item in listed.data["items"]])

    def test_posponer_mueve_parada_a_otro_dia_y_no_modifica_orden_vinculada(self):
        stop = self._create_stop()
        client = self._client(self.logistica)

        response = client.post(
            f"/api/hoja-ruta/{stop['id']}/posponer/",
            {"routeDate": "2026-06-30", "note": "No pudo ir"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "pendiente")
        self.assertEqual(response.data["routeDate"], "2026-06-30")
        self.assertEqual(response.data["postponeNote"], "No pudo ir")
        today = self._client(self.supervisor).get("/api/hoja-ruta/?date=2026-06-29")
        tomorrow = self._client(self.supervisor).get("/api/hoja-ruta/?date=2026-06-30")
        self.assertNotIn(stop["id"], [item["id"] for item in today.data["items"]])
        self.assertIn(stop["id"], [item["id"] for item in tomorrow.data["items"]])
        with connection.cursor() as cur:
            cur.execute("SELECT status, delivered_by_user_id FROM delivery_orders WHERE id = 'do-ruta-1'")
            status, delivered_by = cur.fetchone()
        self.assertEqual(status, "armado_pendiente_entrega")
        self.assertIsNone(delivered_by)

    def test_ordenes_sugeridas_incluyen_pendientes_y_preparadas_con_orden_de_entrega(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_orders (
                  id, order_number, customer_name, delivery_type, status, remito_number,
                  seller_name, order_date, raw_pedido, created_at
                )
                VALUES
                  (
                    'do-ruta-2', 'OE-RUTA-002', 'Cliente Pendiente', 'sale', 'pendiente_armado',
                    NULL, 'Ventas', '2026-06-29', 'Pedido pendiente', '2026-06-30 09:00:00-03'
                  ),
                  (
                    'do-ruta-3', 'OE-RUTA-003', 'Cliente Preparado', 'sale', 'armado_pendiente_entrega',
                    NULL, 'Ventas', '2026-06-29', 'Pedido preparado', '2026-07-01 09:00:00-03'
                  ),
                  (
                    'do-ruta-4', 'OE-RUTA-004', 'Cliente Facturado', 'sale', 'facturado',
                    'RT 0001-00000004', 'Ventas', '2026-06-29', 'Pedido cerrado', '2026-07-02 09:00:00-03'
                  )
                """
            )

        response = self._client(self.supervisor).get("/api/hoja-ruta/ordenes-sugeridas/?date=2026-06-29")

        self.assertEqual(response.status_code, 200, response.data)
        order_numbers = [item["orderNumber"] for item in response.data["items"]]
        self.assertEqual(order_numbers[:3], ["OE-RUTA-003", "OE-RUTA-002", "OE-RUTA-001"])
        self.assertNotIn("OE-RUTA-004", order_numbers)

    def test_hoja_ruta_rechaza_orden_pendiente_stock(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_orders (
                  id, order_number, customer_name, delivery_type, status, remito_number,
                  seller_name, order_date, raw_pedido, created_at
                )
                VALUES (
                  'do-ruta-stock', 'OE-RUTA-STOCK', 'Cliente Stock', 'sale', 'pendiente_stock',
                  NULL, 'Ventas', '2026-06-29', 'Pedido sin stock', '2026-06-29 10:00:00-03'
                )
                """
            )

        response = self._client(self.supervisor).post(
            "/api/hoja-ruta/",
            {
                "routeDate": "2026-06-29",
                "requestedDate": "2026-06-29",
                "requesterName": "Ventas",
                "placeName": "Cliente Stock",
                "address": "Calle Stock 123",
                "task": "Entrega programada",
                "deliveryOrderId": "do-ruta-stock",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "DELIVERY_ORDER_STOCK_PENDING")

    def test_parada_creada_para_otro_dia_solo_aparece_en_ese_dia(self):
        response = self._client(self.supervisor).post(
            "/api/hoja-ruta/",
            {
                "routeDate": "2026-06-30",
                "placeName": "Cliente Mañana",
                "address": "Calle Mañana 123",
                "task": "Entrega programada",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        stop_id = response.data["id"]

        today = self._client(self.supervisor).get("/api/hoja-ruta/?date=2026-06-29")
        tomorrow = self._client(self.supervisor).get("/api/hoja-ruta/?date=2026-06-30")

        self.assertEqual(today.status_code, 200, today.data)
        self.assertEqual(tomorrow.status_code, 200, tomorrow.data)
        self.assertNotIn(stop_id, [item["id"] for item in today.data["items"]])
        self.assertIn(stop_id, [item["id"] for item in tomorrow.data["items"]])

    def test_supervisor_puede_posponer(self):
        stop = self._create_stop()
        response = self._client(self.supervisor).post(
            f"/api/hoja-ruta/{stop['id']}/posponer/",
            {"routeDate": "2026-06-30", "note": "Se reprograma"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "pendiente")
        self.assertEqual(response.data["routeDate"], "2026-06-30")

    def test_posponer_requiere_nueva_fecha(self):
        stop = self._create_stop()
        response = self._client(self.logistica).post(
            f"/api/hoja-ruta/{stop['id']}/posponer/",
            {"note": "Sin fecha"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "ROUTE_DATE_REQUIRED")
