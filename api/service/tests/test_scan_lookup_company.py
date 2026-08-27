from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from service.bejerman_sdk import BejermanSdkResponseError
from service.views.scan_views import _company_key_from_request, _find_sale_for_new_device


class ScanLookupCompanyTests(SimpleTestCase):
    def test_company_key_from_request_accepts_mg_bio_alias(self):
        request = RequestFactory().get("/api/scan/lookup/", {"code": "NS-1", "companyKey": "MG BIO"})

        self.assertEqual(_company_key_from_request(request), "MGBIO")

    def test_find_sale_for_new_device_uses_requested_bejerman_company(self):
        with (
            patch(
                "service.views.scan_views.find_latest_sale_by_serial_any_company",
                return_value={
                    "found": False,
                    "source": "bejerman_sale_cache",
                },
            ) as find_sale,
            patch(
                "service.views.scan_views.lookup_and_cache_sale_by_serial",
                return_value={
                    "found": False,
                    "source": "bejerman_sale_live",
                },
            ) as live_lookup,
            patch("service.views.scan_views.BejermanSDKClient") as client_cls,
        ):
            result = _find_sale_for_new_device(
                "NS-MGBIO-NUEVO",
                user_id=123,
                company_key="MGBIO",
            )

        self.assertFalse(result["found"])
        find_sale.assert_called_once_with("NS-MGBIO-NUEVO", preferred_company_key="MGBIO")
        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=123)
        live_lookup.assert_called_once_with(client_cls.return_value, "NS-MGBIO-NUEVO")

    def test_find_sale_any_company_cae_a_las_demas_empresas(self):
        from service.bejerman_sales import find_latest_sale_by_serial_any_company

        mgbio_sale = {"found": True, "companyKey": "MGBIO", "serial": "S12BEA05054"}
        with patch(
            "service.bejerman_sales.find_latest_sale_by_serial",
            side_effect=[{"found": False}, mgbio_sale],
        ) as find_sale:
            result = find_latest_sale_by_serial_any_company(
                "S12BEA05054",
                preferred_company_key="SEPID",
            )

        self.assertEqual(result, mgbio_sale)
        self.assertEqual(find_sale.call_args_list[0].kwargs.get("company_key"), "SEPID")
        self.assertEqual(find_sale.call_args_list[1].kwargs.get("company_key"), None)

    def test_find_sale_for_new_device_no_expone_sql_crudo_de_bejerman(self):
        with (
            patch(
                "service.views.scan_views.find_latest_sale_by_serial_any_company",
                return_value={"found": False, "source": "bejerman_sale_cache"},
            ),
            patch(
                "service.views.scan_views.lookup_and_cache_sale_by_serial",
                side_effect=BejermanSdkResponseError(
                    "Error en la ejecución. // Invalid column name 'IMPORTE_TOTAL'. "
                    'The multi-part identifier "CabVenta.cve_ID" could not be bound. '
                    "ORDER BY items must appear in the select list if the statement contains a UNION operator."
                ),
            ),
            patch("service.views.scan_views.BejermanSDKClient"),
        ):
            result = _find_sale_for_new_device("NS-SQL-CRUDO", user_id=123, company_key="SEPID")

        self.assertFalse(result["found"])
        self.assertNotIn("IMPORTE_TOTAL", result["lookup_error"])
        self.assertNotIn("CabVenta", result["lookup_error"])
        self.assertIn("error interno", result["lookup_error"])
