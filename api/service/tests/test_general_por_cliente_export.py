from datetime import datetime
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.test import APIRequestFactory, force_authenticate

from service.views.ingresos_views import GeneralPorClienteExportView


class GeneralPorClienteExportViewTests(SimpleTestCase):
    def setUp(self):
        self.user = type("AuthUser", (), {"is_authenticated": True, "id": 1})()
        self.factory = APIRequestFactory()

    def test_export_formatea_fecha_ingreso_dd_mm_aa_sin_hora(self):
        request = self.factory.get("/api/clientes/10/general/export/")
        force_authenticate(request, user=self.user)

        cursor = MagicMock()
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        cursor_context.__exit__.return_value = None
        connection = MagicMock()
        connection.cursor.return_value = cursor_context

        fecha_dt = timezone.make_aware(datetime(2024, 5, 15, 14, 30))

        rows = [
            {
                "id": 1001,
                "cliente": "Hospital Central",
                "tipo_equipo": "CONCENTRADOR DE OXÍGENO",
                "marca": "AirSep",
                "modelo": "VisionAire",
                "equipo_variante": "",
                "numero_serie": "VA-998877",
                "numero_interno": "",
                "estado": "en_revision",
                "presupuesto_estado": "pendiente",
                "fecha_ingreso": fecha_dt,
            },
            {
                "id": 1002,
                "cliente": "Hospital Central",
                "tipo_equipo": "MONITOR MULTIPARAMÉTRICO",
                "marca": "Mindray",
                "modelo": "uMEC10",
                "equipo_variante": "",
                "numero_serie": "SN-112233",
                "numero_interno": "",
                "estado": "reparado",
                "presupuesto_estado": "aprobado",
                "fecha_ingreso": "2023-11-04 09:15:00",
            },
            {
                "id": 1003,
                "cliente": "Hospital Central",
                "tipo_equipo": "BOMBA DE INFUSIÓN",
                "marca": "Terumo",
                "modelo": "TE-171",
                "equipo_variante": "",
                "numero_serie": "SN-0001",
                "numero_interno": "",
                "estado": "ingresado",
                "presupuesto_estado": None,
                "fecha_ingreso": None,
            },
        ]

        with (
            patch("service.views.ingresos_views._mg_list_select_sql", return_value=""),
            patch("service.views.ingresos_views._owner_list_select_sql", return_value=""),
            patch("service.views.ingresos_views._set_audit_user"),
            patch("service.views.ingresos_views.connection", connection),
            patch("service.views.ingresos_views._fetchall_dicts", return_value=rows),
        ):
            response = GeneralPorClienteExportView.as_view()(request, customer_id=10)

        self.assertEqual(response.status_code, 200)

        workbook = load_workbook(BytesIO(response.content), data_only=True)
        sheet = workbook.active

        headers = [cell.value for cell in sheet[1]]
        self.assertEqual(
            headers,
            ["OS", "Cliente", "Equipo", "N/S", "Estado", "Presupuesto", "Fecha ingreso"],
        )
        self.assertEqual(headers[-1], "Fecha ingreso")

        # Fila 1 (15-05-24)
        self.assertEqual(sheet["G2"].value, "15-05-24")

        # Fila 2 (04-11-23)
        self.assertEqual(sheet["G3"].value, "04-11-23")

        # Fila 3 (None -> "-")
        self.assertEqual(sheet["G4"].value, "-")
