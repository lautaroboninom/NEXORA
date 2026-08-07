from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase, override_settings

from service.pdf import _exit_order_date_context, _exit_order_observations


class ExitOrderPdfContentTests(SimpleTestCase):
    @override_settings(TIME_ZONE="America/Argentina/Buenos_Aires", USE_TZ=True)
    def test_vendido_entregado_usa_fecha_historica_de_venta(self):
        label, value = _exit_order_date_context(
            {
                "ingreso_estado": "vendido_entregado",
                "fecha_venta": datetime(2026, 7, 21, 15, 30, tzinfo=dt_timezone.utc),
                "mg_venta_fecha": datetime(2026, 7, 22, tzinfo=dt_timezone.utc),
            }
        )

        self.assertEqual(label, "Fecha venta")
        self.assertEqual(value, "21/07/26")

    def test_comprobante_distingue_problema_de_trabajos_realizados(self):
        observations = _exit_order_observations(
            {
                "descripcion_problema": "El equipo no concentraba correctamente.",
                "trabajos_realizados": "Se cambió el kit y se verificó la concentración.",
            }
        )

        self.assertIn("Descripción del problema:\nEl equipo no concentraba correctamente.", observations)
        self.assertIn("Trabajos realizados:\nSe cambió el kit", observations)
        self.assertNotIn("Trabajos a realizar", observations)
