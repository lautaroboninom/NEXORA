"""Request -> permission policy matrix for PERMISSIONS_V2."""


# Mapping by DRF view class name and HTTP method.
# Methods omitted in a class do not enforce extra permission in this layer.
# Value may be:
# - string: single required permission
# - list[str]: OR semantics (any permission grants access)
VIEW_PERMISSION_MATRIX = {
    # Ingresos / operacion
    "MisPendientesView": {"GET": "page.work_queues"},
    "MarcarParaRepararView": {"POST": "action.ingreso.repair_transitions"},
    "HabilitarReparacionCotizacionView": {"POST": "action.ingreso.repair_transitions"},
    "MarcarReparadoView": {"POST": "action.ingreso.repair_transitions"},
    "MarcarControladoSinDefectoView": {"POST": "action.ingreso.repair_transitions"},
    "EntregarIngresoView": {"POST": "action.ingreso.edit_delivery"},
    "DarBajaIngresoView": {"POST": "action.ingreso.baja_alta"},
    "DarAltaIngresoView": {"POST": "action.ingreso.baja_alta"},
    "IngresoCorreccionesHistoricasView": {"POST": "action.ingreso.force_historical"},
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
    "CustomersListView": {"GET": ["page.home_search", "action.ingreso.edit_basics", "action.ingreso.create", "page.new_ingreso"]},
    # Reportes
    "RemitoSalidaPdfView": {"GET": "action.ingreso.print_exit_order"},
    "RemitoDerivacionPdfView": {"GET": "action.ingreso.manage_derivations"},
    # Presupuestos
    "QuoteDetailView": {"GET": ["page.ingresos_history", "page.budget_queues", "action.presupuesto.manage"]},
    "QuoteItemsView": {"POST": "action.presupuesto.manage"},
    "QuoteItemDetailView": {"PATCH": "action.presupuesto.manage", "DELETE": "action.presupuesto.manage"},
    "QuoteResumenView": {"PATCH": "action.presupuesto.manage"},
    "EmitirPresupuestoView": {"POST": "action.presupuesto.manage"},
    "AprobarPresupuestoView": {"POST": "action.presupuesto.manage"},
    "AnularPresupuestoView": {"POST": "action.presupuesto.manage"},
    "NoAplicaPresupuestoView": {"POST": "action.presupuesto.manage"},
    "QuitarNoAplicaPresupuestoView": {"POST": "action.presupuesto.manage"},
    "QuotePdfView": {"GET": ["page.budget_queues", "action.presupuesto.manage"]},
    # Usuarios
    "UsuariosView": {"GET": "page.users", "POST": "action.users.manage"},
    "UsuarioActivoView": {"PATCH": "action.users.manage"},
    "UsuarioResetPassView": {"PATCH": "action.users.manage"},
    "UsuarioRolePermView": {"PATCH": "action.users.manage"},
    "UsuarioDeleteView": {"DELETE": "action.users.manage"},
    "CatalogoRolesView": {"GET": "action.users.manage"},
    "CatalogoTecnicosView": {
        "GET": ["action.ingreso.change_assignment", "action.ingreso.create", "page.new_ingreso"]
    },
    "UsuarioNotificacionesView": {"GET": "action.users.manage_permissions", "PUT": "action.users.manage_permissions"},
    # Repuestos
    "CatalogoRepuestosView": {"GET": "page.spare_parts"},
    "RepuestosView": {"GET": "page.spare_parts", "POST": "action.spare_parts.manage"},
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
    "DeviceDirectCreateView": {"POST": "action.devices_preventivos.manage"},
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
    "GeneralPorClienteView": {"GET": "page.ingresos_history"},
    "GeneralPorClienteExportView": {"GET": "page.ingresos_history"},
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
