import base64
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_companies import list_ingress_companies, require_company
from service.bejerman_delivery import (
    BillingError,
    _assert_partidas_ready,
    _bridge_order,
    _profile_for_order,
    _remito_requires_billing,
    _validate_remito_orders,
    get_delivery_order_invoice_pdf,
    get_facturacion_pdf,
    list_bejerman_article_stock,
    list_bejerman_articles,
    list_bejerman_depositos,
    list_facturacion_from_bejerman,
    resolve_delivery_order_invoice_document,
)
from service.delivery_orders import DeliveryOrderError, _service_release_equipment_detail, insert_item_partidas, remito_profile_for_type
from service.views.delivery_orders_views import _remito_print_wait_page
from service.bejerman_ris import _build_payload
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanPdfPendingError,
    BejermanPdfReference,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    build_article_filters,
    build_article_stock_lots,
    build_delivery_remito_comprobante,
    build_document_id,
    build_partida_rows,
    build_sales_filters,
    build_service_ingress_comprobante,
    build_stock_rows,
    delivery_remito_config,
    extract_pdf_bytes,
    fetch_comprobante_pdf,
)


class FakeSDKClient:
    def __init__(self, customer_records=None):
        self.calls = []
        self.customer_records = customer_records or [
            {
                "Cliente_Codigo": "ACU",
                "Cliente_RazonSocial": "ACUMAR",
                "Cliente_NroDocumento": "30711111111",
            }
        ]

    def list_clientes(self):
        self.calls.append(("list_clientes", []))
        return {"DatosJSON": self.customer_records}

    def list_comprobantes_ventas(self, filters):
        self.calls.append(("list_comprobantes_ventas", filters))
        values = {item.get("Campo"): item.get("Valor") for item in filters}
        if values.get("Comprobante_Tipo") != "FC":
            return {"DatosJSON": []}
        customer_code = values.get("Cliente_Codigo") or ""
        if customer_code and customer_code.strip().upper() not in {"ACU", "NOV"}:
            return {"DatosJSON": []}
        response_customer_code = customer_code or "GEN"
        return {
            "DatosJSON": [
                {
                    "Comprobante_Tipo": "FC",
                    "Comprobante_Letra": "A",
                    "Comprobante_PtoVenta": "0001",
                    "Comprobante_Numero": "00000012",
                    "Comprobante_FechaEmision": "2024-06-01",
                    "Cliente_Codigo": response_customer_code,
                    "Cliente_RazonSocial": "NOVAMED S.A." if customer_code.strip().upper() == "NOV" else "ACUMAR",
                    "Comprobante_TipoOperacion": "REP",
                    "Comprobante_ImporteTotal": 10,
                }
            ]
        }


class FakePdfClient:
    def __init__(self, consult_responses):
        self.consult_responses = list(consult_responses)
        self.consult_calls = 0
        self.generate_calls = 0
        self.timeout = 30
        self.timeout_values = []

    def consultar_comprobante_ventas_pdf(self, reference):
        self.consult_calls += 1
        self.timeout_values.append(self.timeout)
        if self.consult_responses:
            response = self.consult_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"DatosJSON": {}}

    def generar_comprobante_ventas_pdf(self, reference):
        self.generate_calls += 1
        self.timeout_values.append(self.timeout)
        return {"Resultado": "OK", "DatosJSON": "{}"}


class FakePdfGenerateTimeoutClient(FakePdfClient):
    def generar_comprobante_ventas_pdf(self, reference):
        self.generate_calls += 1
        self.timeout_values.append(self.timeout)
        raise BejermanSdkUnavailable("Read timed out")


class FakeArticleCatalogClient:
    def __init__(self):
        self.calls = []

    def list_articulos(self, filters, limit):
        self.calls.append((filters, limit))
        return {
            "DatosJSON": [
                {
                    "Articulo_Codigo": "1202018",
                    "Articulo_Descripcion": "Máscara nasal adulto",
                    "Articulo_Nombre": "Máscara nasal adulto",
                },
                {
                    "Articulo_Codigo": "1202018",
                    "Articulo_Descripcion": "Máscara nasal adulto duplicada",
                    "Articulo_Nombre": "Máscara duplicada",
                },
                {
                    "Articulo_Codigo": "999",
                    "Articulo_Descripcion": "Filtro descartable",
                    "Articulo_Nombre": "Filtro descartable",
                },
            ]
        }


class FakeArticleStockClient:
    def __init__(self):
        self.deposit_calls = []
        self.partida_calls = 0

    def obtener_stock_deposito(self, deposit_code):
        self.deposit_calls.append(deposit_code)
        return {
            "DatosJSON": [
                {
                    "Articulo_Codigo": "1202018",
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "L-VAL-1",
                    "Art_DispUM1": 2,
                    "Art_RealUM1": 3,
                },
                {
                    "Articulo_Codigo": "1202018",
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "L-VAL-0",
                    "Art_DispUM1": 0,
                    "Art_RealUM1": 5,
                },
                {
                    "Articulo_Codigo": "1202018",
                    "Art_CodDeposito": "STL",
                    "Art_Partida": "L-STL-1",
                    "Art_DispUM1": 4,
                    "Art_RealUM1": 4,
                },
                {
                    "Articulo_Codigo": "1202019",
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "L-OTRO",
                    "Art_DispUM1": 9,
                    "Art_RealUM1": 9,
                },
            ]
        }

    def obtener_depositos(self):
        return {
            "DatosJSON": [
                {"Deposito_Codigo": "VAL", "Deposito_Descripcion": "Ventas"},
                {"Deposito_Codigo": "STL", "Deposito_Descripcion": "Listos"},
            ]
        }

    def obtener_partidas(self):
        self.partida_calls += 1
        return {
            "DatosJSON": [
                {
                    "Partida_Codigo": "L-VAL-1",
                    "Deposito_Codigo": "VAL",
                    "Partida_FechaVtoIngreso": "2030-12-31",
                },
                {
                    "Partida_Codigo": "L-STL-1",
                    "Deposito_Codigo": "STL",
                    "Partida_FechaVtoIngreso": "2030-12-31",
                },
            ]
        }


