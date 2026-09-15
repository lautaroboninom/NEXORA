"""`MG1234` y `MG 1234` son el mismo equipo.

El número interno se escribe indistintamente pegado, separado, en minúsculas o con ceros a la
izquierda, y hasta ahora se guardaba tal cual: sólo se le agregaba el prefijo si faltaba. Dos
fichas del mismo equipo podían convivir sin que nada avisara, porque las comparaciones exactas
—y el índice `uq_devices_numint_norm`, que espera `XX ####`— las veían distintas.

Normalizar al escribir hace que el valor guardado sea el mismo que compara la base.
"""
from django.test import SimpleTestCase

from service.views.devices_views import _normalize_mg


class NormalizacionDelNumeroInternoTests(SimpleTestCase):
    def test_el_espacio_no_cambia_la_identidad(self):
        self.assertEqual(_normalize_mg("MG1234"), "MG 1234")
        self.assertEqual(_normalize_mg("MG 1234"), "MG 1234")
        self.assertEqual(_normalize_mg("MG   1234"), "MG 1234")

    def test_no_distingue_mayusculas_ni_espacios_de_los_bordes(self):
        self.assertEqual(_normalize_mg("  mg1234 "), "MG 1234")
        self.assertEqual(_normalize_mg("Mg 1234"), "MG 1234")

    def test_sin_prefijo_asume_mg(self):
        self.assertEqual(_normalize_mg("1234"), "MG 1234")

    def test_completa_a_cuatro_digitos(self):
        # Es la misma forma que arma el índice único de la base (LPAD a 4).
        self.assertEqual(_normalize_mg("MG 12"), "MG 0012")
        self.assertEqual(_normalize_mg("MG12"), "MG 0012")

    def test_los_ceros_a_la_izquierda_son_formato_no_identidad(self):
        # Los 13 equipos cargados como `MG 00xxxx` en producción son los mismos que su
        # versión corta; escritos así quedaban fuera de todo lo que compara por 4 dígitos.
        self.assertEqual(_normalize_mg("MG 001678"), "MG 1678")
        self.assertEqual(_normalize_mg("MG001678"), "MG 1678")
        self.assertEqual(_normalize_mg("MG 1678"), "MG 1678")

    def test_los_otros_prefijos_se_normalizan_igual(self):
        self.assertEqual(_normalize_mg("NM0258"), "NM 0258")
        self.assertEqual(_normalize_mg("nm 258"), "NM 0258")
        self.assertEqual(_normalize_mg("NV12"), "NV 0012")
        self.assertEqual(_normalize_mg("CE102"), "CE 0102")

    def test_lo_que_no_es_un_codigo_canonico_se_deja_como_esta(self):
        # Forzarlo sería inventar un número que nadie escribió.
        self.assertEqual(_normalize_mg("MG SM-004"), "MG SM-004")
        self.assertEqual(_normalize_mg("MG"), "MG")

    def test_vacio_sigue_siendo_vacio(self):
        self.assertEqual(_normalize_mg(""), "")
        self.assertEqual(_normalize_mg(None), "")
        self.assertEqual(_normalize_mg("   "), "")

    def test_es_idempotente(self):
        # Se aplica en el alta y en la edición: pasar dos veces no puede cambiar el valor.
        for value in ("MG1234", "MG 001678", "1234", "NM0258", "MG SM-004", ""):
            once = _normalize_mg(value)
            self.assertEqual(_normalize_mg(once), once, value)
