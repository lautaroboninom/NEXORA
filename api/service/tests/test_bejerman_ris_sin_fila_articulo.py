"""RIS de un artículo que nunca tuvo fila de stock en STR.

El importador de ventas de Bejerman no crea esa fila: un RIS que ingresa stock contesta
"No hay stock ingresado para el artículo X" (OS 29470, videolaringoscopio 1120002,
02/07/2026). Lo resuelve el mismo ENT de la recuperación de partida, que crea la fila y
la partida. Verificado en la empresa de prueba el 15/10/2026 (RIS 00004-00004414).
"""
from django.test import SimpleTestCase, override_settings

from service.bejerman_ris import _ris_rejection_is_recoverable
from service.tests.test_bejerman_remito_contracts import BejermanStockSemanticsClient, _payload

SIN_FILA = "El comprobante no se importó. No hay stock ingresado para el artículo 1120002 ."


class SinFilaDeArticuloClient(BejermanStockSemanticsClient):
    """Además de lo que modela el cliente base, exige la fila (artículo, depósito) para un
    renglón que ingresa stock. La fila la crea el circuito de STOCK, como en Bejerman."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.filas = {(articulo, deposito) for (_partida, articulo, deposito) in self.stock}

    def ingresar_lista_comprobantes_stock_json(self, comprobantes, *, numera_flex="N"):
        for item in comprobantes or []:
            self.filas.add(
                (
                    str(item.get("Comprobante_Art_CodGen") or "").strip(),
                    str(item.get("Comprobante_ArtDeposito") or "").strip(),
                )
            )
        return super().ingresar_lista_comprobantes_stock_json(comprobantes, numera_flex=numera_flex)

    def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
        if comprobante.get("Comprobante_ActualizaStock") == "S":
            for item in comprobante.get("Comprobante_Items") or []:
                if item.get("Item_Tipo") != "A" or item.get("Item_CantidadUM1", 0) >= 0:
                    continue
                fila = (
                    str(item.get("Item_CodigoArticulo") or "").strip(),
                    str(item.get("Item_Deposito") or "").strip(),
                )
                if fila not in self.filas:
                    self._rechazar(
                        comprobante,
                        kwargs,
                        f"El comprobante no se importó. No hay stock ingresado para el artículo {fila[0]} .",
                    )
        return super().ingresar_comprobante_ventas_json(comprobante, **kwargs)


@override_settings(
    BEJERMAN_RIS_UPDATE_STOCK=True,
    BEJERMAN_RIS_NUMERA_FLEX="S",
    BEJERMAN_RIS_EMITE_REG="E",
    BEJERMAN_STOCK_ENTRY_COMPROBANTE="ENT",
    BEJERMAN_STOCK_NUMERA_FLEX="N",
    BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=1,
    BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0,
    BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="RD,RDN,RDA,RIS",
)
class RisSinFilaDeArticuloTests(SimpleTestCase):
    def test_ris_de_articulo_sin_fila_en_str_se_recupera_con_ent(self):
        from unittest.mock import patch

        from service.bejerman_ris import _emit_payload_to_bejerman
        from service.tests.test_bejerman_remito_contracts import CUSTOMER_FIELDS

        fake = SinFilaDeArticuloClient(number="00005491")
        with (
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=CUSTOMER_FIELDS),
            patch("service.bejerman_ris._ensure_equipment_remito_not_duplicated"),
        ):
            request_payload, response = _emit_payload_to_bejerman(_payload(serial="SN-RIS-SIN-FILA"))

        self.assertEqual(response["remitoNumber"], "RIS R 00004-00005491")
        self.assertEqual(fake.stock[("SN-RIS-SIN-FILA", "NXTEST-PART", "STR")], 1)
        movimientos = [
            (item["Comprobante_Tipo"], item["Comprobante_CantidadUM1"])
            for lote in fake.stock_comprobantes
            for item in lote["comprobantes"]
        ]
        self.assertEqual(movimientos, [("ENT", 1), ("SAL", -1)])
        self.assertEqual(len(fake.comprobantes), 2)
        self.assertTrue(request_payload["partidaNeutralized"])

    def test_solo_el_ris_que_ingresa_recupera_la_falta_de_fila(self):
        self.assertTrue(_ris_rejection_is_recoverable(SIN_FILA, {"Comprobante_Tipo": "RIS"}))
        self.assertTrue(
            _ris_rejection_is_recoverable("Partida inexistente y/o no informada", {"Comprobante_Tipo": "RIS"})
        )
        # Falta de stock real: nunca se recupera.
        self.assertFalse(
            _ris_rejection_is_recoverable(
                "El comprobante no se importó. No hay stock suficiente en la partida X para el artículo Y .",
                {"Comprobante_Tipo": "RIS"},
            )
        )
        # Las devoluciones quedan como estaban: el caso nuevo es sólo del RIS.
        self.assertFalse(_ris_rejection_is_recoverable(SIN_FILA, {"Comprobante_Tipo": "RDA"}))

    @override_settings(BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="RD,RDN,RDA")
    def test_un_ris_en_positivo_no_recupera_la_falta_de_fila(self):
        # En positivo el RIS sería una salida y ese texto, falta de stock real.
        self.assertFalse(_ris_rejection_is_recoverable(SIN_FILA, {"Comprobante_Tipo": "RIS"}))
