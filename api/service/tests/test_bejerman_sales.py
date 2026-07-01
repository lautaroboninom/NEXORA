from django.test import SimpleTestCase

from service.bejerman_sales import sale_items_from_detail_response


class BejermanSaleItemsParserTests(SimpleTestCase):
    def test_extrae_serie_cliente_cuit_articulo_y_comprobante(self):
        response = {
            "DatosJSON": {
                "Comprobante_ID": 123,
                "Comprobante_Tipo": "FC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "0001",
                "Comprobante_Numero": "00000042",
                "Comprobante_FechaEmision": "2026-06-20",
                "Cliente_Codigo": "C001",
                "Cliente_RazonSocial": "Clínica Demo",
                "Cliente_NroDocumento": "30-12345678-9",
                "Comprobante_Items": [
                    {
                        "Item_CodigoArticulo": "ART-1",
                        "Item_DescripArticulo": "Equipo médico Demo",
                        "Item_Partida": "SN-001",
                        "Item_CantidadUM1": "1",
                    }
                ],
            }
        }

        items = sale_items_from_detail_response(response, company_key="SEPID")

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["company_key"], "SEPID")
        self.assertEqual(item["comprobante_id"], "123")
        self.assertEqual(item["document_label"], "FC A 0001-00000042")
        self.assertEqual(item["issue_date"], "2026-06-20")
        self.assertEqual(item["customer_code"], "C001")
        self.assertEqual(item["customer_name"], "Clínica Demo")
        self.assertEqual(item["customer_cuit"], "30123456789")
        self.assertEqual(item["article_code"], "ART-1")
        self.assertEqual(item["article_description"], "Equipo médico Demo")
        self.assertEqual(item["item_partida"], "SN-001")
        self.assertEqual(item["normalized_serial"], "SN001")