def _pdf_response():
    return {
        "DatosJSON": json.dumps(
            {"archivoPdf": base64.b64encode(b"%PDF-1.4\nRIS test").decode("ascii")},
            ensure_ascii=False,
        )
    }


def _non_pdf_response():
    return {
        "DatosJSON": json.dumps(
            {"archivoPdf": base64.b64encode(b"<html>PDF pendiente</html>").decode("ascii")},
            ensure_ascii=False,
        )
    }


class BejermanPdfHelperTests(SimpleTestCase):
    def test_extract_pdf_bytes_ignora_base64_que_no_es_pdf(self):
        self.assertIsNone(extract_pdf_bytes(_non_pdf_response()))

    def test_fetch_comprobante_pdf_genera_y_reintenta_hasta_encontrar_pdf(self):
        client = FakePdfClient([{"DatosJSON": "{}"}, {"DatosJSON": "{}"}, _pdf_response()])

        pdf_bytes, content_type = fetch_comprobante_pdf(
            client,
            BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
            retry_attempts=2,
            retry_delay_ms=0,
        )

        self.assertEqual(content_type, "application/pdf")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 3)

    def test_fetch_comprobante_pdf_no_devuelve_200_con_bytes_no_pdf(self):
        client = FakePdfClient([_non_pdf_response(), _non_pdf_response()])

        with self.assertRaises(BejermanPdfPendingError):
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                retry_attempts=1,
                retry_delay_ms=0,
            )

        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 2)

    def test_fetch_comprobante_pdf_pendiente_no_es_error_generico(self):
        client = FakePdfClient([{"DatosJSON": "{}"}, {"DatosJSON": "{}"}, {"DatosJSON": "{}"}])

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                retry_attempts=2,
                retry_delay_ms=0,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 1000)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 3)

    def test_fetch_comprobante_pdf_consulta_error_persistente_queda_pendiente(self):
        client = FakePdfClient(
            [
                BejermanSdkResponseError("HTTP 406 Bejerman: PDF no disponible"),
                BejermanSdkResponseError("HTTP 406 Bejerman: PDF no disponible"),
                BejermanSdkResponseError("HTTP 406 Bejerman: PDF no disponible"),
            ]
        )

        with self.assertRaises(BejermanPdfPendingError):
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                retry_attempts=2,
                retry_delay_ms=0,
            )

        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 3)

    @override_settings(
        BEJERMAN_PDF_INTERACTIVE_REQUEST_TIMEOUT=4,
        BEJERMAN_PDF_INTERACTIVE_RETRY_ATTEMPTS=1,
        BEJERMAN_PDF_INTERACTIVE_RETRY_DELAY_MS=0,
        BEJERMAN_PDF_INTERACTIVE_RETRY_AFTER_MS=2500,
    )
    def test_fetch_comprobante_pdf_interactivo_usa_timeout_y_reintentos_cortos(self):
        client = FakePdfClient([{"DatosJSON": "{}"}, {"DatosJSON": "{}"}])

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                interactive=True,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 2500)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 2)
        self.assertEqual(client.timeout, 30)
        self.assertTrue(client.timeout_values)
        self.assertTrue(all(value == 4 for value in client.timeout_values))

    def test_fetch_comprobante_pdf_interactivo_timeout_generando_queda_pendiente_rapido(self):
        client = FakePdfGenerateTimeoutClient([{"DatosJSON": "{}"}])

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                interactive=True,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 1200)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 1)
        self.assertEqual(client.timeout, 30)
        self.assertTrue(client.timeout_values)
        self.assertTrue(all(value == 4 for value in client.timeout_values))


