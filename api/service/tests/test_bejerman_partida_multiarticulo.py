"""Una partida con dos artículos en el mismo depósito no es un error de datos.

El CPAP y su humidificador comparten el número de serie (ver la identidad del
equipo: N/S + tipo), así que Bejerman informa stock de la misma partida para dos
códigos de artículo distintos en STR. Antes eso bloqueaba el movimiento; ahora
desempata el artículo que el equipo ya tiene resuelto.
"""

from django.test import SimpleTestCase

from service.bejerman_sync import (
    BejermanBlockedError,
    _article_code_for_partida_deposit,
    _records_for_equipment_article,
)

SERIAL = "Y662124170064"
CPAP = "1103015"
HUMIDIFICADOR = "1111009"


def _record(article_code: str, *, deposit: str = "STR") -> dict:
    return {
        "Art_CodGen": article_code,
        "Art_Partida": SERIAL,
        "Art_CodDeposito": deposit,
        "Art_RealUM1": 1,
    }


class PartidaConVariosArticulosTests(SimpleTestCase):
    def test_elige_el_articulo_del_equipo(self):
        code = _article_code_for_partida_deposit(
            [_record(CPAP), _record(HUMIDIFICADOR)],
            serial=SERIAL,
            deposit="STR",
            expected_article_codes=[HUMIDIFICADOR],
        )
        self.assertEqual(code, HUMIDIFICADOR)

    def test_elige_el_cpap_cuando_el_equipo_es_el_cpap(self):
        code = _article_code_for_partida_deposit(
            [_record(HUMIDIFICADOR), _record(CPAP)],
            serial=SERIAL,
            deposit="STR",
            expected_article_codes=[CPAP],
        )
        self.assertEqual(code, CPAP)

    def test_respeta_la_prioridad_del_articulo_confirmado(self):
        code = _article_code_for_partida_deposit(
            [_record(CPAP), _record(HUMIDIFICADOR)],
            serial=SERIAL,
            deposit="STR",
            expected_article_codes=[CPAP, HUMIDIFICADOR],
        )
        self.assertEqual(code, CPAP)

    def test_ignora_los_codigos_esperados_vacios(self):
        code = _article_code_for_partida_deposit(
            [_record(CPAP), _record(HUMIDIFICADOR)],
            serial=SERIAL,
            deposit="STR",
            expected_article_codes=["", None, f" {HUMIDIFICADOR} "],
        )
        self.assertEqual(code, HUMIDIFICADOR)

    def test_bloquea_si_el_equipo_no_tiene_articulo(self):
        with self.assertRaises(BejermanBlockedError) as ctx:
            _article_code_for_partida_deposit(
                [_record(CPAP), _record(HUMIDIFICADOR)],
                serial=SERIAL,
                deposit="STR",
            )
        self.assertIn("no tiene artículo confirmado", str(ctx.exception))

    def test_bloquea_si_el_articulo_del_equipo_no_tiene_stock(self):
        with self.assertRaises(BejermanBlockedError) as ctx:
            _article_code_for_partida_deposit(
                [_record(CPAP), _record(HUMIDIFICADOR)],
                serial=SERIAL,
                deposit="STR",
                expected_article_codes=["1115001"],
            )
        self.assertIn("ninguno es el artículo del equipo", str(ctx.exception))

    def test_un_solo_articulo_sigue_resolviendo_sin_esperado(self):
        code = _article_code_for_partida_deposit(
            [_record(CPAP)],
            serial=SERIAL,
            deposit="STR",
        )
        self.assertEqual(code, CPAP)


class IdempotenciaPorArticuloTests(SimpleTestCase):
    """El destino se mira por artículo: que la partida ya esté ahí no alcanza.

    El caso real: el humidificador de la OS 29645 se movió a STC y dejó al CPAP
    de la OS 29644 —misma serie, otro artículo— marcado como movido sin moverlo.
    """

    def test_la_partida_del_gemelo_en_destino_no_cuenta(self):
        destino = [_record(HUMIDIFICADOR, deposit="STC")]
        self.assertEqual(_records_for_equipment_article(destino, CPAP), [])

    def test_el_propio_articulo_en_destino_si_cuenta(self):
        destino = [_record(HUMIDIFICADOR, deposit="STC"), _record(CPAP, deposit="STC")]
        quedan = _records_for_equipment_article(destino, CPAP)
        self.assertEqual([_r["Art_CodGen"] for _r in quedan], [CPAP])

    def test_sin_articulo_conocido_no_filtra_nada(self):
        destino = [_record(HUMIDIFICADOR, deposit="STC")]
        self.assertEqual(_records_for_equipment_article(destino, ""), destino)

    def test_compara_sin_distinguir_espacios(self):
        destino = [_record(CPAP, deposit="STC")]
        self.assertEqual(len(_records_for_equipment_article(destino, f" {CPAP} ")), 1)
