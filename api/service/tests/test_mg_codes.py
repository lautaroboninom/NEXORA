"""El criterio único de número interno (`service/mg_codes.py`).

Sin base ni Django: el módulo no depende de nada, así que esto corre con `python -m unittest`.
La parte SQL (`sql_mg_key`) la cubre `test_numero_interno_dedupe_sql` contra Postgres.
"""
import unittest

from service.mg_codes import (
    MG_CODE_PATTERN,
    NORMALIZE_NUMERO_INTERNO_FUNCTION_SQL,
    is_mg_code,
    normalize_mg_code,
    normalize_mg_input,
    sql_is_mg_code,
)


class CriterioUnicoDeNumeroInternoTests(unittest.TestCase):
    def test_con_o_sin_espacio_es_el_mismo_codigo(self):
        for valor in ("MG1678", "MG 1678", "MG  1678", "mg 1678", "Mg1678", " MG 1678 "):
            with self.subTest(valor=valor):
                self.assertTrue(is_mg_code(valor))
                self.assertEqual(normalize_mg_code(valor), "MG 1678")

    def test_los_ceros_a_la_izquierda_no_lo_dejan_afuera(self):
        # Los 13 equipos de producción cargados así no se reconocían como propios con el
        # tope de 4 dígitos.
        for valor in ("MG 001678", "MG001678", "mg 0001678"):
            with self.subTest(valor=valor):
                self.assertTrue(is_mg_code(valor))
                self.assertEqual(normalize_mg_code(valor), "MG 1678")

    def test_numeros_cortos_se_rellenan_a_cuatro(self):
        self.assertEqual(normalize_mg_code("MG 12"), "MG 0012")
        self.assertEqual(normalize_mg_code("MG 0012"), "MG 0012")
        self.assertEqual(normalize_mg_code("nm 258"), "NM 0258")
        self.assertEqual(normalize_mg_code("CE102"), "CE 0102")

    def test_los_cuatro_prefijos(self):
        for prefijo in ("MG", "NM", "NV", "CE"):
            with self.subTest(prefijo=prefijo):
                self.assertTrue(is_mg_code(f"{prefijo} 1234"))

    def test_lo_que_no_es_codigo_queda_afuera(self):
        for valor in ("", None, "MG", "MG SM-004", "MZJ5S121244", "1234", "MG 1234567", "XX 1234", "MG-1234"):
            with self.subTest(valor=valor):
                self.assertFalse(is_mg_code(valor))
                self.assertIsNone(normalize_mg_code(valor))

    def test_normalizar_dos_veces_no_cambia_nada(self):
        for valor in ("MG1678", "MG 001678", "nm 258", "MG 12"):
            with self.subTest(valor=valor):
                una = normalize_mg_code(valor)
                self.assertEqual(normalize_mg_code(una), una)

    def test_el_sql_usa_el_mismo_patron(self):
        condicion = sql_is_mg_code("d.numero_interno")
        self.assertIn(MG_CODE_PATTERN, condicion)
        self.assertIn("COALESCE(d.numero_interno,'')", condicion)
        # Un f-string con `{1,6}` sin escapar manda `(1, 6)` a Postgres: el bug que tenía la
        # lista de pendientes de presupuesto.
        self.assertNotIn("(1, 6)", condicion)


class NumeroInternoSeGuardaNormalizadoTests(unittest.TestCase):
    """Regla del usuario 14/10/2026: si escriben "mg", NEXORA lo guarda "MG".

    El caso real: la OS 29098 entró con "mg 7359", no encontró la ficha "MG 7359" (N/S 3676) y
    creó otra sin número de serie.
    """

    def test_minusculas_se_guardan_en_mayusculas(self):
        for valor in ("mg 7359", "Mg 7359", "mG7359", "MG7359", "MG  7359", " mg 7359 ", "MG 007359"):
            with self.subTest(valor=valor):
                self.assertEqual(normalize_mg_input(valor), "MG 7359")

    def test_los_cuatro_prefijos_van_en_mayusculas(self):
        self.assertEqual(normalize_mg_input("nm 31"), "NM 0031")
        self.assertEqual(normalize_mg_input("nv12"), "NV 0012")
        self.assertEqual(normalize_mg_input("ce 102"), "CE 0102")

    def test_un_numero_suelto_es_un_mg(self):
        self.assertEqual(normalize_mg_input("7359"), "MG 7359")
        self.assertEqual(normalize_mg_input("42"), "MG 0042")

    def test_espacio_duro_pegado_desde_excel(self):
        # La ficha 28620 tenía "MG<NBSP>3089" y no aparecía en ninguna búsqueda por MG.
        self.assertEqual(normalize_mg_input("MG 3089"), "MG 3089")
        self.assertEqual(normalize_mg_input("mg 3089 "), "MG 3089")

    def test_lo_que_no_es_codigo_numerico_solo_sube_el_prefijo(self):
        self.assertEqual(normalize_mg_input("mg sm-004"), "MG sm-004")
        self.assertEqual(normalize_mg_input("MG SM-004"), "MG SM-004")
        self.assertEqual(normalize_mg_input("mg"), "MG")
        self.assertEqual(normalize_mg_input("MG-1234"), "MG-1234")

    def test_vacio_da_vacio(self):
        for valor in ("", "   ", None):
            with self.subTest(valor=valor):
                self.assertEqual(normalize_mg_input(valor), "")

    def test_normalizar_dos_veces_no_cambia_nada(self):
        for valor in ("mg 7359", "7359", "mg sm-004", "MG 3089", "nm 31"):
            with self.subTest(valor=valor):
                una = normalize_mg_input(valor)
                self.assertEqual(normalize_mg_input(una), una)

    def test_la_funcion_sql_usa_el_mismo_patron(self):
        self.assertIn(f"'{MG_CODE_PATTERN}'", NORMALIZE_NUMERO_INTERNO_FUNCTION_SQL)
        self.assertNotIn("__MG_CODE_PATTERN__", NORMALIZE_NUMERO_INTERNO_FUNCTION_SQL)
        self.assertNotIn("(1, 6)", NORMALIZE_NUMERO_INTERNO_FUNCTION_SQL)


if __name__ == "__main__":
    unittest.main()
