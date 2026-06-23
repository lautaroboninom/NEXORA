from django.test import SimpleTestCase

from service.views.mg_state import resolve_mg_flags


class MgStateTests(SimpleTestCase):
    def test_codigo_mg_activo_en_cliente_sigue_siendo_propiedad_mg(self):
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
        self.assertTrue(flags["es_propietario_mg"])
        self.assertTrue(flags["es_cliente_mg_owner"])
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
        self.assertFalse(flags["es_propietario_mg"])
        self.assertFalse(flags["es_cliente_mg_owner"])
        self.assertTrue(flags["mg_inactivo_venta"])
        self.assertTrue(flags["vendido"])

    def test_codigo_stock_madre_ce_cuenta_como_patrimonio(self):
        flags = resolve_mg_flags(
            {
                "customer_id": 20,
                "numero_interno": "CE 0010",
                "numero_serie": "7001021",
            },
            mg_owner_id=10,
        )

        self.assertTrue(flags["tiene_codigo_mg"])
        self.assertTrue(flags["es_propietario_mg"])
        self.assertFalse(flags["mg_inactivo_venta"])

    def test_codigo_en_n_de_control_cuenta_como_patrimonio(self):
        flags = resolve_mg_flags(
            {
                "customer_id": 20,
                "numero_interno": "",
                "numero_serie": "922975",
                "n_de_control": "MG 2562",
            },
            mg_owner_id=10,
        )

        self.assertTrue(flags["tiene_codigo_mg"])
        self.assertTrue(flags["es_propietario_mg"])
