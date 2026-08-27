import json
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from service import bejerman_catalog_cache
from service.delivery_orders import (
    DeliveryOrderError,
    cancel_order,
    create_delivery_order,
    get_delivery_order,
    list_delivery_orders,
    mark_prepared,
)
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DeliveryOrderBejermanGroupRecoveryTests(TestCase):
    """OE-00232 quedó trabada en failed_review sin forma de reintentar ni de
    cancelarla limpiamente. Cubre: cancel_order retira un grupo Bejerman colgado
    (no deja huérfanos) y mark_prepared bloquea "armar" si falta stock real de
    Bejerman en artículos sin partida (antes se salteaba por completo ese chequeo)."""

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
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT NULL")
        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.filter(email__endswith="@group-recovery.test").delete()
        cls.recepcion = User.objects.create(
            nombre="Recepcion de prueba",
            email="recepcion@group-recovery.test",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers(cod_empresa, razon_social)
                VALUES (%s, %s)
                RETURNING id
                """,
                ["CLI-RECOVERY", "Cliente Recovery"],
            )
            cls.customer_id = int(cur.fetchone()[0])

    def _create_order(self, order_id, *, quantity=1, article_requires_partida=False, company_key="MGBIO"):
        payload = {
            "id": order_id,
            "orderNumber": f"OE-{order_id.upper()}",
            "customerId": self.customer_id,
            "bejermanCustomerCode": "CLI-RECOVERY",
            "customerName": "Cliente Recovery",
            "deliveryType": "sale",
            "companyKey": company_key,
            "sellerName": "Ventas",
            "operationCompanyLabel": company_key,
            "items": [
                {
                    "articleCode": f"ART-{order_id}",
                    "articleRequiresPartida": article_requires_partida,
                    "description": f"Articulo {order_id}",
                    "quantity": quantity,
                    "unitPrice": "100.00",
                    "priceCurrency": "ARS",
                    "discountPercent": "0",
                    "stockDepositCode": "VAL",
                }
            ],
        }
        with (
            patch("service.delivery_orders.notify_delivery_order_created"),
            patch("service.delivery_orders.notify_delivery_order_remito_ready"),
        ):
            return create_delivery_order(payload, self.recepcion.id)

    def _attach_group(self, group_id, order_ids, *, status, company_key="MGBIO"):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra,
                    customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status, order_ids, response_summary
                )
                VALUES (
                    %s, %s, 'RT', 'R',
                    'CLI-RECOVERY', 'Cliente Recovery', 'ADM', '30', 'MC', 'VAL',
                    %s, %s::jsonb, '{}'::jsonb
                )
                """,
                [group_id, company_key, status, json.dumps(order_ids)],
            )
            cur.execute(
                "UPDATE delivery_orders SET bejerman_remito_group_id = %s WHERE id = ANY(%s)",
                [group_id, order_ids],
            )

    def setUp(self):
        # El cache de catálogos vive a nivel de proceso: sin esto, el stock que
        # dejó cacheado un test con su SDK stubeado se le sirve al siguiente.
        bejerman_catalog_cache.invalidate_all()

    def tearDown(self):
        bejerman_catalog_cache.invalidate_all()

    def _group_status(self, group_id):
        with connection.cursor() as cur:
            cur.execute("SELECT status FROM bejerman_remito_groups WHERE id = %s", [group_id])
            row = cur.fetchone()
            return row[0] if row else None

    # -- cancel_order retira grupos colgados --------------------------------

    def test_cancel_retires_failed_review_group_and_clears_order_fk(self):
        order = self._create_order("cancel1")
        self._attach_group("brg-cancel1", [order["id"]], status="failed_review")

        result = cancel_order(order["id"], self.recepcion.id)

        self.assertEqual(result["status"], "cancelado")
        self.assertFalse(result.get("bejermanRemitoGroupId"))
        self.assertEqual(self._group_status("brg-cancel1"), "failed")

    def test_cancel_retires_sent_unknown_group(self):
        order = self._create_order("cancel2")
        self._attach_group("brg-cancel2", [order["id"]], status="sent_unknown")

        cancel_order(order["id"], self.recepcion.id)

        self.assertEqual(self._group_status("brg-cancel2"), "failed")

    def test_cancel_does_not_touch_group_actively_sending(self):
        order = self._create_order("cancel3")
        self._attach_group("brg-cancel3", [order["id"]], status="sending")

        result = cancel_order(order["id"], self.recepcion.id)

        # Un grupo en curso (no "colgado") no se retira desde acá: no está en
        # failed_review/sent_unknown, así que el WHERE del UPDATE no matchea.
        self.assertEqual(self._group_status("brg-cancel3"), "sending")
        self.assertEqual(result.get("bejermanRemitoGroupId"), "brg-cancel3")

    # -- mark_prepared bloquea armado sin stock real -------------------------

    def test_mark_prepared_blocks_when_non_partida_article_stock_insufficient(self):
        order = self._create_order("stock1", quantity=10)
        with patch(
            "service.delivery_orders._delivery_article_available_quantity",
            return_value=Decimal("3"),
        ):
            with self.assertRaises(DeliveryOrderError) as ctx:
                mark_prepared(order["id"], self.recepcion.id)
        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_ARTICLE_STOCK_INSUFFICIENT")

    def test_mark_prepared_succeeds_when_stock_sufficient(self):
        order = self._create_order("stock2", quantity=10)
        with (
            patch("service.delivery_orders._delivery_article_available_quantity", return_value=Decimal("50")),
            patch("service.delivery_orders.notify_delivery_order_remito_ready"),
        ):
            result = mark_prepared(order["id"], self.recepcion.id)
        self.assertEqual(result["status"], "armado_pendiente_entrega")

    def test_mark_prepared_stock_lookup_failure_blocks_with_502(self):
        order = self._create_order("stock3", quantity=10)
        with patch(
            "service.delivery_orders._delivery_article_available_quantity",
            side_effect=RuntimeError("Bejerman no disponible"),
        ):
            with self.assertRaises(DeliveryOrderError) as ctx:
                mark_prepared(order["id"], self.recepcion.id)
        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_ARTICLE_STOCK_UNAVAILABLE")

    # -- lastRemitoFailure: aviso + "Reintentar" sin pasar por el panel Bejerman ------

    def _record_remito_failure(self, order_id, message):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_order_events(order_id, event_type, metadata)
                VALUES (%s, 'bejerman_remito_failed', %s::jsonb)
                """,
                [order_id, json.dumps({"error": message})],
            )

    def test_get_delivery_order_exposes_last_remito_failure_when_retryable(self):
        order = self._create_order("fail1")
        self._record_remito_failure(order["id"], "No hay stock suficiente para el artículo 1203012")

        fetched = get_delivery_order(order["id"])

        self.assertEqual(
            fetched["lastRemitoFailure"]["message"],
            "No hay stock suficiente para el artículo 1203012",
        )

    def test_last_remito_failure_hidden_once_a_new_group_is_attached(self):
        order = self._create_order("fail2")
        self._record_remito_failure(order["id"], "No hay stock suficiente para el artículo 1203012")
        self._attach_group("brg-fail2", [order["id"]], status="sending")

        fetched = get_delivery_order(order["id"])

        self.assertIsNone(fetched.get("lastRemitoFailure"))

    def test_list_delivery_orders_exposes_last_remito_failure(self):
        order = self._create_order("fail3")
        self._record_remito_failure(order["id"], "No hay stock suficiente para el artículo 1203012")

        listing = list_delivery_orders({"status": "pendiente_armado"})
        by_id = {row["id"]: row for row in listing["items"]}

        self.assertEqual(
            by_id[order["id"]]["lastRemitoFailure"]["message"],
            "No hay stock suficiente para el artículo 1203012",
        )
