from django.test import SimpleTestCase
from django.urls import resolve

from service.delivery_orders import normalize_delivery_type, normalize_priority, remito_status
from service.management.commands.import_portal_delivery_orders import _delivery_type, _priority, _status
from service.permission_catalog import get_role_defaults
from service.permission_policy import VIEW_PERMISSION_MATRIX
from service.permissions import MappedPermissionGuard
from service.views.delivery_orders_views import (
    DeliveryOrderBejermanArticlesView,
    DeliveryOrderBejermanRemitoPrintView,
    DeliveryOrderBejermanRemitoView,
    FacturacionCompanyOptionsView,
)


class NexoraRoleDefaultsTests(SimpleTestCase):
    def test_recepcion_sees_reception_and_delivery_without_billing(self):
        permissions = get_role_defaults("recepcion")

        self.assertTrue(permissions["page.recepcion"])
        self.assertTrue(permissions["page.home_search"])
        self.assertTrue(permissions["page.general_cliente"])
        self.assertTrue(permissions["page.logistics"])
        self.assertTrue(permissions["page.service_sheet_principal"])
        self.assertTrue(permissions["page.delivery_orders"])
        self.assertTrue(permissions["action.ingreso.create"])
        self.assertTrue(permissions["action.delivery_order.create"])
        self.assertTrue(permissions["action.delivery_order.prepare"])
        self.assertTrue(permissions["action.delivery_order.deliver"])
        self.assertTrue(permissions["action.delivery_order.cancel"])
        self.assertTrue(permissions["action.delivery_order.update_remito_location"])
        self.assertTrue(permissions["action.delivery_order.generate_bejerman_remito"])
        self.assertTrue(permissions["action.delivery_order.assign_articles"])
        self.assertFalse(permissions["page.ingresos_history"])
        self.assertFalse(permissions["page.liberados"])
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

    def test_tecnico_does_not_get_administrative_delivery_workspaces(self):
        permissions = get_role_defaults("tecnico")

        self.assertFalse(permissions["page.delivery_orders"])
        self.assertFalse(permissions["page.billing"])
        self.assertFalse(permissions["action.delivery_order.generate_bejerman_remito"])

    def test_customer_lookup_allows_delivery_order_creation(self):
        required = VIEW_PERMISSION_MATRIX["CustomersListView"]["GET"]

        self.assertIn("action.delivery_order.create", required)

    def test_general_por_cliente_accepts_dedicated_permission_and_history(self):
        required = VIEW_PERMISSION_MATRIX["GeneralPorClienteView"]["GET"]

        self.assertIn("page.general_cliente", required)
        self.assertIn("page.ingresos_history", required)


class NexoraDeliveryOrderHelpersTests(SimpleTestCase):
    def test_bejerman_remito_route_resolves_before_dynamic_order_detail(self):
        match = resolve("/api/ordenes-entrega/remito-bejerman/")

        self.assertEqual(match.func.view_class.__name__, "DeliveryOrderBejermanRemitoView")

    def test_bejerman_article_and_print_routes_resolve_before_dynamic_order_detail(self):
        article_match = resolve("/api/ordenes-entrega/bejerman-articulos/")
        print_match = resolve("/api/ordenes-entrega/remito-bejerman/brg-test/print/")

        self.assertEqual(article_match.func.view_class.__name__, "DeliveryOrderBejermanArticlesView")
        self.assertEqual(print_match.func.view_class.__name__, "DeliveryOrderBejermanRemitoPrintView")

    def test_migrated_bejerman_views_use_mapped_permissions(self):
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanRemitoView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanArticlesView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanRemitoPrintView.permission_classes)
        self.assertIn(MappedPermissionGuard, FacturacionCompanyOptionsView.permission_classes)

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
