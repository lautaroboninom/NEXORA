from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from service.delivery_orders import create_delivery_order, mark_delivered
from service.models import User


class DeliveryOrderServiceReleaseSyncTests(TestCase):
    """Cubre la sincronizacion de mark_delivered() hacia `ingresos`.

    Reproduce el caso OS-29473: una orden de entrega service_release se marca
    entregada (con o sin remito cargado a mano) y eso debe reflejarse en el
    bloque "Entrega" de la hoja de servicio sin que el tecnico tenga que
    cargar el remito de nuevo.
    """

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
                    numero_serie TEXT,
                    numero_interno TEXT,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    mg_estado TEXT NOT NULL DEFAULT 'activo'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingresos (
                    id BIGSERIAL PRIMARY KEY,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    estado TEXT NOT NULL DEFAULT 'ingresado',
                    motivo TEXT NOT NULL DEFAULT 'reparación',
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ubicacion_id INTEGER NULL REFERENCES locations(id),
                    remito_salida TEXT NULL,
                    factura_numero TEXT NULL,
                    fecha_entrega TIMESTAMPTZ NULL,
                    alquilado BOOLEAN NOT NULL DEFAULT FALSE,
                    alquiler_a TEXT NULL,
                    alquiler_remito TEXT NULL,
                    alquiler_fecha TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id BIGSERIAL PRIMARY KEY,
                    ticket_id INTEGER,
                    ingreso_id INTEGER,
                    a_estado TEXT,
                    usuario_id INTEGER NULL,
                    comentario TEXT NULL,
                    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.filter(email__endswith="@delivery-sync.test").delete()
        cls.jefe = User.objects.create(
            nombre="Jefatura de prueba",
            email="jefe@delivery-sync.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social) VALUES (%s, %s) RETURNING id",
                ["CLI-SYNC", "Cliente Sync"],
            )
            cls.customer_id = int(cur.fetchone()[0])

    def _create_device(self, *, alquilado=False, mg_estado="activo", numero_serie="", numero_interno=""):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, numero_serie, numero_interno, alquilado, mg_estado)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                [self.customer_id, numero_serie, numero_interno, alquilado, mg_estado],
            )
            return int(cur.fetchone()[0])

    def _create_ingreso(self, *, estado, device_id=None, remito_salida=None, alquilado=False):
        device_id = device_id if device_id is not None else self._create_device()
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, remito_salida, alquilado)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                [device_id, estado, remito_salida, alquilado],
            )
            return int(cur.fetchone()[0])

    def _create_service_release_order(self, order_id, ingreso_id, *, status="armado_pendiente_entrega"):
        payload = {
            "id": order_id,
            "orderNumber": f"OE-{order_id}",
            "customerId": self.customer_id,
            "bejermanCustomerCode": "CLI-SYNC",
            "customerName": "Cliente Sync",
            "deliveryType": "service_release",
            "companyKey": "SEPID",
            "status": status,
            "sellerName": "Servicio Tecnico",
            "operationCompanyLabel": "SEPID",
            "ingresoId": ingreso_id,
            "items": [
                {
                    "articleCode": "SVC-001",
                    "description": "Servicio tecnico",
                    "quantity": 1,
                    "unitPrice": "0",
                    "priceCurrency": "ARS",
                    "discountPercent": "0",
                }
            ],
        }
        with (
            patch("service.delivery_orders.notify_delivery_order_created"),
            patch("service.delivery_orders.notify_delivery_order_remito_ready"),
        ):
            return create_delivery_order(payload, self.jefe.id)

    def _mark_delivered(self, order_id, remito):
        with patch("service.delivery_orders.notify_delivery_order_remito_ready"):
            return mark_delivered(order_id, self.jefe.id, remito)

    def _ingreso_row(self, ingreso_id):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT estado, remito_salida, fecha_entrega, alquilado
                  FROM ingresos WHERE id = %s
                """,
                [ingreso_id],
            )
            row = cur.fetchone()
        return {"estado": row[0], "remito_salida": row[1], "fecha_entrega": row[2], "alquilado": row[3]}

    def _event_count(self, ingreso_id, a_estado):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ingreso_events WHERE ticket_id = %s AND a_estado = %s",
                [ingreso_id, a_estado],
            )
            return int(cur.fetchone()[0] or 0)

    def test_mark_delivered_sincroniza_estado_remito_y_fecha(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-basico", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026325")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "RSS 0001-00026325")
        self.assertIsNotNone(row["fecha_entrega"])
        self.assertGreaterEqual(self._event_count(ingreso_id, "entregado"), 1)

    def test_mark_delivered_no_pisa_remito_ya_cargado(self):
        ingreso_id = self._create_ingreso(estado="liberado", remito_salida="MANUAL-999")
        order = self._create_service_release_order("sync-no-pisa", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026325")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "MANUAL-999")

    def test_mark_delivered_detecta_alquiler(self):
        device_id = self._create_device(alquilado=True)
        ingreso_id = self._create_ingreso(estado="liberado", device_id=device_id, alquilado=True)
        order = self._create_service_release_order("sync-alquiler", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026400")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "alquilado")
        self.assertTrue(row["alquilado"])

    def test_mark_delivered_detecta_venta_pendiente_a_vendido_entregado(self):
        ingreso_id = self._create_ingreso(estado="vendido_pendiente_entrega")
        order = self._create_service_release_order("sync-venta", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026500")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "vendido_entregado")
        self.assertFalse(row["alquilado"])

    def test_mark_delivered_no_toca_ingreso_ya_entregado(self):
        ingreso_id = self._create_ingreso(estado="entregado", remito_salida="YA-ENTREGADO")
        order = self._create_service_release_order("sync-ya-entregado", ingreso_id)

        self._mark_delivered(order["id"], "RSS 0001-00026600")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "entregado")
        self.assertEqual(row["remito_salida"], "YA-ENTREGADO")

    def test_mark_delivered_ignora_ordenes_que_no_son_service_release(self):
        ingreso_id = self._create_ingreso(estado="liberado")
        order = self._create_service_release_order("sync-scope", ingreso_id)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE delivery_orders SET delivery_type = 'sale' WHERE id = %s",
                [order["id"]],
            )

        self._mark_delivered(order["id"], "RT 0001-00026700")

        row = self._ingreso_row(ingreso_id)
        self.assertEqual(row["estado"], "liberado")
        self.assertIsNone(row["remito_salida"])