class BejermanDeliveryBillingTests(SimpleTestCase):
    def test_facturacion_uses_sdk_sales_filters(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_facturacion_from_bejerman(
                "ACU",
                {
                    "dateFrom": "2024-01-01",
                    "dateTo": "2024-12-31",
                    "origin": "Reparación",
                    "page": "2",
                    "pageSize": "25",
                    "search": "FAC",
                },
            )

        self.assertEqual(client.calls[0][0], "list_clientes")
        self.assertEqual(client.calls[1][0], "list_comprobantes_ventas")
        self.assertEqual(len([call for call in client.calls if call[0] == "list_comprobantes_ventas"]), 3)
        self.assertEqual(
            client.calls[1][1],
            [
                {"Campo": "Cliente_Codigo", "Accion": "IGUAL", "Valor": "ACU", "Operacion": "Y", "Enlazada": False},
                {"Campo": "Comprobante_Tipo", "Accion": "IGUAL", "Valor": "FC", "Operacion": "Y", "Enlazada": False},
                {
                    "Campo": "Comprobante_FechaEmision",
                    "Accion": "MAYOR O IGUAL",
                    "Valor": "2024-01-01",
                    "Operacion": "Y",
                    "Enlazada": False,
                },
                {
                    "Campo": "Comprobante_FechaEmision",
                    "Accion": "MENOR O IGUAL",
                    "Valor": "2024-12-31",
                    "Operacion": "Y",
                    "Enlazada": False,
                },
            ],
        )
        self.assertEqual(result["pagination"]["pageSize"], 25)

    def test_facturacion_accepts_legacy_filter_aliases(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            list_facturacion_from_bejerman(
                "ACU",
                {
                    "desde": "2024-01-01",
                    "hasta": "2024-12-31",
                    "tipo": "Reparación",
                },
            )

        self.assertEqual(client.calls[1][1][2]["Valor"], "2024-01-01")
        self.assertEqual(client.calls[1][1][3]["Valor"], "2024-12-31")

    def test_facturacion_resuelve_codigo_bejerman_por_nombre_local(self):
        client = FakeSDKClient(
            [
                {
                    "Cliente_Codigo": "   NOV",
                    "Cliente_RazonSocial": "NOVAMED S.A.",
                    "Cliente_NroDocumento": "30710659768",
                }
            ]
        )

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=client),
            patch(
                "service.bejerman_delivery._local_customer_for_code",
                return_value={"name": "NOVAMED S.A.", "cuit": "", "code": "ame"},
            ),
        ):
            result = list_facturacion_from_bejerman("ame", {"dateFrom": "2024-01-01", "dateTo": "2024-12-31"})

        self.assertEqual(result["effectiveCustomerCode"], "NOV")
        self.assertEqual(result["bejermanCustomerCode"], "   NOV")
        self.assertEqual(client.calls[1][1][0]["Valor"], "   NOV")
        self.assertEqual(result["pagination"]["total"], 1)

    def test_facturacion_general_omite_filtro_de_cliente_y_pagina_de_a_25(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_facturacion_from_bejerman("", {"dateFrom": "2024-01-01", "dateTo": "2024-12-31"})

        self.assertEqual(client.calls[0][0], "list_comprobantes_ventas")
        self.assertEqual(len([call for call in client.calls if call[0] == "list_clientes"]), 0)
        self.assertEqual(client.calls[0][1][0], {"Campo": "Comprobante_Tipo", "Accion": "IGUAL", "Valor": "FC", "Operacion": "Y", "Enlazada": False})
        self.assertFalse(any(item["Campo"] == "Cliente_Codigo" for item in client.calls[0][1]))
        self.assertEqual(result["scope"], "company")
        self.assertEqual(result["pagination"]["pageSize"], 25)

    def test_build_sales_filters_preserves_padded_customer_code(self):
        filters = build_sales_filters("   NOV", "2024-01-01", "2024-12-31", "FC")

        self.assertEqual(filters[0]["Valor"], "   NOV")
        self.assertEqual(filters[1]["Valor"], "FC")

    def test_build_sales_filters_allows_company_scope(self):
        filters = build_sales_filters("", "2024-01-01", "2024-12-31", "FC")

        self.assertFalse(any(item["Campo"] == "Cliente_Codigo" for item in filters))
        self.assertEqual(filters[0]["Campo"], "Comprobante_Tipo")

    def test_facturacion_pdf_interactivo_timeout_queda_pendiente(self):
        document_id = build_document_id(
            {"t": "RSS", "n": "00000012", "l": "R", "p": "0004", "f": "2026-06-12", "c": "ACU"}
        )

        with patch(
            "service.bejerman_delivery.fetch_comprobante_pdf",
            side_effect=BejermanSdkUnavailable("Error HTTP Bejerman: Read timed out"),
        ):
            with self.assertRaises(BillingError) as ctx:
                get_facturacion_pdf("ACU", document_id, interactive=True)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_PENDING")
        self.assertEqual(ctx.exception.status_code, 202)
        self.assertEqual(ctx.exception.retry_after_ms, 5000)

    def test_facturacion_pdf_usa_empresa_bejerman_indicada(self):
        document_id = build_document_id(
            {"t": "RT", "n": "00000012", "l": "R", "p": "00007", "f": "2026-06-12", "c": "ACU"}
        )

        with patch("service.bejerman_delivery.BejermanSDKClient") as client_cls:
            with patch("service.bejerman_delivery.fetch_comprobante_pdf", return_value=(b"%PDF-1.4", "application/pdf")):
                get_facturacion_pdf("ACU", document_id, actor_user_id=1152, company_key="MGBIO")

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152)

    def test_resuelve_factura_de_orden_por_punto_y_numero_y_empresa(self):
        document_id = build_document_id(
            {"t": "FC", "n": "00000012", "l": "A", "p": "0001", "f": "2026-06-18", "c": "ACU"}
        )
        order = {
            "id": "do-invoice",
            "invoiceNumber": "000100000012",
            "bejermanCustomerCode": "ACU",
            "companyKey": "MGBIO",
            "invoicedAt": "2026-06-18T12:00:00-03:00",
        }

        with patch(
            "service.bejerman_delivery.list_facturacion_from_bejerman",
            return_value={
                "items": [
                    {
                        "documentId": document_id,
                        "documentNumber": "FC A 0001-00000012",
                        "type": "FC",
                        "letter": "A",
                        "pointOfSale": "0001",
                        "number": "00000012",
                        "bejermanCustomerCode": "ACU",
                    }
                ],
                "pagination": {"hasNextPage": False, "totalPages": 1},
                "effectiveCustomerCode": "ACU",
            },
        ) as list_mock:
            document = resolve_delivery_order_invoice_document(order, actor_user_id=1152)

        self.assertEqual(document["documentId"], document_id)
        self.assertEqual(document["companyKey"], "MGBIO")
        args, kwargs = list_mock.call_args
        self.assertEqual(args[0], "ACU")
        self.assertEqual(args[1]["origin"], "FC")
        self.assertNotIn("search", args[1])
        self.assertEqual(kwargs["actor_user_id"], 1152)
        self.assertEqual(kwargs["company_key"], "MGBIO")

    def test_resuelve_factura_de_orden_rechaza_datos_incompletos(self):
        with self.assertRaises(BillingError) as invoice_ctx:
            resolve_delivery_order_invoice_document({"bejermanCustomerCode": "ACU"})
        self.assertEqual(invoice_ctx.exception.code, "INVOICE_REQUIRED")

        with self.assertRaises(BillingError) as customer_ctx:
            resolve_delivery_order_invoice_document({"invoiceNumber": "FC A 0001-00000012"})
        self.assertEqual(customer_ctx.exception.code, "CUSTOMER_CODE_REQUIRED")

    def test_resuelve_factura_de_orden_rechaza_no_encontrada_y_ambigua(self):
        base_order = {
            "id": "do-invoice",
            "invoiceNumber": "FC A 0001-00000012",
            "bejermanCustomerCode": "ACU",
            "companyKey": "SEPID",
        }

        with patch(
            "service.bejerman_delivery.list_facturacion_from_bejerman",
            return_value={"items": [], "pagination": {"hasNextPage": False, "totalPages": 1}},
        ):
            with self.assertRaises(BillingError) as not_found_ctx:
                resolve_delivery_order_invoice_document(base_order)
        self.assertEqual(not_found_ctx.exception.code, "INVOICE_DOCUMENT_NOT_FOUND")
        self.assertEqual(not_found_ctx.exception.status_code, 404)

        with patch(
            "service.bejerman_delivery.list_facturacion_from_bejerman",
            return_value={
                "items": [
                    {"documentId": "doc-1", "documentNumber": "FC A 0001-00000012", "pointOfSale": "0001", "number": "00000012"},
                    {"documentId": "doc-2", "documentNumber": "FC A 0001-00000012", "pointOfSale": "0001", "number": "00000012"},
                ],
                "pagination": {"hasNextPage": False, "totalPages": 1},
            },
        ):
            with self.assertRaises(BillingError) as ambiguous_ctx:
                resolve_delivery_order_invoice_document(base_order)
        self.assertEqual(ambiguous_ctx.exception.code, "INVOICE_DOCUMENT_AMBIGUOUS")
        self.assertEqual(ambiguous_ctx.exception.status_code, 409)

    def test_pdf_factura_de_orden_usa_empresa_de_la_orden(self):
        document_id = build_document_id(
            {"t": "FC", "n": "00000012", "l": "A", "p": "0007", "f": "2026-06-18", "c": "ACU"}
        )
        order = {
            "id": "do-invoice",
            "invoiceNumber": "FC A 0007-00000012",
            "bejermanCustomerCode": "ACU",
            "companyKey": "MGBIO",
        }

        with (
            patch("service.bejerman_delivery.get_delivery_order", return_value=order),
            patch(
                "service.bejerman_delivery.resolve_delivery_order_invoice_document",
                return_value={"documentId": document_id, "bejermanCustomerCode": "ACU", "companyKey": "MGBIO"},
            ),
            patch("service.bejerman_delivery.get_facturacion_pdf", return_value=(b"%PDF-1.4", "application/pdf")) as pdf_mock,
        ):
            pdf_bytes, content_type, filename = get_delivery_order_invoice_pdf("do-invoice", actor_user_id=1152)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(filename, "factura-FC-A-0007-00000012.pdf")
        pdf_mock.assert_called_once_with(
            "ACU",
            document_id,
            interactive=True,
            actor_user_id=1152,
            company_key="MGBIO",
        )


