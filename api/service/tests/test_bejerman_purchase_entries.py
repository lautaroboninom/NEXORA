import json
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

from service.bejerman_purchase_entries import (
    BejermanPurchaseError,
    add_purchase_line,
    add_purchase_scan,
    build_purchase_entry_comprobante,
    create_purchase_entry,
    discard_purchase_entry,
    emit_purchase_entry,
    get_purchase_entry,
    list_purchase_entries,
    update_purchase_scan,
    validate_purchase_entry,
)
from service.bejerman_sdk import BejermanSDKClient


class FakePurchaseClient:
    def __init__(self, *, duplicate=False):
        self.duplicate = duplicate
        self.list_calls = []
        self.ingresar_calls = []

    def list_comprobantes_compras(self, filters):
        self.list_calls.append(filters)
        if not self.duplicate:
            return {"DatosJSON": []}
        return {
            "DatosJSON": json.dumps(
                [
                    {
                        "Proveedor_Codigo": "     1",
                        "Comprobante_Tipo": "RT",
                        "Comprobante_Letra": "R",
                        "Comprobante_PtoVenta": "00001",
                        "Comprobante_Numero": "00001234",
                    }
                ]
            )
        }

    def ingresar_lista_comprobantes_compras_json(self, comprobantes, **kwargs):
        self.ingresar_calls.append((comprobantes, kwargs))
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(
                {
                    "Comprobante_Tipo": "RT",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00001",
                    "Comprobante_Numero": "00001234",
                }
            ),
        }


class BejermanPurchaseBuilderTests(SimpleTestCase):
    def test_builds_rt_mc_purchase_payload_with_factors_values_and_partidas(self):
        built = build_purchase_entry_comprobante(
            {
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
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RT")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "MC")
        self.assertEqual(comprobante["Proveedor_Codigo"], "     1")
        self.assertEqual(comprobante["Comprobante_CondicionPago"], "30")
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 1050.0)
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_CantidadUM1"], 1.0)
        self.assertEqual(comprobante["Comprobante_Items"][0]["Item_Partida"], "NS001")
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_CantidadUM1"], 10.0)
        self.assertEqual(comprobante["Comprobante_Items"][1]["Item_ImporteItem"], 50.0)


class BejermanPurchaseSdkTests(SimpleTestCase):
    def test_sdk_uses_compras_list_and_ingresar_lista_json_params(self):
        client = BejermanSDKClient(company_key=None, bejerman_company="SEP")

        with patch.object(client, "execute", return_value={"Resultado": "OK"}) as execute:
            client.list_comprobantes_compras([{"Campo": "Comprobante_Tipo", "Valor": "RT"}])
            client.ingresar_lista_comprobantes_compras_json([{"Comprobante_Tipo": "RT"}])

        self.assertEqual(execute.call_args_list[0].args[:2], ("COMPRAS", "WSListarComprobantesJSON"))
        self.assertEqual(execute.call_args_list[1].args[:2], ("COMPRAS", "IngresarListaComprobantesJSON"))
        self.assertEqual(execute.call_args_list[1].kwargs["params_json"][1:], ["N", "R"])


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
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
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_purchase_entry_events")
            cur.execute("DELETE FROM bejerman_purchase_entry_scans")
            cur.execute("DELETE FROM bejerman_purchase_entry_lines")
            cur.execute("DELETE FROM bejerman_purchase_entries")

    def _draft_with_line(self):
        entry = create_purchase_entry(
            {
                "supplierCode": "1",
                "supplierCodeRaw": "     1",
                "supplierName": "Proveedor Test",
                "paymentTermCode": "30",
                "comprobanteLetra": "R",
                "comprobantePtoVenta": "00001",
                "comprobanteNumero": "00001234",
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
        return entry, entry["lines"][0]

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

    def test_emit_persists_request_response_and_generated_number(self):
        entry, line = self._draft_with_line()
        entry = add_purchase_scan(entry["id"], line["id"], {"barcode": "NS001", "conversionFactor": 1, "unitValue": 1000}, None)

        emitted = emit_purchase_entry(entry["id"], None, client=FakePurchaseClient())

        self.assertEqual(emitted["status"], "generated")
        self.assertEqual(emitted["remitoNumber"], "RT R 00001-00001234")
        saved = get_purchase_entry(entry["id"])
        self.assertEqual(saved["requestPayload"]["Circuito"], "COMPRAS")
        self.assertEqual(saved["requestPayload"]["Operacion"], "IngresarListaComprobantesJSON")
        self.assertEqual(saved["responsePayload"]["lineCount"], 1)

    def test_remote_duplicate_blocks_validation_for_emit(self):
        entry, line = self._draft_with_line()
        add_purchase_scan(entry["id"], line["id"], {"barcode": "NS001", "conversionFactor": 1, "unitValue": 1000}, None)

        result = validate_purchase_entry(entry["id"], check_remote=True, client=FakePurchaseClient(duplicate=True))

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "REMOTE_DUPLICATE")
