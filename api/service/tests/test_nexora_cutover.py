from django.test import SimpleTestCase

from service.delivery_orders import normalize_delivery_type, normalize_priority, remito_status
from service.management.commands.import_portal_delivery_orders import _delivery_type, _priority, _status
from service.permission_catalog import get_role_defaults


class NexoraRoleDefaultsTests(SimpleTestCase):
    def test_recepcion_sees_reception_and_delivery_without_billing(self):
        permissions = get_role_defaults("recepcion")

        self.assertTrue(permissions["page.recepcion"])
        self.assertTrue(permissions["page.delivery_orders"])
        self.assertTrue(permissions["action.ingreso.create"])
        self.assertTrue(permissions["action.delivery_order.update_remito_location"])
        self.assertFalse(permissions["page.billing"])
        self.assertFalse(permissions["action.delivery_order.invoice"])

    def test_cobranzas_scope_is_billing_and_invoice_registration(self):
        permissions = get_role_defaults("cobranzas")

        self.assertTrue(permissions["page.billing"])
        self.assertTrue(permissions["page.delivery_orders"])
        self.assertTrue(permissions["action.billing.view"])
        self.assertTrue(permissions["action.delivery_order.invoice"])
        self.assertFalse(permissions["action.ingreso.create"])
        self.assertFalse(permissions["action.delivery_order.generate_bejerman_remito"])

    def test_admin_can_create_and_emit_delivery_orders(self):
        permissions = get_role_defaults("admin")

        self.assertTrue(permissions["page.recepcion"])
        self.assertTrue(permissions["page.delivery_orders"])
        self.assertTrue(permissions["action.delivery_order.create"])
        self.assertTrue(permissions["action.delivery_order.generate_bejerman_remito"])
        self.assertFalse(permissions["page.billing"])


class NexoraDeliveryOrderHelpersTests(SimpleTestCase):
    def test_remito_status_moves_to_pending_billing_only_when_remito_exists(self):
        self.assertEqual(remito_status(""), "armado_pendiente_entrega")
        self.assertEqual(remito_status("RT 0002-00012345"), "entregado_pendiente_facturacion")

    def test_delivery_order_normalizers_accept_only_supported_values(self):
        self.assertEqual(normalize_delivery_type("service_release"), "service_release")
        self.assertEqual(normalize_priority("urgente"), "urgente")


class PortalImportDryRunHelpersTests(SimpleTestCase):
    def test_import_dry_run_normalizers_preserve_valid_portal_values(self):
        self.assertEqual(_status("facturado"), "facturado")
        self.assertEqual(_delivery_type("rental"), "rental")
        self.assertEqual(_priority("urgente"), "urgente")

    def test_import_dry_run_normalizers_fallback_for_unknown_values(self):
        self.assertEqual(_status("legacy"), "pendiente_armado")
        self.assertEqual(_delivery_type("legacy"), "sale")
        self.assertEqual(_priority("legacy"), "normal")
