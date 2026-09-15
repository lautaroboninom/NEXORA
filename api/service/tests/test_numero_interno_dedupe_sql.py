"""La dedupe de número interno corre en Postgres: hay que probarla contra Postgres.

`MG1234`, `mg 1234` y `MG 001234` son el mismo equipo. La comparación vive en SQL, así que
verificar sólo el normalizador de Python dejaría afuera justo lo que falla: la expresión que
compara contra las filas ya guardadas, que pueden estar en cualquiera de esas formas.

Dos bugs concretos que cubre este módulo, los dos en la expresión anterior:

* el `REGEXP_REPLACE` corría **sin `UPPER` previo**, así que una fila guardada en minúsculas
  no normalizaba y esquivaba el control de duplicados;
* el `LPAD('\\2',4,'0')` padeaba la cadena literal «\\2», no los dígitos capturados.
"""
from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from service.views.devices_views import MG_KEY_PATTERN, MG_KEY_SQL, _normalize_mg


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class ClaveDeNumeroInternoEnSqlTests(TestCase):
    def _clave(self, value):
        with connection.cursor() as cur:
            cur.execute(f"SELECT {MG_KEY_SQL.format(col='%s')}", [value])
            return cur.fetchone()[0]

    def _entra_al_filtro(self, value):
        with connection.cursor() as cur:
            cur.execute(f"SELECT %s ~* '{MG_KEY_PATTERN}'", [value])
            return cur.fetchone()[0]

    def test_las_variantes_de_escritura_dan_la_misma_clave(self):
        esperada = self._clave("MG 1234")
        for variante in ("MG1234", "mg1234", "mg 1234", "MG  1234", "MG 001234"):
            self.assertEqual(self._clave(variante), esperada, variante)

    def test_las_minusculas_normalizan(self):
        # Sin el UPPER previo, `mg1234` no matcheaba el patrón del reemplazo y volvía tal
        # cual: la fila quedaba fuera del control de duplicados.
        self.assertEqual(self._clave("mg1234"), "MG 1234")

    def test_los_ceros_a_la_izquierda_no_cambian_la_clave(self):
        self.assertEqual(self._clave("MG 0012"), self._clave("MG 12"))
        self.assertEqual(self._clave("MG 001678"), self._clave("MG 1678"))

    def test_equipos_distintos_no_colapsan(self):
        self.assertNotEqual(self._clave("MG 1234"), self._clave("MG 1235"))
        self.assertNotEqual(self._clave("MG 1234"), self._clave("NM 1234"))

    def test_lo_que_no_es_codigo_canonico_no_entra_al_filtro(self):
        self.assertFalse(self._entra_al_filtro("MG SM-004"))
        self.assertFalse(self._entra_al_filtro("MZJ5S121244"))
        self.assertTrue(self._entra_al_filtro("MG1234"))
        self.assertTrue(self._entra_al_filtro("mg 001234"))

    def test_la_clave_sql_coincide_con_el_normalizador_de_python(self):
        """Las dos normalizaciones tienen que estar de acuerdo sobre qué es el mismo equipo.

        No producen el mismo texto —Python guarda `MG 0012` y la clave SQL usa `MG 12`—, pero
        lo que importa es que agrupen igual: si discreparan, el alta guardaría un valor que la
        comparación no reconoce como duplicado.
        """
        grupos = [
            ("MG1234", "MG 1234", "mg1234", "MG 001234"),
            ("MG12", "MG 0012", "mg 12"),
            ("NM0258", "nm 258"),
        ]
        for variantes in grupos:
            claves_sql = {self._clave(v) for v in variantes}
            claves_py = {_normalize_mg(v) for v in variantes}
            self.assertEqual(len(claves_sql), 1, variantes)
            self.assertEqual(len(claves_py), 1, variantes)
        # Y grupos distintos siguen separados en las dos.
        self.assertNotEqual(self._clave("MG 12"), self._clave("MG 1234"))
        self.assertNotEqual(_normalize_mg("MG 12"), _normalize_mg("MG 1234"))
