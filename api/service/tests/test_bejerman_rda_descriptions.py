import json
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from django.test import SimpleTestCase, override_settings
from requests import Response

from service import bejerman_catalog_cache
from service.bejerman_ris import (
    BejermanRisError, _apply_bejerman_article_descriptions,
    _build_service_ingress_request, _emit_payload_to_bejerman,
)
from service.bejerman_sdk import (
    BejermanSDKClient, BejermanSdkResponseError, BejermanSdkUnavailable,
    _json_param, build_exact_article_description_map, build_service_ingress_comprobante,
)
from service.tests.test_bejerman_remito_contracts import (
    CUSTOMER_FIELDS, FakeRemitoContractClient, _payload,
)


def catalog(description="Concentrador de oxígeno Longfian JAY-5", code="1115001"):
    return {"Resultado": "OK", "DatosJSON": json.dumps([
        {"Articulo_Codigo": code, "Articulo_Descripcion": description},
    ], ensure_ascii=False)}


def rda_payload():
    payload = _payload()
    payload["documentProfile"].update(type="RDA", key="rda", operation="ALQ")
    payload["equipment"]["articleCode"] = "1115001"
    payload["equipment"]["mappedArticleName"] = "Texto local anterior"
    return payload


@override_settings(BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="RD,RDN,RDA")
class RdaMasterDescriptionTests(SimpleTestCase):
    def test_fresh_master_ignores_cached_description(self):
        client = Mock()
        client.list_articulos.return_value = catalog()
        with patch.object(bejerman_catalog_cache, "article_descriptions", return_value={"1115001": "Viejo"}) as cached:
            resolved = _apply_bejerman_article_descriptions(rda_payload(), client)
        cached.assert_not_called()
        client.list_articulos.assert_called_once_with([], 5000)
        self.assertEqual(resolved["equipment"]["bejermanMasterDescription"], "Concentrador de oxígeno Longfian JAY-5")

    def test_rental_return_keeps_nexora_model_when_article_master_was_renamed(self):
        client = Mock()
        client.list_articulos.return_value = catalog(
            "Concentrador de oxígeno Longfian JAY-5AW",
            code="1115005",
        )
        payload = rda_payload()
        payload["equipment"].update(
            articleCode="1115005",
            rentalReturnDescription="Concentrador de oxígeno Longfian JAY-5Q",
        )

        resolved = _apply_bejerman_article_descriptions(payload, client)

        self.assertEqual(
            resolved["equipment"]["mappedArticleName"],
            "Concentrador de oxígeno Longfian JAY-5Q",
        )
        self.assertEqual(
            resolved["equipment"]["bejermanMasterDescription"],
            "Concentrador de oxígeno Longfian JAY-5Q",
        )

    def test_preserves_characters_through_builder_json_and_soap_utf8(self):
        for description in ("  Oxígeno  pediátrico ñ & <equipo>  ", "Equipo\u00a0niño – portátil"):
            with self.subTest(description=description):
                fake = Mock()
                fake.list_articulos.return_value = catalog(description)
                payload = _apply_bejerman_article_descriptions(rda_payload(), fake)
                built = build_service_ingress_comprobante(payload, CUSTOMER_FIELDS)
                article = next(i for i in built["comprobante"]["Comprobante_Items"] if i["Item_Tipo"] == "A")
                self.assertEqual(article["Item_DescripArticulo"], description)
                client = BejermanSDKClient(bejerman_company="SEP")
                client.token = "TOKEN_TEST"
                client.endpoint_url = "https://bejerman.invalid/sdk"
                response = Response()
                response.status_code = 200
                response.encoding = "utf-8"
                response._content = b"<Envelope><Resultado>OK</Resultado></Envelope>"
                with patch.object(client._http, "post", return_value=response) as post:
                    client._execute_once("VENTAS", "IngresarComprobanteJSON", params_json=[_json_param(built["comprobante"])])
                body = post.call_args.kwargs["data"]
                self.assertIsInstance(body, bytes)
                self.assertIn("utf-8", post.call_args.kwargs["headers"]["Content-Type"])
                root = ET.fromstring(body)
                data = next(node.text for node in root.iter() if node.tag.endswith("ParametrosJson"))
                transmitted = json.loads(json.loads(data)[0])
                actual = next(i for i in transmitted["Comprobante_Items"] if i["Item_Tipo"] == "A")
                self.assertEqual(actual["Item_DescripArticulo"].encode("utf-8"), description.encode("utf-8"))
                client.close()

    def test_missing_empty_ambiguous_or_unrepresentable_master_is_rejected(self):
        cases = [catalog(code="999"), catalog(" "), catalog("A" * 51), catalog("Oxígeno \ufffd"), catalog("oxi\u0301geno")]
        conflicting = catalog()
        records = json.loads(conflicting["DatosJSON"])
        records.append({"Articulo_Codigo": "1115001", "Articulo_Descripcion": "Otro texto"})
        conflicting["DatosJSON"] = json.dumps(records)
        cases.append(conflicting)
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(BejermanSdkResponseError):
                    build_exact_article_description_map(response, ["1115001"])

    def test_catalog_failure_stops_before_emission_or_stock_writes(self):
        fake = FakeRemitoContractClient()
        with patch.object(fake, "list_articulos", side_effect=BejermanSdkUnavailable("Sin respuesta")), \
             patch("service.bejerman_ris.BejermanSDKClient", return_value=fake):
            with self.assertRaisesRegex(BejermanRisError, "descripción exacta"):
                _emit_payload_to_bejerman(rda_payload())
        self.assertEqual(fake.comprobantes, [])
        self.assertEqual(fake.stock_comprobantes, [])

    def test_batch_preserves_each_master_description(self):
        payload = rda_payload()
        payload["equipments"] = [payload["equipment"], {**payload["equipment"], "articleCode": "1115003", "serial": "OTRO"}]
        fake = FakeRemitoContractClient()
        resolved = _apply_bejerman_article_descriptions(payload, fake)
        self.assertEqual([i["bejermanMasterDescription"] for i in resolved["equipments"]],
                         ["Concentrador de oxígeno", "Concentrador de oxígeno Longfian JAY-5"])

    def test_last_guard_rejects_modified_item_description(self):
        fake = Mock()
        fake.list_articulos.return_value = catalog()
        payload = _apply_bejerman_article_descriptions(rda_payload(), fake)
        built = build_service_ingress_comprobante(payload, CUSTOMER_FIELDS)
        next(i for i in built["comprobante"]["Comprobante_Items"] if i["Item_Tipo"] == "A")["Item_DescripArticulo"] += " "
        with patch("service.bejerman_ris.build_service_ingress_comprobante", return_value=built):
            with self.assertRaisesRegex(BejermanRisError, "no coincide exactamente"):
                _build_service_ingress_request(payload, CUSTOMER_FIELDS, operation="IngresarComprobanteJSON", numera_flex="S", emite_reg="E")

    def test_non_rda_keeps_previous_optional_catalog_behavior(self):
        fake = Mock()
        with patch.object(bejerman_catalog_cache, "article_descriptions", return_value={}):
            payload = _payload()
            self.assertEqual(_apply_bejerman_article_descriptions(payload, fake), payload)
        fake.list_articulos.assert_not_called()
