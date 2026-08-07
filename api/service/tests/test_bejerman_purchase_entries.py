import json
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve

from service.bejerman_purchase_entries import (
    BejermanPurchaseError,
    add_purchase_line,
    add_purchase_scan,
    build_purchase_entry_comprobante,
    create_purchase_entry,
    discard_purchase_entry,
    emit_purchase_entry,
    _catalog_cache_key,
    _purchase_currency_options,
    get_purchase_entry,
    list_purchase_articles,
    list_purchase_entries,
    list_purchase_orders,
    populate_purchase_entry_lines_from_order,
    reopen_purchase_entry,
    update_purchase_entry,
    update_purchase_scan,
    validate_purchase_entry,
    verify_purchase_entry,
)
from service.permission_policy import VIEW_PERMISSION_MATRIX
from service.bejerman_sdk import BejermanSDKClient, BejermanSdkResponseError
from service.views.bejerman_purchase_views import (
    BejermanPurchaseEntryReopenView,
    BejermanPurchaseEntryVerifyView,
    BejermanPurchaseOrdersView,
)


def purchase_article(
    code,
    *,
    requires_partida=False,
    purchase_enabled=True,
    vat_code="01",
    tax_category="7",
):
    return {
        "code": code,
        "name": f"Artículo {code}",
        "description": f"Artículo {code}",
        "purchaseEnabled": purchase_enabled,
        "controlsStock": True,
        "stockEnabled": True,
        "stockByPartida": requires_partida,
        "requiresPartida": requires_partida,
        "purchaseVatType": "1",
        "purchaseVatCode": vat_code,
        "taxCreditAllocation": "1",
        "taxCreditCategory": tax_category,
        "raw": {
            "Art_Codigo": code,
            "Art_DescripcionGeneral": f"Artículo {code}",
            "Art_ParticipaCircuitoCompras": purchase_enabled,
            "Art_ControlaStock": True,
            "Art_ParticipaCircuitoStock": True,
            "Art_StockPorPartida": requires_partida,
            "Art_TipoTasaCompras": "1",
            "Art_CodTasaIVACompras": vat_code,
            "Art_ImpCreditoFiscal": "1",
            "Art_CodRubroCreditoFiscal": tax_category,
        },
    }


class FakePurchaseClient:
    def __init__(
        self,
        *,
        duplicate=False,
        response_number="00001234",
        emit_error=None,
        purchase_enabled=True,
        document_after_emit=True,
        remote_operation="MC",
        remote_items=None,
        stock_records=None,
        partida_records=None,
        remote_total=1210,
        remote_date="2026-06-12",
    ):
        self.duplicate = duplicate
        self.response_number = response_number
        self.emit_error = emit_error
        self.purchase_enabled = purchase_enabled
        self.document_after_emit = document_after_emit
        self.remote_operation = remote_operation
        self.remote_items = list(remote_items or [])
        self.stock_records = list(stock_records or [])
        self.partida_records = list(partida_records or [])
        self.remote_total = remote_total
        self.remote_date = remote_date
        self.emitted = False
        self.list_calls = []
        self.ingresar_calls = []
        self.stock_calls = []
        self.partida_calls = 0

    def list_proveedores(self):
        return {
            "DatosJSON": json.dumps(
                [
                    {
                        "Proveedor_Codigo": "     1",
                        "Proveedor_RazonSocial": "Proveedor Test",
                        "Proveedor_NroDocumento": "30-00000000-0",
                        "Proveedor_TipoDocumento": 1,
                        "Proveedor_Provincia": "001",
                        "Proveedor_SitIVA": "1",
                        "Proveedor_Habilitado": True,
                        "Proveedor_CondicionPagoCodigo": "30",
                    }
                ]
            )
        }

    def list_articulos(self, filters, limit):
        records = []
        for article in (
            purchase_article(
                "INS-010",
                purchase_enabled=self.purchase_enabled,
            ),
            purchase_article(
                "EQ-001",
                requires_partida=True,
                purchase_enabled=self.purchase_enabled,
            ),
        ):
            records.append(article["raw"])
        return {"DatosJSON": json.dumps(records)}

    def obtener_depositos(self):
        return {
            "DatosJSON": json.dumps(
                [{"Deposito_Codigo": "VAL", "Deposito_Habilitado": True}]
            )
        }

    def obtener_stock_deposito(self, deposit_code):
        self.stock_calls.append(deposit_code)
        return {"DatosJSON": json.dumps(self.stock_records)}

    def obtener_partidas(self):
        self.partida_calls += 1
        return {"DatosJSON": json.dumps(self.partida_records)}

    def list_comprobantes_compras(self, filters):
        self.list_calls.append(filters)
        if not self.duplicate and not (
            self.emitted and self.document_after_emit
        ):
            return {"DatosJSON": []}
        record = {
            "Proveedor_Codigo": "     1",
            "Comprobante_Tipo": "RT",
            "Comprobante_Letra": "R",
            "Comprobante_PtoVenta": "00001",
            "Comprobante_Numero": "00001234",
            "Comprobante_TipoOperacion": self.remote_operation,
            "Comprobante_Items": self.remote_items,
        }
        if self.remote_total is not None:
            record["Comprobante_ImporteTotal"] = self.remote_total
        if self.remote_date is not None:
            record["Comprobante_FechaEmision"] = self.remote_date
        return {"DatosJSON": json.dumps([record])}

    def ingresar_lista_comprobantes_compras_json(self, comprobantes, **kwargs):
        self.ingresar_calls.append((comprobantes, kwargs))
        if self.emit_error is not None:
            raise self.emit_error
        self.emitted = True
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(
                {
                    "Comprobante_Tipo": "RT",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00001",
                    "Comprobante_Numero": self.response_number,
                }
            ),
        }


class QueryFilteringPurchaseClient:
    def __init__(self):
        self.article_queries = []

    def list_articulos(self, filters, limit):
        query = str((filters or [{}])[0].get("Valor") or "").upper()
        self.article_queries.append((query, limit))
        article = (
            purchase_article("EQ-001", requires_partida=True)
            if "EQ" in query
            else purchase_article("INS-010")
        )
        return {"DatosJSON": json.dumps([article["raw"]])}


class OrdersPurchaseClient:
    def __init__(self):
        self.calls = []

    def list_ordenes_compra(self, provider_code="", en_mon_ext="N"):
        self.calls.append(provider_code)
        records = [
            {
                "Comprobante_Tipo": "OC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00005678",
                "Comprobante_FechaEmision": "2026-06-01",
                "Proveedor_Codigo": provider_code,
                "Proveedor_RazonSocial": "MG BIO S.A.",
                "Comprobante_ImporteTotal": 1000.0,
            },
            {
                "Comprobante_Tipo": "RT",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00001",
                "Comprobante_Numero": "00009999",
                "Proveedor_Codigo": provider_code,
            },
        ]
        return {"DatosJSON": json.dumps(records)}


class MultiOrdersPurchaseClient:
    def __init__(self):
        self.calls = []
        self.company_key = "SEPID"

    def list_ordenes_compra(self, provider_code="", en_mon_ext="N"):
        self.calls.append(provider_code)
        records = [
            {
                "Comprobante_ID": "101",
                "Comprobante_Tipo": "OC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00005678",
                "Comprobante_FechaEmision": "2026-06-01",
                "Proveedor_Codigo": provider_code,
                "Proveedor_RazonSocial": "MG BIO S.A.",
                "Comprobante_ImporteTotal": 1000.0,
            },
            {
                "Comprobante_ID": "102",
                "Comprobante_Tipo": "OC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00009999",
                "Comprobante_FechaEmision": "2026-06-15",
                "Proveedor_Codigo": provider_code,
                "Proveedor_RazonSocial": "MG BIO S.A.",
                "Comprobante_ImporteTotal": 2000.0,
            },
        ]
        return {"DatosJSON": json.dumps(records)}


def oc_item(article_code, *, quantity, unit_value, description=""):
    total = round(quantity * unit_value, 2)
    return {
        "Item_CodigoArticulo": article_code,
        "Item_DescripArticulo": description or f"Artículo {article_code}",
        "Item_CantidadUM1": quantity,
        "Item_PrecioUnitario": unit_value,
        "Item_ImporteItem": total,
        "Item_ImporteTotal": total,
    }


