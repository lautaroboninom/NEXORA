import json
from unittest.mock import patch

from django.test import SimpleTestCase

from service.bejerman_ris import BejermanRisError, force_stock_entry_for_ingreso


class FakeForceStockClient:
    """Simula Bejerman devolviendo una partida "fantasma" (existe pero con
    cantidad 0) en la primera consulta y stock positivo recién después de que
    se reenvía el ENT -- el escenario real que dispara `force_stock_entry_for_ingreso`."""

    def __init__(self, *, stock_sequence, comprobante_response=None):
        self.stock_sequence = list(stock_sequence)
        self.stock_calls = []
        self.sent_entries = []
        self.comprobante_response = comprobante_response or {"Resultado": "OK"}

    def obtener_stock_partida(self, partida):
        self.stock_calls.append(partida)
        index = min(len(self.stock_calls) - 1, len(self.stock_sequence) - 1)
        records = self.stock_sequence[index]
        return {"Resultado": "OK", "DatosJSON": json.dumps(records)}

    def ingresar_lista_comprobantes_stock_json(self, entries, numera_flex="N"):
        self.sent_entries.append(entries)
        return self.comprobante_response


class ForceStockEntryForIngresoTests(SimpleTestCase):
    PARTIDA = "S12BEA05054"
    DEPOSIT = "STR"

    def _ghost_record(self, real=0, comp=0, disp=0):
        return {
            "Art_CodDeposito": self.DEPOSIT,
            "Art_Partida": self.PARTIDA,
            "Art_CodGen": "1115006",
            "Art_RealUM1": real,
            "Art_CompUM1": comp,
            "Art_DispUM1": disp,
        }

    def _row(self):
        payload = {
            "equipment": {
                "serial": self.PARTIDA,
                "articleCode": "1115006",
                "ingresoId": 29566,
            },
            "documentProfile": {"deposit": self.DEPOSIT},
        }
        return {
            "ingreso_id": 29566,
            "remito_number": "R 00004-00004902",
            "status": "generated",
            "request_payload": json.dumps(payload),
            "company_key": "SEPID",
        }

    def _force(self, client):
        with (
            patch("service.bejerman_ris._row_for_ingreso", return_value=self._row()),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=client),
        ):
            return force_stock_entry_for_ingreso(29566, user_id=1152)

    def test_reenvia_el_ent_cuando_la_partida_existe_con_cantidad_cero(self):
        # Antes del fix, `_stock_partida_present` (cualquier cantidad) confundía
        # esta fila fantasma con stock real y devolvía "already_positive" sin
        # llamar a Bejerman -- el botón "Forzar ingreso" no hacía nada.
        client = FakeForceStockClient(
            stock_sequence=[[self._ghost_record()], [self._ghost_record(real=1, comp=0, disp=1)]]
        )

        result = self._force(client)

        self.assertEqual(result["forced"], True)
        self.assertEqual(result["partida"], self.PARTIDA)
        self.assertEqual(result["deposit"], self.DEPOSIT)
        self.assertEqual(len(client.sent_entries), 1)
        entry = client.sent_entries[0][0]
        self.assertEqual(entry["Comprobante_ArtPartida"], self.PARTIDA)
        self.assertEqual(entry["Comprobante_ArtDeposito"], self.DEPOSIT)

    def test_no_reenvia_si_ya_hay_stock_positivo(self):
        client = FakeForceStockClient(stock_sequence=[[self._ghost_record(real=3, comp=0, disp=3)]])

        result = self._force(client)

        self.assertEqual(result, {"forced": False, "reason": "already_positive", "deposit": self.DEPOSIT, "partida": self.PARTIDA})
        self.assertEqual(client.sent_entries, [])

    def test_falla_fuerte_si_bejerman_vuelve_a_dejar_la_partida_en_cero(self):
        with self.settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=1, BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0):
            client = FakeForceStockClient(stock_sequence=[[self._ghost_record()], [self._ghost_record()]])
            with self.assertRaises(BejermanRisError):
                self._force(client)
