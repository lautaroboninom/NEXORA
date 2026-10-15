from django.test import SimpleTestCase

from service.delivery_orders import delivery_order_order_sql
from service.views.ingresos_views import _merge_historico_rows


class PaginatedTableSortingTests(SimpleTestCase):
    def test_delivery_orders_accept_only_known_fields_and_add_stable_tiebreaker(self):
        ascending = delivery_order_order_sql("cliente")
        descending = delivery_order_order_sql("-cliente")

        self.assertIn("CASE WHEN o.priority = 'urgente' THEN 0 ELSE 1 END ASC", ascending)
        self.assertIn("customer_name", ascending)
        self.assertIn("ASC NULLS LAST", ascending)
        self.assertIn("o.id ASC", ascending)
        self.assertIn("DESC NULLS LAST", descending)
        self.assertIn("o.id DESC", descending)

    def test_delivery_orders_default_prioritizes_urgent_then_latest_grouped_service_release(self):
        order_sql = delivery_order_order_sql("campo_inexistente")

        self.assertIn("CASE WHEN o.priority = 'urgente' THEN 0 ELSE 1 END ASC", order_sql)
        self.assertIn("WHEN o.delivery_type = 'service_release'", order_sql)
        self.assertIn("FROM ingreso_events ie", order_sql)
        self.assertIn("ie.a_estado = 'liberado'", order_sql)
        self.assertRegex(order_sql, r"END\s+DESC,")
        self.assertIn("o.id DESC", order_sql)
        self.assertNotIn("campo_inexistente", order_sql)

    def test_delivery_orders_date_sort_uses_latest_grouped_service_release(self):
        order_sql = delivery_order_order_sql("-fecha")

        self.assertIn("CASE WHEN o.priority = 'urgente' THEN 0 ELSE 1 END ASC", order_sql)
        self.assertIn("WHEN o.delivery_type = 'service_release'", order_sql)
        self.assertIn("FROM delivery_order_items oi", order_sql)
        self.assertIn("ie.a_estado = 'liberado'", order_sql)
        self.assertRegex(order_sql, r"END\s+DESC NULLS LAST")
        self.assertIn("DESC NULLS LAST", order_sql)

    def test_billing_queue_keeps_urgent_first_then_oldest(self):
        order_sql = delivery_order_order_sql("", billing_queue_requested=True)

        self.assertIn("CASE WHEN o.priority = 'urgente' THEN 0 ELSE 1 END ASC", order_sql)
        self.assertIn("o.created_at ASC", order_sql)

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