class PopulateOrderPurchaseClient:
    def __init__(self, *, items=None, order_records=None, wrap_detail_in_list=False):
        self.company_key = "SEPID"
        self.calls = []
        self.detail_calls = []
        self.items = items if items is not None else [oc_item("INS-010", quantity=12, unit_value=5)]
        self.order_records = order_records if order_records is not None else [
            {
                "Comprobante_ID": "555",
                "Comprobante_Tipo": "OC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00005678",
                "Comprobante_FechaEmision": "2026-06-01",
                "Proveedor_Codigo": "1",
                "Proveedor_RazonSocial": "Proveedor Test",
                "Comprobante_ImporteTotal": 60.0,
            },
        ]
        self.wrap_detail_in_list = wrap_detail_in_list

    def list_ordenes_compra(self, provider_code="", en_mon_ext="N"):
        self.calls.append(provider_code)
        return {"DatosJSON": json.dumps(self.order_records)}

    def detalle_comprobante_compras(self, comprobante_id):
        self.detail_calls.append(comprobante_id)
        payload = {"Comprobante_Items": self.items}
        if self.wrap_detail_in_list:
            return {"DatosJSON": json.dumps([payload])}
        return {"DatosJSON": json.dumps(payload)}


class PopulateAndEmitPurchaseClient(FakePurchaseClient):
    """Combina la citación/autocompletado de OC con el circuito completo de
    emisión de FakePurchaseClient, para poder probar de punta a punta el
    aviso de cantidad OC vs RT."""

    def __init__(self, *, items=None, order_records=None, **kwargs):
        super().__init__(**kwargs)
        self.order_calls = []
        self.detail_calls = []
        self.items = items if items is not None else [oc_item("INS-010", quantity=12, unit_value=5)]
        self.order_records = order_records if order_records is not None else [
            {
                "Comprobante_ID": "555",
                "Comprobante_Tipo": "OC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00005678",
                "Comprobante_FechaEmision": "2026-06-01",
                "Proveedor_Codigo": "     1",
                "Proveedor_RazonSocial": "Proveedor Test",
                "Comprobante_ImporteTotal": 60.0,
            },
        ]

    def list_ordenes_compra(self, provider_code="", en_mon_ext="N"):
        self.order_calls.append(provider_code)
        return {"DatosJSON": json.dumps(self.order_records)}

    def detalle_comprobante_compras(self, comprobante_id):
        self.detail_calls.append(comprobante_id)
        return {"DatosJSON": json.dumps({"Comprobante_Items": self.items})}


class ExactArticleLookupClient(FakePurchaseClient):
    def __init__(self):
        super().__init__()
        self.article_queries = []

    def list_articulos(self, filters, limit):
        query = str((filters or [{}])[0].get("Valor") or "").upper()
        self.article_queries.append((query, limit))
        return {
            "DatosJSON": json.dumps(
                [
                    purchase_article("OUT-999")["raw"],
                    purchase_article("INS-010")["raw"],
                ]
            )
        }


