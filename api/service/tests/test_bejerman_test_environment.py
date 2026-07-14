from decimal import Decimal

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from service.management.commands.seed_bejerman_test_environment import (
    TEST_ARTICLES,
    TEST_CUSTOMER_CODE,
    TEST_CUSTOMER_SITIVA6_CODE,
    _assert_test_target as assert_seed_target,
    _customer_payload,
    _article_payload,
    _ensure_valued_rt_stock,
)
from service.management.commands.smoke_bejerman_remitos_test import (
    _assert_test_target as assert_smoke_target,
    _rt_point,
    _scenarios_for_company,
)


TEST_WSDL = "http://bejerman.test/Bejerman-SDK-WS-TEST/EFlexSDK_Service.svc"


class FakeTestStockClient:
    def __init__(self, stock: Decimal = Decimal("0")):
        self.stock = stock
        self.movements = []

    def obtener_stock_deposito(self, deposit):
        rows = []
        if self.stock:
            rows.append(
                {
                    "Art_CodGen": TEST_ARTICLES[1]["code"],
                    "Art_CodDeposito": deposit,
                    "Art_RealUM1": str(self.stock),
                }
            )
        return {"Resultado": "OK", "DatosJSON": rows}

    def ingresar_lista_comprobantes_stock_json(self, comprobantes, *, numera_flex="N"):
        self.movements.append({"items": comprobantes, "numeraFlex": numera_flex})
        self.stock += Decimal(str(comprobantes[0]["Comprobante_CantidadUM1"]))
        return {"Resultado": "OK", "ErrorMsg": "", "DatosJSON": comprobantes}


@override_settings(
    BEJERMAN_WSDL_URL=TEST_WSDL,
    BEJERMAN_COMPANY_TEST="SEP",
    BEJERMAN_COMPANY_SEPID="SEP",
    BEJERMAN_COMPANY_MGBIO="MGBI",
    BEJERMAN_STOCK_ENTRY_COMPROBANTE="ENT",
    BEJERMAN_STOCK_NUMERA_FLEX="N",
)
class BejermanTestEnvironmentTests(SimpleTestCase):
    def test_codigos_de_cliente_respetan_el_maximo_bejerman(self):
        self.assertLessEqual(len(TEST_CUSTOMER_CODE), 6)
        self.assertLessEqual(len(TEST_CUSTOMER_SITIVA6_CODE), 6)
        self.assertNotEqual(TEST_CUSTOMER_CODE, TEST_CUSTOMER_SITIVA6_CODE)

    def test_codigos_de_articulo_son_genericos_y_caben_en_stock(self):
        for spec in TEST_ARTICLES:
            self.assertLessEqual(len(spec["code"]), 8)
            self.assertNotIn("-", spec["code"])

    def test_payload_de_articulo_incluye_definibles_requeridos(self):
        options = {
            "article_class": "UNI",
            "vat_code": "01",
            "currency_code": "1",
            "exchange_type": "UNI",
            "supplier_code": "MGBIO",
            "tax_credit_category": "7",
            "definible1_code": "12",
            "definible2_code": "1402",
        }
        payload = _article_payload(TEST_ARTICLES[0], options)

        self.assertEqual(payload["Clase_Codigo"], "UNI")
        self.assertEqual(payload["Proveedor_Codigo"], "MGBIO")
        self.assertEqual(payload["Art_CodRubroCreditoFiscal"], "7")
        self.assertEqual(payload["Art_MascaraPartida"], "A")
        self.assertEqual(payload["Art_CodMonedaPrecioProveedor"], "1")
        self.assertEqual(payload["Art_CodTipoCambioProveedor"], "UNI")
        self.assertLessEqual(len(payload["Art_DescripcionReducida"]), 10)
        no_partida = _article_payload(TEST_ARTICLES[1], options)
        self.assertNotIn("Art_MascaraPartida", no_partida)
        self.assertEqual(payload["Art_CodDefinible1"], "12")
        self.assertEqual(payload["Art_CodDefinible2"], "1402")

    def test_payload_de_cliente_usa_codigos_maestros_validos(self):
        regular = _customer_payload()
        final = _customer_payload(TEST_CUSTOMER_SITIVA6_CODE, iva_code="6", cuit="20000000001")

        self.assertEqual(regular["Cliente_CodigoProvincia"], "001")
        self.assertEqual(regular["Cliente_SituacionIVACodigo"], "1")
        self.assertEqual(regular["Cliente_TipoDocumentoCodigo"], 1)
        self.assertEqual(regular["Cliente_CUIT"], "30700000008")
        self.assertEqual(final["Cliente_SituacionIVACodigo"], "6")
        self.assertEqual(final["Cliente_CUIT"], "20000000001")

    def test_guardas_aceptan_test_sep_y_mgbi(self):
        self.assertEqual(assert_seed_target("TEST").bejerman_company, "SEP")
        self.assertEqual(assert_seed_target("SEPID").bejerman_company, "SEP")
        self.assertEqual(assert_smoke_target("MGBIO").bejerman_company, "MGBI")

    @override_settings(BEJERMAN_WSDL_URL="http://bejerman.test/Bejerman-SDK-WS/EFlexSDK_Service.svc")
    def test_guardas_rechazan_el_wsdl_productivo(self):
        with self.assertRaisesRegex(CommandError, "se rechaza para evitar producción"):
            assert_seed_target("TEST")
        with self.assertRaisesRegex(CommandError, "se rechaza para evitar producción"):
            assert_smoke_target("SEPID")

    @override_settings(BEJERMAN_COMPANY_TEST="MODE")
    def test_guardas_rechazan_la_empresa_mode_inexistente(self):
        with self.assertRaisesRegex(CommandError, "debe resolver a SEP o MGBI"):
            assert_seed_target("TEST")

    def test_seed_de_stock_val_es_idempotente(self):
        client = FakeTestStockClient(Decimal("2"))

        stock, created = _ensure_valued_rt_stock(client, apply=True, minimum=Decimal("10"), delay=0)
        second_stock, second_created = _ensure_valued_rt_stock(client, apply=True, minimum=Decimal("10"), delay=0)

        self.assertEqual(stock, Decimal("10"))
        self.assertTrue(created)
        self.assertEqual(second_stock, Decimal("10"))
        self.assertFalse(second_created)
        self.assertEqual(len(client.movements), 1)
        item = client.movements[0]["items"][0]
        self.assertEqual(item["Comprobante_Tipo"], "ENT")
        self.assertEqual(item["Comprobante_ArtDeposito"], "VAL")
        self.assertEqual(item["Comprobante_CantidadUM1"], 8)

    def test_punto_de_venta_rt_test_respeta_la_empresa(self):
        self.assertEqual(_rt_point(assert_smoke_target("SEPID")), "00004")
        self.assertEqual(_rt_point(assert_smoke_target("MGBIO")), "00007")

    def test_scenario_all_respeta_los_comprobantes_definidos_por_empresa(self):
        self.assertEqual(
            _scenarios_for_company(assert_smoke_target("SEPID"), "all"),
            ("emission", "manual", "rt-valued"),
        )
        self.assertEqual(_scenarios_for_company(assert_smoke_target("MGBIO"), "all"), ("rt-valued",))
        with self.assertRaisesRegex(CommandError, "no está definido para MGBIO"):
            _scenarios_for_company(assert_smoke_target("MGBIO"), "emission")
