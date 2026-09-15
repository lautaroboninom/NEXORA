"""Un equipo del parque de alquiler va a STL, nunca a STC.

Regresion del caso del 04/09/2026 (MG 7112, partida F1121504012): equipos nuestros
terminaron en el deposito de equipos liberados para cliente (STC) en vez del de listos
para alquiler (STL). El RTA de alquiler descuenta siempre de STL, asi que una unidad
parada en STC deja el saldo de STL en negativo, en silencio.

Los dos agujeros que se tapan aca:

* `MG 001678`: el numero interno tambien se carga con ceros a la izquierda y la regex
  aceptaba hasta 4 digitos, asi que esos equipos no se reconocian como propios.
* MG 2562: le vaciaron el `numero_interno` 24 segundos antes de imprimir la orden de
  salida y el equipo se fue a STC. El motivo del ingreso no depende de ese campo.

Son SimpleTestCase a proposito: no necesitan Postgres ni el SDK.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from service.bejerman_sync import (
    _is_internal_equipment,
    ingreso_targets_rental_stock,
)

BASE = {
    "ingreso_id": 29668,
    "customer_id": 12,
    "numero_serie": "F1121504012",
    "numero_interno": "MG 7112",
    "n_de_control": "",
    "motivo": "baja alquiler",
    "mg_estado": "activo",
    "mg_venta_fecha": None,
    "mg_venta_factura_numero": "",
    "mg_venta_remito_numero": "",
    "mg_venta_customer_id": None,
}


def contexto(**cambios):
    return {**BASE, **cambios}


class NumeroInternoTests(SimpleTestCase):
    def test_acepta_el_numero_interno_con_ceros_a_la_izquierda(self):
        # 13 equipos en produccion se cargaron asi y quedaban fuera del circuito propio.
        self.assertTrue(_is_internal_equipment("MG 001678"))
        self.assertTrue(_is_internal_equipment("MG 002562"))

    def test_sigue_aceptando_el_formato_corto_y_los_otros_prefijos(self):
        self.assertTrue(_is_internal_equipment("MG 7112"))
        self.assertTrue(_is_internal_equipment("NM 0258"))
        self.assertTrue(_is_internal_equipment("CE 0102"))

    def test_no_confunde_una_serie_de_cliente_con_un_numero_interno(self):
        self.assertFalse(_is_internal_equipment("F1121504012"))
        self.assertFalse(_is_internal_equipment("MZJ5S121244"))
        self.assertFalse(_is_internal_equipment("MG"))
        self.assertFalse(_is_internal_equipment("MG SM-004"))


class RutaDeStockTests(SimpleTestCase):
    def _corre(self, row):
        with (
            patch("service.bejerman_sync._ingreso_context", return_value=row),
            patch("service.bejerman_sync._mg_owner_customer_id", return_value=None),
        ):
            return ingreso_targets_rental_stock(row["ingreso_id"])

    def test_equipo_con_numero_interno_va_al_parque_de_alquiler(self):
        self.assertTrue(self._corre(contexto(motivo="reparación")))

    def test_la_baja_de_alquiler_alcanza_aunque_falte_el_numero_interno(self):
        # El caso MG 2562: el numero se borro justo antes de liberar el equipo.
        self.assertTrue(self._corre(contexto(numero_interno="", motivo="baja alquiler")))

    def test_la_reparacion_de_alquiler_tambien_alcanza(self):
        self.assertTrue(self._corre(contexto(numero_interno="", motivo="reparación alquiler")))

    def test_un_equipo_de_cliente_sigue_yendo_a_stc(self):
        self.assertFalse(self._corre(contexto(numero_interno="", motivo="reparación")))

    def test_un_mg_ya_vendido_es_del_cliente_aunque_vuelva_de_un_alquiler(self):
        self.assertFalse(
            self._corre(contexto(numero_interno="", mg_estado="inactivo_venta", motivo="baja alquiler"))
        )
        self.assertFalse(
            self._corre(
                contexto(numero_interno="MG 7112", mg_venta_factura_numero="A-0001", motivo="baja alquiler")
            )
        )


class SalidaDeUnEquipoPropioTests(SimpleTestCase):
    databases = {"default"}
    """Un MG no sale por RSS: sale por RTA/RTN si se alquila, o por RT si se vende.

    El RSS es la salida de un equipo de CLIENTE y su perfil pide la partida en STC, donde
    un equipo propio no puede estar. Antes de esta guarda, liberar un MG creaba igual la
    orden de salida: el 04/09/2026 el RSS 00004-00005202 de MG 4552 dejo una unidad
    fantasma en STC y salio sin mover stock.
    """

    CONTEXTO = {"ingreso_id": 29534, "estado": "liberado"}

    def _corre(self, es_propio):
        from service import delivery_orders

        with (
            patch.object(delivery_orders, "_service_release_context", return_value=dict(self.CONTEXTO)),
            patch.object(delivery_orders, "_ingreso_belongs_to_rental_fleet", return_value=es_propio),
            patch.object(delivery_orders, "create_delivery_order") as crear,
            patch.object(delivery_orders, "_existing_service_release_order_id", return_value=None),
            patch.object(delivery_orders, "_article_mapping_for_service_release", return_value={}),
        ):
            resultado = delivery_orders.ensure_service_release_order_for_ingreso(29534)
        return resultado, crear

    def test_un_equipo_propio_no_genera_orden_de_salida(self):
        resultado, crear = self._corre(es_propio=True)
        self.assertIsNone(resultado)
        crear.assert_not_called()

    def test_un_equipo_de_cliente_sigue_generando_la_orden(self):
        # No llega a crearla en este test (falta el resto del contexto), pero tiene que
        # pasar la guarda: lo que se verifica es que NO corta por ser propio.
        from service import delivery_orders

        with (
            patch.object(delivery_orders, "_service_release_context", return_value=dict(self.CONTEXTO)),
            patch.object(delivery_orders, "_ingreso_belongs_to_rental_fleet", return_value=False) as gate,
            patch.object(delivery_orders, "_article_mapping_for_service_release", side_effect=RuntimeError("siguio")),
        ):
            with self.assertRaises(RuntimeError):
                delivery_orders.ensure_service_release_order_for_ingreso(29534)
        gate.assert_called_once_with(29534)
