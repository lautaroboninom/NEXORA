"""La SAL de corrección de stock tiene que entrar en los límites de Bejerman.

Caso real (OS 29131, 14/09/2026): el SDK rechazó la SAL porque `Comprobante_IdOrigen` tenía 51
caracteres y Bejerman acepta 50. `build_stock_correction_sal_payload` recortaba a 80, así que toda
corrección de una partida de 13 caracteres o más fallaba igual.
"""
from unittest import mock

from django.test import SimpleTestCase

from service import bejerman_stock_corrections as corrections

MAX_BEJERMAN = 50


class IdOrigenDeLaSalDeCorreccionTests(SimpleTestCase):
    def _payload(self, numero_serie, sequence=1):
        with mock.patch.object(corrections, "observacion_partida_de_serie", return_value=""):
            return corrections.build_stock_correction_sal_payload(
                numero_serie=numero_serie,
                articulo="1115003",
                deposito="STR",
                cantidad=1,
                run_id="20260914_101500",
                sequence=sequence,
            )[0]

    def test_un_id_origen_corto_queda_legible(self):
        item = self._payload("MZJ5")
        self.assertEqual(item["Comprobante_IdOrigen"], "NEXORA-STOCKFIX-20260914_101500-MZJ5-STR-1")

    def test_una_partida_larga_no_pasa_los_50_caracteres(self):
        item = self._payload("MA2604048002612")
        self.assertLessEqual(len(item["Comprobante_IdOrigen"]), MAX_BEJERMAN)
        self.assertTrue(item["Comprobante_IdOrigen"].startswith("NEXORA-"))

    def test_dos_correcciones_largas_no_colapsan_en_el_mismo_id_origen(self):
        # Bejerman deduplica por IdOrigen: si colapsaran, la segunda se daría por "ya existe".
        primera = self._payload("MA2604048002612", sequence=1)
        segunda = self._payload("MA2604048002612", sequence=2)
        self.assertNotEqual(primera["Comprobante_IdOrigen"], segunda["Comprobante_IdOrigen"])

    def test_el_mismo_pedido_da_el_mismo_id_origen(self):
        # Estable entre reintentos: es lo que evita emitir dos veces la misma SAL.
        self.assertEqual(
            self._payload("MA2604048002612")["Comprobante_IdOrigen"],
            self._payload("MA2604048002612")["Comprobante_IdOrigen"],
        )
