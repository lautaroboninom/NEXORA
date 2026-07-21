import json

from django.test import SimpleTestCase

from service.bejerman_sdk import (
    BejermanSdkResponseError,
    build_customer_document_fields,
    build_delivery_remito_comprobante,
    delivery_remito_config,
    resolve_customer_document_fields,
)


class CustomerDocumentFieldsTests(SimpleTestCase):
    @staticmethod
    def _customer_response(*, include_document_type=True):
        record = {
            "Cliente_Codigo": "SEDE",
            "Cliente_RazonSocial": "SEDESA S.A.",
            "Cliente_CUIT": "30710698267",
            "Cliente_CodigoProvincia": "002",
            "Cliente_SituacionIVACodigo": "1",
            "Cliente_NroIIBB": "30710698267",
        }
        if include_document_type:
            record["Cliente_TipoDocumentoCodigo"] = 1
        return {"Resultado": "OK", "DatosJSON": json.dumps([record])}

    def test_normaliza_y_envia_el_tipo_de_documento_del_cliente(self):
        fields = build_customer_document_fields(self._customer_response(), "SEDE")

        self.assertEqual(fields["Cliente_TipoDocumento"], "1")
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sedesa",
                "issueDate": "2026-07-14",
                "customerCode": "SEDE",
                "customerName": "SEDESA S.A.",
                "sellerCode": "EZE",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-00085",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1103001",
                                "articleName": "CPAP RESmart BMC GII E-20C-H-O",
                                "quantity": 1,
                                "partida": "E21BE704159",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config(
                {"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}
            ),
            fields,
        )

        self.assertEqual(built["comprobante"]["Cliente_TipoDocumento"], "1")

    def test_bloquea_la_emision_si_bejerman_no_informa_tipo_de_documento(self):
        client = type(
            "FakeClient",
            (),
            {"list_clientes": lambda _self: self._customer_response(include_document_type=False)},
        )()

        with self.assertRaisesRegex(BejermanSdkResponseError, "BEJERMAN_CUSTOMER_DOCUMENT_TYPE_MISSING"):
            resolve_customer_document_fields(client, "SEDE")
