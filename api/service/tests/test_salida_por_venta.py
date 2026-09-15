"""Salida por venta: la OS cierra como vendida cuando el comprobante es un RT.

Caso real, OS 29629: un concentrador que volvió por devolución de demo se revisó, quedó
"controlado sin defecto" y después se vendió. Salió con `RT R 00001-00024051` en vez del
RSS de servicio técnico, y NEXORA no tenía forma de cerrarla: quedaba abierta en el taller.

Se prueba acá la regla que decide eso —el tipo del comprobante— con `q` mockeado, porque
lo que importa es la decisión y no la lectura de la base (esa la cubre `test_bejerman_sync`
para el resto de los caminos de entrega).
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from service.bejerman_remito_lookup import REMITO_TYPES_BY_FLOW
from service.views.ingresos_views import is_sale_exit_remito, resolve_ingreso_entrega_estado


def _ingreso(**overrides):
    base = {
        "alquilado": False,
        "estado": "controlado_sin_defecto",
        "resolucion": "",
        "numero_serie": "MA26040480026",
        "numero_interno": "",
        "mg_estado": "activo",
        "ubicacion_nombre": "Taller",
    }
    base.update(overrides)
    return base


class SaleExitRemitoTests(SimpleTestCase):
    def test_reconoce_el_rt_y_no_confunde_a_los_parecidos(self):
        for remito in ("RT R 00001-00024051", "rt r 00001-00024051", "  RT R 00001-1"):
            self.assertTrue(is_sale_exit_remito(remito), remito)
        # RTA (alta de alquiler) y RTN comparten el prefijo pero no son ventas.
        for remito in ("RTA R 00004-00004833", "RTN R 00004-1", "RSS R 00004-00004833", "RDN R 00007-747", "", None):
            self.assertFalse(is_sale_exit_remito(remito), remito)

    def test_el_buscador_por_serie_conoce_el_flujo_de_venta(self):
        self.assertEqual(REMITO_TYPES_BY_FLOW["sale"], ("RT", "RTN"))


class ResolveEntregaEstadoTests(SimpleTestCase):
    def _resolver(self, remito, venta=False, **overrides):
        with patch("service.views.ingresos_views.q", return_value=_ingreso(**overrides)):
            return resolve_ingreso_entrega_estado(29629, remito_salida=remito, venta=venta)

    def test_un_rt_cierra_la_os_como_vendida(self):
        self.assertEqual(
            self._resolver("RT R 00001-00024051"),
            {"estado_to": "vendido_entregado", "set_alquilado": False, "clear_alquilado": True},
        )

    def test_sin_rt_la_entrega_sigue_siendo_una_entrega(self):
        self.assertEqual(self._resolver("RSS R 00004-00004833")["estado_to"], "entregado")

    def test_un_equipo_mg_vendido_no_queda_marcado_como_alquilado(self):
        # Sin el corte por RT, la heurística de MG lo mandaría a 'alquilado' y el equipo
        # figuraría en poder de un cliente que en realidad lo compró.
        resultado = self._resolver("RT R 00001-00024051", numero_interno="MG 4700")
        self.assertEqual(resultado["estado_to"], "vendido_entregado")
        self.assertFalse(resultado["set_alquilado"])
        self.assertTrue(resultado["clear_alquilado"])
        self.assertEqual(self._resolver("RSS R 00004-1", numero_interno="MG 4700")["estado_to"], "alquilado")

    def test_el_cambio_de_equipo_manda_sobre_el_comprobante(self):
        # En un cambio el equipo de la OS es el que trajo el cliente y se queda acá; el RT
        # es del equipo que se lleva, que sale por su propia orden.
        self.assertEqual(self._resolver("RT R 00001-00024051", resolucion="cambio")["estado_to"], "entregado")

    def test_si_falla_la_lectura_la_venta_sigue_siendo_venta(self):
        with patch("service.views.ingresos_views.q", side_effect=RuntimeError("sin base")):
            resultado = resolve_ingreso_entrega_estado(29629, remito_salida="RT R 00001-00024051")
        self.assertEqual(resultado["estado_to"], "vendido_entregado")
        self.assertTrue(resultado["clear_alquilado"])

    def test_un_rtn_solo_no_es_una_venta(self):
        # El RTN también es la entrega en alquiler: sin la marca del operador no cierra vendido.
        self.assertEqual(self._resolver("RTN R 00004-00004033")["estado_to"], "entregado")

    def test_un_rtn_de_aporte_marcado_como_venta_cierra_vendido(self):
        # OS 29131: el MG 6770 salió a NOVAMED con RTN R 00004-00004033, un aporte que no se
        # factura, desde la estantería de alquiler.
        resultado = self._resolver(
            "RTN R 00004-00004033",
            venta=True,
            numero_interno="MG 6770",
            ubicacion_nombre="Estantería de Alquiler",
        )
        self.assertEqual(resultado["estado_to"], "vendido_entregado")
        self.assertFalse(resultado["set_alquilado"])
        self.assertTrue(resultado["clear_alquilado"])

    def test_la_marca_de_venta_acepta_rt_o_rtn_y_nada_mas(self):
        from service.views.ingresos_views import _SALE_MARKED_REMITO_RE

        for remito in ("RT R 00001-00024051", "RTN R 00004-00004033", "  rtn r 00004-1"):
            self.assertTrue(_SALE_MARKED_REMITO_RE.match(remito), remito)
        for remito in ("RTA R 00004-00004833", "RSS R 00004-1", "RDN R 00007-747", ""):
            self.assertFalse(_SALE_MARKED_REMITO_RE.match(remito), remito)
