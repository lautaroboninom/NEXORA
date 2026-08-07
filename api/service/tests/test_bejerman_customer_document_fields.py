import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from service.bejerman_sdk import (
    BEJERMAN_OCCASIONAL_CUSTOMER_CODE,
    BejermanSdkResponseError,
    bejerman_customer_code_for,
    build_customer_document_fields,
    build_delivery_remito_comprobante,
    build_occasional_customer_fields,
    build_service_ingress_comprobante,
    delivery_remito_config,
    is_occasional_customer_name,
    resolve_customer_document_fields,
    resolve_customer_document_fields_cached,
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

    def test_resolucion_cacheada_evita_consultar_clientes_si_la_cache_esta_completa(self):
        client = Mock()
        client.company_key = "SEPID"
        cached_fields = build_customer_document_fields(self._customer_response(), "SEDE")
        local_lookup = Mock(return_value=cached_fields)

        fields = resolve_customer_document_fields_cached(
            client,
            "SEDE",
            local_lookup,
            retry_delay_seconds=0,
        )

        self.assertEqual(fields, cached_fields)
        local_lookup.assert_called_once_with(client, "SEDE")
        client.list_clientes.assert_not_called()

    def test_resolucion_cacheada_consulta_bejerman_si_la_cache_esta_incompleta(self):
        client = Mock()
        client.company_key = "SEPID"
        client.list_clientes.return_value = self._customer_response()
        cached_fields = build_customer_document_fields(self._customer_response(), "SEDE")
        cached_fields.pop("Cliente_Provincia")

        fields = resolve_customer_document_fields_cached(
            client,
            "SEDE",
            Mock(return_value=cached_fields),
            retry_delay_seconds=0,
        )

        self.assertEqual(fields["Cliente_Provincia"], "002")
        client.list_clientes.assert_called_once_with()

    def test_resolucion_cacheada_reintenta_una_vez_un_error_de_cliente(self):
        client = Mock()
        client.company_key = "SEPID"
        client.list_clientes.side_effect = [
            {"Resultado": "OK", "DatosJSON": "[]"},
            self._customer_response(),
        ]

        with patch("service.bejerman_sdk.time.sleep") as sleep_mock:
            fields = resolve_customer_document_fields_cached(
                client,
                "SEDE",
                Mock(return_value=None),
                retry_delay_seconds=2.5,
            )

        self.assertEqual(fields["Cliente_RazonSocial"], "SEDESA S.A.")
        self.assertEqual(client.list_clientes.call_count, 2)
        sleep_mock.assert_called_once_with(2.5)

    def test_resolucion_cacheada_no_reintenta_un_error_sdk_generico(self):
        client = Mock()
        client.company_key = "SEPID"
        client.list_clientes.side_effect = BejermanSdkResponseError("ERROR_SDK_GENERICO")

        with (
            patch("service.bejerman_sdk.time.sleep") as sleep_mock,
            self.assertRaisesRegex(BejermanSdkResponseError, "ERROR_SDK_GENERICO"),
        ):
            resolve_customer_document_fields_cached(
                client,
                "SEDE",
                Mock(return_value=None),
                retry_delay_seconds=2.5,
            )

        client.list_clientes.assert_called_once_with()
        sleep_mock.assert_not_called()


class OccasionalCustomerFieldsTests(SimpleTestCase):
    def test_arma_campos_cliente_ocasional_desde_nombre_y_cuit(self):
        fields = build_occasional_customer_fields("Juan Perez", "20-30710698-2")

        self.assertEqual(
            fields,
            {
                "Cliente_RazonSocial": "Juan Perez",
                "Cliente_TipoDocumento": "1",
                "Cliente_NroDocumento": "20307106982",
                "Cliente_SitIVA": "3",
                "Cliente_Provincia": "",
                "Cliente_NumeroIIBB": "",
            },
        )

    def test_cliente_ocasional_manda_provincia_e_iibb_vacios_en_la_cabecera(self):
        # La doc de Bejerman los marca obligatorios con "pasar null o vacio si no hay valor":
        # tienen que viajar presentes, no omitidos.
        built = build_service_ingress_comprobante(
            {
                "companyKey": "SEPID",
                "issueDate": "2026-08-04",
                "customerCode": BEJERMAN_OCCASIONAL_CUSTOMER_CODE,
                "customerName": "Juan Perez",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "documentProfile": {"type": "RIS", "letter": "R", "pointOfSale": "00004", "operation": "REP"},
                "equipment": {"articleCode": "SERVICIO", "articleName": "Equipo", "serial": "A123"},
            },
            build_occasional_customer_fields("Juan Perez", "30710698"),
        )
        comprobante = built["comprobante"]

        self.assertEqual(comprobante["Cliente_Codigo"], BEJERMAN_OCCASIONAL_CUSTOMER_CODE)
        self.assertEqual(comprobante["Cliente_RazonSocial"], "Juan Perez")
        self.assertEqual(comprobante["Cliente_NroDocumento"], "30710698")
        self.assertEqual(comprobante["Cliente_TipoDocumento"], "5")
        self.assertIn("Cliente_Provincia", comprobante)
        self.assertEqual(comprobante["Cliente_Provincia"], "")
        self.assertIn("Cliente_NumeroIIBB", comprobante)
        self.assertEqual(comprobante["Cliente_NumeroIIBB"], "")

    def test_arma_campos_cliente_ocasional_desde_nombre_y_dni(self):
        fields = build_occasional_customer_fields("Maria Gomez", "30.710.698")

        self.assertEqual(fields["Cliente_TipoDocumento"], "5")
        self.assertEqual(fields["Cliente_NroDocumento"], "30710698")
        self.assertEqual(fields["Cliente_SitIVA"], "3")

    def test_cliente_ocasional_falla_sin_nombre(self):
        with self.assertRaisesRegex(BejermanSdkResponseError, "BEJERMAN_OCCASIONAL_CUSTOMER_NAME_MISSING"):
            build_occasional_customer_fields("   ", "30710698")

    def test_cliente_ocasional_falla_sin_documento(self):
        with self.assertRaisesRegex(BejermanSdkResponseError, "BEJERMAN_OCCASIONAL_CUSTOMER_DOCUMENT_MISSING"):
            build_occasional_customer_fields("Juan Perez", "")

    def test_codigo_ocasional_tiene_seis_caracteres(self):
        self.assertEqual(len(BEJERMAN_OCCASIONAL_CUSTOMER_CODE), 6)


class ParticularCustomerCodeTests(SimpleTestCase):
    """El placeholder local de particulares no debe mandar codigo de cliente a Bejerman.

    Regresion: la fila 'Particular' de customers tenia cargado cod_empresa='PTL', que no existe en
    Bejerman. Como todas las guardas del circuito preguntan `if not customer_code`, un codigo no
    vacio desactivaba el camino de cliente ocasional y la emision fallaba con
    BEJERMAN_CUSTOMER_NOT_FOUND, tanto por emision como por registracion.
    """

    def test_detecta_el_placeholder_de_particulares(self):
        for name in ("Particular", "particular", "PARTICULAR", "Particulares", "Consumidor Final"):
            with self.subTest(name=name):
                self.assertTrue(is_occasional_customer_name(name))

    def test_no_confunde_clientes_reales_con_particulares(self):
        for name in ("SEDESA S.A.", "Particular Motors SRL", "", None):
            with self.subTest(name=name):
                self.assertFalse(is_occasional_customer_name(name))

    def test_descarta_el_codigo_cargado_en_la_fila_particular(self):
        self.assertEqual(bejerman_customer_code_for("Particular", "PTL"), "")

    def test_descarta_el_codigo_aunque_la_razon_social_tenga_acentos_o_espacios(self):
        self.assertEqual(bejerman_customer_code_for("  Pärticular  ", "PTL"), "")

    def test_respeta_el_codigo_de_un_cliente_real(self):
        self.assertEqual(bejerman_customer_code_for("SEDESA S.A.", "SEDE"), "SEDE")

    def test_devuelve_vacio_cuando_el_cliente_real_no_tiene_codigo(self):
        self.assertEqual(bejerman_customer_code_for("SEDESA S.A.", None), "")
