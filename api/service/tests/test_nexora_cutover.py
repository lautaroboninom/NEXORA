from django.test import SimpleTestCase
from django.urls import resolve

from service.delivery_orders import normalize_delivery_type, normalize_priority, remito_status
from service.management.commands.import_portal_delivery_orders import _delivery_type, _priority, _status
from service.permission_catalog import get_role_defaults
from service.permission_policy import VIEW_PERMISSION_MATRIX
from service.permissions import MappedPermissionGuard
from service.views.devices_views import DeviceDirectCreateView, DevicesListView
from service.views.delivery_orders_views import (
    DeliveryOrderBejermanArticleStockView,
    DeliveryOrderBejermanArticlesView,
    DeliveryOrderBejermanDepositsView,
    DeliveryOrderBejermanRemitoPrintView,
    DeliveryOrderBejermanRemitoView,
    DeliveryOrderInvoicePdfView,
    FacturacionCompanyOptionsView,
    ServiceOrderBillingInvoiceView,
    ServiceOrderBillingListView,
    ServiceOrderBillingPdfView,
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
        self.assertTrue(permissions["page.bejerman_purchase_entries"])
        self.assertTrue(permissions["action.ingreso.create"])
        self.assertTrue(permissions["action.ingreso.fix_ris_preflight"])
        self.assertTrue(permissions["action.bejerman_purchase_entries.manage"])
        self.assertTrue(permissions["action.bejerman_purchase_entries.emit"])
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
        self.assertTrue(permissions["action.billing.register_os_invoice"])
        self.assertTrue(permissions["action.delivery_order.invoice"])
        self.assertFalse(permissions["action.ingreso.create"])
        self.assertFalse(permissions["action.delivery_order.generate_bejerman_remito"])

    def test_admin_no_recibe_permisos_comerciales_de_entrega_por_defecto(self):
        permissions = get_role_defaults("admin")

        self.assertTrue(permissions["page.recepcion"])
        self.assertFalse(permissions["page.delivery_orders"])
        self.assertFalse(permissions["action.delivery_order.create"])
        self.assertFalse(permissions["action.delivery_order.generate_bejerman_remito"])
        self.assertFalse(permissions["action.delivery_order.assign_articles"])
        self.assertTrue(permissions["action.delivery_order.prepare"])
        self.assertTrue(permissions["action.delivery_order.deliver"])
        self.assertTrue(permissions["action.delivery_order.cancel"])
        self.assertTrue(permissions["action.delivery_order.update_remito_location"])
        self.assertFalse(permissions["page.billing"])

    def test_ventas_conserva_permisos_comerciales_de_admin(self):
        permissions = get_role_defaults("ventas")

        self.assertTrue(permissions["page.recepcion"])
        self.assertTrue(permissions["page.delivery_orders"])
        self.assertTrue(permissions["action.delivery_order.create"])
        self.assertTrue(permissions["action.delivery_order.generate_bejerman_remito"])
        self.assertTrue(permissions["action.delivery_order.assign_articles"])
        self.assertTrue(permissions["action.delivery_order.prepare"])
        self.assertTrue(permissions["action.delivery_order.deliver"])
        self.assertTrue(permissions["action.delivery_order.cancel"])
        self.assertTrue(permissions["action.delivery_order.update_remito_location"])
        self.assertFalse(permissions["page.billing"])

    def test_admin_can_view_devices_and_create_direct_devices_by_default(self):
        permissions = get_role_defaults("admin")

        self.assertTrue(permissions["page.devices_preventivos"])
        self.assertTrue(permissions["action.devices_preventivos.manage"])
        self.assertEqual(VIEW_PERMISSION_MATRIX["DevicesListView"]["GET"], "page.devices_preventivos")
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["DeviceDirectCreateView"]["POST"],
            "action.devices_preventivos.manage",
        )
        self.assertIn(MappedPermissionGuard, DevicesListView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeviceDirectCreateView.permission_classes)

    def test_tecnico_does_not_get_administrative_delivery_workspaces(self):
        permissions = get_role_defaults("tecnico")

        self.assertFalse(permissions["page.delivery_orders"])
        self.assertFalse(permissions["page.billing"])
        self.assertFalse(permissions["action.delivery_order.generate_bejerman_remito"])

    def test_customer_lookup_allows_delivery_order_creation(self):
        required = VIEW_PERMISSION_MATRIX["CustomersListView"]["GET"]

        self.assertIn("action.delivery_order.create", required)

    def test_ris_preflight_fix_views_use_scoped_permission(self):
        customer_fix = VIEW_PERMISSION_MATRIX["IngresoRisPreflightCustomerFixView"]["POST"]
        article_fix = VIEW_PERMISSION_MATRIX["IngresoRisPreflightArticleFixView"]["POST"]
        mapping_get = VIEW_PERMISSION_MATRIX["BejermanArticleMappingsView"]["GET"]
        mapping_post = VIEW_PERMISSION_MATRIX["BejermanArticleMappingsView"]["POST"]
        article_search = VIEW_PERMISSION_MATRIX["BejermanArticlesView"]["GET"]

        self.assertIn("action.ingreso.fix_ris_preflight", customer_fix)
        self.assertIn("action.bejerman_sync.manage", customer_fix)
        self.assertEqual(customer_fix, article_fix)
        self.assertEqual(mapping_get, customer_fix)
        self.assertEqual(mapping_post, customer_fix)
        self.assertEqual(article_search, customer_fix)

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
        deposits_match = resolve("/api/ordenes-entrega/bejerman-depositos/")
        stock_match = resolve("/api/ordenes-entrega/bejerman-articulos-stock/")
        print_match = resolve("/api/ordenes-entrega/remito-bejerman/brg-test/print/")
        invoice_match = resolve("/api/ordenes-entrega/do-test/factura/pdf/")
        os_billing_match = resolve("/api/cobranzas/os-a-facturar/")
        os_billing_pdf_match = resolve("/api/cobranzas/os-a-facturar/123/pdf/")

        self.assertEqual(article_match.func.view_class.__name__, "DeliveryOrderBejermanArticlesView")
        self.assertEqual(deposits_match.func.view_class.__name__, "DeliveryOrderBejermanDepositsView")
        self.assertEqual(stock_match.func.view_class.__name__, "DeliveryOrderBejermanArticleStockView")
        self.assertEqual(print_match.func.view_class.__name__, "DeliveryOrderBejermanRemitoPrintView")
        self.assertEqual(invoice_match.func.view_class.__name__, "DeliveryOrderInvoicePdfView")
        self.assertEqual(os_billing_match.func.view_class.__name__, "ServiceOrderBillingListView")
        self.assertEqual(os_billing_pdf_match.func.view_class.__name__, "ServiceOrderBillingPdfView")

    def test_migrated_bejerman_views_use_mapped_permissions(self):
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanRemitoView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanArticlesView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanDepositsView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanArticleStockView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderBejermanRemitoPrintView.permission_classes)
        self.assertIn(MappedPermissionGuard, DeliveryOrderInvoicePdfView.permission_classes)
        self.assertIn(MappedPermissionGuard, FacturacionCompanyOptionsView.permission_classes)
        self.assertIn(MappedPermissionGuard, ServiceOrderBillingListView.permission_classes)
        self.assertIn(MappedPermissionGuard, ServiceOrderBillingInvoiceView.permission_classes)
        self.assertIn(MappedPermissionGuard, ServiceOrderBillingPdfView.permission_classes)
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["DeliveryOrderBejermanArticleStockView"]["GET"],
            "action.delivery_order.assign_articles",
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["DeliveryOrderBejermanDepositsView"]["GET"],
            "action.delivery_order.assign_articles",
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["DeliveryOrderInvoicePdfView"]["GET"],
            "page.delivery_orders",
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["ServiceOrderBillingListView"]["GET"],
            "action.billing.view",
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["ServiceOrderBillingInvoiceView"]["POST"],
            "action.billing.register_os_invoice",
        )
        self.assertEqual(
            VIEW_PERMISSION_MATRIX["ServiceOrderBillingPdfView"]["GET"],
            "action.billing.view",
        )

    def test_remito_status_moves_to_pending_billing_only_when_remito_exists(self):
        self.assertEqual(remito_status(""), "armado_pendiente_entrega")
        self.assertEqual(remito_status("RT 0002-00012345"), "entregado_pendiente_facturacion")
        for remito_number in (
            "RSS R 00004-00001234",
            "RTN 0002-00012345",
            "RTA R 00004-00004567",
            "RDA R 00001-00004571",
            "RDN R 00004-00004573",
        ):
            with self.subTest(remito_number=remito_number):
                self.assertEqual(remito_status(remito_number), "entregado_no_facturable")

    def test_delivery_order_normalizers_accept_only_supported_values(self):
        self.assertEqual(normalize_delivery_type("service_release"), "service_release")
        self.assertEqual(normalize_delivery_type("demo"), "demo")
        self.assertEqual(normalize_priority("urgente"), "urgente")


class PortalImportDryRunHelpersTests(SimpleTestCase):
    def test_import_dry_run_normalizers_preserve_valid_portal_values(self):
        self.assertEqual(_status("facturado"), "facturado")
        self.assertEqual(_status("entregado_no_facturable"), "entregado_no_facturable")
        self.assertEqual(_delivery_type("rental"), "rental")
        self.assertEqual(_delivery_type("demo"), "demo")
        self.assertEqual(_priority("urgente"), "urgente")

    def test_import_dry_run_normalizers_fallback_for_unknown_values(self):
        self.assertEqual(_status("legacy"), "pendiente_armado")
        self.assertEqual(_delivery_type("legacy"), "sale")
        self.assertEqual(_priority("legacy"), "normal")
