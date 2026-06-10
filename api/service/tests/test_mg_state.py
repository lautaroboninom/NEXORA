from django.test import SimpleTestCase

from service.views.mg_state import resolve_mg_flags


class MgStateTests(SimpleTestCase):
    def test_codigo_mg_en_cliente_no_implica_venta(self):
        flags = resolve_mg_flags(
            {
                "customer_id": 20,
                "numero_interno": "MG 3875",
                "numero_serie": "974-12",
                "alquilado": False,
            },
            mg_owner_id=10,
        )

        self.assertTrue(flags["tiene_codigo_mg"])
        self.assertFalse(flags["es_propietario_mg"])
        self.assertEqual(flags["mg_estado"], "activo")
        self.assertFalse(flags["mg_inactivo_venta"])
        self.assertFalse(flags["vendido"])

    def test_mg_inactivo_venta_sigue_marcando_vendido(self):
        flags = resolve_mg_flags(
            {
                "customer_id": 20,
                "numero_interno": "MG 3875",
                "numero_serie": "974-12",
                "mg_estado": "inactivo_venta",
            },
            mg_owner_id=10,
        )

        self.assertEqual(flags["mg_estado"], "inactivo_venta")
        self.assertTrue(flags["mg_inactivo_venta"])
        self.assertTrue(flags["vendido"])
