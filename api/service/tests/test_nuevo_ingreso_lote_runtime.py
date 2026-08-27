from django.test import SimpleTestCase

from service.views.ingresos_views import (
    NuevoIngresoLoteView,
    _ris_lote_result_fields,
)


class NuevoIngresoLoteRuntimeTests(SimpleTestCase):
    def test_dispatch_queda_fuera_de_atomic_requests(self):
        databases = getattr(NuevoIngresoLoteView.dispatch, "_non_atomic_requests", set())

        self.assertIn("default", databases)

    def test_resultado_multiple_expone_numeros_y_pdf_conjunto(self):
        batch_result = {
            "multiple": True,
            "remitos": [
                {
                    "ingresoIds": [14, 15, 16, 17, 18, 19, 20],
                    "remitoNumber": "RIS R 00004-00004360",
                    "lineCount": 22,
                    "pdf_status": "pending",
                },
                {
                    "ingresoIds": [21, 22, 23, 24, 25],
                    "remitoNumber": "RIS R 00004-00004361",
                    "lineCount": 16,
                    "pdf_status": "pending",
                },
            ],
        }

        result = _ris_lote_result_fields(list(range(14, 26)), 14, batch_result)

        self.assertTrue(result["multiple"])
        self.assertEqual(
            result["remito_numbers"],
            ["RIS R 00004-00004360", "RIS R 00004-00004361"],
        )
        self.assertEqual([item["ingreso_id"] for item in result["remitos"]], [14, 21])
        self.assertEqual([item["line_count"] for item in result["remitos"]], [22, 16])
        self.assertEqual(result["pdf_url"], "/api/ingresos/ris/lote/pdf/?ids=14,21")
        self.assertEqual(result["print_url"], "/api/ingresos/ris/lote/print/?ids=14,21")

    def test_resultado_simple_conserva_urls_historicas(self):
        result = _ris_lote_result_fields(
            [14],
            14,
            {
                "remitos": [
                    {
                        "ingreso_ids": [14],
                        "remito_number": "RIS R 00004-00004360",
                        "lineCount": 4,
                    }
                ]
            },
        )

        self.assertFalse(result["multiple"])
        self.assertEqual(result["pdf_url"], "/api/ingresos/14/ris/pdf/")
        self.assertEqual(result["print_url"], "/api/ingresos/14/ris/print/")
