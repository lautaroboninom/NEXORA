"""El deposito de una reparacion es STC, no VAL.

Regresion del caso del 02/09/2026: el modal "Editar orden de entrega" de una OS liberada
consultaba el stock del articulo en VAL (el deposito de venta) y devolvia
"La partida ... no figura para este articulo en el deposito seleccionado", aunque la serie
estaba en Bejerman con stock 1 en STC. El renglon nacia sin deposito y cada refresco de la
orden lo volvia a anular, asi que el front caia en su default de venta.

Son SimpleTestCase a proposito: no necesitan Postgres ni el SDK.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from service.delivery_orders import (
    DeliveryOrderError,
    _service_release_item_payload,
    _service_release_order_payload,
    _validate_delivery_partidas_stock,
    remito_profile_for_type,
)

CONTEXTO_OS = {
    "ingreso_id": 29645,
    "device_id": 815,
    "customer_id": 42,
    "customer_name": "Cliente Reparacion",
    "bejerman_customer_code": "CLIREP",
    "company_key": "SEPID",
    "equipment_serial": "Y662124170133",
    "equipment_internal_number": "",
    "tipo_equipo": "Calentador humidificador",
    "marca": "Yamind",
    "modelo": "DM26",
    "resolucion": "reparado",
    "order_date": "2026-08-26",
}
MAPEO = {"article_code": "1111009", "article_description": "Calentador humidificador"}


class ServiceReleaseDepositTests(SimpleTestCase):
    def test_el_renglon_de_una_reparacion_nace_en_stc(self):
        item = _service_release_item_payload(CONTEXTO_OS, MAPEO)

        self.assertEqual(item["stockDepositCode"], "STC")
        self.assertEqual(item["stockDepositCode"], remito_profile_for_type("service_release").deposit_code)
        self.assertEqual(item["partida"], "Y662124170133")
        self.assertEqual(item["articleCode"], "1111009")

    def test_la_orden_automatica_tambien_lleva_el_deposito(self):
        payload = _service_release_order_payload(29645, CONTEXTO_OS, MAPEO)

        self.assertEqual(payload["deliveryType"], "service_release")
        self.assertEqual(payload["items"][0]["stockDepositCode"], "STC")


class ServiceReleasePartidaValidationTests(SimpleTestCase):
    """La validacion de partidas ahora alcanza a las reparaciones, con dos concesiones."""

    def _item(self, **cambios):
        item = {
            "articleCode": "1111009",
            "quantity": 1,
            "partida": "Y662124170133",
            "stockDepositCode": "STC",
        }
        item.update(cambios)
        return item

    def test_bloquea_la_partida_que_no_esta_en_el_deposito(self):
        with patch("service.delivery_orders._delivery_partida_stock_lots", return_value=[]):
            with self.assertRaises(DeliveryOrderError) as ctx:
                _validate_delivery_partidas_stock([self._item()], "SEPID", "service_release", None)

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDA_NOT_FOUND")
        self.assertIn("STC", str(ctx.exception))
        # El operador tiene que poder distinguir "esta en otro lado" de "todavia no llego".
        self.assertIn("sincronización de stock", str(ctx.exception))

    def test_acepta_la_partida_que_si_esta_en_stc(self):
        lote = {
            "partida": "Y662124170133",
            "availableQuantity": 1,
            "depositCode": "STC",
            "expirationDate": "",
        }
        item = self._item()
        with patch("service.delivery_orders._delivery_partida_stock_lots", return_value=[lote]):
            _validate_delivery_partidas_stock([item], "SEPID", "service_release", None)

        # La validacion tambien devuelve la foto de stock al renglon.
        self.assertEqual(item["stockDepositCode"], "STC")
        self.assertEqual(str(item["stockAvailableQuantity"]), "1")

    def test_una_reparacion_sin_articulo_mapeado_no_queda_bloqueada(self):
        def no_deberia_consultarse(*args, **kwargs):
            raise AssertionError("sin articulo no hay stock contra el cual comparar la partida")

        with patch("service.delivery_orders._delivery_partida_stock_lots", no_deberia_consultarse):
            _validate_delivery_partidas_stock(
                [self._item(articleCode="")], "SEPID", "service_release", None
            )

    def test_una_venta_sin_articulo_sigue_bloqueada(self):
        with patch("service.delivery_orders._delivery_partida_stock_lots", return_value=[]):
            with self.assertRaises(DeliveryOrderError) as ctx:
                _validate_delivery_partidas_stock(
                    [self._item(articleCode="", stockDepositCode="VAL")], "SEPID", "sale", None
                )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDA_ARTICLE_REQUIRED")

    def test_bejerman_caido_no_frena_la_edicion_de_una_reparacion(self):
        def bejerman_caido(*args, **kwargs):
            raise RuntimeError("ServiceActivationException")

        with patch("service.delivery_orders._delivery_partida_stock_lots", bejerman_caido):
            _validate_delivery_partidas_stock([self._item()], "SEPID", "service_release", None)

    def test_bejerman_caido_sigue_frenando_una_venta(self):
        def bejerman_caido(*args, **kwargs):
            raise RuntimeError("ServiceActivationException")

        with patch("service.delivery_orders._delivery_partida_stock_lots", bejerman_caido):
            with self.assertRaises(DeliveryOrderError) as ctx:
                _validate_delivery_partidas_stock(
                    [self._item(stockDepositCode="VAL")], "SEPID", "sale", None
                )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_PARTIDA_STOCK_UNAVAILABLE")
