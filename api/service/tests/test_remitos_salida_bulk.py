"""Lote de órdenes de salida: qué pasa cuando una OS del lote no se puede liberar.

Estos tests ejercitan `RemitosSalidaBulkPdfView` con la view individual mockeada:
lo que se prueba acá es el reparto entre emitidas y fallidas, no la liberación en
sí, que ya cubre `test_bejerman_sync.test_remitos_salida_bulk_devuelve_un_pdf_y_
libera_todos_los_ingresos` contra la base real.
"""

import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from rest_framework.response import Response

from service.views.reportes_views import RemitosSalidaBulkPdfView


def _pdf(marker: bytes) -> HttpResponse:
    return HttpResponse(marker, content_type="application/pdf")


def _error(detail: str, status: int = 409) -> Response:
    return Response({"detail": detail}, status=status)


class RemitosSalidaBulkTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view = RemitosSalidaBulkPdfView()

    def _get(self, query: str, single_side_effect):
        request = self.factory.get(f"/api/ingresos/remitos-salida/?{query}")
        with (
            patch("service.views.reportes_views.require_roles", return_value=None),
            patch(
                "service.views.reportes_views.RemitoSalidaPdfView.get",
                side_effect=single_side_effect,
            ),
            patch(
                "service.views.reportes_views._merge_pdf_documents",
                side_effect=lambda docs: b"|".join(docs),
            ),
        ):
            return self.view.get(request)

    def test_sin_skip_errors_sigue_cortando_en_el_primer_fallo(self):
        response = self._get(
            "ids=1,2,3",
            [_pdf(b"uno"), _error("No se puede liberar sin resolución"), _pdf(b"tres")],
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("OS 2", response.data["detail"])
        self.assertIn("No se puede liberar sin resolución", response.data["detail"])
        self.assertNotIn("X-Nexora-Remitos-Emitidos", response)

    def test_skip_errors_emite_las_que_salen_y_reporta_las_que_no(self):
        response = self._get(
            "ids=1,2,3&skip_errors=1",
            [_pdf(b"uno"), _error("No se puede liberar sin resolución"), _pdf(b"tres")],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"uno|tres")
        self.assertEqual(response["X-Nexora-Remitos-Emitidos"], "1,3")

        fallidos = json.loads(response["X-Nexora-Remitos-Fallidos"])
        self.assertEqual(len(fallidos), 1)
        self.assertEqual(fallidos[0]["id"], 2)
        self.assertEqual(fallidos[0]["status"], 409)
        self.assertEqual(fallidos[0]["detail"], "No se puede liberar sin resolución")
        self.assertIn("2_OS.pdf", response["Content-Disposition"])

    def test_los_headers_quedan_en_ascii_aunque_el_motivo_tenga_acentos(self):
        response = self._get(
            "ids=1,2&skip_errors=1",
            [_pdf(b"uno"), _error("Falta la resolución de reparación")],
        )

        raw = response["X-Nexora-Remitos-Fallidos"]
        self.assertTrue(raw.isascii())
        self.assertEqual(
            json.loads(raw)[0]["detail"], "Falta la resolución de reparación"
        )

    def test_los_headers_se_exponen_al_navegador(self):
        response = self._get("ids=1,2&skip_errors=1", [_pdf(b"uno"), _pdf(b"dos")])

        expuestos = response["Access-Control-Expose-Headers"]
        self.assertIn("X-Nexora-Remitos-Emitidos", expuestos)
        self.assertIn("X-Nexora-Remitos-Fallidos", expuestos)

    def test_si_no_sale_ninguna_devuelve_409_con_el_detalle_por_os(self):
        response = self._get(
            "ids=1,2&skip_errors=1",
            [_error("sin resolución"), _error("ya fue entregado", status=400)],
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(response.data["fallidos"]), 2)
        self.assertEqual(response.data["fallidos"][0]["id"], 1)
        self.assertEqual(response.data["fallidos"][1]["status"], 400)

    def test_todas_bien_no_reporta_fallidos(self):
        response = self._get("ids=7,9&skip_errors=1", [_pdf(b"a"), _pdf(b"b")])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Nexora-Remitos-Emitidos"], "7,9")
        self.assertEqual(json.loads(response["X-Nexora-Remitos-Fallidos"]), [])

    def test_ids_invalidos_siguen_devolviendo_400(self):
        request = self.factory.get("/api/ingresos/remitos-salida/?ids=abc")
        with patch("service.views.reportes_views.require_roles", return_value=None):
            response = self.view.get(request)

        self.assertEqual(response.status_code, 400)
