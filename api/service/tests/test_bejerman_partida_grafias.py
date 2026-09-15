"""La partida se busca con todas sus grafías: el SDK distingue mayúsculas y SQL Server no.

Caso real: MG 4771 (OS 29707). El ENT a STL con `g04850336` sumó sobre la fila
`G04850336` de 2024; la verificación preguntó en minúsculas y nunca vio el stock.
"""

import json

from django.test import SimpleTestCase

from service.bejerman_partida_grafias import (
    grafias_de_partida,
    stock_partida_sin_distinguir_mayusculas,
)


def _fila(partida, deposito, cantidad):
    return {
        "Art_CodDeposito": deposito,
        "Art_Partida": partida,
        "Art_CodGen": "1109020",
        "Art_RealUM1": cantidad,
    }


class FakeSdk:
    """Imita a `ObtenerStockPartida`: sólo devuelve las filas con la grafía exacta."""

    def __init__(self, filas):
        self.filas = filas
        self.consultas = []

    def __call__(self, partida):
        self.consultas.append(partida)
        propias = [fila for fila in self.filas if fila["Art_Partida"] == partida]
        return {"Resultado": "OK", "EsLista": "true", "DatosJSON": json.dumps(propias)}


def _filas(respuesta):
    return json.loads(respuesta["DatosJSON"])


class GrafiasDePartidaTests(SimpleTestCase):
    def test_serie_en_minusculas_prueba_tambien_mayusculas(self):
        self.assertEqual(grafias_de_partida("g04850336"), ["g04850336", "G04850336"])

    def test_serie_en_mayusculas_prueba_tambien_minusculas(self):
        self.assertEqual(grafias_de_partida("T9022575"), ["T9022575", "t9022575"])

    def test_serie_mezclada_prueba_las_tres(self):
        self.assertEqual(grafias_de_partida("AbC1"), ["AbC1", "ABC1", "abc1"])

    def test_serie_sin_letras_es_una_sola(self):
        self.assertEqual(grafias_de_partida("18433452"), ["18433452"])


class StockPartidaSinDistinguirMayusculasTests(SimpleTestCase):
    def test_junta_la_fila_de_cada_deposito_aunque_tenga_otra_grafia(self):
        sdk = FakeSdk([_fila("g04850336", "STR", 0), _fila("G04850336", "STL", 1)])

        respuesta = stock_partida_sin_distinguir_mayusculas(sdk, "g04850336")

        self.assertEqual(
            sorted((fila["Art_CodDeposito"], fila["Art_RealUM1"]) for fila in _filas(respuesta)),
            [("STL", 1), ("STR", 0)],
        )
        self.assertEqual(respuesta["Resultado"], "OK")

    def test_la_primera_consulta_es_la_de_siempre(self):
        sdk = FakeSdk([])

        stock_partida_sin_distinguir_mayusculas(sdk, "g04850336")

        self.assertEqual(sdk.consultas, ["g04850336", "G04850336"])

    def test_serie_sin_letras_hace_una_sola_consulta(self):
        sdk = FakeSdk([_fila("573", "STR", 1)])

        respuesta = stock_partida_sin_distinguir_mayusculas(sdk, "573")

        self.assertEqual(sdk.consultas, ["573"])
        self.assertEqual(len(_filas(respuesta)), 1)

    def test_no_repite_filas_identicas(self):
        fila = _fila("X1", "STL", 1)
        respuestas = {"x1": {"DatosJSON": json.dumps([fila])}, "X1": {"DatosJSON": json.dumps([fila])}}

        respuesta = stock_partida_sin_distinguir_mayusculas(respuestas.__getitem__, "x1")

        self.assertEqual(_filas(respuesta), [fila])

    def test_acepta_datos_ya_parseados(self):
        respuestas = {"x1": {"DatosJSON": []}, "X1": {"DatosJSON": [_fila("X1", "STL", 1)]}}

        respuesta = stock_partida_sin_distinguir_mayusculas(respuestas.__getitem__, "x1")

        self.assertEqual(len(_filas(respuesta)), 1)

    def test_respuesta_sin_lista_no_inventa_una_fusion(self):
        respuestas = {"x1": {"DatosJSON": "[]", "Resultado": "OK"}, "X1": {"DatosJSON": "<no-json>"}}

        respuesta = stock_partida_sin_distinguir_mayusculas(respuestas.__getitem__, "x1")

        self.assertIs(respuesta, respuestas["x1"])

    def test_un_error_en_otra_grafia_no_se_calla(self):
        def sdk(partida):
            if partida == "X1":
                raise RuntimeError("timeout")
            return {"DatosJSON": "[]"}

        with self.assertRaises(RuntimeError):
            stock_partida_sin_distinguir_mayusculas(sdk, "x1")