class BejermanDeliveryArticleTests(SimpleTestCase):
    def test_sdk_article_catalog_fetches_full_catalog_without_search_parametros(self):
        client = BejermanSDKClient(company_key="SEPID")
        client.token = "TOKEN"

        with patch.object(client, "_post", return_value={"Resultado": "OK", "ErrorMsg": "", "DatosJSON": []}) as post:
            client.list_articulos(build_article_filters("máscara", field="description"), 20)

        body = post.call_args.args[1]
        self.assertIn("<a:Operacion>ObtenerArticulos</a:Operacion>", body)
        self.assertIn('<a:Parametros i:nil="true" />', body)
        self.assertIn("<a:ParametrosJson>[]</a:ParametrosJson>", body)
        self.assertNotIn("máscara", body)
        self.assertNotIn("<b:anyType", body)

    def test_article_search_filters_full_catalog_by_description_and_code_without_duplicates(self):
        client = FakeArticleCatalogClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            by_description = list_bejerman_articles("máscara", 20)
            by_code = list_bejerman_articles("1202018", 20)

        self.assertEqual([item["code"] for item in by_description["items"]], ["1202018"])
        self.assertEqual([item["code"] for item in by_code["items"]], ["1202018"])
        self.assertEqual(len({item["code"] for item in by_description["items"]}), len(by_description["items"]))
        self.assertEqual(len(client.calls), 4)

    def test_article_stock_returns_only_positive_val_lots_with_expiration(self):
        client = FakeArticleStockClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_bejerman_article_stock("1202018", 100)

        self.assertEqual(client.deposit_calls, ["VAL"])
        self.assertEqual(client.partida_calls, 1)
        self.assertFalse(result["unavailable"])
        self.assertEqual(result["depositCode"], "VAL")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["partida"], "L-VAL-1")
        self.assertEqual(item["stockDepositCode"], "VAL")
        self.assertEqual(item["stockAvailableQuantity"], 2)
        self.assertEqual(item["partidaExpirationDate"], "2030-12-31")
        self.assertTrue(item["stockCheckedAt"])

    def test_article_stock_uses_requested_company_and_deposit(self):
        client = FakeArticleStockClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client) as client_cls:
            result = list_bejerman_article_stock("1202018", 100, actor_user_id=1152, company_key="MGBIO", deposit_code="STL")

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152)
        self.assertEqual(client.deposit_calls, ["STL"])
        self.assertEqual(result["companyKey"], "MGBIO")
        self.assertEqual(result["depositCode"], "STL")
        self.assertEqual([item["partida"] for item in result["items"]], ["L-STL-1"])
        self.assertEqual(result["items"][0]["stockDepositCode"], "STL")

    def test_bejerman_depositos_returns_sdk_deposits_for_company(self):
        client = FakeArticleStockClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client) as client_cls:
            result = list_bejerman_depositos("MGBIO", actor_user_id=1152)

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152)
        self.assertFalse(result["unavailable"])
        self.assertEqual([item["code"] for item in result["items"]], ["VAL", "STL"])

    def test_article_stock_helper_filters_deposit_and_zero_stock(self):
        stock_rows = build_stock_rows(FakeArticleStockClient().obtener_stock_deposito("VAL"))
        partida_rows = build_partida_rows(FakeArticleStockClient().obtener_partidas())

        lots = build_article_stock_lots(stock_rows, partida_rows, "1202018", "VAL")

        self.assertEqual([lot["partida"] for lot in lots], ["L-VAL-1"])
        self.assertTrue(all(lot["depositCode"] == "VAL" for lot in lots))
        self.assertTrue(all((lot["availableQuantity"] if lot["availableQuantity"] is not None else lot["realQuantity"]) > 0 for lot in lots))

    def test_insert_item_partidas_persists_stock_metadata(self):
        cur = Mock()

        insert_item_partidas(
            cur,
            "doi-1",
            [
                {
                    "partida": "L-VAL-1",
                    "assignedQuantity": "1",
                    "partidaExpirationDate": "2030-12-31",
                    "stockDepositCode": "VAL",
                    "stockAvailableQuantity": "2",
                    "stockCheckedAt": "2026-06-17T10:00:00-03:00",
                }
            ],
        )

        values = cur.execute.call_args.args[1]
        self.assertEqual(values[2], "L-VAL-1")
        self.assertEqual(values[4], "2030-12-31")
        self.assertEqual(values[5], "VAL")
        self.assertEqual(str(values[6]), "2")
        self.assertEqual(values[7], "2026-06-17T10:00:00-03:00")


