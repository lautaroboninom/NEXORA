import base64
import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_companies import list_ingress_companies, require_company
from service.bejerman_delivery import BillingError, _bridge_order, list_facturacion_from_bejerman
from service.views.delivery_orders_views import _remito_print_wait_page
from service.bejerman_ris import _build_payload
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanPdfPendingError,
    BejermanPdfReference,
    BejermanSdkResponseError,
    build_delivery_remito_comprobante,
    build_sales_filters,
    build_service_ingress_comprobante,
    delivery_remito_config,
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

    def consultar_comprobante_ventas_pdf(self, reference):
        self.consult_calls += 1
        if self.consult_responses:
            response = self.consult_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"DatosJSON": {}}

    def generar_comprobante_ventas_pdf(self, reference):
        self.generate_calls += 1
        return {"Resultado": "OK", "DatosJSON": "{}"}


def _pdf_response():
    return {
        "DatosJSON": json.dumps(
            {"archivoPdf": base64.b64encode(b"%PDF-1.4\nRIS test").decode("ascii")},
            ensure_ascii=False,
        )
    }


class BejermanPdfHelperTests(SimpleTestCase):
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


class BejermanIngressCompanyTests(SimpleTestCase):
    def test_ingress_companies_are_controlled(self):
        companies = list_ingress_companies()

        self.assertEqual([company.key for company in companies], ["SEPID", "MGBIO", "TEST"])
        self.assertEqual(require_company("MGBI").key, "MGBIO")
        self.assertEqual(require_company("MODE").key, "TEST")

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
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_Partida"], " ")


class BejermanDeliveryRemitoPayloadTests(SimpleTestCase):
    def test_nexora_order_payload_uses_equipment_serial_as_item_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OES-1",
                "deliveryType": "service_release",
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
                                    {"partida": "P1", "assignedQuantity": 1},
                                    {"partida": "P2", "assignedQuantity": 1},
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
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 6)

    def test_service_release_uses_resolution_delivery_legend(self):
        cases = [
            ("reparado", "Se entrega reparado"),
            ("no_reparado", "Se entrega sin reparar"),
            ("controlado_sin_defecto", "Se entrega controlado sin defecto"),
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
                self.assertEqual(legend_lines[0], expected)

    def test_print_wait_page_does_not_force_pdf_accept_header(self):
        html = _remito_print_wait_page("brg-test")

        self.assertIn("/api/ordenes-entrega/remito-bejerman/brg-test/pdf/", html)
        self.assertNotIn("Accept: 'application/pdf'", html)
