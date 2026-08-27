from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase, override_settings

from decimal import Decimal

from service.pdf import (
    _exit_order_date_context,
    _exit_order_net_charge,
    _exit_order_observations,
)


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


class ExitOrderNetChargeTests(SimpleTestCase):
    """La caja "Costo neto" es lo que se cobra, no lo que decia el presupuesto."""

    def test_presupuesto_rechazado_cobra_la_revision_aunque_la_resolucion_sea_otra(self):
        # OS 29471: el presupuesto quedo rechazado pero el equipo salio por "No
        # se repara", asi que la resolucion es `no_reparado`. Se cobra la
        # revision, no los $879.046,95 del presupuesto que el cliente rechazo.
        neto = _exit_order_net_charge(
            {
                "resolucion": "no_reparado",
                "quote_estado": "rechazado",
                "subtotal": Decimal("879046.95"),
            },
            {"presupuesto_rechazado_cobro_neto": Decimal("12800.00")},
        )

        self.assertEqual(neto, Decimal("12800.00"))

    def test_resolucion_presupuesto_rechazado_sigue_cobrando_la_revision(self):
        neto = _exit_order_net_charge(
            {
                "resolucion": "presupuesto_rechazado",
                "quote_estado": "rechazado",
                "subtotal": Decimal("879046.95"),
            },
            {"presupuesto_rechazado_cobro_neto": Decimal("12800.00")},
        )

        self.assertEqual(neto, Decimal("12800.00"))

    def test_presupuesto_vigente_cobra_el_presupuesto(self):
        neto = _exit_order_net_charge(
            {
                "resolucion": "reparado",
                "quote_estado": "aprobado",
                "subtotal": Decimal("173970.00"),
            },
            {"presupuesto_rechazado_cobro_neto": Decimal("12800.00")},
        )

        self.assertEqual(neto, Decimal("173970.00"))

    def test_sin_cobro_cargado_la_caja_va_en_blanco(self):
        neto = _exit_order_net_charge(
            {
                "resolucion": "no_reparado",
                "quote_estado": "rechazado",
                "subtotal": Decimal("879046.95"),
            },
            {"presupuesto_rechazado_cobro_neto": None},
        )

        self.assertIsNone(neto)