class BejermanIngressCompanyTests(SimpleTestCase):
    def test_ingress_companies_are_controlled(self):
        companies = list_ingress_companies()

        self.assertEqual([company.key for company in companies], ["SEPID", "MGBIO", "TEST"])
        self.assertEqual(require_company("MGBI").key, "MGBIO")
        self.assertEqual(require_company("MODE").key, "TEST")

    @override_settings(BEJERMAN_COMPANY_TEST="MODE")
    def test_sdk_resolves_company_key(self):
        self.assertEqual(BejermanSDKClient(company_key="SEPID").company, "SEP")
        self.assertEqual(BejermanSDKClient(company_key="MGBIO").company, "MGBI")
        self.assertEqual(BejermanSDKClient(company_key="TEST").company, "MODE")

    def test_sdk_sends_parametros_json_as_single_json_string(self):
        client = BejermanSDKClient(company_key="SEPID")
        client.token = "TOKEN"

        with patch.object(client, "_post", return_value={"Resultado": "OK", "ErrorMsg": ""}) as post:
            client.list_comprobantes_ventas([{"Campo": "Cliente_Codigo", "Valor": "ALCLA"}])

        body = post.call_args.args[1]
        self.assertIn("<a:ParametrosJson>", body)
        self.assertIn('\\"ALCLA\\"', body)
        self.assertIn('"N"', body)
        self.assertNotIn("<b:string>", body)

    def test_ris_payload_includes_company_key(self):
        payload = _build_payload(
            {
                "id": 29012,
                "empresa_bejerman": "MGBIO",
                "customer_code": "ARGAS",
                "customer_name": "ARGENTINA DE GASES S.A.",
                "fecha_ingreso": "2026-06-09",
                "numero_serie": "A3125412159",
                "numero_interno": "",
                "tipo_equipo": "BPAP",
                "marca": "BMC",
                "modelo": "GI",
                "equipo_variante": "25",
                "device_variante": "",
                "modelo_variante": "",
                "motivo": "No enciende",
                "accesorios": "",
                "comentarios": "",
                "model_id": None,
            }
        )

        self.assertEqual(payload["companyKey"], "MGBIO")
        self.assertEqual(payload["companyLabel"], "MG BIO")
        self.assertEqual(payload["bejermanCompany"], "MGBI")
        self.assertEqual(payload["documentProfile"]["deposit"], "STR")
        self.assertEqual(payload["equipment"]["articleCode"], "")
        self.assertTrue(payload["equipment"]["mappingRequired"])
        self.assertEqual(payload["equipment"]["articleName"], "BPAP BMC GI 25")
        self.assertEqual(payload["equipment"]["mappedArticleName"], "Equipo recibido para servicio técnico")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_uses_bejerman_customer_fiscal_fields_and_no_stock(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-1",
                "ingresoId": 1,
                "issueDate": "2026-06-12",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipment": {"articleCode": "SERVICIO", "serial": "SN-1"},
            },
            {
                "Cliente_RazonSocial": "ALCLA SRL",
                "Cliente_NroDocumento": "30700000000",
                "Cliente_Provincia": "02",
                "Cliente_SitIVA": "RI",
                "Cliente_NumeroIIBB": "123",
            },
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Cliente_SitIVA"], "RI")
        self.assertEqual(comprobante["Cliente_RazonSocial"], "ALCLA SRL")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "N")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_Partida"], " ")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_article_line_uses_equipment_details(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-29382",
                "ingresoId": 29382,
                "issueDate": "2026-06-12",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipment": {
                    "articleCode": "1115003",
                    "articleName": "Equipo recibido para servicio técnico",
                    "serial": "G0588065",
                    "internalNumber": "MG 6697",
                    "equipmentType": "OXÍMETRO DE PULSO",
                    "brand": "Nellcor",
                    "model": "N-595",
                    "repairReason": "Reparación",
                },
            },
            {"Cliente_RazonSocial": "ALCLA SRL"},
        )

        comprobante = built["comprobante"]
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_DescripArticulo"], "OXÍMETRO DE PULSO | Nellcor | N-595")
        self.assertEqual(legend_lines[0], "------------------------------")
        self.assertIn("OS 29382", legend_lines[1])
        self.assertNotIn("OXÍMETRO", legend_lines[1])

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_accepts_multiple_equipments(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-lote-1-2",
                "ingresoId": 1,
                "ingresoIds": [1, 2],
                "issueDate": "2026-06-12",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipments": [
                    {
                        "articleCode": "SERVICIO",
                        "articleName": "Equipo recibido",
                        "serial": "SN-1",
                        "equipmentType": "CPAP",
                        "brand": "BMC",
                        "model": "G3",
                        "repairReason": "No enciende",
                        "accessories": "Bolso (ref: B-1)",
                        "osLabel": "00001",
                    },
                    {
                        "articleCode": "SERVICIO",
                        "articleName": "Equipo recibido",
                        "serial": "SN-2",
                        "brand": "ResMed",
                        "model": "AirSense 10",
                        "repairReason": "Ruido",
                        "osLabel": "00002",
                    },
                ],
            },
            {"Cliente_RazonSocial": "ALCLA SRL"},
        )

        comprobante = built["comprobante"]
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(legend_lines[0], "Se recibe para servicio técnico:")
        self.assertEqual(legend_lines.count("------------------------------"), 2)
        self.assertTrue(any("SN-1" in line for line in legend_lines))
        self.assertTrue(any("Motivo: No enciende - Accesorios: Bolso (ref: B-1)" in line for line in legend_lines))
        self.assertEqual(article_lines[0]["Item_DescripArticulo"], "CPAP | BMC | G3")
        self.assertEqual(article_lines[1]["Item_DescripArticulo"], "ResMed | AirSense 10")
        self.assertEqual(len(article_lines), 2)
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "N")