class BejermanPurchaseBuilderTests(SimpleTestCase):
    def test_builds_rt_mc_purchase_payload_with_factors_values_and_partidas(self):
        entry = {
                "id": "bpe-test",
                "companyKey": "SEPID",
                "comprobanteTipo": "RT",
                "comprobanteLetra": "R",
                "comprobantePtoVenta": "00001",
                "comprobanteNumero": "00001234",
                "supplierCode": "1",
                "supplierCodeRaw": "     1",
                "supplierName": "Proveedor Test",
                "supplierSnapshot": {"Proveedor_TipoDocumento": 1, "Proveedor_Provincia": "001", "Proveedor_SitIVA": "1"},
                "paymentTermCode": "30",
                "issueDate": "2026-06-12",
                "accountingDate": "2026-06-12",
                "ddjjDate": "2026-06-12",
                "operationCode": "MC",
                "depositCode": "VAL",
                "notes": "Compra NEXORA",
                "lines": [
                    {
                        "articleCode": "EQ-001",
                        "articleDescription": "Equipo seriado",
                        "depositCode": "VAL",
                        "scans": [{"barcode": "NS001", "conversionFactor": 1, "unitValue": 1000, "articleCode": "EQ-001"}],
                    },
                    {
                        "articleCode": "INS-010",
                        "articleDescription": "Máscara descartable",
                        "depositCode": "VAL",
                        "scans": [{"barcode": "CAJA-10", "conversionFactor": 10, "unitValue": 5, "articleCode": "INS-010"}],
                    },
                ],
        }
        built = build_purchase_entry_comprobante(
            entry,
            {
                "articles": {
                    "EQ001": purchase_article("EQ-001", requires_partida=True),
                    "INS010": purchase_article("INS-010"),
                }
            },
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RT")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "MC")
        self.assertEqual(comprobante["Proveedor_Codigo"], "     1")
        self.assertEqual(comprobante["Comprobante_CondicionPago"], "30")
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 1270.5)
        self.assertEqual(comprobante["Comprobante_Moneda"], "1")
        self.assertEqual(comprobante["Comprobante_TipoCambio"], "UNI")
        self.assertEqual(comprobante["Comprobante_CotizacionCambio"], 1.0)
        self.assertEqual(built["taxValue"], 220.5)
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_NumeroRenglon"], 1)
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_NumeroRenglon"], 2)
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_CantidadUM1"], 1.0)
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_Partida"], "NS001")
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_TasaIVAInscrip"], 21.0)
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_ImporteIVAInscrip"], 210.0)
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_CantidadUM1"], 10.0)
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_ImporteItem"], 50.0)
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_ImporteIVAInscrip"], 10.5)
        self.assertIn("Item_ImporteDescPorItem", comprobante["Comprobante_Items"][0])
        self.assertIn(
            "Item_ImporteConceptoNoGravado",
            comprobante["Comprobante_Items"][0],
        )
        self.assertNotIn("Item_ImporteDescPorLinea", comprobante["Comprobante_Items"][0])
        self.assertNotIn("Item_ImporteIVANoGravado", comprobante["Comprobante_Items"][0])
        self.assertNotIn("Item_Importe", comprobante["Comprobante_Items"][0])
        for sales_only_field in (
            "Item_ImporteDescGeneral",
            "Item_CodigoDescPorLinea",
            "Item_FechaEntrega",
            "Item_Kit",
            "Item_RenglonKit",
            "Item_EsPromocion",
        ):
            self.assertNotIn(
                sales_only_field,
                comprobante["Comprobante_Items"][0],
            )
        self.assertNotIn("Comprobante_Mensaje", comprobante)

    def _origin_test_entry(self, **origin_overrides):
        entry = {
            "id": "bpe-origin-test",
            "companyKey": "SEPID",
            "comprobanteTipo": "RT",
            "comprobanteLetra": "R",
            "comprobantePtoVenta": "00001",
            "comprobanteNumero": "00001234",
            "comprobanteTipoOrigen": "",
            "comprobanteLetraOrigen": "",
            "comprobantePtoVentaOrigen": "",
            "comprobanteNumeroOrigen": "",
            "supplierCode": "1",
            "supplierCodeRaw": "     1",
            "supplierName": "Proveedor Test",
            "supplierSnapshot": {},
            "paymentTermCode": "30",
            "issueDate": "2026-06-12",
            "accountingDate": "2026-06-12",
            "ddjjDate": "2026-06-12",
            "operationCode": "MC",
            "depositCode": "VAL",
            "notes": "",
            "lines": [
                {
                    "articleCode": "INS-010",
                    "articleDescription": "Máscara descartable",
                    "depositCode": "VAL",
                    "scans": [{"barcode": "CAJA-10", "conversionFactor": 10, "unitValue": 5, "articleCode": "INS-010"}],
                },
            ],
        }
        entry.update(origin_overrides)
        return entry

    def test_builds_rt_mc_purchase_payload_with_origin_purchase_order(self):
        entry = self._origin_test_entry(
            comprobanteTipoOrigen="OC",
            comprobanteLetraOrigen="A",
            comprobantePtoVentaOrigen="00002",
            comprobanteNumeroOrigen="00005678",
        )
        built = build_purchase_entry_comprobante(
            entry,
            {"articles": {"INS010": purchase_article("INS-010")}},
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Comprobante_Tipo_Origen"], "OC")
        self.assertEqual(comprobante["Comprobante_Letra_Origen"], "A")
        self.assertEqual(comprobante["Comprobante_PtoVenta_Origen"], "00002")
        self.assertEqual(comprobante["Comprobante_Numero_Origen"], "00005678")

    def test_builds_rt_mc_purchase_payload_without_origin_purchase_order(self):
        entry = self._origin_test_entry()
        built = build_purchase_entry_comprobante(
            entry,
            {"articles": {"INS010": purchase_article("INS-010")}},
        )

        comprobante = built["comprobante"]
        for key in (
            "Comprobante_Tipo_Origen",
            "Comprobante_Letra_Origen",
            "Comprobante_PtoVenta_Origen",
            "Comprobante_Numero_Origen",
        ):
            self.assertNotIn(key, comprobante)

    @override_settings(
        BEJERMAN_PURCHASE_CURRENCY="",
        BEJERMAN_PURCHASE_EXCHANGE_TYPE="",
    )
    def test_purchase_currency_configuration_fails_closed(self):
        with self.assertRaises(BejermanPurchaseError) as invalid:
            _purchase_currency_options()

        self.assertEqual(
            invalid.exception.code,
            "PURCHASE_CURRENCY_CONFIG_INVALID",
        )

    def test_purchase_vat_mapping_and_rounding_stay_in_the_header(self):
        entry = {
            "id": "bpe-vat",
            "companyKey": "SEPID",
            "comprobanteTipo": "RT",
            "comprobanteLetra": "R",
            "comprobantePtoVenta": "00001",
            "comprobanteNumero": "00001235",
            "supplierCode": "1",
            "supplierCodeRaw": "     1",
            "supplierName": "Proveedor Test",
            "supplierSnapshot": {
                "Proveedor_TipoDocumento": 1,
                "Proveedor_Provincia": "001",
                "Proveedor_SitIVA": "1",
            },
            "paymentTermCode": "30",
            "issueDate": "2026-06-12",
            "accountingDate": "2026-06-12",
            "ddjjDate": "2026-06-12",
            "operationCode": "MC",
            "depositCode": "VAL",
            "notes": "",
            "lines": [
                {
                    "articleCode": code,
                    "articleDescription": code,
                    "depositCode": "VAL",
                    "scans": [
                        {
                            "barcode": f"BULTO-{code}",
                            "conversionFactor": 1,
                            "unitValue": 100,
                            "articleCode": code,
                        }
                    ],
                }
                for code in ("IVA-21", "IVA-105", "IVA-0")
            ],
        }
        built = build_purchase_entry_comprobante(
            entry,
            {
                "articles": {
                    "IVA21": purchase_article("IVA-21", vat_code="01"),
                    "IVA105": purchase_article("IVA-105", vat_code="03"),
                    "IVA0": purchase_article("IVA-0", vat_code="04"),
                }
            },
        )

        self.assertEqual(built["taxValue"], 31.5)
        self.assertEqual(built["totalValue"], 331.5)
        self.assertEqual(
            built["comprobante"]["Comprobante_ImporteTotal"],
            331.5,
        )
        expected_vat = {
            "IVA-21": (21.0, 21.0),
            "IVA-105": (10.5, 10.5),
            "IVA-0": (0.0, 0.0),
        }
        for item in built["comprobante"]["Comprobante_Items"]:
            expected_rate, expected_amount = expected_vat[
                item["Item_CodigoArticulo"]
            ]
            self.assertEqual(item["Item_TasaIVAInscrip"], expected_rate)
            self.assertEqual(item["Item_TasaIVANoInscrip"], 0)
            self.assertEqual(
                item["Item_ImporteIVAInscrip"],
                expected_amount,
            )
            self.assertEqual(item["Item_ImporteIVANoInscrip"], 0)
            self.assertEqual(item["Item_TipoIVA"], "1")


class BejermanPurchaseSdkTests(SimpleTestCase):
    def test_sdk_uses_compras_list_and_ingresar_lista_json_params(self):
        client = BejermanSDKClient(company_key=None, bejerman_company="SEP")

        with patch.object(client, "execute", return_value={"Resultado": "OK"}) as execute:
            client.list_comprobantes_compras([{"Campo": "Comprobante_Tipo", "Valor": "RT"}])
            client.ingresar_lista_comprobantes_compras_json([{"Comprobante_Tipo": "RT"}])

        self.assertEqual(execute.call_args_list[0].args[:2], ("COMPRAS", "WSListarComprobantesJSON"))
        self.assertEqual(execute.call_args_list[1].args[:2], ("COMPRAS", "IngresarListaComprobantesJSON"))
        self.assertEqual(execute.call_args_list[1].kwargs["params_json"][1:], ["N", "R"])

    def test_sdk_lists_ordenes_compra_filtered_by_type_and_provider(self):
        client = BejermanSDKClient(company_key=None, bejerman_company="SEP")

        with patch.object(client, "execute", return_value={"Resultado": "OK"}) as execute:
            client.list_ordenes_compra("MGBIO")

        self.assertEqual(execute.call_args_list[0].args[:2], ("COMPRAS", "WSListarComprobantesJSON"))
        filters = json.loads(execute.call_args_list[0].kwargs["params_json"][0])
        self.assertEqual(
            [(f["Campo"], f["Valor"]) for f in filters],
            [("Comprobante_Tipo", "OC"), ("Proveedor_Codigo", "MGBIO")],
        )

    def test_sdk_lists_ordenes_compra_without_provider_filter(self):
        client = BejermanSDKClient(company_key=None, bejerman_company="SEP")

        with patch.object(client, "execute", return_value={"Resultado": "OK"}) as execute:
            client.list_ordenes_compra()

        filters = json.loads(execute.call_args_list[0].kwargs["params_json"][0])
        self.assertEqual([f["Campo"] for f in filters], ["Comprobante_Tipo"])


class BejermanPurchaseRoutingTests(SimpleTestCase):
    def test_verify_and_reopen_routes_are_exposed_with_emit_permission(self):
        verify_match = resolve(
            "/api/bejerman/purchase-entries/bpe-test/verify/"
        )
        reopen_match = resolve(
            "/api/bejerman/purchase-entries/bpe-test/reopen/"
        )

        self.assertIs(
            verify_match.func.view_class,
            BejermanPurchaseEntryVerifyView,
        )
        self.assertIs(
            reopen_match.func.view_class,
            BejermanPurchaseEntryReopenView,
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["BejermanPurchaseEntryVerifyView"]["POST"],
            "action.bejerman_purchase_entries.emit",
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["BejermanPurchaseEntryReopenView"]["POST"],
            "action.bejerman_purchase_entries.emit",
        )

    def test_purchase_orders_route_is_exposed_with_read_permission(self):
        match = resolve("/api/bejerman/purchase-orders/")

        self.assertIs(match.func.view_class, BejermanPurchaseOrdersView)
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["BejermanPurchaseOrdersView"]["GET"],
            "page.bejerman_purchase_entries",
        )


class BejermanPurchaseCatalogTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_article_cache_is_scoped_to_each_search(self):
        client = QueryFilteringPurchaseClient()

        with patch(
            "service.bejerman_purchase_entries.BejermanSDKClient",
            return_value=client,
        ):
            equipment = list_purchase_articles(
                {"q": "EQ", "field": "code", "limit": 20},
                actor_user_id=77,
            )
            supplies = list_purchase_articles(
                {"q": "INS", "field": "code", "limit": 20},
                actor_user_id=77,
            )

        self.assertEqual(
            [item["code"] for item in equipment["items"]],
            ["EQ-001"],
        )
        self.assertEqual(
            [item["code"] for item in supplies["items"]],
            ["INS-010"],
        )
        self.assertEqual(client.article_queries, [("EQ", 20), ("INS", 20)])

    def test_catalog_cache_is_scoped_to_bejerman_endpoint(self):
        with self.settings(
            BEJERMAN_WSDL_URL="http://bejerman.test/Bejerman-SDK-WS-TEST/service",
            BEJERMAN_COMPANY_SEPID="SEP",
        ):
            test_key = _catalog_cache_key("providers", 77)
        with self.settings(
            BEJERMAN_WSDL_URL="http://bejerman.prod/Bejerman-SDK-WS/service",
            BEJERMAN_COMPANY_SEPID="SEP",
        ):
            production_key = _catalog_cache_key("providers", 77)
        with self.settings(
            BEJERMAN_WSDL_URL="http://bejerman.prod/Bejerman-SDK-WS/service",
            BEJERMAN_COMPANY_SEPID="OTRA",
        ):
            other_company_key = _catalog_cache_key("providers", 77)

        self.assertNotEqual(test_key, production_key)
        self.assertNotEqual(production_key, other_company_key)

    def test_purchase_orders_require_a_supplier(self):
        with self.assertRaises(BejermanPurchaseError) as ctx:
            list_purchase_orders({}, actor_user_id=77)
        self.assertEqual(ctx.exception.code, "PURCHASE_ORDER_SUPPLIER_REQUIRED")

    def test_purchase_orders_are_filtered_to_type_oc(self):
        client = OrdersPurchaseClient()

        with patch(
            "service.bejerman_purchase_entries.BejermanSDKClient",
            return_value=client,
        ):
            result = list_purchase_orders({"supplierCode": "MGBIO"}, actor_user_id=77)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["comprobanteNumero"], "00005678")
        self.assertEqual(result["items"][0]["comprobanteTipo"], "OC")
        self.assertEqual(client.calls, ["MGBIO"])

    def test_purchase_orders_preserves_supplier_code_padding_for_exact_match(self):
        # Bejerman guarda Proveedor_Codigo con padding de espacios (ej. "   DCD")
        # y filtra por igualdad exacta; si recortáramos el código acá, la
        # búsqueda nunca encontraría nada para ese proveedor.
        client = OrdersPurchaseClient()

        with patch(
            "service.bejerman_purchase_entries.BejermanSDKClient",
            return_value=client,
        ):
            list_purchase_orders({"supplierCode": "   DCD"}, actor_user_id=77)

        self.assertEqual(client.calls, ["   DCD"])

    def test_purchase_orders_search_filters_by_number_ignoring_leading_zeros(self):
        client = MultiOrdersPurchaseClient()

        with patch(
            "service.bejerman_purchase_entries.BejermanSDKClient",
            return_value=client,
        ):
            result = list_purchase_orders(
                {"supplierCode": "MGBIO", "q": "5678"},
                actor_user_id=77,
            )

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["comprobanteNumero"], "00005678")
        self.assertEqual(result["items"][0]["comprobanteId"], "101")

    def test_purchase_orders_without_query_are_sorted_by_issue_date_desc(self):
        client = MultiOrdersPurchaseClient()

        with patch(
            "service.bejerman_purchase_entries.BejermanSDKClient",
            return_value=client,
        ):
            result = list_purchase_orders({"supplierCode": "MGBIO"}, actor_user_id=77)

        self.assertEqual(
            [item["comprobanteNumero"] for item in result["items"]],
            ["00009999", "00005678"],
        )

    def test_purchase_orders_default_company_is_sepid_and_respects_companyKey(self):
        client = MultiOrdersPurchaseClient()

        with patch(
            "service.bejerman_purchase_entries.BejermanSDKClient",
            return_value=client,
        ):
            default_result = list_purchase_orders({"supplierCode": "MGBIO"}, actor_user_id=77)
            mgbio_result = list_purchase_orders(
                {"supplierCode": "MGBIO", "companyKey": "MGBIO"},
                actor_user_id=77,
            )

        self.assertEqual(default_result["companyKey"], "SEPID")
        self.assertEqual(mgbio_result["companyKey"], "MGBIO")


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
@override_settings(BEJERMAN_PURCHASE_EMISSION_ENABLED=True)
class BejermanPurchaseServiceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo BOOLEAN DEFAULT TRUE
                )
                """
            )
        call_command("apply_bejerman_purchase_entries_schema", verbosity=0)

    def setUp(self):
        cache.clear()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_purchase_entry_events")
            cur.execute("DELETE FROM bejerman_purchase_entry_scans")
            cur.execute("DELETE FROM bejerman_purchase_entry_lines")
            cur.execute("DELETE FROM bejerman_purchase_entries")

    def _draft_with_line(
        self,
        *,
        article_code="INS-010",
        article_description="Máscara descartable",
        document_number="00001234",
    ):
        entry = create_purchase_entry(
            {
                "supplierCode": "1",
                "supplierCodeRaw": "     1",
                "supplierName": "Proveedor Test",
                "paymentTermCode": "30",
                "comprobanteLetra": "R",
                "comprobantePtoVenta": "00001",
                "comprobanteNumero": document_number,
                "issueDate": "2026-06-12",
            },
            None,
        )
        entry = add_purchase_line(
            entry["id"],
            {
                "articleCode": article_code,
                "articleDescription": article_description,
                "defaultConversionFactor": 12,
                "defaultUnitValue": 5,
            },
            None,
        )
        return entry, entry["lines"][0]

    def _emit_tracked_purchase(
        self,
        client,
        *,
        barcode="SERIE-001",
    ):
        entry, line = self._draft_with_line(
            article_code="EQ-001",
            article_description="Equipo seriado",
        )
        add_purchase_scan(
            entry["id"],
            line["id"],
            {
                "barcode": barcode,
                "conversionFactor": 1,
                "unitValue": 1000,
            },
            None,
        )
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )
        self.assertTrue(preflight["ok"], preflight["errors"])
        emitted = emit_purchase_entry(
            entry["id"],
            None,
            {
                "confirmed": True,
                "preflightFingerprint": preflight["preflightFingerprint"],
            },
            client=client,
        )
        return entry["id"], emitted

    def _draft_with_citation(self, *, letra="A", pto_venta="00002", numero="00005678"):
        entry = create_purchase_entry(
            {
                "supplierCode": "1",
                "supplierCodeRaw": "     1",
                "supplierName": "Proveedor Test",
                "comprobanteLetra": "R",
                "comprobantePtoVenta": "00001",
                "comprobanteNumero": "00001234",
                "issueDate": "2026-06-12",
            },
            None,
        )
        return update_purchase_entry(
            entry["id"],
            {
                "comprobanteTipoOrigen": "OC",
                "comprobanteLetraOrigen": letra,
                "comprobantePtoVentaOrigen": pto_venta,
                "comprobanteNumeroOrigen": numero,
            },
            None,
        )

    def test_populate_purchase_entry_lines_from_order_creates_lines_with_expected_quantity(self):
        entry = self._draft_with_citation()
        client = PopulateOrderPurchaseClient(
            items=[
                oc_item("INS-010", quantity=12, unit_value=5, description="Máscara descartable"),
                oc_item("INS-020", quantity=3, unit_value=10),
            ],
        )

        result = populate_purchase_entry_lines_from_order(entry["id"], None, client=client)

        self.assertEqual(result["populatedLineCount"], 2)
        lines = {line["articleCode"]: line for line in result["lines"]}
        self.assertEqual(lines["INS-010"]["expectedQuantity"], 12.0)
        self.assertEqual(lines["INS-010"]["defaultUnitValue"], 5.0)
        self.assertEqual(lines["INS-010"]["articleDescription"], "Máscara descartable")
        self.assertEqual(lines["INS-020"]["expectedQuantity"], 3.0)
        self.assertEqual(lines["INS-020"]["defaultUnitValue"], 10.0)
        self.assertEqual(client.detail_calls, ["555"])

    def test_populate_purchase_entry_lines_from_order_computes_unit_value_when_missing(self):
        entry = self._draft_with_citation()
        item = oc_item("INS-030", quantity=4, unit_value=2.5)
        del item["Item_PrecioUnitario"]
        client = PopulateOrderPurchaseClient(items=[item])

        result = populate_purchase_entry_lines_from_order(entry["id"], None, client=client)

        line = result["lines"][0]
        self.assertEqual(line["expectedQuantity"], 4.0)
        self.assertEqual(line["defaultUnitValue"], 2.5)

    def test_populate_purchase_entry_lines_from_order_handles_wrapped_single_element_response(self):
        entry = self._draft_with_citation()
        client = PopulateOrderPurchaseClient(
            items=[oc_item("INS-040", quantity=6, unit_value=1)],
            wrap_detail_in_list=True,
        )

        result = populate_purchase_entry_lines_from_order(entry["id"], None, client=client)

        self.assertEqual(result["populatedLineCount"], 1)
        self.assertEqual(result["lines"][0]["articleCode"], "INS-040")

    def test_populate_purchase_entry_lines_from_order_requires_cited_order(self):
        entry, _line = self._draft_with_line()

        with self.assertRaises(BejermanPurchaseError) as ctx:
            populate_purchase_entry_lines_from_order(entry["id"], None, client=PopulateOrderPurchaseClient())
        self.assertEqual(ctx.exception.code, "PURCHASE_ORDER_NOT_CITED")

    def test_populate_purchase_entry_lines_from_order_raises_when_order_not_found(self):
        entry = self._draft_with_citation(numero="00009999")
        client = PopulateOrderPurchaseClient()  # sólo conoce la OC 00005678

        with self.assertRaises(BejermanPurchaseError) as ctx:
            populate_purchase_entry_lines_from_order(entry["id"], None, client=client)
        self.assertEqual(ctx.exception.code, "PURCHASE_ORDER_NOT_FOUND")

    def test_populate_purchase_entry_lines_from_order_raises_when_no_items(self):
        entry = self._draft_with_citation()
        client = PopulateOrderPurchaseClient(items=[])

        with self.assertRaises(BejermanPurchaseError) as ctx:
            populate_purchase_entry_lines_from_order(entry["id"], None, client=client)
        self.assertEqual(ctx.exception.code, "PURCHASE_ORDER_WITHOUT_ITEMS")

    def test_populate_purchase_entry_lines_from_order_uses_items_embedded_in_the_order_listing(self):
        # WSListarComprobantesJSON (list_ordenes_compra) ya trae los renglones
        # embebidos en Comprobante_Items de cada OC. Si están, hay que usarlos
        # directo y NUNCA llamar a detalle_comprobante_compras — ese endpoint
        # puede tirar una excepción cruda del lado servidor de Bejerman para
        # alguna OC puntual (caso real: OC 00002-00001446) aunque el listado
        # de esa misma OC esté perfecto.
        entry = self._draft_with_citation()
        order_records = [
            {
                "Comprobante_ID": "8087",
                "Comprobante_Tipo": "OC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00005678",
                "Comprobante_FechaEmision": "2026-06-01",
                "Proveedor_Codigo": "1",
                "Proveedor_RazonSocial": "Proveedor Test",
                "Comprobante_ImporteTotal": 60.0,
                "Comprobante_Items": [
                    oc_item("INS-010", quantity=12, unit_value=5, description="Máscara descartable"),
                ],
            },
        ]
        client = PopulateOrderPurchaseClient(order_records=order_records)

        def _must_not_be_called(comprobante_id):
            raise AssertionError(
                "no debería llamar a detalle_comprobante_compras si la OC ya trae items embebidos"
            )

        client.detalle_comprobante_compras = _must_not_be_called

        result = populate_purchase_entry_lines_from_order(entry["id"], None, client=client)

        self.assertEqual(result["populatedLineCount"], 1)
        self.assertEqual(result["lines"][0]["articleCode"], "INS-010")
        self.assertEqual(result["lines"][0]["expectedQuantity"], 12.0)
        self.assertEqual(result["lines"][0]["defaultUnitValue"], 5.0)

    def test_populate_purchase_entry_lines_from_order_surfaces_bejerman_detail_failure(self):
        # Caso real visto en producción: la OC se encuentra y se resuelve su
        # Comprobante_ID, pero Bejerman tira una excepción cruda del lado
        # servidor al pedir DetalleComprobanteJSON (dato corrupto en esa OC
        # puntual). El lote debe quedar con la OC citada, sin renglones, y con
        # rastro del incidente en los eventos del lote.
        entry = self._draft_with_citation()
        client = PopulateOrderPurchaseClient()

        def _broken_detail(comprobante_id):
            client.detail_calls.append(comprobante_id)
            raise BejermanSdkResponseError(
                "Error en la ejecución. Exception has been thrown by the target of an invocation. "
                "The null value cannot be assigned to a member with type System.Int32 which is a "
                "non-nullable value type."
            )

        client.detalle_comprobante_compras = _broken_detail

        with self.assertRaises(BejermanPurchaseError) as ctx:
            populate_purchase_entry_lines_from_order(entry["id"], None, client=client)
        self.assertEqual(ctx.exception.code, "PURCHASE_ORDER_DETAIL_UNAVAILABLE")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("System.Int32", str(ctx.exception))

        current = get_purchase_entry(entry["id"], include_events=True)
        self.assertEqual(current["lines"], [])
        self.assertEqual(current["comprobanteNumeroOrigen"], "00005678")

        events = [event for event in current["events"] if event["eventType"] == "order_detail_unavailable"]
        self.assertEqual(len(events), 1)
        self.assertIn("System.Int32", events[0]["note"])
        self.assertEqual(events[0]["metadata"]["comprobanteId"], "555")
        self.assertEqual(events[0]["metadata"]["comprobanteNumeroOrigen"], "00005678")

    def test_scans_inherit_line_factor_value_and_block_duplicates(self):
        entry, line = self._draft_with_line()

        entry = add_purchase_scan(entry["id"], line["id"], {"barcodes": "CAJA-1\nCAJA-2"}, None)

        self.assertEqual(entry["totalQuantity"], 24.0)
        self.assertEqual(entry["totalValue"], 120.0)
        self.assertEqual(entry["lines"][0]["scans"][0]["articleCode"], "INS-010")
        with self.assertRaises(BejermanPurchaseError) as ctx:
            add_purchase_scan(entry["id"], line["id"], {"barcode": "CAJA-1"}, None)
        self.assertEqual(ctx.exception.code, "DUPLICATE_BARCODE")

        scan_id = entry["lines"][0]["scans"][0]["id"]
        entry = update_purchase_scan(entry["id"], scan_id, {"conversionFactor": 10, "unitValue": 6}, None)
        self.assertEqual(entry["totalQuantity"], 22.0)
        self.assertEqual(entry["totalValue"], 120.0)
        self.assertTrue(validate_purchase_entry(entry["id"])["ok"])

    def test_scan_partida_expiration_date_is_set_preserved_and_cleared(self):
        entry, line = self._draft_with_line()
        entry = add_purchase_scan(entry["id"], line["id"], {"barcode": "CAJA-1"}, None)
        scan_id = entry["lines"][0]["scans"][0]["id"]

        entry = update_purchase_scan(
            entry["id"], scan_id, {"partidaExpirationDate": "2027-03-01"}, None
        )
        self.assertEqual(entry["lines"][0]["scans"][0]["partidaExpirationDate"], "2027-03-01")

        entry = update_purchase_scan(entry["id"], scan_id, {"unitValue": 7}, None)
        self.assertEqual(entry["lines"][0]["scans"][0]["partidaExpirationDate"], "2027-03-01")

        entry = update_purchase_scan(entry["id"], scan_id, {"partidaExpirationDate": ""}, None)
        self.assertIsNone(entry["lines"][0]["scans"][0]["partidaExpirationDate"])

    def test_update_purchase_entry_citation_survives_partial_patch(self):
        entry, _line = self._draft_with_line()

        entry = update_purchase_entry(
            entry["id"],
            {
                "comprobanteTipoOrigen": "OC",
                "comprobanteLetraOrigen": "A",
                "comprobantePtoVentaOrigen": "00002",
                "comprobanteNumeroOrigen": "00005678",
            },
            None,
        )
        self.assertEqual(entry["comprobanteNumeroOrigen"], "00005678")
        self.assertEqual(entry["comprobanteOrigenLabel"], "OC A 00002-00005678")

        entry = update_purchase_entry(entry["id"], {"notes": "Actualización de auditoría"}, None)
        self.assertEqual(entry["notes"], "Actualización de auditoría")
        self.assertEqual(entry["comprobanteNumeroOrigen"], "00005678")
        self.assertEqual(entry["comprobanteLetraOrigen"], "A")

        entry = update_purchase_entry(
            entry["id"],
            {
                "comprobanteTipoOrigen": "",
                "comprobanteLetraOrigen": "",
                "comprobantePtoVentaOrigen": "",
                "comprobanteNumeroOrigen": "",
            },
            None,
        )
        self.assertEqual(entry["comprobanteNumeroOrigen"], "")
        self.assertEqual(entry["comprobanteOrigenLabel"], "")

    def test_payment_term_is_optional_for_validation(self):
        entry = create_purchase_entry(
            {
                "supplierCode": "1",
                "supplierCodeRaw": "     1",
                "supplierName": "Proveedor Test",
                "comprobanteLetra": "R",
                "comprobantePtoVenta": "00001",
                "comprobanteNumero": "00001235",
                "issueDate": "2026-06-12",
            },
            None,
        )
        entry = add_purchase_line(
            entry["id"],
            {
                "articleCode": "INS-010",
                "articleDescription": "Máscara descartable",
                "defaultConversionFactor": 12,
                "defaultUnitValue": 5,
            },
            None,
        )
        entry = add_purchase_scan(entry["id"], entry["lines"][0]["id"], {"barcode": "CAJA-SIN-COND"}, None)

        result = validate_purchase_entry(entry["id"])

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["entry"]["paymentTermCode"], "")

    def test_discard_draft_hides_it_from_active_list_and_locks_edits(self):
        entry, line = self._draft_with_line()
        self.assertTrue(any(item["id"] == entry["id"] for item in list_purchase_entries()["items"]))

        discarded = discard_purchase_entry(entry["id"], None)

        self.assertEqual(discarded["status"], "cancelled")
        self.assertFalse(any(item["id"] == entry["id"] for item in list_purchase_entries()["items"]))
        with self.assertRaises(BejermanPurchaseError) as ctx:
            add_purchase_scan(entry["id"], line["id"], {"barcode": "CAJA-DESCARTADA"}, None)
        self.assertEqual(ctx.exception.code, "PURCHASE_ENTRY_LOCKED")

    def test_emit_persists_request_response_and_enters_verifying(self):
        entry, line = self._draft_with_line()
        entry = add_purchase_scan(entry["id"], line["id"], {"barcode": "NS001", "conversionFactor": 1, "unitValue": 1000}, None)
        client = FakePurchaseClient()
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )
        self.assertEqual(client.stock_calls, ["VAL"])

        emitted = emit_purchase_entry(
            entry["id"],
            None,
            {
                "confirmed": True,
                "preflightFingerprint": preflight["preflightFingerprint"],
            },
            client=client,
        )

        self.assertEqual(
            client.stock_calls,
            ["VAL", "VAL"],
            "La emisión debe tomar una línea base fresca y no reutilizar la prevalidación",
        )
        self.assertEqual(emitted["status"], "verifying")
        self.assertEqual(emitted["remitoNumber"], "RT R 00001-00001234")
        saved = get_purchase_entry(entry["id"])
        self.assertEqual(saved["requestPayload"]["Circuito"], "COMPRAS")
        self.assertEqual(saved["requestPayload"]["Operacion"], "IngresarListaComprobantesJSON")
        self.assertEqual(saved["responsePayload"]["lineCount"], 1)

    def test_validate_purchase_entry_warns_when_scanned_quantity_diverges_from_order(self):
        entry = self._draft_with_citation()
        client = PopulateAndEmitPurchaseClient()
        populated = populate_purchase_entry_lines_from_order(entry["id"], None, client=client)
        line = populated["lines"][0]
        self.assertEqual(line["expectedQuantity"], 12.0)
        # La OC pedía 12 y sólo se escanea 1: diferencia muy por encima del
        # umbral por defecto (20%), tiene que aparecer como warning.
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "CAJA-1", "conversionFactor": 1, "unitValue": 5},
            None,
        )

        preflight = validate_purchase_entry(entry["id"], check_remote=True, client=client)

        self.assertTrue(preflight["ok"], preflight["errors"])
        mismatches = [
            warning
            for warning in preflight["warnings"]
            if warning["code"] == "PURCHASE_ORDER_QUANTITY_MISMATCH"
        ]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["articleCode"], "INS-010")
        self.assertEqual(mismatches[0]["expectedQuantity"], 12.0)
        self.assertEqual(mismatches[0]["totalQuantity"], 1.0)

    def test_validate_purchase_entry_does_not_warn_without_order_citation(self):
        # Carga manual sin citar OC: expectedQuantity queda None y no debe
        # dispararse ningún aviso de cantidad, sin importar cuánto se escanee.
        entry, line = self._draft_with_line()
        add_purchase_scan(entry["id"], line["id"], {"barcode": "NS001", "conversionFactor": 1, "unitValue": 1000}, None)
        client = FakePurchaseClient()

        preflight = validate_purchase_entry(entry["id"], check_remote=True, client=client)

        self.assertTrue(preflight["ok"], preflight["errors"])
        self.assertEqual(preflight["warnings"], [])

    def test_emit_purchase_entry_requires_explicit_confirmation_for_quantity_mismatch(self):
        entry = self._draft_with_citation()
        client = PopulateAndEmitPurchaseClient()
        populated = populate_purchase_entry_lines_from_order(entry["id"], None, client=client)
        line = populated["lines"][0]
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "CAJA-1", "conversionFactor": 1, "unitValue": 5},
            None,
        )
        preflight = validate_purchase_entry(entry["id"], check_remote=True, client=client)
        self.assertTrue(preflight["ok"], preflight["errors"])

        with self.assertRaises(BejermanPurchaseError) as ctx:
            emit_purchase_entry(
                entry["id"],
                None,
                {
                    "confirmed": True,
                    "preflightFingerprint": preflight["preflightFingerprint"],
                },
                client=client,
            )
        self.assertEqual(ctx.exception.code, "PURCHASE_QUANTITY_MISMATCH_CONFIRMATION_REQUIRED")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(client.ingresar_calls, [])

        emitted = emit_purchase_entry(
            entry["id"],
            None,
            {
                "confirmed": True,
                "preflightFingerprint": preflight["preflightFingerprint"],
                "confirmedQuantityMismatch": True,
            },
            client=client,
        )
        self.assertEqual(emitted["status"], "verifying")
        self.assertEqual(len(client.ingresar_calls), 1)

    def test_verification_generates_only_after_rt_mc_items_stock_and_partida(self):
        client = FakePurchaseClient(
            remote_items=[
                {
                    "Item_CodigoArticulo": "EQ-001",
                    "Item_Deposito": "VAL",
                    "Item_Partida": "SERIE-001",
                    "Item_CantidadUM1": 1,
                    "Item_ImporteItem": 1000,
                }
            ]
        )
        entry_id, emitted = self._emit_tracked_purchase(client)

        self.assertEqual(emitted["status"], "verifying")
        self.assertEqual(len(client.ingresar_calls), 1)

        pending = verify_purchase_entry(
            entry_id,
            None,
            client=client,
        )

        self.assertFalse(pending["postconditions"]["confirmed"])
        self.assertEqual(
            pending["postconditions"]["overallStatus"],
            "pending",
        )
        self.assertTrue(pending["postconditions"]["document"]["confirmed"])
        self.assertFalse(pending["postconditions"]["stock"]["confirmed"])
        self.assertEqual(pending["entry"]["status"], "verifying")
        self.assertFalse(pending["canReopen"])

        client.stock_records = [
            {
                "Art_Codigo": "EQ-001",
                "Art_CodDeposito": "VAL",
                "Art_Partida": "SERIE-001",
                "Art_RealUM1": 1,
            }
        ]
        client.partida_records = [
            {
                "Partida_Codigo": "SERIE-001",
                "Deposito_Codigo": "VAL",
            }
        ]
        verified = verify_purchase_entry(
            entry_id,
            None,
            client=client,
        )

        snapshot = verified["postconditions"]
        self.assertTrue(snapshot["confirmed"])
        self.assertEqual(snapshot["overallStatus"], "confirmed")
        self.assertEqual(snapshot["status"], "confirmed")
        self.assertTrue(snapshot["document"]["confirmed"])
        self.assertTrue(snapshot["document"]["items"]["confirmed"])
        self.assertTrue(snapshot["header"]["confirmed"])
        self.assertTrue(snapshot["stock"]["confirmed"])
        self.assertEqual(snapshot["stock"]["depositCode"], "VAL")
        self.assertEqual(snapshot["partidas"]["required"], 1)
        self.assertTrue(snapshot["partidas"]["confirmed"])
        self.assertEqual(snapshot["partidas"]["confirmedCount"], 1)
        self.assertEqual(verified["entry"]["status"], "generated")
        self.assertFalse(verified["canReopen"])
        self.assertEqual(len(client.ingresar_calls), 1)

        persisted = get_purchase_entry(entry_id)
        persisted_snapshot = persisted["responsePayload"][
            "postconditionVerification"
        ]
        self.assertTrue(persisted_snapshot["confirmed"])
        self.assertEqual(persisted_snapshot["overallStatus"], "confirmed")

    def test_pending_postconditions_do_not_generate_or_allow_reopen(self):
        client = FakePurchaseClient()
        entry_id, emitted = self._emit_tracked_purchase(client)
        self.assertEqual(emitted["status"], "verifying")

        pending = verify_purchase_entry(
            entry_id,
            None,
            client=client,
        )

        self.assertFalse(pending["postconditions"]["confirmed"])
        self.assertEqual(pending["postconditions"]["overallStatus"], "pending")
        self.assertFalse(pending["postconditions"]["document"]["confirmed"])
        self.assertFalse(pending["postconditions"]["stock"]["confirmed"])
        self.assertEqual(pending["entry"]["status"], "verifying")
        self.assertFalse(pending["canReopen"])
        with self.assertRaises(BejermanPurchaseError):
            reopen_purchase_entry(
                entry_id,
                None,
                {"confirmedNotImported": True},
            )
        self.assertEqual(get_purchase_entry(entry_id)["status"], "verifying")
        self.assertEqual(len(client.ingresar_calls), 1)

    def test_missing_remote_total_or_date_keeps_postconditions_pending(self):
        client = FakePurchaseClient(
            remote_items=[
                {
                    "Item_CodigoArticulo": "EQ-001",
                    "Item_Deposito": "VAL",
                    "Item_Partida": "SERIE-001",
                    "Item_CantidadUM1": 1,
                    "Item_ImporteItem": 1000,
                }
            ],
            remote_total=None,
            remote_date=None,
        )
        entry_id, emitted = self._emit_tracked_purchase(client)
        self.assertEqual(emitted["status"], "verifying")
        client.stock_records = [
            {
                "Art_Codigo": "EQ-001",
                "Art_CodDeposito": "VAL",
                "Art_Partida": "SERIE-001",
                "Art_RealUM1": 1,
            }
        ]
        client.partida_records = [
            {
                "Partida_Codigo": "SERIE-001",
                "Deposito_Codigo": "VAL",
            }
        ]

        pending = verify_purchase_entry(entry_id, None, client=client)

        document = pending["postconditions"]["document"]
        self.assertEqual(document["status"], "pending")
        self.assertEqual(document["totalStatus"], "pending")
        self.assertEqual(document["dateStatus"], "pending")
        self.assertTrue(pending["postconditions"]["stock"]["confirmed"])
        self.assertEqual(pending["postconditions"]["overallStatus"], "pending")
        self.assertEqual(pending["entry"]["status"], "verifying")

    def test_unmapped_remote_item_requires_manual_review(self):
        client = FakePurchaseClient(
            remote_items=[
                {
                    "Item_CodigoArticulo": "EQ-001",
                    "Item_Deposito": "VAL",
                    "Item_Partida": "SERIE-001",
                    "Item_CantidadUM1": 1,
                    "Item_ImporteItem": 1000,
                },
                {
                    "Item_Descripcion": "Línea remota sin artículo",
                    "Item_CantidadUM1": 1,
                    "Item_ImporteItem": 0,
                },
            ]
        )
        entry_id, emitted = self._emit_tracked_purchase(client)
        self.assertEqual(emitted["status"], "verifying")
        client.stock_records = [
            {
                "Art_Codigo": "EQ-001",
                "Art_CodDeposito": "VAL",
                "Art_Partida": "SERIE-001",
                "Art_RealUM1": 1,
            }
        ]
        client.partida_records = [
            {
                "Partida_Codigo": "SERIE-001",
                "Deposito_Codigo": "VAL",
            }
        ]

        reviewed = verify_purchase_entry(entry_id, None, client=client)

        document = reviewed["postconditions"]["document"]
        self.assertEqual(document["status"], "mismatch")
        self.assertFalse(document["items"]["confirmed"])
        self.assertEqual(
            document["items"]["mismatches"][0]["reason"],
            "unexpected_unmapped",
        )
        self.assertEqual(reviewed["postconditions"]["overallStatus"], "ambiguous")
        self.assertEqual(reviewed["entry"]["status"], "failed_review")

    def test_active_untracked_article_blocks_a_second_purchase_claim(self):
        client = FakePurchaseClient()
        first_entry, first_line = self._draft_with_line()
        add_purchase_scan(
            first_entry["id"],
            first_line["id"],
            {"barcode": "BULTO-PRIMERO", "conversionFactor": 1, "unitValue": 10},
            None,
        )
        first_preflight = validate_purchase_entry(
            first_entry["id"],
            check_remote=True,
            client=client,
        )
        first_emitted = emit_purchase_entry(
            first_entry["id"],
            None,
            {
                "confirmed": True,
                "preflightFingerprint": first_preflight["preflightFingerprint"],
            },
            client=client,
        )
        self.assertEqual(first_emitted["status"], "verifying")

        second_entry, second_line = self._draft_with_line(
            document_number="00001235",
        )
        add_purchase_scan(
            second_entry["id"],
            second_line["id"],
            {"barcode": "BULTO-SEGUNDO", "conversionFactor": 1, "unitValue": 10},
            None,
        )
        second_preflight = validate_purchase_entry(
            second_entry["id"],
            check_remote=True,
            client=client,
        )
        self.assertTrue(second_preflight["ok"], second_preflight["errors"])

        with self.assertRaises(BejermanPurchaseError) as blocked:
            emit_purchase_entry(
                second_entry["id"],
                None,
                {
                    "confirmed": True,
                    "preflightFingerprint": second_preflight[
                        "preflightFingerprint"
                    ],
                },
                client=client,
            )

        self.assertEqual(
            blocked.exception.code,
            "PURCHASE_ARTICLE_EMISSION_PENDING",
        )
        self.assertEqual(len(client.ingresar_calls), 1)
        self.assertNotEqual(
            get_purchase_entry(second_entry["id"])["status"],
            "running",
        )

    def test_generated_entry_preserves_successful_verification_evidence(self):
        client = FakePurchaseClient(
            remote_items=[
                {
                    "Item_CodigoArticulo": "EQ-001",
                    "Item_Deposito": "VAL",
                    "Item_Partida": "SERIE-001",
                    "Item_CantidadUM1": 1,
                    "Item_ImporteItem": 1000,
                }
            ]
        )
        entry_id, emitted = self._emit_tracked_purchase(client)
        self.assertEqual(emitted["status"], "verifying")
        client.stock_records = [
            {
                "Art_Codigo": "EQ-001",
                "Art_CodDeposito": "VAL",
                "Art_Partida": "SERIE-001",
                "Art_RealUM1": 1,
            }
        ]
        client.partida_records = [
            {
                "Partida_Codigo": "SERIE-001",
                "Deposito_Codigo": "VAL",
            }
        ]
        first = verify_purchase_entry(entry_id, None, client=client)
        self.assertEqual(first["entry"]["status"], "generated")
        self.assertTrue(first["postconditions"]["confirmed"])

        client.document_after_emit = False
        client.stock_records = []
        client.partida_records = []
        refreshed = verify_purchase_entry(entry_id, None, client=client)

        self.assertEqual(refreshed["entry"]["status"], "generated")
        self.assertTrue(refreshed["postconditions"]["confirmed"])
        persisted = get_purchase_entry(entry_id)
        self.assertTrue(
            persisted["responsePayload"]["postconditionVerification"][
                "confirmed"
            ]
        )
        self.assertEqual(len(client.ingresar_calls), 1)

    def test_remote_duplicate_blocks_validation_for_emit(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(entry["id"], line["id"], {"barcode": "NS001", "conversionFactor": 1, "unitValue": 1000}, None)

        result = validate_purchase_entry(entry["id"], check_remote=True, client=FakePurchaseClient(duplicate=True))

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "REMOTE_DUPLICATE")

    def test_remote_preflight_returns_net_tax_total_and_fingerprint(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "CAJA-IVA", "conversionFactor": 2, "unitValue": 100},
            None,
        )

        result = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=FakePurchaseClient(),
        )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["documentTotals"]["netValue"], 200.0)
        self.assertEqual(result["documentTotals"]["taxValue"], 42.0)
        self.assertEqual(result["documentTotals"]["totalValue"], 242.0)
        self.assertRegex(result["preflightFingerprint"], r"^[0-9a-f]{64}$")

    def test_remote_preflight_resolves_article_by_exact_code(self):
        entry = create_purchase_entry(
            {
                "supplierCode": "1",
                "supplierCodeRaw": "     1",
                "supplierName": "Proveedor Test",
                "comprobanteLetra": "R",
                "comprobantePtoVenta": "00001",
                "comprobanteNumero": "00009999",
                "issueDate": "2026-06-12",
            },
            None,
        )
        entry = add_purchase_line(
            entry["id"],
            {
                "articleCode": "OUT-999",
                "articleDescription": "Artículo fuera de los primeros 50",
                "defaultConversionFactor": 1,
                "defaultUnitValue": 10,
            },
            None,
        )
        add_purchase_scan(
            entry["id"],
            entry["lines"][0]["id"],
            {"barcode": "OUTSIDE-CATALOG", "unitValue": 10},
            None,
        )
        entry = add_purchase_line(
            entry["id"],
            {
                "articleCode": "INS-010",
                "articleDescription": "Segundo artículo exacto",
                "defaultConversionFactor": 1,
                "defaultUnitValue": 10,
            },
            None,
        )
        add_purchase_scan(
            entry["id"],
            entry["lines"][1]["id"],
            {"barcode": "SECOND-ARTICLE", "unitValue": 10},
            None,
        )
        client = ExactArticleLookupClient()

        result = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(client.article_queries, [("", 5000)])

    def test_emit_requires_explicit_confirmation_and_enabled_flag(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "CONFIRM-1", "unitValue": 10},
            None,
        )
        client = FakePurchaseClient()
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )

        with self.assertRaises(BejermanPurchaseError) as confirmation:
            emit_purchase_entry(
                entry["id"],
                None,
                {
                    "preflightFingerprint": preflight["preflightFingerprint"],
                },
                client=client,
            )
        self.assertEqual(
            confirmation.exception.code,
            "PURCHASE_EMISSION_CONFIRMATION_REQUIRED",
        )

        with self.settings(BEJERMAN_PURCHASE_EMISSION_ENABLED=False):
            with self.assertRaises(BejermanPurchaseError) as disabled:
                emit_purchase_entry(
                    entry["id"],
                    None,
                    {
                        "confirmed": True,
                        "preflightFingerprint": preflight["preflightFingerprint"],
                    },
                    client=client,
                )
        self.assertEqual(disabled.exception.code, "PURCHASE_EMISSION_DISABLED")
        self.assertEqual(len(client.ingresar_calls), 0)

    def test_emit_rejects_stale_preflight_without_calling_sdk(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "STALE-1", "unitValue": 10},
            None,
        )
        client = FakePurchaseClient()
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )
        update_purchase_scan(
            entry["id"],
            get_purchase_entry(entry["id"])["lines"][0]["scans"][0]["id"],
            {"unitValue": 20},
            None,
        )

        with self.assertRaises(BejermanPurchaseError) as stale:
            emit_purchase_entry(
                entry["id"],
                None,
                {
                    "confirmed": True,
                    "preflightFingerprint": preflight["preflightFingerprint"],
                },
                client=client,
            )

        self.assertEqual(stale.exception.code, "PURCHASE_PREFLIGHT_STALE")
        self.assertEqual(len(client.ingresar_calls), 0)

    def test_mismatched_bejerman_document_requires_review(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "MISMATCH-1", "unitValue": 10},
            None,
        )
        client = FakePurchaseClient(response_number="00009999")
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )

        with self.assertRaises(BejermanPurchaseError) as mismatch:
            emit_purchase_entry(
                entry["id"],
                None,
                {
                    "confirmed": True,
                    "preflightFingerprint": preflight["preflightFingerprint"],
                },
                client=client,
            )

        self.assertEqual(
            mismatch.exception.code,
            "BEJERMAN_PURCHASE_EMIT_REVIEW_REQUIRED",
        )
        self.assertEqual(get_purchase_entry(entry["id"])["status"], "failed_review")

    def test_failed_review_not_found_stays_blocked_for_manual_review(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "TIMEOUT-1", "unitValue": 10},
            None,
        )
        failing_client = FakePurchaseClient(emit_error=TimeoutError("timeout"))
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=failing_client,
        )
        with self.assertRaises(BejermanPurchaseError):
            emit_purchase_entry(
                entry["id"],
                None,
                {
                    "confirmed": True,
                    "preflightFingerprint": preflight["preflightFingerprint"],
                },
                client=failing_client,
            )

        verification = verify_purchase_entry(
            entry["id"],
            None,
            client=FakePurchaseClient(duplicate=False),
        )
        self.assertEqual(verification["outcome"], "pending")
        self.assertEqual(
            verification["entry"]["responsePayload"]["verification"][
                "outcome"
            ],
            "not_found",
        )
        self.assertFalse(verification["canReopen"])

        with self.assertRaises(BejermanPurchaseError) as blocked:
            reopen_purchase_entry(
                entry["id"],
                None,
                {"confirmedNotImported": True},
            )
        self.assertEqual(
            blocked.exception.code,
            "PURCHASE_REOPEN_MANUAL_REVIEW_REQUIRED",
        )
        self.assertEqual(
            get_purchase_entry(entry["id"])["status"],
            "failed_review",
        )

    def test_provider_article_and_deposit_contract_fail_closed(self):
        with self.assertRaises(BejermanPurchaseError) as deposit:
            create_purchase_entry({"depositCode": "SAR"}, None)
        self.assertEqual(deposit.exception.code, "PURCHASE_DEPOSIT_NOT_ALLOWED")

        entry, line = self._draft_with_line()
        add_purchase_scan(
            entry["id"],
            line["id"],
            {"barcode": "DISABLED-1", "unitValue": 10},
            None,
        )
        result = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=FakePurchaseClient(purchase_enabled=False),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "PURCHASE_ARTICLE_DISABLED")

    @override_settings(BEJERMAN_PURCHASE_MAX_BULK_SCANS=9999)
    def test_bulk_load_accepts_5000_codes_and_rejects_5001(self):
        entry, line = self._draft_with_line()
        barcodes = [f"IMP-{index:05d}" for index in range(1, 5001)]

        loaded = add_purchase_scan(
            entry["id"],
            line["id"],
            {
                "barcodes": barcodes,
                "conversionFactor": 1,
                "unitValue": 10,
            },
            None,
        )
        rejected_entry, rejected_line = self._draft_with_line(
            document_number="00001235",
        )

        self.assertEqual(len(loaded["lines"][0]["scans"]), 5000)
        self.assertEqual(loaded["totalQuantity"], 5000.0)
        self.assertEqual(loaded["totalValue"], 50000.0)
        with self.assertRaises(BejermanPurchaseError) as limit:
            add_purchase_scan(
                rejected_entry["id"],
                rejected_line["id"],
                {
                    "barcodes": barcodes + ["IMP-05001"],
                    "conversionFactor": 1,
                    "unitValue": 10,
                },
                None,
            )
        self.assertEqual(limit.exception.code, "PURCHASE_SCAN_LIMIT_EXCEEDED")
        self.assertEqual(
            len(get_purchase_entry(rejected_entry["id"])["lines"][0]["scans"]),
            0,
        )

        incremental_entry, incremental_line = self._draft_with_line(
            document_number="00001236",
        )
        first_batch = [f"PASO-{index:05d}" for index in range(1, 3001)]
        second_batch = [f"PASO-{index:05d}" for index in range(3001, 5002)]
        add_purchase_scan(
            incremental_entry["id"],
            incremental_line["id"],
            {
                "barcodes": first_batch,
                "conversionFactor": 1,
                "unitValue": 10,
            },
            None,
        )
        with self.assertRaises(BejermanPurchaseError) as accumulated_limit:
            add_purchase_scan(
                incremental_entry["id"],
                incremental_line["id"],
                {
                    "barcodes": second_batch,
                    "conversionFactor": 1,
                    "unitValue": 10,
                },
                None,
            )
        self.assertEqual(
            accumulated_limit.exception.code,
            "PURCHASE_SCAN_LIMIT_EXCEEDED",
        )
        self.assertEqual(
            len(
                get_purchase_entry(incremental_entry["id"])["lines"][0][
                    "scans"
                ]
            ),
            3000,
        )
        with self.settings(BEJERMAN_PURCHASE_MAX_BULK_SCANS=2999):
            legacy_over_limit = validate_purchase_entry(
                incremental_entry["id"],
            )
        self.assertFalse(legacy_over_limit["ok"])
        self.assertIn(
            "PURCHASE_SCAN_LIMIT_EXCEEDED",
            [
                item["code"]
                for item in legacy_over_limit["errors"]
            ],
        )

        client = FakePurchaseClient()
        preflight = validate_purchase_entry(
            entry["id"],
            check_remote=True,
            client=client,
        )
        self.assertTrue(preflight["ok"], preflight["errors"])
        emitted = emit_purchase_entry(
            entry["id"],
            None,
            {
                "confirmed": True,
                "preflightFingerprint": preflight["preflightFingerprint"],
            },
            client=client,
        )
        self.assertEqual(emitted["status"], "verifying")
        sent_items = client.ingresar_calls[0][0][0]["Comprobante_Items"]
        self.assertEqual(
            len(sent_items),
            5000,
        )
        self.assertEqual(sent_items[999]["Item_NumeroRenglon"], 1000)
        self.assertEqual(sent_items[-1]["Item_NumeroRenglon"], 5000)
        persisted = get_purchase_entry(entry["id"])
        serialized = json.loads(
            persisted["requestPayload"]["ParametrosJson"][0]
        )
        self.assertEqual(
            len(serialized[0]["Comprobante_Items"]),
            5000,
        )
