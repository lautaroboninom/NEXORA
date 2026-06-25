# service/urls.py
from django.urls import path, include
from .views import (
    # salud / login
    ping, LoginView, LogoutView, SessionView, BejermanCredentialsView, BejermanSellerCodeView, ForgotPasswordView, ResetPasswordView,

    # flujo ingresos / ténico
    MisPendientesView,
    EmitirPresupuestoView, AprobarPresupuestoView, QuotePdfView,
    NoAplicaPresupuestoView, QuitarNoAplicaPresupuestoView, RechazarPresupuestoView, QuoteVersionesView,
    PendientesPresupuestoView, PresupuestadosView, PresupuestadosExportView,
    MarcarReparadoView, MarcarParaRepararView, MarcarControladoSinDefectoView, EntregarIngresoView, GarantiaReparacionCheckView, GarantiaFabricaCheckView,
    HabilitarReparacionCotizacionView,
    DarBajaIngresoView, DarAltaIngresoView,
    IngresoConvertirPropioMgView,
    IngresoCorreccionesHistoricasView,
    ListosParaRetiroView,
    ScanLookupView,

    # listados / generales
    CustomersListView, PendientesGeneralView,
    AprobadosParaRepararView, AprobadosYReparadosView, AprobadosView, LiberadosView,
    GeneralEquiposView, GeneralEquiposExportView, GeneralPorClienteView, GeneralPorClienteExportView,

    # ingresos nuevos + derivación
    NuevoIngresoView, DerivarIngresoView, DerivacionesPorIngresoView, DevolverDerivacionView,
    

    # catálogos
    CatalogoMarcasView, CatalogoModelosView, CatalogoVariantesPorMarcaView, CatalogoUbicacionesView,
    CatalogoAccesoriosView, IngresoAccesoriosView, IngresoAccesorioDetailView,
    BuscarAccesorioPorReferenciaView,
    IngresoAlquilerAccesoriosView, IngresoAlquilerAccesorioDetailView,
    CatalogoRepuestosView, RepuestosView, RepuestoDetailView, RepuestosConfigView, RepuestosMovimientosView, RepuestosCompraMovimientoView,
    RepuestosSubrubrosView, RepuestosSubrubroDetailView,
    RepuestosCambiosView,
    RepuestosStockPermisosView, RepuestosStockPermisoDetailView,
    CatalogoMarcasView, CatalogoModelosView,
    ModeloVarianteView,
    # catálogos jerárquico marca/tipo/modelo/variante
    CatalogoTiposView, CatalogoModelosDeTipoView, CatalogoVariantesView, ModeloVariantesView, CatalogoMarcasPorTipoView,
    # Tipos equipo general (ABM)
    TiposEquipoView,  # tipos equipo (sugerencias + ABM)
    
    # ABM tipos-equipo (por marca)
    CatalogoTiposCreateView, CatalogoTipoDetailView,
    CatalogoModelosCreateView, CatalogoModeloDetailView, CatalogoVariantesCreateView, CatalogoVarianteDetailView,

    # administración de usuarios
    UsuariosView, UsuarioActivoView, UsuarioResetPassView, UsuarioRolePermView, UsuarioDeleteView,
    CatalogoRolesView, CatalogoPermisosView, UsuarioPermisosView, UsuarioPermisosResetView, CerrarReparacionView,
    NotificacionesView, NotificacionClickView, NotificacionesReadAllView,
    NotificacionesPushConfigView, NotificacionesPushSubscriptionView,
    NotificacionesConfiguracionView, NotificacionesConfiguracionEmailsView,
    NotificacionesConfiguracionEmailDetailView, UsuarioNotificacionesView,

    # clientes / marcas-modelos / proveedores externos
    ClientesView, ClienteBejermanCandidatesView, ClientesBejermanSyncView, ClienteDeleteView, ClienteMergeView,
    MarcaDeleteView, MarcaDeleteCascadeView, ModelosPorMarcaView, ModeloDeleteView,
    ModelMergeView, MarcaMergeView,
    
    ProveedoresExternosView,

    # detalle de ingreso
    IngresoDetalleView, IngresoAsignarTecnicoView, CatalogoTecnicosView,
    IngresoTestView, IngresoTestPdfView,
    TestProtocolCatalogView, TestProtocolDetailView,
    IngresoSolicitarAsignacionView, IngresoSolicitarBajaView, IngresoSolicitarBajaRechazarView,
    MarcaTecnicoView,MarcaAplicarTecnicoAModelosView,ModeloTecnicoView,
    EquiposDerivadosView,
    IngresoMediaListCreateView, IngresoMediaDetailView, IngresoMediaFileView, IngresoMediaThumbnailView,

    QuoteDetailView, QuoteItemsView, QuoteItemDetailView, QuoteResumenView, AnularPresupuestoView,
    RemitoSalidaPdfView, RemitosSalidaBulkPdfView, RemitoDerivacionPdfView, TiposEquipoView, ModeloTipoEquipoView, IngresoHistorialView,
    MetricasResumenView, MetricasSeriesView, MetricasFinanzasView, MetricasFinanzasLiberadosView, MetricasActividadTecnicosView, MetricasCalibracionView, FeriadosView, MetricasConfigView,
    CatalogoMotivosView,
    WarrantyRulesView, WarrantyRuleDetailView, DevicesMergeView,
    WorkResumenView, WorkObjectivesView, WorkAlertRulesView, GlobalSearchView,
    BejermanIngressCompaniesView, BejermanJobsView, BejermanJobRetryView, BejermanArticleMappingsView, BejermanArticlesView,
    BejermanPurchaseProvidersView, BejermanPurchaseArticlesView, BejermanPurchaseEntriesView,
    BejermanPurchaseEntryDetailView, BejermanPurchaseEntryLinesView, BejermanPurchaseEntryLineDetailView,
    BejermanPurchaseEntryLineScansView, BejermanPurchaseEntryScanDetailView,
    BejermanPurchaseEntryValidateView, BejermanPurchaseEntryEmitView, BejermanPurchaseHistoryView,
    IngresoRisStatusView, IngresoRisPreflightPayloadView, IngresoRisPreflightView,
    IngresoRisPreflightCustomerFixView, IngresoRisPreflightArticleFixView,
    IngresoRisEmitirView, IngresoRisPdfView, IngresoRisPrintView, SerialBarcodePdfView, IngresoBarcodePdfView,
    NuevoIngresoLoteView,
    DeliveryOrdersView, DeliveryOrderDriveSyncView, DeliveryOrderDetailView, DeliveryOrderExitRemitoPdfView, DeliveryOrderPreparedView, DeliveryOrderDeliveredView,
    DeliveryOrderInvoicedView, DeliveryOrderCancelView, DeliveryOrderRemitoLocationView,
    DeliveryOrderItemArticleView, DeliveryOrderItemPartidasView,
    DeliveryOrderBejermanRemitoView, DeliveryOrderBejermanArticlesView, DeliveryOrderBejermanDepositsView, DeliveryOrderBejermanArticleStockView,
    DeliveryOrderRentalEquipmentView,
    DeliveryOrderBejermanRemitoHistoryView, DeliveryOrderBejermanRemitoPdfView, DeliveryOrderBejermanRemitoPrintView,
    DeliveryOrderInvoicePdfView,
    FacturacionCompanyOptionsView, FacturacionClienteDocumentosView, FacturacionDocumentoPdfView,
    CobranzasRemitosView, CobranzasRemitoPdfView,
    ServiceOrderBillingListView, ServiceOrderBillingInvoiceView, ServiceOrderBillingPdfView,
)

