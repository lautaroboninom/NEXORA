from django.test import SimpleTestCase

from service.delivery_orders import delivery_order_order_sql
from service.views.ingresos_views import _merge_historico_rows


class PaginatedTableSortingTests(SimpleTestCase):
    def test_delivery_orders_accept_only_known_fields_and_add_stable_tiebreaker(self):
        ascending = delivery_order_order_sql("cliente")
        descending = delivery_order_order_sql("-cliente")

        self.assertIn("customer_name", ascending)
        self.assertIn("ASC NULLS LAST", ascending)
        self.assertIn("o.id ASC", ascending)
        self.assertIn("DESC NULLS LAST", descending)
        self.assertIn("o.id DESC", descending)

    def test_delivery_orders_invalid_sort_uses_original_default(self):
        order_sql = delivery_order_order_sql("campo_inexistente")

        self.assertIn("o.created_at DESC", order_sql)
        self.assertIn("o.id DESC", order_sql)
        self.assertNotIn("campo_inexistente", order_sql)

    def test_historico_merges_deleted_rows_before_sorting_and_keeps_empty_last(self):
        rows = [
            {"id": 10, "razon_social": "Beta"},
            {"id": 20, "razon_social": ""},
        ]
        deleted_rows = [{"id": 5, "razon_social": "Alfa"}]

        result = _merge_historico_rows(rows, deleted_rows, "-cliente")

        self.assertEqual([item["id"] for item in result], [10, 5, 20])

    def test_historico_invalid_sort_restores_default_order(self):
        result = _merge_historico_rows(
            [{"id": 2}, {"id": 4}],
            [{"id": 3}],
            "campo_inexistente",
        )

        self.assertEqual([item["id"] for item in result], [4, 3, 2])
