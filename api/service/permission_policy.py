"""Request -> permission policy matrix for PERMISSIONS_V2."""


# Mapping by DRF view class name and HTTP method.
# Methods omitted in a class do not enforce extra permission in this layer.
# Value may be:
# - string: single required permission
# - list[str]: OR semantics (any permission grants access)
VIEW_PERMISSION_MATRIX = {
    "TechnicalInterventionView": {"GET": ["action.ingreso.edit_diagnosis", "action.presupuesto.manage"], "PATCH": "action.ingreso.edit_diagnosis"},
    "TechnicalSubmitView": {"POST": ["action.ingreso.edit_diagnosis", "action.presupuesto.manage"]},
    "TechnicalPartsView": {"GET": ["action.ingreso.edit_diagnosis", "action.presupuesto.manage"]},
    "TechnicalWorkCatalogView": {"GET": ["action.ingreso.edit_diagnosis", "action.presupuesto.manage"], "POST": "action.presupuesto.manage"},
    "TechnicalWorkCatalogDetailView": {"PATCH": "action.presupuesto.manage"},
    "TechnicalRatesView": {"GET": "action.presupuesto.manage", "PUT": "action.presupuesto.manage"},
    "TechnicalLaborReviewView": {"PATCH": "action.presupuesto.manage"},
    "TechnicalCatalogRequestsView": {"GET": "action.presupuesto.manage", "POST": ["action.ingreso.edit_diagnosis", "action.presupuesto.manage"]},
    "TechnicalCatalogRequestDetailView": {"PATCH": "action.presupuesto.manage"},
    # Ingresos / operacion
    "MisPendientesView": {"GET": "page.work_queues"},
    # Autorizar la reparación es decisión de jefatura. Con `repair_transitions` el técnico
    # podía autorizarse su propio equipo por API aunque la pantalla no le mostrara el botón.
    "MarcarParaRepararView": {"POST": "action.ingreso.authorize_repair"},
    "QuitarRepararView": {"POST": "action.ingreso.authorize_repair"},
    "HabilitarReparacionCotizacionView": {"POST": "action.ingreso.authorize_repair"},
    "MarcarReparadoView": {"POST": "action.ingreso.repair_transitions"},
    "MarcarControladoSinDefectoView": {"POST": "action.ingreso.repair_transitions"},
    "MarcarNoSeReparaView": {"POST": "action.ingreso.repair_transitions"},
    "QuitarEstadoFinalReparacionView": {"POST": "action.ingreso.repair_transitions"},
    "EntregarIngresoView": {"POST": "action.ingreso.edit_delivery"},
    "DarBajaIngresoView": {"POST": "action.ingreso.baja_alta"},
    "DarAltaIngresoView": {"POST": "action.ingreso.baja_alta"},
    "IngresoConvertirPropioMgView": {"POST": ["action.devices_preventivos.manage", "action.ingreso.baja_alta"]},
    "IngresoCambioEquipoView": {
        "GET": "action.ingreso.device_swap",
        "POST": "action.ingreso.device_swap",
        "DELETE": "action.ingreso.device_swap",
    },
    "IngresoCambioEquipoCandidatosView": {"GET": "action.ingreso.device_swap"},
    "IngresoCorreccionesHistoricasView": {"POST": "action.ingreso.force_historical"},
    "IngresoConflictoEvidenciaView": {"POST": "action.ingreso.resolve_open_conflict"},
    # El resolver chequea el permiso adentro: con prueba en Bejerman alcanza
    # resolve_open_conflict, sin prueba exige force_historical.
    "IngresoConflictoResolverView": {"POST": "action.ingreso.resolve_open_conflict"},
    "PendientesPresupuestoView": {"GET": "page.budget_queues"},
    "PresupuestadosView": {"GET": "page.budget_queues"},
    "PresupuestadosExportView": {"GET": "page.budget_queues"},
    "PendientesGeneralView": {"GET": "page.work_queues"},
    "AprobadosParaRepararView": {"GET": "page.work_queues"},
    "AprobadosYReparadosView": {"GET": "page.work_queues"},
    "AprobadosView": {"GET": "page.work_queues"},
    "LiberadosView": {"GET": "page.liberados"},
    "ListosParaRetiroView": {"GET": "page.liberados"},
    "GeneralEquiposView": {"GET": "page.ingresos_history"},
    "GeneralEquiposExportView": {"GET": "page.ingresos_history"},
    "IngresoAsignarTecnicoView": {"PATCH": "action.ingreso.change_assignment"},
    "IngresoSolicitarAsignacionView": {"POST": "action.ingreso.edit_diagnosis"},
    "IngresoSolicitarBajaView": {
        "POST": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_basics",
            "action.ingreso.edit_diagnosis",
            "action.ingreso.edit_location",
            "action.ingreso.edit_delivery",
            "action.ingreso.manage_derivations",
            "action.ingreso.repair_transitions",
            "action.presupuesto.manage",
        ]
    },
    "IngresoSolicitarBajaRechazarView": {"POST": "action.ingreso.baja_alta"},
    "IngresoHistorialView": {"GET": "page.ingresos_history"},
    "CerrarReparacionView": {"POST": "action.ingreso.repair_transitions"},
    "NuevoIngresoView": {"POST": ["action.ingreso.create", "page.new_ingreso"]},
    "IngresoDetalleView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "page.liberados",
            "page.service_sheet_principal",
            "action.ingreso.edit_basics",
            "action.ingreso.edit_diagnosis",
            "action.ingreso.edit_location",
            "action.ingreso.edit_delivery",
            "action.ingreso.manage_derivations",
            "action.ingreso.repair_transitions",
            "action.ingreso.baja_alta",
            "action.presupuesto.manage",
        ]
    },
    "IngresoBejermanEstadoView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "page.liberados",
            "page.service_sheet_principal",
            "action.ingreso.edit_basics",
            "action.ingreso.edit_diagnosis",
            "action.ingreso.edit_location",
            "action.ingreso.edit_delivery",
            "action.ingreso.manage_derivations",
            "action.ingreso.repair_transitions",
            "action.ingreso.baja_alta",
            "action.presupuesto.manage",
        ]
    },
    "IngresoAlquilerRemitoView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "page.liberados",
            "page.service_sheet_principal",
            "action.ingreso.edit_basics",
            "action.ingreso.edit_diagnosis",
            "action.ingreso.edit_location",
            "action.ingreso.edit_delivery",
            "action.ingreso.manage_derivations",
            "action.ingreso.repair_transitions",
            "action.ingreso.baja_alta",
            "action.presupuesto.manage",
        ]
    },
    "IngresoAlquilerRemitoAccionView": {
        "POST": ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"]
    },
    "IngresoTestView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_diagnosis",
        ],
        "PATCH": "action.ingreso.edit_diagnosis",
    },
    "IngresoTestPdfView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_diagnosis",
        ]
    },
    "GarantiaReparacionCheckView": {
        "GET": ["page.ingresos_history", "action.ingreso.create", "page.new_ingreso"]
    },
    "GarantiaFabricaCheckView": {
        "GET": ["page.ingresos_history", "action.ingreso.create", "page.new_ingreso"]
    },
    "DerivarIngresoView": {"POST": "action.ingreso.manage_derivations"},
    "DerivacionesPorIngresoView": {"GET": ["page.logistics", "action.ingreso.manage_derivations"]},
    "DevolverDerivacionView": {"POST": "action.ingreso.manage_derivations"},
    "EquiposDerivadosView": {"GET": "page.logistics"},
    "ScanLookupView": {"GET": ["page.home_search", "action.ingreso.create", "page.new_ingreso"]},
    "CustomersListView": {
        "GET": [
            "page.home_search",
            "action.ingreso.edit_basics",
            "action.ingreso.create",
            "page.new_ingreso",
            "action.delivery_order.create",
        ]
    },
    # Reportes
    "RemitoSalidaPdfView": {"GET": "action.ingreso.print_exit_order"},
    "RemitosSalidaBulkPdfView": {"GET": "action.ingreso.print_exit_order"},
    "RemitoDerivacionPdfView": {"GET": "action.ingreso.manage_derivations"},
    "IngresoRisStatusView": {"GET": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso", "page.service_sheet_principal"]},
    "IngresoRisPreflightPayloadView": {"POST": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso", "page.service_sheet_principal"]},
    "IngresoRisPreflightView": {"POST": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso", "page.service_sheet_principal"]},
    "IngresoRisPreflightCustomerFixView": {"POST": ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"]},
    "IngresoRisPreflightArticleFixView": {"POST": ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"]},
    "IngresoRisEmitirView": {"POST": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"]},
    "SerialBarcodePdfView": {"GET": ["action.ingreso.print_barcode", "action.ingreso.create", "page.new_ingreso", "action.devices_preventivos.manage"]},
    "IngresoBarcodePdfView": {"GET": ["action.ingreso.print_barcode", "action.devices_preventivos.manage", "page.service_sheet_principal"]},
    "BejermanJobsView": {"GET": "page.bejerman_sync"},
    "BejermanPdfOutputSettingsView": {"GET": "page.bejerman_sync", "PUT": "action.bejerman_sync.manage"},
    "BejermanJobRetryView": {"POST": "action.bejerman_sync.manage"},
    "BejermanRemitoProcessesView": {"GET": "page.bejerman_sync"},
    "BejermanRemitoProcessRetryView": {"POST": "action.bejerman_sync.manage"},
    "BejermanArticleMappingsView": {
        "GET": ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"],
        "POST": ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"],
    },
    "BejermanArticlesView": {"GET": ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"]},
    "BejermanPurchaseProvidersView": {"GET": "page.bejerman_purchase_entries"},
    "BejermanPurchaseOrdersView": {"GET": "page.bejerman_purchase_entries"},
    "BejermanPurchaseArticlesView": {"GET": "page.bejerman_purchase_entries"},
    "BejermanPurchaseEntriesView": {
        "GET": "page.bejerman_purchase_entries",
        "POST": "action.bejerman_purchase_entries.manage",
    },
    "BejermanPurchaseEntryDetailView": {
        "GET": "page.bejerman_purchase_entries",
        "PATCH": "action.bejerman_purchase_entries.manage",
        "DELETE": "action.bejerman_purchase_entries.manage",
    },
    "BejermanPurchaseEntryLinesView": {"POST": "action.bejerman_purchase_entries.manage"},
    "BejermanPurchaseEntryLineDetailView": {
        "PATCH": "action.bejerman_purchase_entries.manage",
        "DELETE": "action.bejerman_purchase_entries.manage",
    },
    "BejermanPurchaseEntryLineScansView": {"POST": "action.bejerman_purchase_entries.manage"},
    "BejermanPurchaseEntryScanDetailView": {
        "PATCH": "action.bejerman_purchase_entries.manage",
        "DELETE": "action.bejerman_purchase_entries.manage",
    },
    "BejermanPurchaseEntryValidateView": {
        "POST": [
            "action.bejerman_purchase_entries.manage",
            "action.bejerman_purchase_entries.emit",
        ]
    },
    "BejermanPurchaseEntryEmitView": {"POST": "action.bejerman_purchase_entries.emit"},
    "BejermanPurchaseEntryVerifyView": {"POST": "action.bejerman_purchase_entries.emit"},
    "BejermanPurchaseEntryReopenView": {"POST": "action.bejerman_purchase_entries.emit"},
    "BejermanPurchaseHistoryView": {"GET": "page.bejerman_purchase_entries"},
    # NEXORA / órdenes de entrega y cobranzas
    "DeliveryOrdersView": {
        "GET": "page.delivery_orders",
        "POST": "action.delivery_order.create",
    },
    "DeliveryOrderDetailView": {"GET": "page.delivery_orders", "PATCH": "action.delivery_order.assign_articles"},
    "DeliveryOrderExitRemitoPdfView": {"GET": "page.delivery_orders"},
    "DeliveryOrderPreparedView": {"POST": "action.delivery_order.prepare"},
    "DeliveryOrderRemitosView": {"POST": "action.delivery_order.prepare"},
    "DeliveryOrderDeliveredView": {"POST": "action.delivery_order.deliver"},
    "DeliveryOrderInvoicedView": {"POST": "action.delivery_order.invoice"},
    "DeliveryOrdersBatchInvoicedView": {"POST": "action.delivery_order.invoice"},
    "DeliveryOrderNotBillableView": {"POST": "action.delivery_order.invoice"},
    "DeliveryOrderBillingPostponementView": {"POST": "action.delivery_order.invoice"},
    "DeliveryOrderBillingReactivationView": {"POST": "action.delivery_order.invoice"},
    "DeliveryOrderCancelView": {"POST": "action.delivery_order.cancel"},
    "DeliveryOrderRemitoLocationView": {"PATCH": "action.delivery_order.update_remito_location"},
    # Cambiar la urgencia es editar la orden: el mismo permiso que "Editar orden".
    "DeliveryOrderPriorityView": {"PATCH": "action.delivery_order.assign_articles"},
    "DeliveryOrderCommercialCorrectionView": {"PATCH": "action.delivery_order.correct_commercial_terms"},
    "DeliveryOrderItemArticleView": {"PATCH": "action.delivery_order.assign_articles"},
    "DeliveryOrderItemPartidasView": {"PATCH": "action.delivery_order.assign_articles"},
    "DeliveryOrderBejermanRemitoView": {"POST": "action.delivery_order.generate_bejerman_remito"},
    "DeliveryOrderBejermanRemitoPreflightView": {"POST": "action.delivery_order.generate_bejerman_remito"},
    "DeliveryOrderBejermanArticlesView": {"GET": "action.delivery_order.assign_articles"},
    "DeliveryOrderBejermanDepositsView": {"GET": "action.delivery_order.assign_articles"},
    "DeliveryOrderBejermanArticleStockView": {"GET": "action.delivery_order.assign_articles"},
    "DeliveryOrderPartidasMasivasValidarView": {"POST": "action.delivery_order.assign_articles"},
    "DeliveryOrderRentalEquipmentView": {"GET": ["page.delivery_orders", "page.logistics", "action.delivery_order.create"]},
    "DeliveryOrderBejermanRemitoHistoryView": {"GET": "page.delivery_orders"},
    "DeliveryOrderBejermanRemitoPdfView": {"GET": "page.delivery_orders"},
    "DeliveryOrderBejermanRemitoPrintView": {"GET": "page.delivery_orders"},
    "DeliveryOrderRemitoPdfView": {"GET": "page.delivery_orders"},
    "DeliveryOrderRemitoPrintView": {"GET": "page.delivery_orders"},
    "DeliveryOrderInvoicePdfView": {"GET": "page.delivery_orders"},
    "RouteSheetView": {
        "GET": "page.route_sheet",
        "POST": "action.route_sheet.manage",
    },
    "RouteSheetDetailView": {"PATCH": "action.route_sheet.manage"},
    "RouteSheetCompleteView": {"POST": "action.route_sheet.complete"},
    "RouteSheetPostponeView": {"POST": "action.route_sheet.postpone"},
    "RouteSheetCancelView": {"POST": "action.route_sheet.manage"},
    "RouteSheetReorderView": {"POST": "action.route_sheet.manage"},
    "RouteSheetLocationsView": {"GET": "page.route_sheet", "POST": "action.route_sheet.manage"},
    "RouteSheetLocationDetailView": {"PATCH": "action.route_sheet.manage"},
    "RouteSheetSuggestedDeliveryOrdersView": {"GET": "action.route_sheet.manage"},
    "FacturacionCompanyOptionsView": {"GET": ["action.billing.view", "page.billing"]},
    "FacturacionClienteDocumentosView": {"GET": "action.billing.view"},
    "FacturacionDocumentoPdfView": {"GET": "action.billing.view"},
    "CobranzasRemitosView": {"GET": ["action.billing.view", "page.billing"]},
    "CobranzasRemitoPdfView": {"GET": ["action.billing.view", "page.billing"]},
    "CobranzasRemitoPrintView": {"GET": ["action.billing.view", "page.billing"]},
    "ServiceOrderBillingListView": {"GET": "action.billing.view"},
    "ServiceOrderBillingInvoiceView": {"POST": "action.billing.register_os_invoice"},
    "ServiceOrderBillingPdfView": {"GET": "action.billing.view"},
    # Presupuestos
    "QuoteDetailView": {"GET": ["page.budget_queues", "action.presupuesto.manage", "action.presupuesto.view_costs"]},
    "QuoteItemsView": {"POST": "action.presupuesto.manage"},
    "QuoteItemDetailView": {"PATCH": "action.presupuesto.manage", "DELETE": "action.presupuesto.manage"},
    "QuoteResumenView": {"PATCH": "action.presupuesto.manage"},
    "EmitirPresupuestoView": {"POST": "action.presupuesto.manage"},
    "AprobarPresupuestoView": {"POST": "action.presupuesto.manage"},
    "RechazarPresupuestoView": {"POST": "action.presupuesto.manage"},
    "QuoteVersionesView": {"POST": "action.presupuesto.manage"},
    "AnularPresupuestoView": {"POST": "action.presupuesto.manage"},
    "NoAplicaPresupuestoView": {"POST": "action.presupuesto.manage"},
    "QuitarNoAplicaPresupuestoView": {"POST": "action.presupuesto.manage"},
    "QuotePdfView": {"GET": ["page.budget_queues", "action.presupuesto.manage"]},
    # Usuarios
    "UsuariosView": {"GET": "page.users", "POST": "action.users.manage"},
    "UsuarioActivoView": {"PATCH": "action.users.manage"},
    "UsuarioResetPassView": {"PATCH": "action.users.manage"},
    "UsuarioRolePermView": {"PATCH": "action.users.manage"},
    "UsuarioDetailView": {
        "GET": "page.users",
        "PATCH": "action.users.manage",
        "DELETE": "action.users.manage",
    },
    "UsuarioBejermanWorkstationsView": {"GET": "action.users.manage"},
    "UsuarioBejermanCredentialsView": {
        "PUT": "action.users.manage",
        "DELETE": "action.users.manage",
    },
    "CatalogoRolesView": {"GET": "action.users.manage"},
    "CatalogoTecnicosView": {
        "GET": ["action.ingreso.change_assignment", "action.ingreso.create", "page.new_ingreso"]
    },
    "UsuarioNotificacionesView": {"GET": "action.users.manage_permissions", "PUT": "action.users.manage_permissions"},
    # Repuestos
    "CatalogoRepuestosView": {"GET": "page.spare_parts"},
    "RepuestosView": {"GET": "page.spare_parts", "POST": "action.spare_parts.create"},
    "RepuestosSubrubrosView": {"GET": "page.spare_parts", "POST": "action.spare_parts.manage"},
    "RepuestosSubrubroDetailView": {"PATCH": "action.spare_parts.manage", "DELETE": "action.spare_parts.manage"},
    "RepuestoDetailView": {"GET": "page.spare_parts", "PATCH": "action.spare_parts.manage", "DELETE": "action.spare_parts.manage"},
    "RepuestosConfigView": {"GET": "page.spare_parts", "PATCH": "action.spare_parts.manage"},
    "RepuestosMovimientosView": {"GET": "page.spare_parts"},
    "RepuestosCambiosView": {"GET": "page.spare_parts"},
    "RepuestosCompraMovimientoView": {"POST": "action.spare_parts.manage"},
    "RepuestosStockPermisosView": {"GET": "page.spare_parts", "POST": "action.spare_parts.manage_24h_permissions"},
    "RepuestosStockPermisoDetailView": {"PATCH": "action.spare_parts.manage_24h_permissions"},
    # Catalogos / sistema
    "CatalogoMarcasView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"], "POST": "action.catalogs.manage"},
    "CatalogoModelosView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoUbicacionesView": {"GET": ["page.catalogs", "page.ingresos_history", "action.ingreso.create", "page.new_ingreso", "action.ingreso.edit_location"]},
    "CatalogoMotivosView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoAccesoriosView": {
        "GET": ["page.catalogs", "action.ingreso.create", "page.new_ingreso", "action.ingreso.edit_diagnosis"],
        "POST": "action.catalogs.manage",
        "DELETE": "action.catalogs.manage",
    },
    "CatalogoTiposView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoModelosDeTipoView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoVariantesView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "ModeloVariantesView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoVariantesPorMarcaView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoMarcasPorTipoView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    "CatalogoTiposCreateView": {"POST": "action.catalogs.manage"},
    "CatalogoTipoDetailView": {"PATCH": "action.catalogs.manage", "DELETE": "action.catalogs.manage"},
    "CatalogoModelosCreateView": {"POST": "action.catalogs.manage"},
    "CatalogoModeloDetailView": {"PATCH": "action.catalogs.manage", "DELETE": "action.catalogs.manage"},
    "CatalogoVariantesCreateView": {"POST": "action.catalogs.manage"},
    "CatalogoVarianteDetailView": {"PATCH": "action.catalogs.manage", "DELETE": "action.catalogs.manage"},
    "ModeloVarianteView": {"PATCH": "action.catalogs.manage"},
    "ModeloTipoEquipoView": {"PATCH": "action.catalogs.manage"},
    "ModelosPorMarcaView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"], "POST": "action.catalogs.manage"},
    "MarcaDeleteView": {"DELETE": "action.catalogs.manage", "PATCH": "action.catalogs.manage"},
    "MarcaDeleteCascadeView": {"DELETE": "action.catalogs.manage"},
    "ModeloDeleteView": {"DELETE": "action.catalogs.manage", "PATCH": "action.catalogs.manage"},
    "ModeloTecnicoView": {"PATCH": "action.catalogs.manage"},
    "MarcaTecnicoView": {"PATCH": "action.catalogs.manage"},
    "MarcaAplicarTecnicoAModelosView": {"POST": "action.catalogs.manage"},
    "ModelMergeView": {"POST": "action.catalogs.manage"},
    "MarcaMergeView": {"POST": "action.catalogs.manage"},
    "ClientesView": {"GET": ["page.catalogs", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"], "POST": "action.catalogs.manage"},
    "ClienteBejermanCandidatesView": {"GET": "action.catalogs.manage"},
    "ClientesBejermanSyncView": {"POST": "action.catalogs.manage"},
    "ClienteDeleteView": {"DELETE": "action.catalogs.manage", "PATCH": "action.catalogs.manage"},
    "ClienteMergeView": {"POST": "action.catalogs.manage"},
    "ProveedoresExternosView": {"GET": "page.catalogs", "POST": "action.catalogs.manage", "DELETE": "action.catalogs.manage"},
    "WarrantyRulesView": {"GET": "page.warranty", "POST": "action.catalogs.manage"},
    "WarrantyRuleDetailView": {"PATCH": "action.catalogs.manage", "DELETE": "action.catalogs.manage"},
    "TiposEquipoView": {
        "GET": ["page.catalogs", "action.ingreso.create", "page.new_ingreso"],
        "POST": "action.catalogs.manage",
        "DELETE": "action.catalogs.manage",
    },
    "TestProtocolCatalogView": {"GET": "action.tests_protocol.manage", "POST": "action.tests_protocol.manage"},
    "TestProtocolDetailView": {"GET": "action.tests_protocol.manage", "PATCH": "action.tests_protocol.manage", "DELETE": "action.tests_protocol.manage"},
    # Equipos / preventivos
    "DevicesListView": {"GET": "page.devices_preventivos"},
    "DeviceIdentificadoresView": {
        "GET": "page.devices_preventivos",
        "PATCH": "action.devices_preventivos.manage",
    },
    "DeviceDirectCreateView": {"POST": ["action.devices_preventivos.manage", "action.devices_preventivos.create"]},
    # Alta de equipos propios (de a uno o masiva): mueve stock real VAL→STL en Bejerman, así
    # que tiene permiso propio, aparte del alta de equipos de clientes. El buscador de stock
    # es la primera parte de ese alta. `DeviceDirectCreateView` con `propio=true` pide el
    # mismo permiso adentro de la vista.
    "DeviceStockCandidatosView": {"GET": "action.devices_preventivos.create_own"},
    "DeviceAltaMasivaView": {
        "GET": "action.devices_preventivos.create_own",
        "POST": "action.devices_preventivos.create_own",
    },
    "DeviceAltaMasivaValidarView": {"POST": "action.devices_preventivos.create_own"},
    "DevicesMergeView": {"POST": "action.devices_preventivos.manage"},
    "DeviceMgVentaView": {"POST": "action.devices_preventivos.manage"},
    "DeviceMgReactivarView": {"POST": "action.devices_preventivos.manage"},
    "DevicePreventivoPlanView": {"POST": "action.devices_preventivos.manage", "PATCH": "action.devices_preventivos.manage"},
    "DevicePreventivoRevisionCreateView": {"POST": "action.devices_preventivos.manage"},
    "PreventivoAgendaView": {"GET": "page.devices_preventivos"},
    "PreventivoClientesListView": {"GET": "page.devices_preventivos"},
    "CustomerPreventivoPlanView": {"POST": "action.devices_preventivos.manage", "PATCH": "action.devices_preventivos.manage"},
    "CustomerPreventivoRevisionesView": {"GET": "page.devices_preventivos", "POST": "action.devices_preventivos.manage"},
    "PreventivoRevisionDetailView": {"GET": "page.devices_preventivos"},
    "PreventivoRevisionItemsView": {"POST": "action.devices_preventivos.manage"},
    "PreventivoRevisionItemDetailView": {"PATCH": "action.devices_preventivos.manage"},
    "PreventivoRevisionCerrarView": {"POST": "action.devices_preventivos.manage"},
    # Metricas
    "MetricasResumenView": {"GET": "page.metrics"},
    "MetricasSeriesView": {"GET": "page.metrics"},
    "MetricasFinanzasView": {"GET": "page.metrics"},
    "MetricasFinanzasLiberadosView": {"GET": "page.metrics"},
    "MetricasActividadTecnicosView": {"GET": "page.metrics"},
    "MetricasCalibracionView": {"GET": "page.metrics"},
    "MetricasConfigView": {"GET": "page.metrics", "PATCH": "action.metrics.configure"},
    "FeriadosView": {"GET": "page.metrics", "POST": "action.metrics.configure", "DELETE": "action.metrics.configure"},
    # Media de ingresos
    "IngresoMediaListCreateView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_diagnosis",
        ],
        "POST": "action.ingreso.edit_diagnosis",
    },
    "IngresoMediaDetailView": {"PATCH": "action.ingreso.edit_diagnosis", "DELETE": "action.ingreso.edit_diagnosis"},
    "IngresoMediaFileView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_diagnosis",
        ]
    },
    "IngresoMediaThumbnailView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_diagnosis",
        ]
    },
    "BuscarAccesorioPorReferenciaView": {"GET": "page.home_search"},
    "IngresoAccesoriosView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_diagnosis",
        ],
        "POST": "action.ingreso.edit_diagnosis",
    },
    "IngresoAccesorioDetailView": {"DELETE": "action.ingreso.edit_diagnosis"},
    "IngresoAlquilerAccesoriosView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "action.ingreso.edit_location",
            "action.ingreso.edit_basics",
        ],
        "POST": "action.ingreso.edit_basics",
    },
    "IngresoAlquilerAccesorioDetailView": {"DELETE": "action.ingreso.edit_basics"},
    # General por cliente
    "GeneralPorClienteView": {"GET": ["page.general_cliente", "page.ingresos_history"]},
    "GeneralPorClienteExportView": {"GET": ["page.general_cliente", "page.ingresos_history"]},
    # --- Vistas que estaban afuera de la matriz ---------------------------------------
    # Sin entrada acá, una vista con `MappedPermissionGuard` deja pasar a cualquier usuario
    # logueado, y una con `require_roles` sólo mira el rol: en los dos casos lo que se
    # configura en Usuarios no tiene efecto. `test_permission_coverage` obliga a que toda
    # vista ruteada esté en la matriz o en PERMISSION_EXEMPT_VIEWS.
    "BejermanPurchaseEntryPopulateFromOrderView": {"POST": "action.bejerman_purchase_entries.manage"},
    # La usan la carga manual del remito (órdenes de entrega) y la hoja de servicio.
    "BejermanRemitoSerialSearchView": {"GET": ["page.delivery_orders", "page.service_sheet_principal", "page.liberados"]},
    "DeliveryOrderBejermanRemitoJobView": {"GET": ["action.delivery_order.generate_bejerman_remito", "page.delivery_orders"]},
    "DeliveryOrderDriveSyncView": {"POST": "action.delivery_order.sync_drive"},
    "IngresoBejermanEstadoAccionView": {"POST": "action.bejerman_sync.force_stock_entry"},
    "IngresoAlquilerRemitoPdfView": {
        "GET": [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "page.liberados",
            "page.service_sheet_principal",
            "action.ingreso.edit_basics",
            "action.ingreso.edit_diagnosis",
            "action.ingreso.edit_location",
            "action.ingreso.edit_delivery",
            "action.ingreso.manage_derivations",
            "action.ingreso.repair_transitions",
            "action.ingreso.baja_alta",
            "action.presupuesto.manage",
        ]
    },
    "NuevoIngresoLoteView": {"POST": ["action.ingreso.create", "page.new_ingreso"]},
    "IngresoRisPdfView": {"GET": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"]},
    "IngresoRisPrintView": {"GET": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"]},
    "IngresoRisLotePdfView": {"GET": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"]},
    "IngresoRisLotePrintView": {"GET": ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"]},
    "BejermanIngressCompaniesView": {"GET": ["action.ingreso.create", "page.new_ingreso"]},
    "BejermanPdfQueueStatusView": {"GET": "page.bejerman_sync"},
    "BejermanPdfQueueRetryView": {"POST": "action.bejerman_sync.manage"},
    "BejermanJobCancelView": {"POST": "action.bejerman_sync.manage"},
    "BejermanJobDiagnoseView": {"POST": "action.bejerman_sync.manage"},
    "BejermanPartidaStockView": {"GET": "action.bejerman_sync.manage"},
    "DevicePreventivoRepuestosView": {"GET": "page.devices_preventivos", "POST": "action.devices_preventivos.manage"},
    "DevicePreventivoRepuestoDetailView": {
        "PATCH": "action.devices_preventivos.manage",
        "DELETE": "action.devices_preventivos.manage",
    },
    # Tablero de trabajo
    "WorkResumenView": {"GET": "page.work_dashboard"},
    "WorkObjectivesView": {"GET": "page.work_dashboard", "PUT": "action.work_dashboard.configure"},
    "WorkAlertRulesView": {"GET": "action.work_dashboard.configure", "PATCH": "action.work_dashboard.configure"},
    "GlobalSearchView": {"GET": ["page.home_search", "page.work_dashboard"]},
}


# Vistas ruteadas que a propósito no pasan por la matriz. La clave es el nombre de la
# clase (vale para toda la clase y para sus subclases) o "Clase.METODO" para un método
# puntual. El motivo queda escrito para que nadie "arregle" la exención sin entenderla.
PERMISSION_EXEMPT_VIEWS = {
    # Autenticación y datos del propio usuario
    "ping": "público: chequeo de vida (vista función con @api_view)",
    "LoginView": "público: inicio de sesión",
    "LogoutView": "sesión propia",
    "SessionView": "sesión propia",
    "ForgotPasswordView": "público: pedido de recuperación de contraseña",
    "ResetPasswordView": "público: restablecimiento con token",
    "BejermanCredentialsView": "credenciales Bejerman del propio usuario",
    "BejermanWorkstationsView": "puestos Bejerman del propio usuario",
    "BejermanSellerCodeView": "código de vendedor del propio usuario",
    # Notificaciones del propio usuario
    "NotificacionesView": "notificaciones propias",
    "NotificacionesEmailEstadoView": "estado de envíos consultado por id",
    "NotificacionClickView": "notificaciones propias",
    "NotificacionesReadAllView": "notificaciones propias",
    "NotificacionesPushConfigView": "notificaciones propias",
    "NotificacionesPushSubscriptionView": "notificaciones propias",
    "NotificacionesConfiguracionView": "preferencias propias",
    "NotificacionesConfiguracionEmailsView": "mails extra propios (chequeo por rol adentro)",
    "NotificacionesConfiguracionEmailDetailView": "mails extra propios (chequeo por rol adentro)",
    # Integración con el Portal: autentica con su propio token, no con usuario de NEXORA
    "PortalIntegrationBaseView": "integración Portal: auth por token propio",
    # Edición de permisos: exige rol Jefe a propósito. Mapearla haría que un override de
    # `action.users.manage_permissions` le diera a cualquiera la llave de todos los permisos.
    "CatalogoPermisosView": "exige rol Jefe + action.users.manage_permissions adentro",
    "UsuarioPermisosView": "exige rol Jefe + action.users.manage_permissions adentro",
    "UsuarioPermisosResetView": "exige rol Jefe + action.users.manage_permissions adentro",
    # Chequeos que dependen del contenido del pedido
    "ColasNovedadesView": "cuenta cada cola sólo si el usuario tiene su permiso",
    "IngresoDetalleView.PATCH": "exige un permiso distinto según el campo que se edita",
}


def resolve_permission_code_for_request(request):
    rm = getattr(request, "resolver_match", None)
    func = getattr(rm, "func", None)
    view_class = getattr(func, "view_class", None)
    if view_class is None:
        return None
    class_name = getattr(view_class, "__name__", None)
    if not class_name:
        return None
    method = (getattr(request, "method", "") or "").upper()
    class_map = VIEW_PERMISSION_MATRIX.get(class_name, {})
    return class_map.get(method)