from .views.devices_views import (
    DeviceDirectCreateView,
    DeviceIdentificadoresView,
    DevicesListView,
    DeviceMgVentaView,
    DeviceMgReactivarView,
)
from .views.preventivos_views import (
    DevicePreventivoPlanView,
    DevicePreventivoRevisionCreateView,
    DevicePreventivoRepuestosView,
    DevicePreventivoRepuestoDetailView,
    PreventivoAgendaView,
    PreventivoClientesListView,
    CustomerPreventivoPlanView,
    CustomerPreventivoRevisionesView,
    PreventivoRevisionDetailView,
    PreventivoRevisionItemsView,
    PreventivoRevisionItemDetailView,
    PreventivoRevisionCerrarView,
)
from .views.portal_integration_views import (
    PortalInternalBejermanClientUpsertView,
    PortalClienteGeneralView,
    PortalClienteIngresoMediaFileView,
    PortalClienteIngresoSummaryView,
    PortalClienteIngresoTestPdfView,
    PortalClientePresupuestoDecisionView,
    PortalClientePresupuestoPdfView,
    PortalClientePresupuestoSummaryView,
    PortalClientePresupuestosView,
    PortalInternalIngresoMediaFileView,
    PortalInternalIngresoSummaryView,
    PortalInternalIngresoTestPdfView,
    PortalInternalPresupuestosView,
    PortalInternalWorkQueueView,
    PortalInternalWorkSummaryView,
)


