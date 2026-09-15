from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase

from service.bejerman_delivery import DeliveryOrderError, reserve_remito_group
from service.tests.schema_helpers import apply_application_schema


@skipUnless(connection.vendor == "postgresql", "La reserva requiere PostgreSQL")
class RemitoReservationRaceTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        apply_application_schema()

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM delivery_order_remito_links")
            cur.execute("DELETE FROM bejerman_remito_groups")
            cur.execute("DELETE FROM delivery_orders")
            cur.execute("INSERT INTO delivery_orders(id, order_number) VALUES ('race-order', 'OE-RACE')")
            cur.execute("""
                INSERT INTO bejerman_remito_groups(
                    id, comprobante_tipo, customer_code, customer_name,
                    seller_code, payment_term_code, operation_code, deposit_code, status
                ) VALUES ('race-original', 'RSS', 'TEST', 'Cliente de prueba',
                          'ADM', '30', 'REP', 'STC', 'generated')
            """)
            cur.execute("""
                INSERT INTO delivery_order_remito_links(id, order_id, remito_group_id, batch_id, expected_count)
                VALUES ('race-link', 'race-order', 'race-original', 'race-batch', 2)
            """)

    def _reserve_stale_snapshot(self, batch_id=None, sequence=1):
        # La primera solicitud terminó después de que la segunda leyera la OS.
        # Las validaciones del snapshot siguen viendo una orden sin remito.
        with (
            patch("service.bejerman_delivery._validate_remito_orders", return_value={
                "companyKey": "SEPID", "deliveryType": "service_release",
                "customerCode": "TEST", "customerName": "Cliente de prueba",
            }),
            patch("service.bejerman_delivery._profile_for_order", return_value={
                "type": "RSS", "pointOfSale": "00001", "operation": "REP", "deposit": "STC",
            }),
            patch("service.bejerman_delivery._insert_delivery_order_remito_link"),
            patch("service.bejerman_delivery._supersede_stale_failed_groups"),
            patch("service.bejerman_delivery.sync_delivery_order_remito_projection"),
            patch("service.bejerman_delivery._record_bejerman_integration_attempt"),
        ):
            return reserve_remito_group(
                [{"id": "race-order"}], None, seller_code="ADM",
                payment_term_code="30", batch_id=batch_id,
                sequence_by_order={"race-order": sequence},
                expected_by_order={"race-order": 2},
            )

    def test_snapshot_anterior_no_reserva_otro_lote_tras_emision_confirmada(self):
        for status in ("generated", "reconciled"):
            with self.subTest(status=status):
                with connection.cursor() as cur:
                    cur.execute("UPDATE bejerman_remito_groups SET status=%s WHERE id='race-original'", [status])
                with self.assertRaises(DeliveryOrderError) as caught:
                    self._reserve_stale_snapshot()
                self.assertEqual(caught.exception.status_code, 409)
                with connection.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM bejerman_remito_groups")
                    self.assertEqual(cur.fetchone()[0], 1)

    def test_permite_completar_el_mismo_lote_parcial(self):
        group_id, _ = self._reserve_stale_snapshot(batch_id="race-batch", sequence=2)
        self.assertNotEqual(group_id, "race-original")

    def test_reanudacion_repetida_no_reserva_la_misma_secuencia(self):
        with self.assertRaises(DeliveryOrderError) as caught:
            self._reserve_stale_snapshot(batch_id="race-batch", sequence=1)
        self.assertEqual(caught.exception.status_code, 409)
