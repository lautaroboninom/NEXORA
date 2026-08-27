import fitz

from django.test import SimpleTestCase, override_settings

from service.bejerman_remito_legends import format_os_badge, service_release_item_legend
from service.bejerman_remito_pdf import render_remito_pdf
from service.bejerman_sdk import build_service_ingress_comprobante


class BejermanRemitoLegendTests(SimpleTestCase):
    def test_formatea_la_os_como_badge_legible(self):
        self.assertEqual(format_os_badge(13), "- OS 00013 -")
        self.assertEqual(format_os_badge("OS-29381"), "- OS 29381 -")

    def test_rss_conserva_la_resolucion_y_destaca_la_os(self):
        line = service_release_item_legend(
            {"serviceResolution": "no_se_encontro_falla", "sourceReference": "OS 29381"},
            {"articleCode": "1115003"},
        )

        self.assertEqual(line, "Se entrega controlado sin defecto - OS 29381 -")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_pdf_del_ris_lote_no_duplica_serie_y_mantiene_la_columna(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "leyenda-ris-13",
                "ingresoId": 13,
                "issueDate": "2026-08-10",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipments": [
                    {
                        "articleCode": "1115003",
                        "articleName": "Concentrador de oxígeno",
                        "serial": "MZJ5S121916",
                        "internalNumber": "MG 9901",
                        "repairReason": "reparación alquiler",
                        "osLabel": "00013",
                    }
                ],
            },
            {"Cliente_RazonSocial": "ALCLA SRL"},
        )

        pdf_bytes = render_remito_pdf(built["comprobante"], company_key="SEPID")
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            rendered_text = document[0].get_text()

        self.assertIn("- OS 00013 -", rendered_text)
        self.assertIn("Interno: MG 9901", rendered_text)
        self.assertIn("Motivo: reparación alquiler", rendered_text)
        self.assertIn("MZJ5S121916", rendered_text)
        self.assertNotIn("N/S:", rendered_text)
        self.assertNotIn("------------------------------", rendered_text)