urlpatterns = [
    # salud y login

    path("ping/", ping),
    path("auth/login/", LoginView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/session/", SessionView.as_view()),
    path("auth/bejerman-credentials/", BejermanCredentialsView.as_view()),
    path("auth/bejerman-seller-code/", BejermanSellerCodeView.as_view()),
    path("auth/forgot/", ForgotPasswordView.as_view()),
    path("auth/reset/", ResetPasswordView.as_view()),

    # integracion read-only Portal Sepid
    path("integrations/portal/clientes/<int:customer_id>/general/", PortalClienteGeneralView.as_view()),
    path(
        "integrations/portal/clientes/<int:customer_id>/ingresos/<int:ingreso_id>/summary/",
        PortalClienteIngresoSummaryView.as_view(),
    ),
    path(
        "integrations/portal/clientes/<int:customer_id>/ingresos/<int:ingreso_id>/test/pdf/",
        PortalClienteIngresoTestPdfView.as_view(),
    ),
    path(
        "integrations/portal/clientes/<int:customer_id>/ingresos/<int:ingreso_id>/media/<int:media_id>/<str:kind>/",
        PortalClienteIngresoMediaFileView.as_view(),
    ),
    path(
        "integrations/portal/internal/ingresos/<int:ingreso_id>/summary/",
        PortalInternalIngresoSummaryView.as_view(),
    ),
    path(
        "integrations/portal/internal/ingresos/<int:ingreso_id>/test/pdf/",
        PortalInternalIngresoTestPdfView.as_view(),
    ),
    path(
        "integrations/portal/internal/ingresos/<int:ingreso_id>/media/<int:media_id>/<str:kind>/",
        PortalInternalIngresoMediaFileView.as_view(),
    ),
    path(
        "integrations/portal/clientes/<int:customer_id>/presupuestos/",
        PortalClientePresupuestosView.as_view(),
    ),
    path(
        "integrations/portal/clientes/<int:customer_id>/presupuestos/<int:ingreso_id>/summary/",
        PortalClientePresupuestoSummaryView.as_view(),
    ),
    path(
        "integrations/portal/clientes/<int:customer_id>/presupuestos/<int:ingreso_id>/pdf/",
        PortalClientePresupuestoPdfView.as_view(),
    ),
    path(
        "integrations/portal/clientes/<int:customer_id>/presupuestos/<int:ingreso_id>/decision/",
        PortalClientePresupuestoDecisionView.as_view(),
    ),
    path("integrations/portal/internal/presupuestos/", PortalInternalPresupuestosView.as_view()),
    path("integrations/portal/internal/clientes/bejerman-upsert/", PortalInternalBejermanClientUpsertView.as_view()),
    path("integrations/portal/internal/work-summary/", PortalInternalWorkSummaryView.as_view()),
    path("integrations/portal/internal/work-queue/", PortalInternalWorkQueueView.as_view()),
    path("bejerman/ingress-companies/", BejermanIngressCompaniesView.as_view()),
    path("bejerman/jobs/", BejermanJobsView.as_view()),
    path("bejerman/jobs/<int:job_id>/retry/", BejermanJobRetryView.as_view()),
    path("bejerman/articles/", BejermanArticlesView.as_view()),
    path("bejerman/article-mappings/", BejermanArticleMappingsView.as_view()),
    path("bejerman/purchase-providers/", BejermanPurchaseProvidersView.as_view()),
    path("bejerman/purchase-articles/", BejermanPurchaseArticlesView.as_view()),
    path("bejerman/purchase-entries/", BejermanPurchaseEntriesView.as_view()),
    path("bejerman/purchase-entries/historial/", BejermanPurchaseHistoryView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/", BejermanPurchaseEntryDetailView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/lines/", BejermanPurchaseEntryLinesView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/lines/<str:line_id>/", BejermanPurchaseEntryLineDetailView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/lines/<str:line_id>/scans/", BejermanPurchaseEntryLineScansView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/scans/<str:scan_id>/", BejermanPurchaseEntryScanDetailView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/validate/", BejermanPurchaseEntryValidateView.as_view()),
    path("bejerman/purchase-entries/<str:entry_id>/emit/", BejermanPurchaseEntryEmitView.as_view()),
    path("barcodes/serial/", SerialBarcodePdfView.as_view()),

    # NEXORA: órdenes de entrega, remitos y cobranzas.
    path("ordenes-entrega/", DeliveryOrdersView.as_view()),
    path("ordenes-entrega/sincronizar-drive/", DeliveryOrderDriveSyncView.as_view()),
    path("ordenes-entrega/bejerman-articulos/", DeliveryOrderBejermanArticlesView.as_view()),
    path("ordenes-entrega/bejerman-depositos/", DeliveryOrderBejermanDepositsView.as_view()),
    path("ordenes-entrega/bejerman-articulos-stock/", DeliveryOrderBejermanArticleStockView.as_view()),
    path("ordenes-entrega/alquiler/equipos-disponibles/", DeliveryOrderRentalEquipmentView.as_view()),
    path("ordenes-entrega/remito-bejerman/", DeliveryOrderBejermanRemitoView.as_view()),
    path("ordenes-entrega/remito-bejerman/historial/", DeliveryOrderBejermanRemitoHistoryView.as_view()),
    path("ordenes-entrega/remito-bejerman/<str:group_id>/pdf/", DeliveryOrderBejermanRemitoPdfView.as_view()),
    path("ordenes-entrega/remito-bejerman/<str:group_id>/print/", DeliveryOrderBejermanRemitoPrintView.as_view()),
    path("ordenes-entrega/<str:order_id>/remito-salida/", DeliveryOrderExitRemitoPdfView.as_view()),
    path("ordenes-entrega/<str:order_id>/factura/pdf/", DeliveryOrderInvoicePdfView.as_view()),
    path("ordenes-entrega/<str:order_id>/", DeliveryOrderDetailView.as_view()),
    path("ordenes-entrega/<str:order_id>/preparar/", DeliveryOrderPreparedView.as_view()),
    path("ordenes-entrega/<str:order_id>/entregar/", DeliveryOrderDeliveredView.as_view()),
    path("ordenes-entrega/<str:order_id>/facturar/", DeliveryOrderInvoicedView.as_view()),
    path("ordenes-entrega/<str:order_id>/cancelar/", DeliveryOrderCancelView.as_view()),
    path("ordenes-entrega/<str:order_id>/remito-ubicacion/", DeliveryOrderRemitoLocationView.as_view()),
    path("ordenes-entrega/<str:order_id>/items/<str:item_id>/articulo/", DeliveryOrderItemArticleView.as_view()),
    path("ordenes-entrega/<str:order_id>/items/<str:item_id>/partidas/", DeliveryOrderItemPartidasView.as_view()),
    path("cobranzas/facturacion/clientes/", FacturacionCompanyOptionsView.as_view()),
    path("cobranzas/facturacion/documentos/", FacturacionClienteDocumentosView.as_view()),
    path("cobranzas/facturacion/documentos/<str:document_id>/pdf/", FacturacionDocumentoPdfView.as_view()),
    path("cobranzas/remitos/", CobranzasRemitosView.as_view()),
    path("cobranzas/remitos/<str:document_id>/pdf/", CobranzasRemitoPdfView.as_view()),
    path("cobranzas/os-a-facturar/", ServiceOrderBillingListView.as_view()),
    path("cobranzas/os-a-facturar/<int:ingreso_id>/factura/", ServiceOrderBillingInvoiceView.as_view()),
    path("cobranzas/os-a-facturar/<int:ingreso_id>/pdf/", ServiceOrderBillingPdfView.as_view()),

    # ténico / ingresos (acciones)
    path("tecnico/mis-pendientes/", MisPendientesView.as_view()),
    path("ingresos/<int:ingreso_id>/reparar/", MarcarParaRepararView.as_view()),
    path("ingresos/<int:ingreso_id>/habilitar-reparacion/", HabilitarReparacionCotizacionView.as_view()),
    path("ingresos/<int:ingreso_id>/reparado/", MarcarReparadoView.as_view()),
    path("ingresos/<int:ingreso_id>/controlado-sin-defecto/", MarcarControladoSinDefectoView.as_view()),
    path("ingresos/<int:ingreso_id>/entregar/", EntregarIngresoView.as_view()),
    path("ingresos/<int:ingreso_id>/baja/", DarBajaIngresoView.as_view()),
    path("ingresos/<int:ingreso_id>/alta/", DarAltaIngresoView.as_view()),
    path("ingresos/<int:ingreso_id>/convertir-propio-mg/", IngresoConvertirPropioMgView.as_view()),
    path("ingresos/<int:ingreso_id>/correcciones-historicas/", IngresoCorreccionesHistoricasView.as_view()),

    # presupuestos
    path("quotes/<int:ingreso_id>/emitir/", EmitirPresupuestoView.as_view()),
    path("quotes/<int:ingreso_id>/aprobar/", AprobarPresupuestoView.as_view()),
    path("quotes/<int:ingreso_id>/rechazar/", RechazarPresupuestoView.as_view()),
    path("quotes/<int:ingreso_id>/versiones/", QuoteVersionesView.as_view()),
    path("quotes/<int:ingreso_id>/no-aplica/", NoAplicaPresupuestoView.as_view()),
    path("quotes/<int:ingreso_id>/no-aplica/quitar/", QuitarNoAplicaPresupuestoView.as_view()),
    path("presupuestos/pendientes/", PendientesPresupuestoView.as_view()),
    path("ingresos/presupuestados/", PresupuestadosView.as_view()),
    path("ingresos/presupuestados/export/", PresupuestadosExportView.as_view()),

    # listados operativos
    path("clientes/", CustomersListView.as_view()),
    path("trabajo/resumen/", WorkResumenView.as_view()),
    path("trabajo/objetivos/", WorkObjectivesView.as_view()),
    path("trabajo/reglas-alerta/", WorkAlertRulesView.as_view()),
    path("notificaciones/", NotificacionesView.as_view()),
    path("notificaciones/read-all/", NotificacionesReadAllView.as_view()),
    path("notificaciones/configuracion/", NotificacionesConfiguracionView.as_view()),
    path("notificaciones/configuracion/emails/", NotificacionesConfiguracionEmailsView.as_view()),
    path("notificaciones/configuracion/emails/<int:email_id>/", NotificacionesConfiguracionEmailDetailView.as_view()),
    path("notificaciones/push/config/", NotificacionesPushConfigView.as_view()),
    path("notificaciones/push/subscription/", NotificacionesPushSubscriptionView.as_view()),
    path("notificaciones/<int:notification_id>/click/", NotificacionClickView.as_view()),
    path("busqueda/global/", GlobalSearchView.as_view()),
    path("ingresos/pendientes/", PendientesGeneralView.as_view()),
    path("ingresos/aprobados-para-reparar/", AprobadosParaRepararView.as_view()),
    path("ingresos/aprobados-reparados/", AprobadosYReparadosView.as_view()),
    path("ingresos/liberados/", LiberadosView.as_view()),
    path("listos-para-retiro/", ListosParaRetiroView.as_view()),  # alias de compat
    path("scan/lookup/", ScanLookupView.as_view()),

    # ALIAS de compatibilidad con el front (si existian)
    path("ingresos/aprobados/", AprobadosView.as_view()),
    path("ingresos/reparados/", AprobadosYReparadosView.as_view()),
    path("ingresos/pendientes-presupuesto/", PendientesPresupuestoView.as_view()),

    # -------- Tabs superiores --------
    # Histórico de ingresos (antes /equipos/)
    path("ingresos/", GeneralEquiposView.as_view()),
    path("ingresos/historico/", GeneralEquiposView.as_view()),
    path("ingresos/historico/export/", GeneralEquiposExportView.as_view()),
    # Equipos (tabla devices)
    path("equipos/", DevicesListView.as_view()),
    path("equipos/<int:device_id>/mg/venta/", DeviceMgVentaView.as_view()),
    path("equipos/<int:device_id>/mg/reactivar/", DeviceMgReactivarView.as_view()),
    path("devices/alta-directa/", DeviceDirectCreateView.as_view()),
    path("equipos/<int:device_id>/preventivo-plan/", DevicePreventivoPlanView.as_view()),
    path("equipos/<int:device_id>/preventivo-revisiones/", DevicePreventivoRevisionCreateView.as_view()),
    path("equipos/<int:device_id>/preventivo-repuestos/", DevicePreventivoRepuestosView.as_view()),
    path("equipos/<int:device_id>/preventivo-repuestos/<int:item_id>/", DevicePreventivoRepuestoDetailView.as_view()),
    path("devices/merge/", DevicesMergeView.as_view()),
    path("preventivos/agenda/", PreventivoAgendaView.as_view()),
    path("preventivos/clientes/", PreventivoClientesListView.as_view()),
    path("clientes/<int:customer_id>/preventivo-plan/", CustomerPreventivoPlanView.as_view()),
    path("clientes/<int:customer_id>/preventivo-revisiones/", CustomerPreventivoRevisionesView.as_view()),
    path("preventivos/revisiones/<int:revision_id>/", PreventivoRevisionDetailView.as_view()),
    path("preventivos/revisiones/<int:revision_id>/items/", PreventivoRevisionItemsView.as_view()),
    path("preventivos/revisiones/<int:revision_id>/items/<int:item_id>/", PreventivoRevisionItemDetailView.as_view()),
    path("preventivos/revisiones/<int:revision_id>/cerrar/", PreventivoRevisionCerrarView.as_view()),
    path("clientes/<int:customer_id>/general/", GeneralPorClienteView.as_view()),
    path("clientes/<int:customer_id>/general/export/", GeneralPorClienteExportView.as_view()),
    # utilidades
    path("equipos/garantia-reparacion/", GarantiaReparacionCheckView.as_view()),
    path("equipos/garantia-fabrica/", GarantiaFabricaCheckView.as_view()),
    # Garantías (excepciones administrables)
    path("garantias/politicas/", WarrantyRulesView.as_view()),  # GET, POST
    path("garantias/politicas/<int:rule_id>/", WarrantyRuleDetailView.as_view()),  # PATCH, DELETE

    # ingresos nuevos / derivación
    path("ingresos/nuevo/", NuevoIngresoView.as_view()),
    path("ingresos/nuevo/lote/", NuevoIngresoLoteView.as_view()),
    path("ingresos/ris/preflight/", IngresoRisPreflightPayloadView.as_view()),
    path("ingresos/ris/preflight/customer-fix/", IngresoRisPreflightCustomerFixView.as_view()),
    path("ingresos/ris/preflight/article-fix/", IngresoRisPreflightArticleFixView.as_view()),
    path("ingresos/<int:ingreso_id>/derivar/", DerivarIngresoView.as_view()),
    path("ingresos/<int:ingreso_id>/derivaciones/", DerivacionesPorIngresoView.as_view()),
    path("ingresos/<int:ingreso_id>/derivaciones/<int:deriv_id>/devolver/", DevolverDerivacionView.as_view()),
    path("ingresos/<int:ingreso_id>/ris/", IngresoRisStatusView.as_view()),
    path("ingresos/<int:ingreso_id>/ris/preflight/", IngresoRisPreflightView.as_view()),
    path("ingresos/<int:ingreso_id>/ris/emitir/", IngresoRisEmitirView.as_view()),
    path("ingresos/<int:ingreso_id>/ris/pdf/", IngresoRisPdfView.as_view()),
    path("ingresos/<int:ingreso_id>/ris/print/", IngresoRisPrintView.as_view()),
    path("ingresos/<int:ingreso_id>/barcode/", IngresoBarcodePdfView.as_view()),

    # catálogos
    path("catalogos/marcas/", CatalogoMarcasView.as_view()),
    path("catalogos/modelos/", CatalogoModelosView.as_view()),                   # ?marca_id=#
    path("catalogos/modelos/<int:modelo_id>/variantes/", ModeloVariantesView.as_view()),
    path("catalogos/ubicaciones/", CatalogoUbicacionesView.as_view()),
    path("catalogos/motivos/", CatalogoMotivosView.as_view()),
    path("catalogos/accesorios/", CatalogoAccesoriosView.as_view()),
    path("catalogos/tests/protocolos/", TestProtocolCatalogView.as_view()),
    path("catalogos/tests/protocolos/<int:protocol_id>/", TestProtocolDetailView.as_view()),
    path("catalogos/repuestos/", CatalogoRepuestosView.as_view()),
    path("repuestos/", RepuestosView.as_view()),
    path("repuestos/subrubros/", RepuestosSubrubrosView.as_view()),
    path("repuestos/subrubros/<str:subrubro_codigo>/", RepuestosSubrubroDetailView.as_view()),
    path("repuestos/config/", RepuestosConfigView.as_view()),
    path("repuestos/movimientos/compra/", RepuestosCompraMovimientoView.as_view()),
    path("repuestos/movimientos/", RepuestosMovimientosView.as_view()),
    path("repuestos/cambios/", RepuestosCambiosView.as_view()),
    path("repuestos/stock-permisos/", RepuestosStockPermisosView.as_view()),
    path("repuestos/stock-permisos/<int:perm_id>/", RepuestosStockPermisoDetailView.as_view()),
    path("repuestos/<int:repuesto_id>/", RepuestoDetailView.as_view()),
    path("catalogos/proveedores-externos/", ProveedoresExternosView.as_view()),
    path("catalogos/proveedores-externos/<int:pid>/", ProveedoresExternosView.as_view()),
    # variante simple por modelo (v1)
    path('catalogos/marcas/<int:marca_id>/modelos/<int:modelo_id>/variante/', ModeloVarianteView.as_view()),
    path("catalogos/marcas/<int:bid>/variantes/", CatalogoVariantesPorMarcaView.as_view()),

    # catálogo jerárquico (marca -> tipo -> modelo -> variante)
    path("catalogo/marcas/", CatalogoMarcasView.as_view()),
    path("catalogo/marcas/<int:bid>/tipos/", CatalogoTiposView.as_view()),
    path("catalogo/marcas/<int:bid>/tipos/<int:tid>/modelos/", CatalogoModelosDeTipoView.as_view()),
    path("catalogo/marcas/<int:bid>/variantes/", CatalogoVariantesPorMarcaView.as_view()),
    path("catalogo/marcas/<int:bid>/modelos/<int:mid>/variantes/", CatalogoVariantesView.as_view()),
    path("catalogo/tipos/<str:tipo_nombre>/marcas/", CatalogoMarcasPorTipoView.as_view()),
    

    # administración de clientes / marcas / modelos
    path("catalogos/clientes/", ClientesView.as_view()),                         # GET/POST
    path("catalogos/clientes/bejerman-candidatos/", ClienteBejermanCandidatesView.as_view()),
    path("catalogos/clientes/sincronizar-bejerman/", ClientesBejermanSyncView.as_view()),
    path("catalogos/clientes/merge/", ClienteMergeView.as_view()),               # POST {source_id,target_id}
    path("catalogos/clientes/<int:cid>/", ClienteDeleteView.as_view()),          # DELETE
    path("catalogos/marcas/<int:bid>/", MarcaDeleteView.as_view()),              # DELETE
    path("catalogos/marcas/<int:bid>/eliminar-con-modelos/", MarcaDeleteCascadeView.as_view()),  # DELETE (cascade)
    path("catalogos/marcas/<int:bid>/modelos/", ModelosPorMarcaView.as_view()), # GET/POST
    path("catalogos/modelos/<int:mid>/", ModeloDeleteView.as_view()),            # DELETE
    path("catalogos/marcas/merge/", MarcaMergeView.as_view()),                   # POST {source_id,target_id}
    path("catalogos/modelos/merge/", ModelMergeView.as_view()),                  # POST {source_id,target_id}

    # detalle de ingreso (GET, PATCH)
    path("ingresos/<int:ingreso_id>/", IngresoDetalleView.as_view()),
    path("ingresos/<int:ingreso_id>/test/", IngresoTestView.as_view()),
    path("ingresos/<int:ingreso_id>/test/pdf/", IngresoTestPdfView.as_view()),
    path("ingresos/<int:ingreso_id>/solicitar-asignacion/", IngresoSolicitarAsignacionView.as_view()),
    path("ingresos/<int:ingreso_id>/solicitar-baja/", IngresoSolicitarBajaView.as_view()),
    path("ingresos/<int:ingreso_id>/solicitar-baja/rechazar/", IngresoSolicitarBajaRechazarView.as_view()),
    # accesorios por ingreso
    path("ingresos/<int:ingreso_id>/accesorios/", IngresoAccesoriosView.as_view()),
    path("ingresos/<int:ingreso_id>/accesorios/<int:item_id>/", IngresoAccesorioDetailView.as_view()),
    # accesorios por ingreso (alquiler)
    path("ingresos/<int:ingreso_id>/alquiler/accesorios/", IngresoAlquilerAccesoriosView.as_view()),
    path("ingresos/<int:ingreso_id>/alquiler/accesorios/<int:item_id>/", IngresoAlquilerAccesorioDetailView.as_view()),
    path("accesorios/buscar/", BuscarAccesorioPorReferenciaView.as_view()),
    path("ingresos/<int:ingreso_id>/fotos/", IngresoMediaListCreateView.as_view()),
    path("ingresos/<int:ingreso_id>/fotos/<int:media_id>/", IngresoMediaDetailView.as_view()),
    path("ingresos/<int:ingreso_id>/fotos/<int:media_id>/archivo/", IngresoMediaFileView.as_view()),
    path("ingresos/<int:ingreso_id>/fotos/<int:media_id>/miniatura/", IngresoMediaThumbnailView.as_view()),


    # usuarios (class-based)
    path("usuarios/", UsuariosView.as_view()),                                   # GET lista, POST upsert
    path("usuarios/<int:uid>/activar/", UsuarioActivoView.as_view()),            # PATCH {activo}
    path("usuarios/<int:uid>/reset-pass/", UsuarioResetPassView.as_view()),      # PATCH {password}
    path("usuarios/<int:uid>/roleperm/", UsuarioRolePermView.as_view()),         # PATCH {rol}
    path("usuarios/<int:uid>/", UsuarioDeleteView.as_view()),                    # DELETE
    path("catalogos/roles/", CatalogoRolesView.as_view()),
    path("permisos/catalogo/", CatalogoPermisosView.as_view()),
    path("usuarios/<int:uid>/permisos/", UsuarioPermisosView.as_view()),
    path("usuarios/<int:uid>/permisos/reset/", UsuarioPermisosResetView.as_view()),
    path("usuarios/<int:uid>/notificaciones/", UsuarioNotificacionesView.as_view()),
    path("ingresos/<int:ingreso_id>/asignar-tecnico/", IngresoAsignarTecnicoView.as_view()),
    path("catalogos/tecnicos/", CatalogoTecnicosView.as_view()),



    # (si usas los endpoints para asignar ténico y setear ténico de marca/modelo)
    path('catalogos/marcas/<int:bid>/tecnico/', MarcaTecnicoView.as_view()),
    path('catalogos/marcas/<int:bid>/tecnico/aplicar-a-modelos/', MarcaAplicarTecnicoAModelosView.as_view()),
    path('catalogos/marcas/<int:bid>/modelos/<int:mid>/tecnico/', ModeloTecnicoView.as_view()),

    path("ingresos/derivados/", EquiposDerivadosView.as_view()),

    path("quotes/<int:ingreso_id>/", QuoteDetailView.as_view()),  # GET
    path("quotes/<int:ingreso_id>/items/", QuoteItemsView.as_view()),  # POST
    path("quotes/<int:ingreso_id>/items/<int:item_id>/", QuoteItemDetailView.as_view()),  # PATCH/DELETE
    path("quotes/<int:ingreso_id>/resumen/", QuoteResumenView.as_view()),  # PATCH {mano_obra}
    path("quotes/<int:ingreso_id>/pdf/", QuotePdfView.as_view()),
    path("quotes/<int:ingreso_id>/anular/", AnularPresupuestoView.as_view()),

    # ténico / ingresos (acciones)


    path("ingresos/remitos-salida/", RemitosSalidaBulkPdfView.as_view()),
    path("ingresos/<int:ingreso_id>/remito/", RemitoSalidaPdfView.as_view()),   # remito de salida (nuevo)
    path("ingresos/<int:ingreso_id>/derivaciones/<int:deriv_id>/remito/", RemitoDerivacionPdfView.as_view()),  # remito derivación
    path("ingresos/<int:ingreso_id>/cerrar/", CerrarReparacionView.as_view()),

    # tipos de equipo
    # tipos de equipo
    # - listado general (sugerencias para asignacion): plural "catalogos"
    # - listado general (sugerencias): plural "catalogos"
    path("catalogos/tipos-equipo/", TiposEquipoView.as_view()),
    # - ABM catálogos general (no por marca)
    # - ABM por marca (tabla marca_tipos_equipo)
    path("catalogo/tipos-equipo/<int:tipo_id>/", CatalogoTipoDetailView.as_view()),
    # - ABM de series/modelos y variantes
    path("catalogo/modelos/", CatalogoModelosCreateView.as_view()),
    path("catalogo/modelos/<int:serie_id>/", CatalogoModeloDetailView.as_view()),
    path("catalogo/variantes/", CatalogoVariantesCreateView.as_view()),
    path("catalogo/variantes/<int:variante_id>/", CatalogoVarianteDetailView.as_view()),
    # asignacion de tipo de equipo al modelo (alias singular/plural por compat)
    path("catalogos/marcas/<int:marca_id>/modelos/<int:modelo_id>/tipo-equipo/", ModeloTipoEquipoView.as_view()),
    path("catalogo/marcas/<int:marca_id>/modelos/<int:modelo_id>/tipo-equipo/", ModeloTipoEquipoView.as_view()),



    # historial de cambios por ingreso
    path("ingresos/<int:ingreso_id>/historial/", IngresoHistorialView.as_view()),
    # historial de cambios por ingreso
    # metricas
    path("metricas/resumen/", MetricasResumenView.as_view()),
    path("metricas/series/", MetricasSeriesView.as_view()),
    path("metricas/finanzas/", MetricasFinanzasView.as_view()),
    path("metricas/finanzas/liberados/", MetricasFinanzasLiberadosView.as_view()),
    path("metricas/actividad-tecnicos/", MetricasActividadTecnicosView.as_view()),
    path("metricas/calibracion/", MetricasCalibracionView.as_view()),
    path("metricas/feriados/", FeriadosView.as_view()),
    path("metricas/config/", MetricasConfigView.as_view()),

    # devices: corrección de identificadores (NS / MG)
    path("devices/<int:device_id>/identificadores/", DeviceIdentificadoresView.as_view()),
]
