"""La observación de la partida en Bejerman es el MG del equipo, nunca una referencia de NEXORA.

SimpleTestCase a propósito: la regla es de formato y no necesita Postgres ni el SDK.
"""
from django.test import SimpleTestCase

from service.bejerman_partida_observacion import observacion_partida
from service.bejerman_ris import _register_stock_entry_item


class ObservacionPartidaTests(SimpleTestCase):
    def test_el_mg_sale_como_lo_escribe_nexora(self):
        self.assertEqual(observacion_partida("MG7112"), "MG 7112")
        self.assertEqual(observacion_partida(" mg 42 "), "MG 0042")
        self.assertEqual(observacion_partida("NV0208"), "NV 0208")

    def test_gana_el_primer_candidato_que_es_un_codigo_interno(self):
        self.assertEqual(observacion_partida("", "MG 7"), "MG 0007")
        self.assertEqual(observacion_partida("MG 1", "MG 2"), "MG 0001")

    def test_lo_que_no_es_un_mg_no_va(self):
        self.assertEqual(observacion_partida("NEXORA-OS-29148", None, "12345"), "")
        self.assertEqual(observacion_partida(), "")

    def test_un_mg_vendido_ya_es_de_cliente_y_va_sin_observacion(self):
        # F8810: MG 7354 vendido a Novamed. El MG queda como histórico en NEXORA, no en Bejerman.
        self.assertEqual(observacion_partida("MG 7354", mg_estado="inactivo_venta"), "")
        self.assertEqual(observacion_partida("MG 7354", mg_estado="activo"), "MG 7354")


class AltaDePartidaDelRisTests(SimpleTestCase):
    def _item(self, equipment):
        return _register_stock_entry_item(
            {"articleCode": "1115006", **equipment},
            partida="S12BEA05054",
            deposit="STR",
            issue_date="2026-09-11",
            number="00029566",
        )

    def test_la_partida_nueva_lleva_el_mg_del_equipo(self):
        self.assertEqual(self._item({"internalNumber": "MG 2245"})["Partida_Observaciones"], "MG 2245")

    def test_un_equipo_de_cliente_va_sin_observacion(self):
        item = self._item({"internalNumber": ""})
        self.assertEqual(item["Partida_Observaciones"], "")
        # La trazabilidad de NEXORA sigue en el IdOrigen, que es donde Bejerman la usa.
        self.assertEqual(item["Comprobante_IdOrigen"], "NEXORA-ING-00029566")

    def test_un_mg_vendido_que_vuelve_al_taller_va_sin_observacion(self):
        item = self._item({"internalNumber": "MG 7354", "mgEstado": "inactivo_venta"})
        self.assertEqual(item["Partida_Observaciones"], "")
