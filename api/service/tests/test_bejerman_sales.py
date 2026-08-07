from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from service.bejerman_sales import sale_items_from_detail_response, sync_bejerman_sale_items
from service.bejerman_sdk import BejermanSDKClient as DocumentBejermanSDKClient
from service.bejerman_sync import BejermanSDKClient as StockBejermanSDKClient


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


class BejermanSaleItemsSyncTests(SimpleTestCase):
    def _client(self):
        client = Mock()
        client.company_key = "SEPID"
        client.list_comprobantes_ventas.return_value = {"DatosJSON": []}
        client.detalle_comprobante_ventas.return_value = {"DatosJSON": {}}
        return client

    @override_settings(
        BEJERMAN_SALE_ITEMS_SYNC_CHUNK_DAYS=1,
        BEJERMAN_SALE_ITEMS_REFRESH_DAYS=0,
    )
    @patch("service.bejerman_sales.upsert_sale_items", return_value=0)
    @patch("service.bejerman_sales.sale_items_from_detail_response", return_value=[])
    @patch("service.bejerman_sales._cached_comprobante_ids", return_value=set())
    @patch("service.bejerman_sales.headers_from_sales_list")
    def test_aplica_maximo_global_de_detalles_a_todas_las_ventanas(
        self,
        headers_mock,
        _cached_mock,
        _items_mock,
        _upsert_mock,
    ):
        headers_mock.side_effect = [
            [{"Comprobante_ID": "3"}, {"Comprobante_ID": "4"}],
            [{"Comprobante_ID": "1"}, {"Comprobante_ID": "2"}],
        ]
        client = self._client()

        summary = sync_bejerman_sale_items(client, days=2, max_comprobantes=3)

        self.assertEqual(summary["detailAttempts"], 3)
        self.assertEqual(summary["details"], 3)
        self.assertTrue(summary["limitReached"])
        self.assertEqual(client.detalle_comprobante_ventas.call_count, 3)

    @override_settings(
        BEJERMAN_SALE_ITEMS_REFRESH_DAYS=2,
        BEJERMAN_SALE_ITEMS_SYNC_CHUNK_DAYS=365,
    )
    @patch("service.bejerman_sales.upsert_sale_items", return_value=0)
    @patch("service.bejerman_sales.sale_items_from_detail_response", return_value=[])
    @patch("service.bejerman_sales._cached_comprobante_ids", return_value={"viejo", "reciente"})
    @patch("service.bejerman_sales.headers_from_sales_list")
    def test_omite_comprobantes_cacheados_y_refresca_solo_los_recientes(
        self,
        headers_mock,
        _cached_mock,
        _items_mock,
        _upsert_mock,
    ):
        today = timezone.localdate()
        headers_mock.return_value = [
            {
                "Comprobante_ID": "reciente",
                "Comprobante_FechaEmision": today.isoformat(),
            },
            {
                "Comprobante_ID": "viejo",
                "Comprobante_FechaEmision": (today - timedelta(days=30)).isoformat(),
            },
        ]
        client = self._client()

        summary = sync_bejerman_sale_items(client, days=31, max_comprobantes=100)

        self.assertEqual(summary["headers"], 2)
        self.assertEqual(summary["skippedCached"], 1)
        self.assertEqual(summary["detailAttempts"], 1)
        client.detalle_comprobante_ventas.assert_called_once_with("reciente")


class BejermanHttpSessionTests(SimpleTestCase):
    @patch("service.bejerman_sdk.parse_soap_response", return_value={"Resultado": "OK"})
    def test_cliente_documentos_reutiliza_la_sesion_http(self, _parse_mock):
        client = DocumentBejermanSDKClient()
        client.endpoint_url = "http://bejerman.test/EFlexSDK_Service.svc"
        response = Mock(text="<soap />", ok=True, status_code=200, reason="OK")
        client._http = Mock()
        client._http.post.return_value = response

        client._post("accion-1", "<consulta />")
        client._post("accion-2", "<consulta />")

        self.assertEqual(client._http.post.call_count, 2)

    @patch("service.bejerman_sync._response_dict", return_value={"Resultado": "OK"})
    def test_cliente_stock_reutiliza_la_sesion_http(self, _parse_mock):
        client = StockBejermanSDKClient()
        client.endpoint_url = "http://bejerman.test/EFlexSDK_Service.svc"
        response = Mock(text="<soap />")
        response.raise_for_status.return_value = None
        client._http = Mock()
        client._http.post.return_value = response

        client._post("accion-1", "<consulta />")
        client._post("accion-2", "<consulta />")

        self.assertEqual(client._http.post.call_count, 2)
