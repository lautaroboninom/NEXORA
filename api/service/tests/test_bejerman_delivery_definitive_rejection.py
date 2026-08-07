from django.test import SimpleTestCase

from service.bejerman_delivery import _is_definitive_delivery_rejection
from service.bejerman_sdk import BejermanSdkResponseError, BejermanSdkUnavailable


class DefinitiveDeliveryRejectionTests(SimpleTestCase):
    """OE-00232 quedó en failed_review porque un rechazo de stock claro y parseado se
    trató igual que un fallo ambiguo. Estos tests cubren la clasificación que evita eso:
    ver bejerman_delivery.generate_bejerman_remito, rama antes de _mark_group_sent_unknown."""

    def test_stock_insuficiente_response_error_is_definitive(self):
        exc = BejermanSdkResponseError("El comprobante no se importó. No hay stock suficiente para el artículo 1203012 .")
        self.assertTrue(_is_definitive_delivery_rejection(exc))

    def test_generic_no_se_importo_response_error_is_definitive(self):
        exc = BejermanSdkResponseError("El comprobante no se importó. Partida inexistente.")
        self.assertTrue(_is_definitive_delivery_rejection(exc))

    def test_network_timeout_is_not_definitive_even_with_similar_text(self):
        # BejermanSdkUnavailable es lo que levanta el SDK ante timeout/conexión perdida
        # (bejerman_sdk.py, BejermanSDKClient._post) — nunca trae el cuerpo de respuesta
        # real de Bejerman, así que no debe tratarse como un rechazo confirmado aunque el
        # texto sintetizado contenga alguna de las mismas palabras por casualidad.
        exc = BejermanSdkUnavailable("Error HTTP Bejerman: no hay stock suficiente (simulado)")
        self.assertFalse(_is_definitive_delivery_rejection(exc))

    def test_ambiguous_response_error_is_not_definitive(self):
        exc = BejermanSdkResponseError("Error interno del servidor Bejerman")
        self.assertFalse(_is_definitive_delivery_rejection(exc))

    def test_plain_exception_is_not_definitive(self):
        exc = RuntimeError("no hay stock suficiente para el artículo 1203012")
        self.assertFalse(_is_definitive_delivery_rejection(exc))
