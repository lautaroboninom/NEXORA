from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from service.views.ingresos_views import GarantiaFabricaCheckView


class GarantiaFabricaCheckViewTests(SimpleTestCase):
    def setUp(self):
        self.user = type("AuthUser", (), {"is_authenticated": True, "id": 1})()
        self.factory = APIRequestFactory()

    def _request(self, query_string="numero_serie=SN-001"):
        request = self.factory.get(f"/api/equipos/garantia-fabrica/?{query_string}")
        force_authenticate(request, user=self.user)
        return GarantiaFabricaCheckView.as_view()(request)

    def test_preserva_indeterminado_cuando_no_hay_fecha_de_venta(self):
        with (
            patch("service.views.ingresos_views.q", return_value=None),
            patch("service.views.ingresos_views.find_latest_sale_by_serial", return_value={"found": False}),
        ):
            response = self._request()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["within_365_days"])
        self.assertFalse(response.data["found"])
        self.assertIsNone(response.data["fecha_venta"])

    def test_informa_false_cuando_el_calculo_da_fuera_de_garantia(self):
        with (
            patch("service.views.ingresos_views.q", return_value=None),
            patch(
                "service.views.ingresos_views.find_latest_sale_by_serial",
                return_value={
                    "found": True,
                    "source": "bejerman_sale_cache",
                    "serial": "SN-001",
                    "issueDate": "2024-01-01",
                    "documentLabel": "FC A 0001-00000001",
                },
            ),
            patch(
                "service.views.ingresos_views.compute_warranty_from_sale_date",
                return_value={
                    "garantia": False,
                    "fecha_venta": date(2024, 1, 1),
                    "vence_el": date(2024, 12, 31),
                    "meta": {"source": "bejerman_sale"},
                },
            ),
        ):
            response = self._request()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["within_365_days"])
        self.assertTrue(response.data["found"])
        self.assertEqual(response.data["source"], "bejerman_sale")
