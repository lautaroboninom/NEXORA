from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

from service.bejerman_article_catalog_sync import (
    ALL_COLUMNS,
    FK_SINGLE_COLUMNS,
    classify_discrepancy,
    sync_article_catalog,
)


# Pares reales tomados de discrepancias_SEPID_MGBIO.xlsx (24/07/2026), incluyendo
# los casos límite de cada categoría (0.51/0.52 sustantivos más "parecidos" y
# 0.57/0.60 cosméticos más "distintos") para blindar el umbral SIMILARITY_THRESHOLD.
KNOWN_SUSTANTIVE_PAIRS = [
    ("Trampa de agua New Dryline Mindray 115-043024-00", "Sensor neonatal"),
    ("Articulo para reutilzar", "Sensor de sat. c/broche ad. p/MEDITECH 3m 283BLR"),
    ("Monitor Multiparamétrico Aurora 12S Konsung C/PI", "Central de Monitoreo Konsung"),
    ("Tubbing de silicona con conexión luer lock", "Tubuladura de silicona para aspirador"),
    ("Placa base para concentrador BMC SO1 5B", "Placa calefactora para Marbel C-500A"),
    ("Batería externa generica p/terapia de sueño", "Bateria para Prisma Vent40"),
]
KNOWN_COSMETIC_PAIRS = [
    ("Fuente de alimentación 12v A 24V 60W p/BMC", "Convertidor 12V A 24V 60W p/BMC"),
    ("Placa turbina DRIVER CARD PB560 REF", "Placa turbina PB560"),
    ("Aspirador de uso continuo SILFAB N-33V", "Aspirador de uso continuo SILFAB N33V"),
    ("Respirador NEUMOVENT Graphnet TS", "Respirador NEUMOVENT TS"),
    ("Cámara p/calentador BMC GI Genérico", "Cámara p/calentador BMC GI"),
]


class ClassifyDiscrepancyTests(SimpleTestCase):
    def test_known_sustantive_pairs_classified_as_sustantive(self):
        for reference, target in KNOWN_SUSTANTIVE_PAIRS:
            with self.subTest(reference=reference, target=target):
                self.assertEqual(classify_discrepancy(reference, target), "sustantive")

    def test_known_cosmetic_pairs_classified_as_cosmetic(self):
        for reference, target in KNOWN_COSMETIC_PAIRS:
            with self.subTest(reference=reference, target=target):
                self.assertEqual(classify_discrepancy(reference, target), "cosmetic")

    def test_identical_text_is_cosmetic(self):
        self.assertEqual(classify_discrepancy("Igual", "Igual"), "cosmetic")


def _make_row(code: str, **overrides: str) -> dict:
    row = {"codigo": code, "cod_ele1": "", "cod_ele2": "", "cod_ele3": ""}
    for col in ALL_COLUMNS:
        row[f"ref__{col}"] = ""
        row[f"target__{col}"] = ""
    for col in FK_SINGLE_COLUMNS:
        row[f"fkok__{col}"] = "1"
    row.update(overrides)
    return row


class SyncArticleCatalogTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("apply_bejerman_article_sync_schema", verbosity=0)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_article_sync_log")

    def _log_rows(self, code: str) -> list[dict]:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT article_code, classification, applied, skip_reason FROM bejerman_article_sync_log "
                "WHERE article_code = %s ORDER BY id",
                [code],
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_dry_run_never_writes(self, mock_csv, mock_execute):
        mock_csv.return_value = [
            _make_row("1101015", ref__art_DescGen="N-33V", target__art_DescGen="N33V")
        ]

        summary = sync_article_catalog(apply=False)

        mock_execute.assert_not_called()
        self.assertEqual(summary["cosmetic"]["total"], 1)
        self.assertEqual(summary["cosmetic"]["applied"], 0)
        logs = self._log_rows("1101015")
        self.assertEqual(len(logs), 1)
        self.assertFalse(logs[0]["applied"])
        self.assertEqual(logs[0]["skip_reason"], "dry_run")

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_apply_updates_cosmetic_description_only(self, mock_csv, mock_execute):
        mock_csv.return_value = [
            _make_row(
                "1101015",
                ref__art_DescGen="Aspirador SILFAB N-33V",
                target__art_DescGen="Aspirador SILFAB N33V",
            )
        ]
        mock_execute.return_value = {"status": "success", "rowsAffected": 1}

        summary = sync_article_catalog(apply=True)

        mock_execute.assert_called_once()
        sql = mock_execute.call_args[0][0]
        self.assertIn("art_DescGen", sql)
        self.assertIn("Aspirador SILFAB N-33V", sql)
        self.assertIn("artusu_Codigo", sql)
        self.assertNotIn("artcla_Cod", sql)
        self.assertEqual(summary["cosmetic"]["applied"], 1)
        logs = self._log_rows("1101015")
        self.assertTrue(logs[0]["applied"])

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_sustantive_not_applied_without_include_flag(self, mock_csv, mock_execute):
        mock_csv.return_value = [
            _make_row(
                "1223022",
                ref__art_DescGen="Linea de Muestra Ad/Ped Mindray",
                target__art_DescGen="Sensor neonatal",
            )
        ]

        summary = sync_article_catalog(apply=True, include_sustantive=False)

        mock_execute.assert_not_called()
        self.assertEqual(summary["sustantive"]["total"], 1)
        self.assertEqual(summary["sustantive"]["needsReview"], 1)
        self.assertEqual(summary["sustantive"]["appliedFull"], 0)
        logs = self._log_rows("1223022")
        self.assertFalse(logs[0]["applied"])
        self.assertEqual(logs[0]["skip_reason"], "needs_review")

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_sustantive_full_row_skips_column_with_broken_fk(self, mock_csv, mock_execute):
        mock_csv.return_value = [
            _make_row(
                "1223022",
                ref__art_DescGen="Linea de Muestra Ad/Ped Mindray",
                target__art_DescGen="Sensor neonatal",
                ref__artpro_Cod="CENHOS",
                target__artpro_Cod="OTRO",
                fkok__artpro_Cod="0",
                ref__art_Tipo="P",
                target__art_Tipo="S",
            )
        ]
        mock_execute.return_value = {"status": "success", "rowsAffected": 1}

        summary = sync_article_catalog(apply=True, include_sustantive=True)

        mock_execute.assert_called_once()
        sql = mock_execute.call_args[0][0]
        self.assertIn("art_Tipo", sql)
        self.assertNotIn("artpro_Cod", sql)
        self.assertEqual(summary["sustantive"]["appliedFull"], 1)
        row = summary["rows"][0]
        self.assertIn("artpro_Cod", row["fkColumnsSkipped"])

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_sustantive_float_column_uses_dot_decimal_and_no_quotes(self, mock_csv, mock_execute):
        # Bejerman guarda los float con coma decimal ("1628,16"); si se manda
        # tal cual como literal SQL sin comillas, el UPDATE tira
        # "Incorrect syntax near '16'" (bug real encontrado el 27/07/2026).
        mock_csv.return_value = [
            _make_row(
                "1208055",
                ref__art_DescGen="Filtro interno p/concentrador Yuwell",
                target__art_DescGen="Filtro BMC HME con HU",
                ref__art_PrBase="1628,16",
                target__art_PrBase="0",
            )
        ]
        mock_execute.return_value = {"status": "success", "rowsAffected": 1}

        sync_article_catalog(apply=True, include_sustantive=True)

        sql = mock_execute.call_args[0][0]
        self.assertIn("art_PrBase = 1628.16", sql)
        self.assertNotIn("1628,16", sql)
        self.assertNotIn("'1628.16'", sql)

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_sustantive_blank_value_becomes_null_not_empty_string(self, mock_csv, mock_execute):
        # Si SEPID tiene una columna con FK en blanco y MGBIO tiene un valor,
        # mandar '' (en vez de NULL) viola la foreign key porque '' no es un
        # código real de la tabla referenciada (bug real 27/07/2026, caso
        # articulo 1206087 / artdep_Cod).
        mock_csv.return_value = [
            _make_row(
                "1206087",
                ref__art_DescGen="Cánula nasal adulto BESMED",
                target__art_DescGen="Canula traq. c/Endocanula",
                ref__artdep_Cod="",
                target__artdep_Cod="VAL",
                fkok__artdep_Cod="1",
            )
        ]
        mock_execute.return_value = {"status": "success", "rowsAffected": 1}

        sync_article_catalog(apply=True, include_sustantive=True)

        sql = mock_execute.call_args[0][0]
        self.assertIn("artdep_Cod = NULL", sql)
        self.assertNotIn("artdep_Cod = ''", sql)

    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_execute")
    @patch("service.bejerman_article_catalog_sync._remote_bejerman_sql_csv")
    def test_sustantive_bit_column_uses_unquoted_0_or_1(self, mock_csv, mock_execute):
        mock_csv.return_value = [
            _make_row(
                "1199036",
                ref__art_DescGen="Regulador de presión MOCHILA",
                target__art_DescGen="Valvula QF-2C9 ZS",
                ref__art_StockPart="True",
                target__art_StockPart="False",
            )
        ]
        mock_execute.return_value = {"status": "success", "rowsAffected": 1}

        sync_article_catalog(apply=True, include_sustantive=True)

        sql = mock_execute.call_args[0][0]
        self.assertIn("art_StockPart = 1", sql)
        self.assertNotIn("'True'", sql)
