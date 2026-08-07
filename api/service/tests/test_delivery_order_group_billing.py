import json
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.delivery_orders import (
    DeliveryOrderError,
    create_delivery_order,
    get_delivery_order,
    mark_invoiced,
    mark_invoiced_batch,
)
from service.models import User


INVOICE_NUMBER = "FC A 0001-00004567"


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DeliveryOrderGroupBillingTests(TestCase):
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
                CREATE TABLE IF NOT EXISTS devices (
                    id BIGSERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id)
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
                    factura_numero TEXT NULL
                )
                """
            )
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT NULL")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS estado TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS factura_numero TEXT NULL")
            # Identidad de facturacion de un particular (cliente ocasional sin codigo Bejerman).
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_doc TEXT")

        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.filter(email__endswith="@group-billing.test").delete()
        cls.creator = User.objects.create(
            nombre="Jefatura de prueba",
            email="jefatura@group-billing.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.cobranzas = User.objects.create(
            nombre="Cobranzas de prueba",
            email="cobranzas@group-billing.test",
            hash_pw="",
            rol="cobranzas",
            activo=True,
        )
        cls.recepcion = User.objects.create(
            nombre="Recepcion de prueba",
            email="recepcion@group-billing.test",
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
                ["CLI-AGRUPADO", "Cliente Agrupado"],
            )
            cls.customer_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO customers(cod_empresa, razon_social)
                VALUES (%s, %s)
                RETURNING id
                """,
                ["PTL", "Particular"],
            )
            cls.particular_customer_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO devices(customer_id)
                VALUES (%s)
                RETURNING id
                """,
                [cls.customer_id],
            )
            cls.device_id = int(cur.fetchone()[0])

    def setUp(self):
        self.client = APIClient()

    def _create_order(
        self,
        order_id,
        *,
        customer_code="CLI-AGRUPADO",
        customer_name="Cliente Agrupado",
        customer_id=None,
        company_key="SEPID",
        remito_number=None,
        status="entregado_pendiente_facturacion",
        currency="ARS",
        invoice_number=None,
        delivery_type="sale",
        ingreso_id=None,
    ):
        if customer_id is None and customer_code:
            customer_id = self.customer_id
        payload = {
            "id": order_id,
            "orderNumber": f"OE-{order_id}",
            "customerId": customer_id,
            "bejermanCustomerCode": customer_code,
            "customerName": customer_name,
            "deliveryType": delivery_type,
            "companyKey": company_key,
            "status": status,
            "sellerName": "Ventas",
            "operationCompanyLabel": company_key,
            "items": [
                {
                    "articleCode": f"ART-{order_id}",
                    "description": f"Articulo {order_id}",
                    "quantity": 1,
                    "unitPrice": "100.00",
                    "priceCurrency": currency,
                    "discountPercent": "0",
                }
            ],
        }
        if remito_number is not None:
            payload["remitoNumber"] = remito_number
        if invoice_number is not None:
            payload["invoiceNumber"] = invoice_number
        if ingreso_id is not None:
            payload["ingresoId"] = ingreso_id
            payload["sourceExternalId"] = str(ingreso_id)
            payload["sourceReference"] = f"OS-{ingreso_id:05d}"
        with (
            patch("service.delivery_orders.notify_delivery_order_created"),
            patch("service.delivery_orders.notify_delivery_order_remito_ready"),
        ):
            return create_delivery_order(payload, self.creator.id)

    def _create_billable_order(self, order_id, **kwargs):
        kwargs.setdefault("remito_number", f"RT 0001-{len(order_id):08d}")
        return self._create_order(order_id, **kwargs)

    def _create_ingreso(self, factura_numero=None, propietario_nombre=None, propietario_doc=None):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_creacion, factura_numero,
                    propietario_nombre, propietario_doc
                )
                VALUES (%s, 'ingresado', 'reparación', CURRENT_TIMESTAMP, %s, %s, %s)
                RETURNING id
                """,
                [self.device_id, factura_numero, propietario_nombre, propietario_doc],
            )
            return int(cur.fetchone()[0])

    def _create_particular_order(self, order_id, propietario_doc, propietario_nombre="Juan Perez"):
        ingreso_id = self._create_ingreso(
            propietario_nombre=propietario_nombre, propietario_doc=propietario_doc
        )
        return self._create_billable_order(
            order_id,
            # 'PTL' a proposito: es el codigo que la fila Particular tiene cargado en produccion y
            # que no existe en Bejerman. create_delivery_order lo normaliza a vacio.
            customer_code="PTL",
            customer_name="Particular",
            customer_id=self.particular_customer_id,
            ingreso_id=ingreso_id,
        )

    def _attach_group(
        self,
        group_id,
        orders,
        *,
        customer_code="CLI-AGRUPADO",
        company_key="SEPID",
        billing_required=True,
        point_of_sale="00002",
        response_summary=None,
        remito_number=None,
    ):
        order_ids = [order["id"] for order in orders]
        remito_number = remito_number or f"RT {point_of_sale or '00002'}-{len(group_id):08d}"
        summary = {"billingRequired": billing_required, **(response_summary or {})}
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra,
                    comprobante_pto_venta, comprobante_numero, remito_number,
                    customer_code, customer_name, seller_code, payment_term_code,
                    operation_code, deposit_code, status, order_ids, response_summary,
                    generated_at
                )
                VALUES (
                    %s, %s, 'RT', 'R', %s, '00000001', %s,
                    %s, 'Cliente Agrupado', 'ADM', '30', 'MC', 'VAL',
                    'generated', %s::jsonb, %s::jsonb, CURRENT_TIMESTAMP
                )
                """,
                [
                    group_id,
                    company_key,
                    point_of_sale,
                    remito_number,
                    customer_code,
                    json.dumps(order_ids),
                    json.dumps(summary),
                ],
            )
            cur.execute(
                """
                UPDATE delivery_orders
                   SET bejerman_remito_group_id = %s,
                       remito_number = %s
                 WHERE id = ANY(%s)
                """,
                [group_id, remito_number, order_ids],
            )
        return group_id

    def _invoice_rows(self, order_ids):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, invoice_number
                  FROM delivery_orders
                 WHERE id = ANY(%s)
                 ORDER BY id
                """,
                [order_ids],
            )
            return {row[0]: {"status": row[1], "invoice": row[2]} for row in cur.fetchall()}

    def _invoiced_event_count(self, order_id):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM delivery_order_events
                 WHERE order_id = %s
                   AND event_type = 'delivery_order_invoiced'
                """,
                [order_id],
            )
            return int(cur.fetchone()[0])

    def test_factura_varias_ordenes_del_mismo_cliente_empresa_y_monedas_mixtas(self):
        ars = self._create_billable_order("batch-success-ars", currency="ARS")
        usd = self._create_billable_order("batch-success-usd", currency="USD")

        result = mark_invoiced_batch([usd["id"], ars["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(result["invoiceNumber"], INVOICE_NUMBER)
        self.assertEqual(result["count"], 2)
        self.assertEqual(set(result["orderIds"]), {ars["id"], usd["id"]})
        self.assertEqual({order["invoiceNumber"] for order in result["orders"]}, {INVOICE_NUMBER})
        rows = self._invoice_rows([ars["id"], usd["id"]])
        self.assertEqual({row["status"] for row in rows.values()}, {"facturado"})
        self.assertEqual({row["invoice"] for row in rows.values()}, {INVOICE_NUMBER})

    def test_expande_todas_las_ordenes_hermanas_del_mismo_remito(self):
        selected = self._create_billable_order("batch-sibling-selected")
        sibling = self._create_billable_order("batch-sibling-expanded")
        self._attach_group("brg-batch-siblings", [selected, sibling])

        result = mark_invoiced_batch([selected["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(result["count"], 2)
        self.assertEqual(set(result["orderIds"]), {selected["id"], sibling["id"]})
        rows = self._invoice_rows([selected["id"], sibling["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {INVOICE_NUMBER})

    def test_factura_antes_de_entrega_sin_cambiar_el_estado(self):
        order = self._create_order(
            "batch-before-delivery",
            status="armado_pendiente_entrega",
            remito_number=None,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET remito_number = 'RT 0001-00009999',
                       remito_location = 'recepcion',
                       delivered_at = NULL
                 WHERE id = %s
                """,
                [order["id"]],
            )

        result = mark_invoiced_batch([order["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(result["orders"][0]["status"], "armado_pendiente_entrega")
        self.assertEqual(result["orders"][0]["invoiceNumber"], INVOICE_NUMBER)
        self.assertIsNone(result["orders"][0]["deliveredAt"])

    def test_reintento_es_idempotente_y_no_duplica_eventos(self):
        first = self._create_billable_order("batch-idempotent-first")
        second = self._create_billable_order("batch-idempotent-second")

        original = mark_invoiced_batch([first["id"], second["id"]], self.cobranzas.id, INVOICE_NUMBER)
        retried = mark_invoiced_batch([second["id"], first["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(retried["invoiceNumber"], original["invoiceNumber"])
        self.assertEqual(set(retried["orderIds"]), set(original["orderIds"]))
        self.assertEqual(self._invoiced_event_count(first["id"]), 1)
        self.assertEqual(self._invoiced_event_count(second["id"]), 1)

    def test_rechaza_seleccion_vacia(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_IDS_REQUIRED")

    def test_id_inexistente_revierte_la_facturacion_completa(self):
        valid = self._create_billable_order("batch-missing-valid")

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([valid["id"], "batch-does-not-exist"], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_NOT_FOUND")
        self.assertIsNone(self._invoice_rows([valid["id"]])[valid["id"]]["invoice"])
        self.assertEqual(self._invoiced_event_count(valid["id"]), 0)

    def test_rechaza_clientes_diferentes_y_revierte_el_lote(self):
        valid = self._create_billable_order("batch-customer-valid")
        other = self._create_billable_order(
            "batch-customer-other",
            customer_code="CLI-DIFERENTE",
            customer_name="Otro cliente",
            customer_id=None,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([valid["id"], other["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_INVOICE_CUSTOMER")
        rows = self._invoice_rows([valid["id"], other["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_rechaza_empresas_diferentes_y_revierte_el_lote(self):
        sepid = self._create_billable_order("batch-company-sepid", company_key="SEPID")
        mgbio = self._create_billable_order("batch-company-mgbio", company_key="MGBIO")

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([sepid["id"], mgbio["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_INVOICE_COMPANY")
        rows = self._invoice_rows([sepid["id"], mgbio["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_pv_reconocido_prevalece_sobre_company_key_historico(self):
        order = self._create_billable_order("batch-company-pv-overrides", company_key="SEPID")
        self._attach_group(
            "brg-batch-company-pv-overrides",
            [order],
            company_key="SEPID",
            point_of_sale="00007",
        )

        serialized = get_delivery_order(order["id"], include_events=False)

        self.assertEqual(serialized["companyKey"], "SEPID")
        self.assertEqual(serialized["billingCompanyKey"], "MGBIO")
        self.assertEqual(serialized["billingPointOfSale"], "00007")

    def test_serializa_empresa_y_pv_desde_resumen_del_grupo(self):
        order = self._create_billable_order("batch-company-summary", company_key="SEPID")
        self._attach_group(
            "brg-batch-company-summary",
            [order],
            company_key="SEPID",
            point_of_sale=None,
            response_summary={"comprobantePtoVenta": "00007"},
        )

        serialized = get_delivery_order(order["id"], include_events=False)

        self.assertEqual(serialized["billingCompanyKey"], "MGBIO")
        self.assertEqual(serialized["billingPointOfSale"], "00007")

    def test_extrae_pv_del_numero_de_remito_como_ultimo_recurso(self):
        order = self._create_billable_order(
            "batch-company-remito-number",
            company_key="SEPID",
            remito_number="RT R 00007-00004567",
        )

        serialized = get_delivery_order(order["id"], include_events=False)

        self.assertEqual(serialized["billingCompanyKey"], "MGBIO")
        self.assertEqual(serialized["billingPointOfSale"], "00007")

    def test_pv_00001_y_desconocido_usan_metadatos_de_empresa(self):
        manual = self._create_billable_order(
            "batch-company-manual-point",
            company_key="MGBIO",
            remito_number="RT R 00001-00004567",
        )
        unknown = self._create_billable_order(
            "batch-company-unknown-point",
            company_key="SEPID",
            remito_number="RT R 00009-00004568",
        )

        manual_serialized = get_delivery_order(manual["id"], include_events=False)
        unknown_serialized = get_delivery_order(unknown["id"], include_events=False)

        self.assertEqual(
            (manual_serialized["billingCompanyKey"], manual_serialized["billingPointOfSale"]),
            ("MGBIO", "00001"),
        )
        self.assertEqual(
            (unknown_serialized["billingCompanyKey"], unknown_serialized["billingPointOfSale"]),
            ("SEPID", "00009"),
        )

    def test_rechaza_lote_con_empresa_indeterminada_y_conserva_accion_individual(self):
        order = self._create_billable_order(
            "batch-company-undetermined",
            company_key="SEPID",
            remito_number="RT R 00002-00004567",
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET company_key = NULL,
                       operation_company_label = '',
                       source_company_id = NULL
                 WHERE id = %s
                """,
                [order["id"]],
            )

        serialized = get_delivery_order(order["id"], include_events=False)
        self.assertEqual(serialized["billingCompanyKey"], "")
        self.assertEqual(serialized["billingPointOfSale"], "00002")

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([order["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_BILLING_COMPANY_REQUIRED")
        self.assertEqual(ctx.exception.status_code, 409)
        updated = mark_invoiced(order["id"], self.cobranzas.id, INVOICE_NUMBER)
        self.assertEqual(updated["invoiceNumber"], INVOICE_NUMBER)

    def test_permite_varios_pv_no_mapeados_cuando_la_empresa_efectiva_coincide(self):
        first = self._create_billable_order(
            "batch-company-multiple-points-first",
            company_key="SEPID",
            remito_number="RT R 00001-00004567",
        )
        second = self._create_billable_order(
            "batch-company-multiple-points-second",
            company_key="SEPID",
            remito_number="RT R 00002-00004568",
        )

        result = mark_invoiced_batch([first["id"], second["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(set(result["orderIds"]), {first["id"], second["id"]})
        self.assertEqual({order["billingCompanyKey"] for order in result["orders"]}, {"SEPID"})
        self.assertEqual(
            {order["billingPointOfSale"] for order in result["orders"]},
            {"00001", "00002"},
        )

    def test_rechaza_pv_de_empresas_distintas_aunque_company_key_coincida(self):
        sepid = self._create_billable_order("batch-pv-sepid", company_key="SEPID")
        mgbio = self._create_billable_order("batch-pv-mgbio", company_key="SEPID")
        self._attach_group(
            "brg-batch-pv-sepid",
            [sepid],
            company_key="SEPID",
            point_of_sale="00004",
        )
        self._attach_group(
            "brg-batch-pv-mgbio",
            [mgbio],
            company_key="SEPID",
            point_of_sale="00007",
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([sepid["id"], mgbio["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_INVOICE_COMPANY")
        rows = self._invoice_rows([sepid["id"], mgbio["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_usa_empresa_efectiva_cuando_la_orden_antigua_no_tiene_company_key(self):
        legacy = self._create_billable_order("batch-company-legacy", company_key="SEPID")
        mgbio = self._create_billable_order("batch-company-effective-mgbio", company_key="MGBIO")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET company_key = NULL,
                       operation_company_label = 'MG BIO'
                 WHERE id = %s
                """,
                [legacy["id"]],
            )

        result = mark_invoiced_batch([legacy["id"], mgbio["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(set(result["orderIds"]), {legacy["id"], mgbio["id"]})
        self.assertEqual({order["invoiceNumber"] for order in result["orders"]}, {INVOICE_NUMBER})

    def test_rechaza_orden_sin_codigo_bejerman_y_revierte_el_lote(self):
        valid = self._create_billable_order("batch-code-valid")
        missing_code = self._create_billable_order(
            "batch-code-missing",
            customer_code=None,
            customer_name="Cliente sin codigo",
            customer_id=None,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([valid["id"], missing_code["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "CUSTOMER_MAPPING_REQUIRED")
        rows = self._invoice_rows([valid["id"], missing_code["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_factura_en_grupo_dos_ordenes_del_mismo_particular(self):
        # Un particular puede traer dos equipos: no tiene codigo Bejerman, pero es una sola persona
        # y una sola factura.
        primera = self._create_particular_order("batch-particular-1", "30710698")
        segunda = self._create_particular_order("batch-particular-2", "30.710.698")

        result = mark_invoiced_batch([primera["id"], segunda["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(set(result["orderIds"]), {primera["id"], segunda["id"]})
        self.assertEqual({order["invoiceNumber"] for order in result["orders"]}, {INVOICE_NUMBER})

    def test_rechaza_dos_particulares_distintos_en_el_mismo_lote(self):
        # Comparten la fila 'Particular' de customers y el codigo vacio: sin mirar el documento
        # del propietario, una sola factura quedaria estampada sobre equipos de dos personas.
        uno = self._create_particular_order("batch-particular-a", "30710698")
        otro = self._create_particular_order("batch-particular-b", "27263312258")

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([uno["id"], otro["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_INVOICE_CUSTOMER")
        rows = self._invoice_rows([uno["id"], otro["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_rechaza_orden_pospuesta_y_revierte_el_lote(self):
        valid = self._create_billable_order("batch-postponed-valid")
        postponed = self._create_billable_order("batch-postponed-order")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET billing_postponed_until = %s,
                       billing_postponed_by_user_id = %s,
                       billing_postponed_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [timezone.now() + timedelta(days=1), self.cobranzas.id, postponed["id"]],
            )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([valid["id"], postponed["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_BILLING_POSTPONED")
        rows = self._invoice_rows([valid["id"], postponed["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_posposicion_vencida_no_bloquea_la_facturacion(self):
        order = self._create_billable_order("batch-postponed-expired")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET billing_postponed_until = %s,
                       billing_postponed_by_user_id = %s,
                       billing_postponed_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [timezone.now() - timedelta(minutes=1), self.cobranzas.id, order["id"]],
            )

        result = mark_invoiced_batch([order["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(result["orderIds"], [order["id"]])
        self.assertEqual(result["orders"][0]["invoiceNumber"], INVOICE_NUMBER)

    def test_rechaza_remito_no_facturable_y_revierte_el_lote(self):
        valid = self._create_billable_order("batch-billable-valid")
        not_billable = self._create_order(
            "batch-not-billable",
            remito_number="RTA 0001-00000001",
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([valid["id"], not_billable["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "INVALID_INVOICE_STATE")
        rows = self._invoice_rows([valid["id"], not_billable["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})

    def test_rechaza_otra_factura_y_revierte_el_lote(self):
        valid = self._create_billable_order("batch-invoice-valid")
        conflicting = self._create_billable_order("batch-invoice-conflict")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET invoice_number = 'FC A 0001-00000001',
                       status = 'facturado',
                       invoiced_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [conflicting["id"]],
            )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch([valid["id"], conflicting["id"]], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_INVOICE_CONFLICT")
        rows = self._invoice_rows([valid["id"], conflicting["id"]])
        self.assertIsNone(rows[valid["id"]]["invoice"])
        self.assertEqual(rows[conflicting["id"]]["invoice"], "FC A 0001-00000001")

    def test_sincroniza_todas_las_hojas_de_servicio_del_lote(self):
        first_ingreso = self._create_ingreso()
        second_ingreso = self._create_ingreso()
        first = self._create_billable_order(
            "batch-service-first",
            delivery_type="service_release",
            ingreso_id=first_ingreso,
        )
        second = self._create_billable_order(
            "batch-service-second",
            delivery_type="service_release",
            ingreso_id=second_ingreso,
        )

        mark_invoiced_batch([first["id"], second["id"]], self.cobranzas.id, INVOICE_NUMBER)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT id, factura_numero FROM ingresos WHERE id = ANY(%s) ORDER BY id",
                [[first_ingreso, second_ingreso]],
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(first_ingreso, INVOICE_NUMBER), (second_ingreso, INVOICE_NUMBER)])

    def test_endpoint_individual_conserva_sincronizacion_por_source_external_id(self):
        ingreso_id = self._create_ingreso()
        order = self._create_billable_order(
            "batch-service-source-external",
            delivery_type="service_release",
            ingreso_id=ingreso_id,
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET ingreso_id = NULL,
                       source_system = 'nexora',
                       source_external_id = %s
                 WHERE id = %s
                """,
                [str(ingreso_id), order["id"]],
            )

        updated = mark_invoiced(order["id"], self.cobranzas.id, INVOICE_NUMBER)

        self.assertEqual(updated["invoiceNumber"], INVOICE_NUMBER)
        with connection.cursor() as cur:
            cur.execute("SELECT factura_numero FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], INVOICE_NUMBER)

    def test_conflicto_en_hoja_de_servicio_revierte_ordenes_y_otras_hojas(self):
        blank_ingreso = self._create_ingreso()
        conflict_ingreso = self._create_ingreso("FC A 0001-00000002")
        blank_order = self._create_billable_order(
            "a-batch-service-blank",
            delivery_type="service_release",
            ingreso_id=blank_ingreso,
        )
        conflict_order = self._create_billable_order(
            "z-batch-service-conflict",
            delivery_type="service_release",
            ingreso_id=conflict_ingreso,
        )

        with self.assertRaises(DeliveryOrderError) as ctx:
            mark_invoiced_batch(
                [blank_order["id"], conflict_order["id"]],
                self.cobranzas.id,
                INVOICE_NUMBER,
            )

        self.assertEqual(ctx.exception.code, "SERVICE_ORDER_INVOICE_CONFLICT")
        rows = self._invoice_rows([blank_order["id"], conflict_order["id"]])
        self.assertEqual({row["invoice"] for row in rows.values()}, {None})
        with connection.cursor() as cur:
            cur.execute(
                "SELECT id, factura_numero FROM ingresos WHERE id = ANY(%s) ORDER BY id",
                [[blank_ingreso, conflict_ingreso]],
            )
            ingreso_rows = dict(cur.fetchall())
        self.assertIsNone(ingreso_rows[blank_ingreso])
        self.assertEqual(ingreso_rows[conflict_ingreso], "FC A 0001-00000002")

    def test_endpoint_usa_permiso_de_facturacion_y_conserva_contrato(self):
        allowed = self._create_billable_order("batch-api-allowed")
        self.client.force_authenticate(user=self.cobranzas)

        response = self.client.post(
            "/api/ordenes-entrega/facturar-lote/",
            {"orderIds": [allowed["id"]], "invoiceNumber": INVOICE_NUMBER},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["invoiceNumber"], INVOICE_NUMBER)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["orderIds"], [allowed["id"]])
        self.assertEqual(response.data["orders"][0]["invoiceNumber"], INVOICE_NUMBER)

        denied = self._create_billable_order("batch-api-denied")
        self.client.force_authenticate(user=self.recepcion)
        denied_response = self.client.post(
            "/api/ordenes-entrega/facturar-lote/",
            {"orderIds": [denied["id"]], "invoiceNumber": INVOICE_NUMBER},
            format="json",
        )

        self.assertEqual(denied_response.status_code, 403)
        self.assertIsNone(self._invoice_rows([denied["id"]])[denied["id"]]["invoice"])
