from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase
from django.utils import timezone

from service.pdf import _equipment_warranty_text, render_quote_pdf


# Relativas a hoy: la vigencia se evalúa contra la fecha del día, así que fechas
# fijas convertirían estos tests en una bomba de tiempo.
VIGENTE = timezone.localdate() + timedelta(days=200)
VENCIDA = timezone.localdate() - timedelta(days=200)


BASE_HEAD = {
    "ingreso_id": 29345,
    "quote_version_num": 1,
    "quote_id": None,
    "cliente": "DIAGNOSTIC MEDIC S.A.",
    "cliente_razon_social": "DIAGNOSTIC MEDIC S.A.",
    "cliente_alias_interno": "",
    "cliente_cuit": "30708155795",
    "cliente_contacto": "",
    "cliente_telefono": "",
    "cliente_telefono_2": "",
    "cliente_email": "",
    "propietario_nombre": "",
    "propietario_doc": "",
    "propietario_contacto": "",
    "fecha_emitido": datetime(2026, 8, 27, 9, 0, 0),
    "marca": "BMC",
    "modelo": "BPAP G3 B30VT",
    "equipo": "BPAP",
    "equipo_variante": "",
    "numero_serie": "B3CBDB09094",
    "numero_interno": "",
    "remito_ingreso": "26258",
    "etiq_ok": True,
    "garantia": False,
    "garantia_reparacion": False,
    "garantia_vence": None,
    "motivo": "reparación",
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
}


def _head(**overrides):
    head = dict(BASE_HEAD)
    head.update(overrides)
    return head


class EquipmentWarrantyTextTest(SimpleTestCase):
    def test_factory_warranty_shows_expiry_with_full_year(self):
        self.assertEqual(
            _equipment_warranty_text(_head(garantia=True, garantia_vence=VIGENTE)),
            f"En garantía de fábrica (vence {VIGENTE:%d/%m/%Y})",
        )

    def test_factory_warranty_without_expiry_omits_the_date(self):
        self.assertEqual(
            _equipment_warranty_text(_head(garantia=True)),
            "En garantía de fábrica",
        )

    def test_expired_factory_warranty_says_when_it_expired(self):
        self.assertEqual(
            _equipment_warranty_text(_head(garantia_vence=VENCIDA)),
            f"Fuera de garantía (venció {VENCIDA:%d/%m/%Y})",
        )

    def test_ticked_warranty_with_expired_date_does_not_print_the_contradiction(self):
        # La casilla se sella al recibir el equipo; si el presupuesto sale meses
        # después, la fecha puede haber quedado atrás. Manda la casilla, pero la
        # fecha que la desmiente no se imprime.
        self.assertEqual(
            _equipment_warranty_text(_head(garantia=True, garantia_vence=VENCIDA)),
            "En garantía de fábrica",
        )

    def test_unticked_warranty_with_valid_date_does_not_print_the_contradiction(self):
        self.assertEqual(
            _equipment_warranty_text(_head(garantia_vence=VIGENTE)),
            "Fuera de garantía",
        )

    def test_repair_warranty_covers_the_equipment_out_of_factory_warranty(self):
        self.assertEqual(
            _equipment_warranty_text(_head(garantia_reparacion=True)),
            "En garantía de reparación",
        )

    def test_factory_warranty_wins_over_repair_warranty(self):
        self.assertEqual(
            _equipment_warranty_text(_head(garantia=True, garantia_reparacion=True)),
            "En garantía de fábrica",
        )

    def test_no_warranty_at_all(self):
        self.assertEqual(_equipment_warranty_text(_head()), "Fuera de garantía")


class QuotePdfWarrantyTest(SimpleTestCase):
    def _render(self, head):
        with patch("service.pdf.LOGO_PATH", ""), \
             patch("service.pdf._logo_path_for_company", return_value=""), \
             patch("service.pdf._get_empresa_facturar", return_value=""), \
             patch("service.pdf._q", return_value=[]), \
             patch("service.pdf._get_data", return_value=(head, [])):
            _filename, pdf = render_quote_pdf(head["ingreso_id"])
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)

    def test_quote_prints_factory_warranty_in_the_equipment_panel(self):
        text = self._render(_head(garantia=True, garantia_vence=VIGENTE))
        self.assertIn(f"Garantía del equipo: En garantía de fábrica (vence {VIGENTE:%d/%m/%Y})", text)

    def test_quote_prints_that_the_equipment_is_out_of_warranty(self):
        text = self._render(_head(garantia_vence=VENCIDA))
        self.assertIn(f"Garantía del equipo: Fuera de garantía (venció {VENCIDA:%d/%m/%Y})", text)

    def test_warranty_line_does_not_replace_the_offered_repair_warranty(self):
        # "Garantía: 90 DÍAS" es la que ofrece la reparación presupuestada; la nueva
        # línea habla del equipo. Las dos tienen que convivir sin pisarse.
        text = self._render(_head(garantia=True))
        self.assertIn("Garantía del equipo: En garantía de fábrica", text)
        self.assertIn("Garantía: 90 DÍAS", text)

    def test_open_seals_keep_their_own_line_below_the_warranty(self):
        text = self._render(_head(garantia=True, etiq_ok=False))
        self.assertIn("Garantía del equipo: En garantía de fábrica", text)
        self.assertIn("Fajas de garantía: Abiertas", text)
