from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from service.views.scan_views import _company_key_from_request, _find_sale_for_new_device


class ScanLookupCompanyTests(SimpleTestCase):
    def test_company_key_from_request_accepts_mg_bio_alias(self):
        request = RequestFactory().get("/api/scan/lookup/", {"code": "NS-1", "companyKey": "MG BIO"})

        self.assertEqual(_company_key_from_request(request), "MGBIO")

    def test_find_sale_for_new_device_uses_requested_bejerman_company(self):
        with (
            patch(
                "service.views.scan_views.find_latest_sale_by_serial",
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
        find_sale.assert_called_once_with("NS-MGBIO-NUEVO", company_key="MGBIO")
        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=123)
        live_lookup.assert_called_once_with(client_cls.return_value, "NS-MGBIO-NUEVO")
