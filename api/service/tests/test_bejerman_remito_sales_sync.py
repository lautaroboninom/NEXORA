from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_remito_sales_sync import (
    REMOTE_COMPROBANTE_PREFIX,
    _remito_rows_sql,
    item_from_remito_row,
    sync_bejerman_remito_items,
)


def _row(**overrides):
    """Un renglón tal como lo devuelve el SQL directo a SAMTRONIC."""
    row = {
        "tipo": "RT",
        "letra": "R",
        "punto": "00004",
        "numero": "00001916",
        "fecha": "2024-07-19",
        "cliente_codigo": "MGBIO",
        "cliente_nombre": "MG BIO S.A.",
        "cliente_cuit": "30712271740",
        "partida": "Y662124170064",
        "articulo": "1103015",
        "descripcion": "CPAP YAMIND DM26-20C",
        "cantidad": "1",
        "renglon": "2",
        "comprobante_id": "7504",
    }
    row.update(overrides)
    return row


class RemitoRowMappingTests(SimpleTestCase):
    def test_traduce_el_renglon_a_la_forma_de_la_cache(self):
        item = item_from_remito_row(_row(), company_key="SEPID")

        self.assertEqual(item["company_key"], "SEPID")
        self.assertEqual(item["document_type"], "RT")
        self.assertEqual(item["document_label"], "RT R 00004-00001916")
        self.assertEqual(item["issue_date"], "2024-07-19")
        self.assertEqual(item["item_partida"], "Y662124170064")
        self.assertEqual(item["normalized_serial"], "Y662124170064")
        self.assertEqual(item["customer_name"], "MG BIO S.A.")
        self.assertEqual(item["customer_cuit"], "30712271740")
        self.assertEqual(item["article_code"], "1103015")
        self.assertEqual(item["line_index"], 2)

    def test_separa_el_id_del_sql_del_id_del_sdk(self):
        """Las dos numeraciones se solapan; sin prefijo el sync del SDK saltea facturas."""
        item = item_from_remito_row(_row(), company_key="SEPID")

        self.assertEqual(item["comprobante_id"], f"{REMOTE_COMPROBANTE_PREFIX}7504")
        self.assertFalse(item["comprobante_id"].isdigit())

    def test_descarta_el_renglon_sin_partida(self):
        self.assertIsNone(item_from_remito_row(_row(partida="   "), company_key="SEPID"))

    def test_descarta_el_renglon_sin_fecha(self):
        self.assertIsNone(item_from_remito_row(_row(fecha=""), company_key="SEPID"))

    def test_dos_renglones_del_mismo_remito_no_comparten_clave(self):
        cpap = item_from_remito_row(_row(renglon="2", articulo="1103015"), company_key="SEPID")
        humidificador = item_from_remito_row(_row(renglon="3", articulo="1111009"), company_key="SEPID")

        self.assertNotEqual(cpap["sale_item_key"], humidificador["sale_item_key"])


class RemitoRowsSqlTests(SimpleTestCase):
    def test_pide_solo_salidas_de_deposito(self):
        sql = _remito_rows_sql(["RT"], "2024-01-01", "2024-12-31")

        self.assertIn("c.scv_SgnStk = -1", sql)
        self.assertIn("IN ('RT')", sql)

    def test_el_limite_superior_es_exclusivo_sobre_el_dia_siguiente(self):
        """`scv_FEmision` es datetime: con `<=` se pierden los comprobantes con hora."""
        sql = _remito_rows_sql(["RT"], "2024-01-01", "2024-12-31")

        self.assertIn("c.scv_FEmision >= '2024-01-01'", sql)
        self.assertIn("c.scv_FEmision < '2025-01-01'", sql)


class RemitoSyncTests(SimpleTestCase):
    def _sql_ok(self, rows):
        def fake(sql, *, company_key=None, outcome=None, timeout_seconds=None):
            if outcome is not None:
                outcome.update({"status": "success", "rowCount": len(rows)})
            return rows

        return fake

    @override_settings(BEJERMAN_WARRANTY_REMITO_TYPES="RT")
    @patch("service.bejerman_remito_sales_sync.upsert_sale_items", return_value=1)
    @patch("service.bejerman_remito_sales_sync._table_exists", return_value=True)
    def test_carga_los_renglones_en_la_cache(self, _table, upsert):
        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", self._sql_ok([_row()])):
            summary = sync_bejerman_remito_items(
                company_key="SEPID", date_from="2024-07-01", date_to="2024-07-31"
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["rows"], 1)
        self.assertEqual(summary["items"], 1)
        self.assertEqual(summary["upserted"], 1)
        self.assertEqual(upsert.call_count, 1)

    @override_settings(BEJERMAN_WARRANTY_REMITO_TYPES="RT")
    @patch("service.bejerman_remito_sales_sync.upsert_sale_items", return_value=1)
    @patch("service.bejerman_remito_sales_sync._table_exists", return_value=True)
    def test_dry_run_no_escribe(self, _table, upsert):
        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", self._sql_ok([_row()])):
            summary = sync_bejerman_remito_items(
                company_key="SEPID", date_from="2024-07-01", date_to="2024-07-31", dry_run=True
            )

        self.assertEqual(summary["items"], 1)
        self.assertEqual(summary["upserted"], 0)
        upsert.assert_not_called()

    @override_settings(BEJERMAN_WARRANTY_REMITO_TYPES="RT")
    @patch("service.bejerman_remito_sales_sync.upsert_sale_items", return_value=0)
    @patch("service.bejerman_remito_sales_sync._table_exists", return_value=True)
    def test_una_ventana_caida_no_se_reporta_como_exito(self, _table, _upsert):
        def fake(sql, *, company_key=None, outcome=None, timeout_seconds=None):
            if outcome is not None:
                outcome.update({"status": "connection_failed", "detail": "timeout"})
            return []

        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", fake):
            summary = sync_bejerman_remito_items(
                company_key="SEPID", date_from="2024-07-01", date_to="2024-07-31"
            )

        self.assertFalse(summary["ok"])
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["status"], "connection_failed")

    @override_settings(BEJERMAN_WARRANTY_REMITO_TYPES="RT")
    @patch("service.bejerman_remito_sales_sync.upsert_sale_items", return_value=0)
    @patch("service.bejerman_remito_sales_sync._table_exists", return_value=True)
    def test_corta_el_rango_en_ventanas(self, _table, _upsert):
        calls = []

        def fake(sql, *, company_key=None, outcome=None, timeout_seconds=None):
            calls.append(sql)
            if outcome is not None:
                outcome.update({"status": "success", "rowCount": 0})
            return []

        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", fake):
            summary = sync_bejerman_remito_items(
                company_key="SEPID", date_from="2022-01-01", date_to="2024-12-31", chunk_days=366
            )

        self.assertEqual(summary["windows"], len(calls))
        self.assertGreater(summary["windows"], 1)
