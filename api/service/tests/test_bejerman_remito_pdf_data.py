"""PM y VTO del remito: de dónde salen y qué pasa cuando no se consiguen.

El PDF del remito no puede quedar trabado por estos dos datos — ése es justamente el
problema que el renderer propio viene a resolver —, así que lo que más se prueba acá
es que todo lo que falle degrade a columna vacía en vez de romper.
"""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase

from service.bejerman_remito_pdf_data import (
    _PM_CACHE,
    enrich_comprobante_for_pdf,
    expiration_dates_from_nexora,
    pm_from_bejerman,
)


ORDEN = "OE-TEST-PDF"


def _crear_orden():
    """`delivery_order_items` cuelga de una orden; alcanza con id y número."""

    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO delivery_orders (id, order_number) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            [ORDEN, ORDEN],
        )


def _item(id_, *, article_code, partida, vence, updated="2026-08-01"):
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO delivery_order_items
                   (id, order_id, article_code, partida, partida_expiration_date, updated_at)
            VALUES (%s, %s, %s, %s, %s::date, %s::timestamptz)
            """,
            [id_, ORDEN, article_code, partida, vence, updated],
        )


COMPROBANTE = {
    "Comprobante_PtoVenta": "00004",
    "Comprobante_Items": [
        {
            "Item_NumeroRenglon": 1,
            "Item_CodigoArticulo": "1204001",
            "Item_Partida": "20260007",
            "Item_DescripArticulo": "Jarra para aspirador",
        },
        {
            "Item_NumeroRenglon": 2,
            "Item_CodigoArticulo": "1205013",
            "Item_Partida": "R22082822",
            "Item_DescripArticulo": "Cámara p/calentador",
        },
    ],
}


class RemitoPdfDatosTests(TestCase):
    def setUp(self):
        super().setUp()
        _PM_CACHE.clear()
        with connection.cursor() as cur:
            cur.execute("TRUNCATE delivery_order_items CASCADE")
        _crear_orden()

    # --- VTO: ya lo tiene NEXORA ---------------------------------------------

    def test_el_vencimiento_sale_de_la_orden_de_entrega(self):
        _item("a", article_code="1204001", partida="20260007", vence="2029-03-01")
        self.assertEqual(
            expiration_dates_from_nexora(["20260007"], ["1204001"])["20260007"].isoformat(),
            "2029-03-01",
        )

    def test_la_partida_se_busca_sin_distinguir_mayusculas_ni_espacios(self):
        _item("a", article_code="1205013", partida="  r22082822 ", vence="2027-10-28")
        self.assertIn("R22082822", expiration_dates_from_nexora(["R22082822"], []))

    def test_ante_dos_cargas_de_la_misma_partida_gana_la_ultima(self):
        _item("vieja", article_code="1204001", partida="20260007", vence="2028-01-01", updated="2026-01-01")
        _item("nueva", article_code="1204001", partida="20260007", vence="2029-03-01", updated="2026-08-01")
        self.assertEqual(
            expiration_dates_from_nexora(["20260007"], [])["20260007"].isoformat(), "2029-03-01"
        )

    def test_sin_partidas_no_consulta_nada(self):
        self.assertEqual(expiration_dates_from_nexora([], ["1204001"]), {})

    # --- PM: hay que ir a Bejerman -------------------------------------------

    def test_el_pm_sale_del_maestro_de_articulos_de_bejerman(self):
        with patch(
            "service.bejerman_delivery._remote_bejerman_sql_csv",
            return_value=[{"Codigo": "1204001", "PM": "2046-10"}],
        ) as consulta:
            self.assertEqual(pm_from_bejerman(["1204001"]), {"1204001": "2046-10"})
        self.assertIn("art_DescRedu", consulta.call_args[0][0])

    def test_el_pm_se_cachea_para_no_repetir_la_consulta_por_ssh(self):
        with patch(
            "service.bejerman_delivery._remote_bejerman_sql_csv",
            return_value=[{"Codigo": "1204001", "PM": "2046-10"}],
        ) as consulta:
            pm_from_bejerman(["1204001"])
            pm_from_bejerman(["1204001"])
        consulta.assert_called_once()

    def test_si_bejerman_no_responde_el_pm_queda_vacio_pero_no_rompe(self):
        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", side_effect=OSError("sin SSH")):
            self.assertEqual(pm_from_bejerman(["1204001"]), {})

    # --- Enriquecido completo ------------------------------------------------

    def test_completa_pm_y_vencimiento_en_cada_renglon(self):
        _item("a", article_code="1204001", partida="20260007", vence="2029-03-01")
        _item("b", article_code="1205013", partida="R22082822", vence="2027-10-28")
        with patch(
            "service.bejerman_delivery._remote_bejerman_sql_csv",
            return_value=[
                {"Codigo": "1204001", "PM": "2046-10"},
                {"Codigo": "1205013", "PM": "2046-12"},
            ],
        ):
            enriquecido = enrich_comprobante_for_pdf(COMPROBANTE, company_key="SEPID")

        items = enriquecido["Comprobante_Items"]
        self.assertEqual(items[0]["Item_PM"], "2046-10")
        self.assertEqual(items[0]["Item_Vencimiento"].isoformat(), "2029-03-01")
        self.assertEqual(items[1]["Item_PM"], "2046-12")
        self.assertEqual(items[1]["Item_Vencimiento"].isoformat(), "2027-10-28")

    def test_no_pisa_lo_que_ya_venia_en_el_renglon(self):
        comprobante = {
            "Comprobante_Items": [
                {
                    "Item_CodigoArticulo": "1204001",
                    "Item_Partida": "20260007",
                    "Item_PM": "YA-ESTABA",
                    "Item_Vencimiento": "2030-12-31",
                }
            ]
        }
        with patch(
            "service.bejerman_delivery._remote_bejerman_sql_csv",
            return_value=[{"Codigo": "1204001", "PM": "2046-10"}],
        ):
            item = enrich_comprobante_for_pdf(comprobante)["Comprobante_Items"][0]
        self.assertEqual(item["Item_PM"], "YA-ESTABA")
        self.assertEqual(item["Item_Vencimiento"], "2030-12-31")

    def test_no_modifica_el_comprobante_original(self):
        with patch(
            "service.bejerman_delivery._remote_bejerman_sql_csv",
            return_value=[{"Codigo": "1204001", "PM": "2046-10"}],
        ):
            enrich_comprobante_for_pdf(COMPROBANTE, company_key="SEPID")
        self.assertNotIn("Item_PM", COMPROBANTE["Comprobante_Items"][0])

    def test_si_todo_falla_devuelve_los_renglones_con_las_columnas_vacias(self):
        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", side_effect=OSError("sin SSH")):
            items = enrich_comprobante_for_pdf(COMPROBANTE, company_key="SEPID")["Comprobante_Items"]
        self.assertEqual([i["Item_PM"] for i in items], ["", ""])
        self.assertEqual([i["Item_Vencimiento"] for i in items], ["", ""])

    def test_un_comprobante_sin_renglones_pasa_de_largo(self):
        self.assertEqual(enrich_comprobante_for_pdf({"Comprobante_Items": []}), {"Comprobante_Items": []})
