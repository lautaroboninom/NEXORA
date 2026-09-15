"""Que un artículo de Bejerman renombrado para otro modelo no se use sin que alguien lo mire.

Caso real: 1115005 se compró como "Longfian JAY-5Q" y en 2024 se renombró "JAY-5AW"; los 5Q que volvían
entraban con el nombre de otro equipo (corregido en Bejerman el 11/09/2026).

SimpleTestCase a propósito: la regla no necesita Postgres ni el SDK.
"""
from unittest import mock

from django.test import SimpleTestCase

from service.bejerman_article_rename import article_renamed_away_from_model


class ArticuloRenombradoTests(SimpleTestCase):
    def test_el_caso_jay5q_renombrado_jay5aw(self):
        self.assertTrue(
            article_renamed_away_from_model(
                "JAY-5Q",
                "Concentrador de oxígeno Longfian JAY-5Q 2046-33",
                "Concentrador de oxígeno Longfian JAY-5AW 2046-33",
            )
        )

    def test_el_digito_suelto_tambien_identifica_al_modelo(self):
        self.assertTrue(
            article_renamed_away_from_model("JAY-5", "Concentrador Longfian JAY-5", "Concentrador Longfian JAY-5AW")
        )

    def test_un_cambio_cosmetico_no_cuenta(self):
        self.assertFalse(
            article_renamed_away_from_model(
                "JAY-5Q",
                "Concentrador de oxigeno Longfian JAY-5Q",
                "Concentrador de oxígeno LONGFIAN  jay 5q 2046-33",
            )
        )

    def test_un_mapeo_que_nunca_nombro_al_modelo_no_dice_nada(self):
        self.assertFalse(
            article_renamed_away_from_model("CPAP G2", "CPAP RESmart BMC GII E-20C-H-O", "CPAP BMC H60-GII")
        )

    def test_sin_datos_no_frena(self):
        self.assertFalse(article_renamed_away_from_model("JAY-5Q", "", "Concentrador Longfian JAY-5AW"))
        self.assertFalse(article_renamed_away_from_model("JAY-5Q", "Concentrador Longfian JAY-5Q", ""))
        self.assertFalse(article_renamed_away_from_model("", "Concentrador Longfian JAY-5Q", "Otro"))

    def test_despues_de_la_correccion_cada_modelo_nombra_su_articulo(self):
        self.assertFalse(
            article_renamed_away_from_model(
                "JAY-5AW", "Concentrador de oxígeno Longfian JAY-5AW", "Concentrador de oxígeno Longfian JAY-5AW"
            )
        )


class ValidacionDelRemitoTests(SimpleTestCase):
    def _issues(self, *, source="mapping", mapping_code="1115005", live="Concentrador de oxígeno Longfian JAY-5AW 2046-33"):
        from service import bejerman_ris

        issues = []
        mapping = {"article_code": mapping_code, "article_description": "Concentrador de oxígeno Longfian JAY-5Q 2046-33"}
        with mock.patch.object(bejerman_ris, "_article_mapping_for_context", return_value=mapping):
            bejerman_ris._validate_article_not_renamed(
                {"articleCode": "1115005", "articleSource": source},
                {"model_id": 1667, "modelo": "JAY-5Q"},
                {"article_code": "1115005", "article_description": live},
                issues,
                item_index=None,
                variante="",
                company_key="SEPID",
            )
        return issues

    def test_frena_si_el_articulo_del_mapeo_ya_nombra_a_otro_modelo(self):
        issues = self._issues()
        self.assertEqual([issue["code"] for issue in issues], ["BEJERMAN_ARTICLE_RENAMED"])
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[0]["fix"]["type"], "article_mapping")

    def test_si_la_partida_ya_tiene_historia_manda_su_articulo(self):
        self.assertEqual(self._issues(source="stock_partida"), [])

    def test_si_sigue_nombrando_al_modelo_no_frena(self):
        self.assertEqual(self._issues(live="Concentrador de oxígeno Longfian JAY-5Q 2046-33"), [])

    def test_si_el_mapeo_ya_apunta_a_otro_articulo_no_frena(self):
        self.assertEqual(self._issues(mapping_code="1115023"), [])