class BejermanDeliveryRemitoPayloadTests(SimpleTestCase):
    def test_service_release_equipment_detail_matches_rss_visible_data(self):
        detail = _service_release_equipment_detail(
            {
                "tipo_equipo": "OXÍMETRO DE PULSO",
                "marca": "Nellcor",
                "modelo": "N-595",
                "ingreso_id": 29381,
                "equipment_serial": "G0588065",
                "equipment_internal_number": "MG 6697",
            }
        )

        self.assertEqual(
            detail,
            "OXÍMETRO DE PULSO | Nellcor | N-595 - OS-29381 - N° interno (MG) MG 6697",
        )
        self.assertNotIn("G0588065", detail)

    def test_nexora_order_payload_uses_equipment_serial_as_item_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OES-1",
                "deliveryType": "service_release",
                "ingresoId": 29381,
                "sourceReference": "OS 29381",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 1,
                    }
                ],
            }
        )

        self.assertEqual(payload["items"][0]["partida"], "SN-123")
        self.assertEqual(payload["sourceReference"], "OS-29381")

    def test_nexora_order_payload_keeps_explicit_item_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OE-1",
                "deliveryType": "sale",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 1,
                        "partida": "MANUAL-1",
                    }
                ],
            }
        )

        self.assertEqual(payload["items"][0]["partida"], "MANUAL-1")

    def test_nexora_order_payload_reenvia_moneda_cotizacion_y_descuento(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OE-1",
                "deliveryType": "sale",
                "priceCurrency": "USD",
                "commercialExchangeRate": "1200",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 2,
                        "unitPrice": 100,
                        "priceCurrency": "USD",
                        "discountPercent": 10,
                        "partida": "P1",
                    }
                ],
            }
        )

        self.assertEqual(payload["priceCurrency"], "USD")
        self.assertEqual(payload["commercialExchangeRate"], "1200")
        self.assertEqual(payload["items"][0]["priceCurrency"], "USD")
        self.assertEqual(payload["items"][0]["discountPercent"], 10)

    def test_sale_order_payload_does_not_use_equipment_serial_as_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OE-1",
                "deliveryType": "sale",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 1,
                    }
                ],
            }
        )

        self.assertIsNone(payload["items"][0]["partida"])

    def test_sale_order_requires_article_partida_before_remito(self):
        with self.assertRaisesRegex(Exception, "Complete las partidas"):
            _assert_partidas_ready(
                {
                    "deliveryType": "sale",
                    "equipmentSerial": "SN-123",
                    "items": [
                        {
                            "id": "doi-1",
                            "articleCode": "110706",
                            "quantity": 1,
                        }
                    ],
                }
            )

    def test_sale_remito_profile_is_not_rss(self):
        sale_profile = remito_profile_for_type("sale")
        service_profile = remito_profile_for_type("service_release")

        self.assertEqual(sale_profile.comprobante_tipo, "RT")
        self.assertEqual(sale_profile.point_of_sale, "00004")
        self.assertEqual(sale_profile.deposit_code, "VAL")
        self.assertEqual(service_profile.comprobante_tipo, "RSS")
        self.assertNotEqual(sale_profile.comprobante_tipo, service_profile.comprobante_tipo)

    def test_delivery_remito_profile_usa_punto_por_empresa(self):
        with override_settings(
            BEJERMAN_REMITO_SALE_POINT_OF_SALE="00002",
            BEJERMAN_REMITO_SALE_SEPID_POINT_OF_SALE="00003",
            BEJERMAN_REMITO_SALE_MGBIO_POINT_OF_SALE="00008",
        ):
            sepid_profile = _profile_for_order("sale", "SEPID")
            mgbio_profile = _profile_for_order("sale", "MGBIO")

        self.assertEqual(sepid_profile["type"], "RT")
        self.assertEqual(sepid_profile["pointOfSale"], "00004")
        self.assertEqual(mgbio_profile["type"], "RT")
        self.assertEqual(mgbio_profile["pointOfSale"], "00007")
        self.assertEqual(delivery_remito_config(sepid_profile)["salePointOfSale"], "00004")
        self.assertEqual(delivery_remito_config(mgbio_profile)["salePointOfSale"], "00007")

    def test_delivery_remito_lote_no_permite_mezclar_empresas(self):
        def order(order_id, company_label):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "operationCompanyLabel": company_label,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        compatibility = _validate_remito_orders([order("do-1", "SEPID")])
        self.assertEqual(compatibility["companyKey"], "SEPID")
        self.assertEqual(_validate_remito_orders([order("do-2", "MG BIO")])["companyKey"], "MGBIO")
        with self.assertRaisesRegex(Exception, "empresas Bejerman distintas"):
            _validate_remito_orders([order("do-1", "SEPID"), order("do-2", "MG BIO")])

    def test_delivery_remito_lote_no_permite_mezclar_monedas(self):
        def order(order_id, price_currency):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "companyKey": "SEPID",
                "priceCurrency": price_currency,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order("do-ars", "ARS"), order("do-usd", "USD")])

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")

    def test_delivery_remito_lote_no_permite_mezclar_monedas_por_item(self):
        order = {
            "id": "do-mixed-items",
            "bejermanCustomerCode": "ACU",
            "customerName": "ACUMAR",
            "deliveryType": "sale",
            "status": "armado_pendiente_entrega",
            "companyKey": "SEPID",
            "priceCurrency": "ARS",
            "items": [
                {"articleCode": "110706", "quantity": 1, "partida": "P1", "priceCurrency": "ARS"},
                {"articleCode": "110707", "quantity": 1, "partida": "P2", "priceCurrency": "USD"},
            ],
        }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order])

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")

    def test_delivery_remito_lote_usd_no_permite_mezclar_cotizaciones(self):
        def order(order_id, exchange_rate):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "companyKey": "SEPID",
                "priceCurrency": "USD",
                "commercialExchangeRate": exchange_rate,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order("do-usd-1", "1200"), order("do-usd-2", "1210")])

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_REMITO_EXCHANGE_RATE")

    def test_delivery_remito_lote_usa_company_key_explicito(self):
        def order(order_id, company_key, legacy_label="SEPID"):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "companyKey": company_key,
                "operationCompanyLabel": legacy_label,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        compatibility = _validate_remito_orders([order("do-explicit", "MGBIO", "SEPID")])
        self.assertEqual(compatibility["companyKey"], "MGBIO")
        with self.assertRaisesRegex(Exception, "empresas Bejerman distintas"):
            _validate_remito_orders([order("do-1", "SEPID"), order("do-2", "MGBIO")])

    def test_demo_remito_profile_uses_rtn_demo_val(self):
        demo_profile = remito_profile_for_type("demo")

        self.assertEqual(demo_profile.comprobante_tipo, "RTN")
        self.assertEqual(demo_profile.point_of_sale, "00004")
        self.assertEqual(demo_profile.operation_code, "DEMO")
        self.assertEqual(demo_profile.deposit_code, "VAL")

    def test_only_rt_remito_profile_requires_billing(self):
        self.assertTrue(_remito_requires_billing({"type": "RT"}))
        for remito_type in ("RSS", "RTN", "RTA", "RDA", "RDN", ""):
            with self.subTest(remito_type=remito_type):
                self.assertFalse(_remito_requires_billing({"type": remito_type}))

    def test_demo_delivery_comprobante_uses_rtn_profile(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-demo",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OED-1",
                        "deliveryType": "demo",
                        "equipmentSerial": "SN-DEMO",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Equipo demo",
                                "quantity": 1,
                                "partida": "SN-DEMO",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RTN", "operation": "DEMO", "deposit": "VAL", "pointOfSale": "00004"}),
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RTN")
        self.assertEqual(comprobante["Comprobante_PtoVenta"], "00004")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "DEMO")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_Deposito"], "VAL")

    def test_delivery_comprobante_uses_bejerman_customer_fiscal_fields(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sale",
                "issueDate": "2026-06-19",
                "customerCode": "NOV",
                "customerName": "NOVAMED",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1110001",
                                "articleName": "Poligrafo portatil",
                                "quantity": 1,
                                "partida": "S225DC14018",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {
                "Cliente_RazonSocial": "NOVAMED S.A.",
                "Cliente_NroDocumento": "30710659768",
                "Cliente_Provincia": "02",
                "Cliente_SitIVA": "1",
            },
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Cliente_RazonSocial"], "NOVAMED S.A.")
        self.assertEqual(comprobante["Cliente_NroDocumento"], "30710659768")
        self.assertEqual(comprobante["Cliente_Provincia"], "02")
        self.assertEqual(comprobante["Cliente_SitIVA"], "1")
        legend_lines = [item["Item_DescripArticulo"] for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "L"]
        self.assertIn("------------------------------", legend_lines)

    def test_delivery_comprobante_expands_multiple_partidas(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Filtro",
                                "quantity": 2,
                                "unitPrice": 3,
                                "partidas": [
                                    {"partida": "P1", "assignedQuantity": 1, "stockDepositCode": "STL"},
                                    {"partida": "P2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["P1", "P2"])
        self.assertEqual([item["Item_Deposito"] for item in article_lines], ["STL", "VAL"])
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 6)

    def test_delivery_comprobante_no_duplica_cantidad_en_um2(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-23",
                "customerCode": "ETI",
                "customerName": "ETICA S.A.",
                "sellerCode": "TOM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-00027",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1203006",
                                "articleName": "Tubuladura descartable c/punta de goma 285/5064",
                                "quantity": 10,
                                "unitPrice": 16325,
                                "partidas": [
                                    {"partida": "24G0424FAX", "assignedQuantity": 10, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 10)
        self.assertEqual(article_lines[0]["Item_CantidadUM2"], 0)
        self.assertEqual(article_lines[0]["Item_Deposito"], "VAL")
        self.assertEqual(article_lines[0]["Item_Partida"], "24G0424FAX")

    def test_delivery_comprobante_envia_descuento_por_item(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Filtro",
                                "quantity": 2,
                                "unitPrice": 100,
                                "discountPercent": 10,
                                "partidas": [
                                    {"partida": "P1", "assignedQuantity": 1, "stockDepositCode": "STL"},
                                    {"partida": "P2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
        )

        items = built["comprobante"]["Comprobante_Items"]
        article_lines = [item for item in items if item["Item_Tipo"] == "A"]
        article_indexes = [index for index, item in enumerate(items) if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_PrecioUnitario"] for item in article_lines], [100, 100])
        self.assertEqual([item["Item_TasaDescPorItem"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_ImporteDescPorLinea"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_Importe"] for item in article_lines], [90, 90])
        self.assertEqual([item["Item_ImporteTotal"] for item in article_lines], [90, 90])
        self.assertEqual(items[article_indexes[0] + 1]["Item_Tipo"], "L")
        self.assertEqual(items[article_indexes[1] + 1]["Item_Tipo"], "L")
        self.assertEqual(
            items[article_indexes[0] + 1]["Item_DescripArticulo"],
            "Descuento aplicado: 10% - Precio final con descuento: $ 90,00",
        )
        self.assertEqual(
            items[article_indexes[1] + 1]["Item_DescripArticulo"],
            "Descuento aplicado: 10% - Precio final con descuento: $ 90,00",
        )
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 180)

    def test_delivery_comprobante_envia_moneda_usd_y_cotizacion(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-usd",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "exchangeRate": "1200",
                "orders": [
                    {
                        "orderNumber": "OE-USD-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Filtro",
                                "quantity": 2,
                                "unitPrice": 100,
                                "priceCurrency": "USD",
                                "discountPercent": 10,
                                "partidas": [
                                    {"partida": "P1", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                    {"partida": "P2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
        )

        comprobante = built["comprobante"]
        items = comprobante["Comprobante_Items"]
        article_lines = [item for item in items if item["Item_Tipo"] == "A"]
        article_indexes = [index for index, item in enumerate(items) if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_Moneda"], "USD")
        self.assertEqual(comprobante["Comprobante_CotizacionCambio"], 1200)
        self.assertEqual([item["Item_PrecioUnitario"] for item in article_lines], [100, 100])
        self.assertEqual([item["Item_TasaDescPorItem"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_ImporteDescPorLinea"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_ImporteTotal"] for item in article_lines], [90, 90])
        self.assertEqual(
            items[article_indexes[0] + 1]["Item_DescripArticulo"],
            "Descuento aplicado: 10% - Precio final con descuento: U$S 90,00",
        )
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 180)

    def test_delivery_comprobante_no_permite_monedas_mixtas_por_item(self):
        with self.assertRaises(BejermanSdkResponseError) as ctx:
            build_delivery_remito_comprobante(
                {
                    "groupId": "brg-mixed",
                    "issueDate": "2026-06-12",
                    "customerCode": "ACU",
                    "customerName": "ACUMAR",
                    "sellerCode": "ADM",
                    "paymentTermCode": "30",
                    "orders": [
                        {
                            "orderNumber": "OE-MIXED-1",
                            "deliveryType": "sale",
                            "items": [
                                {
                                    "articleCode": "110706",
                                    "articleName": "Filtro",
                                    "quantity": 1,
                                    "unitPrice": 100,
                                    "priceCurrency": "ARS",
                                    "partida": "P1",
                                },
                                {
                                    "articleCode": "110707",
                                    "articleName": "Filtro USD",
                                    "quantity": 1,
                                    "unitPrice": 10,
                                    "priceCurrency": "USD",
                                    "partida": "P2",
                                },
                            ],
                        }
                    ],
                },
                delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
            )

        self.assertEqual(str(ctx.exception), "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")

    def test_service_release_uses_resolution_delivery_legend(self):
        cases = [
            ("reparado", "Se entrega reparado"),
            ("no_reparado", "Se entrega sin reparar"),
            ("controlado_sin_defecto", "Se entrega controlado sin defecto"),
            ("no_se_encontro_falla", "Se entrega controlado sin defecto"),
        ]
        for resolution, expected in cases:
            with self.subTest(resolution=resolution):
                built = build_delivery_remito_comprobante(
                    {
                        "groupId": "brg-test",
                        "issueDate": "2026-06-12",
                        "customerCode": "ACU",
                        "customerName": "ACUMAR",
                        "sellerCode": "ADM",
                        "paymentTermCode": "30",
                        "orders": [
                            {
                                "orderNumber": "OES-1",
                                "deliveryType": "service_release",
                                "sourceReference": "OS 29381",
                                "serviceResolution": resolution,
                                "equipmentModel": "CPAP BMC",
                                "equipmentSerial": "SN-123",
                                "items": [
                                    {
                                        "articleCode": "110706",
                                        "articleName": "Servicio tecnico",
                                        "quantity": 1,
                                        "partida": "SN-123",
                                    }
                                ],
                            }
                        ],
                    },
                    delivery_remito_config({"type": "RSS", "operation": "REP", "deposit": "STC", "pointOfSale": "00004"}),
                )

                legend_lines = [
                    item["Item_DescripArticulo"]
                    for item in built["comprobante"]["Comprobante_Items"]
                    if item["Item_Tipo"] == "L"
                ]
                self.assertEqual(legend_lines[0], "------------------------------")
                self.assertEqual(legend_lines[1], expected)

    def test_service_release_rss_article_line_uses_equipment_detail(self):
        equipment_detail = "OXÍMETRO DE PULSO | Nellcor | N-595 - OS-29381 - N° interno (MG) MG 6697"
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OES-1",
                        "deliveryType": "service_release",
                        "sourceReference": "OS 29381",
                        "serviceResolution": "reparado",
                        "equipmentModel": "OXÍMETRO DE PULSO | Nellcor | N-595",
                        "equipmentSerial": "G0588065",
                        "equipmentInternalNumber": "MG 6697",
                        "items": [
                            {
                                "articleCode": "11",
                                "articleName": equipment_detail,
                                "description": "Liberación servicio técnico",
                                "quantity": 1,
                                "partida": "G0588065",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RSS", "operation": "REP", "deposit": "STC", "pointOfSale": "00004"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_DescripArticulo"], equipment_detail)
        self.assertNotIn("Liberación", article_lines[0]["Item_DescripArticulo"])
        self.assertNotIn("G0588065", article_lines[0]["Item_DescripArticulo"])

    def test_print_wait_page_does_not_force_pdf_accept_header(self):
        html = _remito_print_wait_page("brg-test", "RT")

        self.assertIn("/api/ordenes-entrega/remito-bejerman/brg-test/pdf/", html)
        self.assertIn("Preparando remito RT", html)
        self.assertIn('const documentName = "remito RT";', html)
        self.assertNotIn("Preparando RSS", html)
        self.assertNotIn("Accept: 'application/pdf'", html)
        self.assertIn("Retry-After", html)
        self.assertIn("retry_after_ms", html)
        self.assertIn("Cerrar pestaña", html)
        self.assertIn("Esta pestaña se reemplazará por el PDF", html)
        self.assertNotIn("maxAttempts", html)
        self.assertNotIn("maxWaitMs", html)
        self.assertNotIn("shouldStopWaiting", html)
        self.assertNotIn("Reintente en unos segundos", html)
        self.assertNotIn("no es necesario volver a emitir el remito", html)
        self.assertNotIn("Intento ", html)
        self.assertNotIn("Esperando PDF (", html)
        self.assertNotIn(chr(0x00C3), html)
