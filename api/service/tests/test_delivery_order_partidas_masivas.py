"""Carga masiva de partidas de una orden de entrega: la decisión fila por fila.

`evaluar_partidas` es pura, así que se prueba sin Postgres ni SDK (`SimpleTestCase`). Tiene que
decir lo mismo que después dice el guardado de la orden (`_validate_delivery_partidas_stock`,
`resolve_rental_item_partidas`): una partida que pasa acá y falla al guardar deja al operador sin
saber cuál corregir entre cien.
"""
from django.test import SimpleTestCase

from service.delivery_order_partidas_masivas import (
    ACCION_CARGAR,
    ACCION_YA_CARGADA,
    evaluar_partidas,
    leer_cargadas,
    leer_filas,
)


def _stock(partida, article_code="1105001", deposit="SAR", available=1, real=None):
    return {
        "articleCode": article_code,
        "depositCode": deposit,
        "partida": partida,
        "availableQuantity": available,
        "realQuantity": real,
    }


def _filas(*partidas):
    return [{"fila": index, "partida": partida} for index, partida in enumerate(partidas, start=1)]


class EvaluarPartidasTests(SimpleTestCase):
    def _evaluar(self, filas, stock_rows, **cambios):
        return evaluar_partidas(
            filas,
            article_code=cambios.pop("article_code", "1105001"),
            deposit_code=cambios.pop("deposit_code", "SAR"),
            stock_rows=stock_rows,
            checked_at="2026-09-11T10:00:00",
            **cambios,
        )

    def test_partida_con_stock_del_articulo_se_carga_con_su_deposito(self):
        resultado = self._evaluar(_filas("E238D501001"), [_stock("E238D501001")])
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["accion"], ACCION_CARGAR)
        self.assertEqual(fila["lote"]["partida"], "E238D501001")
        self.assertEqual(fila["lote"]["stockDepositCode"], "SAR")
        self.assertEqual(fila["lote"]["stockAvailableQuantity"], 1)
        self.assertEqual(fila["lote"]["stockCheckedAt"], "2026-09-11T10:00:00")
        self.assertEqual(resultado["resumen"], {
            "total": 1, "ok": 1, "con_error": 0, "cargar": 1, "ya_cargadas": 0, "con_aviso": 0,
        })

    def test_sin_stock_en_el_deposito_es_error(self):
        resultado = self._evaluar(_filas("E238D509999"), [_stock("E238D501001")])
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertEqual(fila["errores"], ["No tiene stock en SAR."])
        self.assertIsNone(fila["lote"])

    def test_stock_cero_o_de_otro_deposito_no_cuenta(self):
        resultado = self._evaluar(
            _filas("A1", "A2"),
            [_stock("A1", available=0, real=1), _stock("A2", deposit="VAL")],
        )
        self.assertEqual([f["estado"] for f in resultado["filas"]], ["error", "error"])

    def test_sin_disponible_informado_usa_el_stock_real_como_el_listado(self):
        resultado = self._evaluar(_filas("A1"), [_stock("A1", available=None, real=1)])
        self.assertEqual(resultado["filas"][0]["estado"], "ok")

    # El CPAP y su humidificador comparten la serie: que la partida exista no alcanza.
    def test_partida_de_otro_articulo_es_error_y_dice_cual(self):
        resultado = self._evaluar(
            _filas("22151772959"),
            [_stock("22151772959", article_code="1111008")],
            article_code="1103020",
            descripciones=lambda codes: {"1111008": "Calentador humidificador RESMED S-9"},
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertEqual(
            fila["errores"],
            ["En SAR la partida tiene stock en el artículo 1111008 (Calentador humidificador RESMED S-9), no en el 1103020."],
        )

    def test_partida_compartida_toma_la_del_articulo_del_renglon(self):
        resultado = self._evaluar(
            _filas("22151772959"),
            [_stock("22151772959", article_code="1111008"), _stock("22151772959", article_code="1103020")],
            article_code="1103020",
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["lote"]["articleCode"], "1103020")

    def test_las_descripciones_solo_se_piden_si_hacen_falta(self):
        def descripciones(codes):
            raise AssertionError("no hacía falta el catálogo")

        resultado = self._evaluar(_filas("A1"), [_stock("A1")], descripciones=descripciones)
        self.assertEqual(resultado["filas"][0]["estado"], "ok")

    def test_espacios_y_guiones_se_resuelven_a_como_figura_en_bejerman(self):
        resultado = self._evaluar(_filas("e238-d501 001"), [_stock("E238D501001")])
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["lote"]["partida"], "E238D501001")
        self.assertEqual(fila["avisos"], ["Se carga como E238D501001, que es como figura en Bejerman."])

    def test_solo_mayusculas_distintas_no_avisa(self):
        resultado = self._evaluar(_filas("e238d501001"), [_stock("E238D501001")])
        self.assertEqual(resultado["filas"][0]["avisos"], [])

    def test_coincidencia_normalizada_ambigua_no_elige(self):
        # Dos partidas distintas en Bejerman que sin el guion quedan iguales.
        resultado = self._evaluar(_filas("AB 12"), [_stock("AB-12"), _stock("AB12")])
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertIn("más de una partida", fila["errores"][0])

    def test_coincidencia_exacta_gana_sobre_la_normalizada(self):
        resultado = self._evaluar(_filas("AB-12"), [_stock("AB-12"), _stock("AB12")])
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["lote"]["partida"], "AB-12")

    def test_repetida_en_la_lista_deja_la_primera_y_marca_las_demas(self):
        resultado = self._evaluar(_filas("A1", "A2", "a1"), [_stock("A1"), _stock("A2")])
        estados = [(f["fila"], f["estado"]) for f in resultado["filas"]]
        self.assertEqual(estados, [(1, "ok"), (2, "ok"), (3, "error")])
        self.assertEqual(resultado["filas"][2]["errores"], ["Repetida: ya está en la fila 1."])

    def test_ya_cargada_en_el_renglon_no_se_repite_ni_es_error(self):
        resultado = self._evaluar(
            _filas("A1", "A2"),
            [_stock("A1"), _stock("A2")],
            cargadas=[{"partida": "A1", "renglon": 1, "actual": True}],
        )
        primera, segunda = resultado["filas"]
        self.assertEqual((primera["estado"], primera["accion"]), ("ok", ACCION_YA_CARGADA))
        self.assertEqual(segunda["accion"], ACCION_CARGAR)
        self.assertEqual(resultado["resumen"]["cargar"], 1)
        self.assertEqual(resultado["resumen"]["ya_cargadas"], 1)

    def test_cargada_en_otro_renglon_es_error(self):
        resultado = self._evaluar(
            _filas("A1"),
            [_stock("A1")],
            cargadas=[{"partida": "A1", "renglon": 2, "actual": False}],
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertEqual(fila["errores"], ["Ya está cargada en el renglón 2."])

    def test_notacion_cientifica_es_error_sin_buscar_stock(self):
        resultado = self._evaluar(_filas("2,1E+11"), [])
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertIn("notación científica", fila["errores"][0])

    def test_stock_fraccionario_no_alcanza_para_una_unidad(self):
        resultado = self._evaluar(_filas("L1"), [_stock("L1", available=0.5)])
        self.assertEqual(resultado["filas"][0]["errores"], ["Tiene stock 0,5 en SAR: no alcanza para una unidad."])

    def test_mg_se_traduce_a_la_serie_del_equipo(self):
        resultado = self._evaluar(
            _filas("MG 1234"),
            [_stock("E21BE708153", article_code="1104011", deposit="STL")],
            article_code="1104011",
            deposit_code="STL",
            series_por_numero_interno={"MG 1234": ["E21BE708153"]},
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["partida"], "MG 1234")
        self.assertEqual(fila["lote"]["partida"], "E21BE708153")
        self.assertEqual(fila["avisos"], ["MG 1234 es el equipo de serie E21BE708153."])

    def test_mg_de_varios_equipos_no_adivina(self):
        resultado = self._evaluar(
            _filas("MG 1234"),
            [_stock("S1"), _stock("S2")],
            series_por_numero_interno={"MG 1234": ["S1", "S2"]},
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertIn("más de un equipo", fila["errores"][0])

    def test_alquiler_rechaza_el_ns_que_no_se_puede_alquilar(self):
        consultadas = []

        def disponibilidad(series):
            consultadas.extend(series)
            return [
                {"available": False, "code": "RENTAL_EQUIPMENT_RESERVED", "reason": "NS ya reservado en la orden OE-00123: A1"},
                {"available": True, "code": None, "reason": None, "equipmentInternalNumber": "MG 9525"},
            ]

        resultado = self._evaluar(
            _filas("A1", "A2", "A9"),
            [_stock("A1", deposit="STL"), _stock("A2", deposit="STL")],
            deposit_code="STL",
            disponibilidad_alquiler=disponibilidad,
        )
        self.assertEqual(consultadas, ["A1", "A2"])  # la que no tiene stock no se consulta
        primera, segunda, tercera = resultado["filas"]
        self.assertEqual(primera["errores"], ["NS ya reservado en la orden OE-00123: A1"])
        self.assertEqual(segunda["estado"], "ok")
        self.assertEqual(segunda["lote"]["equipmentInternalNumber"], "MG 9525")
        self.assertEqual(tercera["errores"], ["No tiene stock en STL."])


class LecturaDelPedidoTests(SimpleTestCase):
    def test_leer_filas_conserva_el_renglon_y_limpia_espacios_duros(self):
        self.assertEqual(
            leer_filas([{"fila": 4, "partida": " E1 "}, {"partida": "E2"}, "E3"]),
            [{"fila": 4, "partida": "E1"}, {"fila": 2, "partida": "E2"}, {"fila": 3, "partida": "E3"}],
        )

    def test_leer_cargadas_descarta_las_vacias(self):
        self.assertEqual(
            leer_cargadas([{"partida": "A1", "renglon": "2", "actual": 1}, {"partida": " "}, "x"]),
            [{"partida": "A1", "renglon": 2, "actual": True}],
        )
