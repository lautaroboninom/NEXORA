from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase

from service.pdf import _customer_display_name, os_version_title, render_quote_pdf


class PdfTitleFormatTest(SimpleTestCase):
    def test_os_version_title_uses_hyphen(self):
        self.assertEqual(os_version_title(29207, 1), "OS 29207-1")

    def test_os_version_title_pads_os_number(self):
        self.assertEqual(os_version_title(12, 3), "OS 00012-3")

    def test_customer_display_prefers_alias(self):
        self.assertEqual(
            _customer_display_name(
                {
                    "cliente": "SIED SERVICIO INTEGRAL DOMICILIARIO SRL",
                    "cliente_razon_social": "SIED SERVICIO INTEGRAL DOMICILIARIO SRL",
                    "cliente_alias_interno": " SIED ",
                }
            ),
            "SIED",
        )

    def test_customer_display_preserves_owner_override(self):
        self.assertEqual(
            _customer_display_name(
                {
                    "cliente": "Juan Pérez",
                    "cliente_razon_social": "Particular",
                    "cliente_alias_interno": "PART",
                }
            ),
            "Juan Pérez",
        )

    @patch("service.pdf.LOGO_PATH", "")
    @patch("service.pdf._logo_path_for_company", return_value="")
    @patch("service.pdf._get_empresa_facturar", return_value="")
    @patch("service.pdf._q", return_value=[])
    @patch("service.pdf._get_data")
    def test_quote_pdf_prints_alias_in_title_and_customer_line(
        self,
        get_data_mock,
        _q_mock,
        _empresa_mock,
        _logo_mock,
    ):
        get_data_mock.return_value = (
            {
                "ingreso_id": 29397,
                "quote_version_num": 1,
                "quote_id": None,
                "cliente": "SIED SERVICIO INTEGRAL DOMICILIARIO SRL",
                "cliente_razon_social": "SIED SERVICIO INTEGRAL DOMICILIARIO SRL",
                "cliente_alias_interno": "SIED",
                "cliente_cuit": "30715975978",
                "cliente_contacto": "",
                "cliente_telefono": "",
                "cliente_telefono_2": "",
                "cliente_email": "",
                "propietario_nombre": "",
                "propietario_doc": "",
                "propietario_contacto": "",
                "fecha_emitido": datetime(2026, 6, 30, 9, 0, 0),
                "marca": "",
                "modelo": "",
                "equipo": "CONCENTRADOR DE OXÍGENO",
                "equipo_variante": "",
                "numero_serie": "",
                "numero_interno": "",
                "remito_ingreso": "",
                "etiq_ok": True,
                "motivo": "",
                "informe_preliminar": "",
                "accesorios": "",
                "descripcion_problema": "",
                "trabajos_realizados": "",
                "subtotal": Decimal("0"),
                "iva_21": Decimal("0"),
                "total": Decimal("0"),
                "forma_pago": "30 F.F.",
                "plazo_entrega_txt": "< 5 DÍAS HÁBILES",
                "garantia_txt": "90 DÍAS",
                "mant_oferta_txt": "7 DÍAS",
            },
            [],
        )

        filename, pdf = render_quote_pdf(29397)

        self.assertEqual(filename, "OS 29397-1 SIED.pdf")
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
        self.assertIn("OS 29397-1 SIED", text)
        self.assertIn("Señor(es): SIED", text)
        self.assertNotIn("Señor(es): SIED SERVICIO INTEGRAL DOMICILIARIO SRL", text)
