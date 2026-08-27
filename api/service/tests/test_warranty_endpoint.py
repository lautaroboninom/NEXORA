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
            patch("service.views.ingresos_views.find_latest_remito_by_serial", return_value={"found": False}),
        ):
            response = self._request()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["within_365_days"])
        self.assertFalse(response.data["found"])
        self.assertIsNone(response.data["fecha_venta"])
        self.assertFalse(response.data["estimated"])

    def test_cae_al_remito_cuando_la_serie_nunca_se_facturo(self):
        """Circuito SEPID -> MG BIO: en SEPID sólo queda el remito de salida."""
        with (
            patch("service.views.ingresos_views.q", return_value=None),
            patch("service.views.ingresos_views.find_latest_sale_by_serial", return_value={"found": False}),
            patch(
                "service.views.ingresos_views.find_latest_remito_by_serial",
                return_value={
                    "found": True,
                    "source": "bejerman_remito_cache",
                    "companyKey": "SEPID",
                    "serial": "Y662124170064",
                    "issueDate": "2024-07-19",
                    "documentType": "RT",
                    "documentLabel": "RT R 00004-00001916",
                    "customerName": "MG BIO S.A.",
                },
            ),
        ):
            response = self._request("numero_serie=Y662124170064")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["found"])
        self.assertTrue(response.data["estimated"])
        self.assertEqual(response.data["fecha_venta"], "2024-07-19")
        self.assertEqual(response.data["meta"]["dateBasis"], "remito")
        self.assertEqual(response.data["meta"]["documentLabel"], "RT R 00004-00001916")

    def test_la_factura_le_gana_al_remito(self):
        """Con factura no se consulta el remito: la fecha de venta real ya está."""
        with (
            patch("service.views.ingresos_views.q", return_value=None),
            patch(
                "service.views.ingresos_views.find_latest_sale_by_serial",
                return_value={
                    "found": True,
                    "source": "bejerman_sale_cache",
                    "serial": "Y662124170034",
                    "issueDate": "2025-04-30",
                    "documentType": "FC",
                    "documentLabel": "FC A 00002-00007340",
                },
            ),
            patch("service.views.ingresos_views.find_latest_remito_by_serial") as find_remito,
        ):
            response = self._request("numero_serie=Y662124170034")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["found"])
        self.assertFalse(response.data["estimated"])
        self.assertEqual(response.data["meta"]["dateBasis"], "factura")
        find_remito.assert_not_called()

    def test_busca_la_venta_en_todas_las_empresas_aunque_el_ingreso_sea_de_otra(self):
        """El equipo puede haberlo vendido MG BIO aunque el ingreso se cargue como SEPID."""
        with (
            patch("service.views.ingresos_views.q", return_value=None),
            patch(
                "service.views.ingresos_views.find_latest_sale_by_serial",
                return_value={
                    "found": True,
                    "source": "bejerman_sale_cache",
                    "companyKey": "MGBIO",
                    "serial": "S12BEA05054",
                    "issueDate": "2026-05-13",
                    "documentLabel": "FC A 00002-00018409",
                },
            ) as find_sale,
        ):
            response = self._request("numero_serie=S12BEA05054&company_key=SEPID")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["found"])
        self.assertEqual(response.data["meta"]["companyKey"], "MGBIO")
        self.assertEqual(find_sale.call_args.kwargs.get("company_key"), None)

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
