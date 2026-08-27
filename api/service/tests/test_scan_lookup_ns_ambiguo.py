"""Un mismo número de serie que responde a dos equipos distintos.

Pasa con los accesorios que no traen serie propia: el calentador humidificador se carga con
la del CPAP con el que vino. Desde que `devices` es único por serie **más** tipo, los dos
existen de verdad, y el autocompletado tiene que preguntar en vez de adivinar.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from service.views.scan_views import _device_candidate_summary, _fetch_device_by_code


CPAP = {
    "id": 11,
    "tipo_equipo": "CPAP",
    "marca": "Yamind",
    "modelo": "DM26",
    "variante": "20C",
    "numero_serie": "y662125150133",
    "numero_interno": "",
    "customer_nombre": "GRIFFO DAMIAN EZEQUIEL",
}
HUMIDIFICADOR = {
    "id": 12,
    "tipo_equipo": "Calentador humidificador",
    "marca": "Yamind",
    "modelo": "DM26",
    "variante": "",
    "numero_serie": "y662125150133",
    "numero_interno": "",
    "customer_nombre": "GRIFFO DAMIAN EZEQUIEL",
}


class FetchDeviceByCodeTests(SimpleTestCase):
    def test_devuelve_los_dos_equipos_que_comparten_la_serie(self):
        with patch("service.views.scan_views.q", return_value=[HUMIDIFICADOR, CPAP]) as consulta:
            elegido, ns_key, candidatos = _fetch_device_by_code("y662125150133", "")

        self.assertEqual(ns_key, "Y662125150133")
        self.assertEqual([row["id"] for row in candidatos], [12, 11])
        # Sin elección explícita se propone el primero, pero la lista completa viaja para que
        # quien pueda preguntar, pregunte.
        self.assertEqual(elegido["id"], 12)
        # El LIMIT 1 era justamente lo que hacía invisible el problema.
        self.assertNotIn("LIMIT 1", consulta.call_args[0][0])

    def test_el_equipo_elegido_manda_sobre_el_orden_por_defecto(self):
        with patch("service.views.scan_views.q", return_value=[HUMIDIFICADOR, CPAP]):
            elegido, _, candidatos = _fetch_device_by_code("y662125150133", "", device_id=11)

        self.assertEqual(elegido["id"], 11)
        self.assertEqual(len(candidatos), 2)

    def test_un_device_id_ajeno_al_codigo_no_se_acepta(self):
        """El id viaja por la query string: no puede servir para traer cualquier equipo."""

        with patch("service.views.scan_views.q", return_value=[HUMIDIFICADOR, CPAP]):
            elegido, _, _ = _fetch_device_by_code("y662125150133", "", device_id=999)

        self.assertEqual(elegido["id"], 12)

    def test_un_solo_equipo_sigue_resolviendo_solo(self):
        with patch("service.views.scan_views.q", return_value=[CPAP]):
            elegido, _, candidatos = _fetch_device_by_code("y662125150133", "")

        self.assertEqual(elegido["id"], 11)
        self.assertEqual(len(candidatos), 1)

    def test_sin_resultados_no_rompe(self):
        with patch("service.views.scan_views.q", return_value=[]):
            elegido, ns_key, candidatos = _fetch_device_by_code("SN-INEXISTENTE", "")

        self.assertIsNone(elegido)
        self.assertEqual(ns_key, "SN-INEXISTENTE".replace("-", ""))
        self.assertEqual(candidatos, [])


class CandidateSummaryTests(SimpleTestCase):
    def test_el_resumen_alcanza_para_distinguir_el_cpap_del_humidificador(self):
        resumen = _device_candidate_summary(HUMIDIFICADOR, 12)

        self.assertEqual(resumen["id"], 12)
        self.assertEqual(resumen["tipo_equipo"], "Calentador humidificador")
        self.assertEqual(resumen["modelo"], "DM26")
        self.assertTrue(resumen["selected"])
        self.assertFalse(_device_candidate_summary(CPAP, 12)["selected"])
