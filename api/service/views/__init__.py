"""Domain-split views package.

Temporary re-exports preserve backwards-compat imports like
`from api.service.views import FooView` while we split code by domain.
"""

# Re-export everything public from legacy first, then override with
# domain-specific implementations moved to dedicated modules.
from .legacy import *  # noqa: F401,F403

# Explicitly re-export underscored helpers consumed elsewhere in the repo
# (e.g., motivos_view imports _fix_text_value from .views).
from .helpers import _fix_text_value  # noqa: F401

# Domain-specific views (override legacy exports where applicable)
from .auth_views import (
    ping,
    LoginView,
    LogoutView,
    SessionView,
    ForgotPasswordView,
    ResetPasswordView,
)

from .metricas_views import (
    MetricasResumenView,
    MetricasSeriesView,
    MetricasFinanzasView,
    MetricasFinanzasLiberadosView,
    MetricasActividadTecnicosView,
    MetricasCalibracionView,
    FeriadosView,
    MetricasConfigView,
)

from .catalogo_tipos_views import (
    TiposEquipoView,
)

from .ingresos_views import (
    MisPendientesView,
    PendientesPresupuestoView,
    PresupuestadosView,
    PresupuestadosExportView,
    AprobadosParaRepararView,
    AprobadosYReparadosView,
    AprobadosView,
    LiberadosView,
    GeneralEquiposView,
    GeneralEquiposExportView,
    GeneralPorClienteView,
    GeneralPorClienteExportView,
    MarcarControladoSinDefectoView,
    MarcarParaRepararView,
    MarcarReparadoView,
    HabilitarReparacionCotizacionView,
    EntregarIngresoView,
    DarBajaIngresoView,
    DarAltaIngresoView,
    IngresoCorreccionesHistoricasView,
    GarantiaReparacionCheckView,
    GarantiaFabricaCheckView,
    NuevoIngresoView,
    IngresoDetalleView,
    IngresoAsignarTecnicoView,
    IngresoSolicitarAsignacionView,
    IngresoSolicitarBajaView,
    IngresoSolicitarBajaRechazarView,
    IngresoHistorialView,
    PendientesGeneralView,
    ListosParaRetiroView,
    CerrarReparacionView,
)

from .ingreso_tests_views import (
    IngresoTestView,
    IngresoTestPdfView,
)
from .test_protocols_views import (
    TestProtocolCatalogView,
    TestProtocolDetailView,
)

from .quotes_views import (
    QuoteDetailView,
    QuoteItemsView,
    QuoteItemDetailView,
    QuoteResumenView,
    EmitirPresupuestoView,
    QuotePdfView,
    AprobarPresupuestoView,
    AnularPresupuestoView,
    NoAplicaPresupuestoView,
    QuitarNoAplicaPresupuestoView,
)

from .media_views import (
    IngresoMediaListCreateView,
    IngresoMediaDetailView,
    IngresoMediaFileView,
    IngresoMediaThumbnailView,
)

from .accesorios_views import (
    CatalogoAccesoriosView,
    IngresoAccesoriosView,
    IngresoAccesorioDetailView,
    BuscarAccesorioPorReferenciaView,
    IngresoAlquilerAccesoriosView,
    IngresoAlquilerAccesorioDetailView,
)

from .repuestos_views import (
    RepuestosSubrubrosView,
    RepuestosSubrubroDetailView,
    CatalogoRepuestosView,
    RepuestosView,
    RepuestoDetailView,
    RepuestosConfigView,
    RepuestosCompraMovimientoView,
    RepuestosMovimientosView,
    RepuestosCambiosView,
    RepuestosStockPermisosView,
    RepuestosStockPermisoDetailView,
)

from .catalogo_hierarquia_views import (
    CatalogoTiposView,
    CatalogoModelosDeTipoView,
    CatalogoVariantesView,
    CatalogoMarcasPorTipoView,
    CatalogoTiposCreateView,
    CatalogoTipoDetailView,
    CatalogoModelosCreateView,
    CatalogoModeloDetailView,
    CatalogoVariantesCreateView,
    CatalogoVarianteDetailView,
    ModeloTipoEquipoView,
)

from .marcas_modelos_views import (
    CatalogoMarcasView,
    CatalogoModelosView,
    CatalogoVariantesPorMarcaView,
    CatalogoUbicacionesView,
    ModeloVarianteView,
    ModelosPorMarcaView,
    MarcaDeleteView,
    MarcaDeleteCascadeView,
    ModeloDeleteView,
    ModeloTecnicoView,
    MarcaTecnicoView,
    MarcaAplicarTecnicoAModelosView,
    ModelMergeView,
    MarcaMergeView,
)

from .usuarios_views import (
    UsuariosView,
    UsuarioActivoView,
    UsuarioResetPassView,
    UsuarioRolePermView,
    UsuarioDeleteView,
    CatalogoRolesView,
    CatalogoPermisosView,
    UsuarioPermisosView,
    UsuarioPermisosResetView,
    CatalogoTecnicosView,
)

from .notifications_views import (
    NotificacionesView,
    NotificacionClickView,
    UsuarioNotificacionesView,
)

from .derivaciones_views import (
    DerivarIngresoView,
    DerivacionesPorIngresoView,
    DevolverDerivacionView,
    EquiposDerivadosView,
)

from .devices_views import (
    DeviceDirectCreateView,
    DeviceIdentificadoresView,
    DevicesListView,
    DevicesMergeView,
    DeviceMgVentaView,
    DeviceMgReactivarView,
)

from .preventivos_views import (
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

from .proveedores_views import (
    ProveedoresExternosView,
)

from .clientes_views import (
    CustomersListView,
    ClientesView,
    ClienteDeleteView,
    ClienteMergeView,
)

from .reportes_views import (
    RemitoSalidaPdfView,
    RemitoDerivacionPdfView,
)

from .scan_views import (
    ScanLookupView,
)

# Motivos catálogo (propio de views/)
from .motivos_view import CatalogoMotivosView
from .warranty_views import WarrantyRulesView, WarrantyRuleDetailView
from .work_views import (
    WorkResumenView,
    WorkObjectivesView,
    WorkAlertRulesView,
    GlobalSearchView,
)
from .bejerman_views import (
    BejermanJobsView,
    BejermanJobRetryView,
    BejermanArticleMappingsView,
)

__all__ = [
    # auth
    "ping",
    "LoginView",
    "LogoutView",
    "SessionView",
    "ForgotPasswordView",
    "ResetPasswordView",
    # metricas
    "MetricasResumenView",
    "MetricasSeriesView",
    "MetricasFinanzasView",
    "MetricasFinanzasLiberadosView",
    "MetricasActividadTecnicosView",
    "MetricasCalibracionView",
    "FeriadosView",
    "MetricasConfigView",
    # catalogo (tipos)
    "TiposEquipoView",
    # ingresos
    "MisPendientesView",
    "PendientesPresupuestoView",
    "PresupuestadosView",
    "PresupuestadosExportView",
    "AprobadosParaRepararView",
    "AprobadosYReparadosView",
    "AprobadosView",
    "LiberadosView",
    "GeneralEquiposView",
    "GeneralEquiposExportView",
    "GeneralPorClienteView",
    "GeneralPorClienteExportView",
    "MarcarControladoSinDefectoView",
    "MarcarParaRepararView",
    "MarcarReparadoView",
    "HabilitarReparacionCotizacionView",
    "EntregarIngresoView",
    "DarBajaIngresoView",
    "DarAltaIngresoView",
    "IngresoCorreccionesHistoricasView",
    "GarantiaReparacionCheckView",
    "GarantiaFabricaCheckView",
    "NuevoIngresoView",
    "IngresoDetalleView",
    "IngresoAsignarTecnicoView",
    "IngresoSolicitarAsignacionView",
    "IngresoSolicitarBajaView",
    "IngresoSolicitarBajaRechazarView",
    "IngresoHistorialView",
    "IngresoTestView",
    "IngresoTestPdfView",
    "TestProtocolCatalogView",
    "TestProtocolDetailView",
    "PendientesGeneralView",
    "ListosParaRetiroView",
    "CerrarReparacionView",
    # quotes
    "QuoteDetailView",
    "QuoteItemsView",
    "QuoteItemDetailView",
    "QuoteResumenView",
    "EmitirPresupuestoView",
    "QuotePdfView",
    "AprobarPresupuestoView",
    "AnularPresupuestoView",
    "NoAplicaPresupuestoView",
    "QuitarNoAplicaPresupuestoView",
    # media
    "IngresoMediaListCreateView",
    "IngresoMediaDetailView",
    "IngresoMediaFileView",
    "IngresoMediaThumbnailView",
    # accesorios
    "CatalogoAccesoriosView",
    "RepuestosSubrubrosView",
    "RepuestosSubrubroDetailView",
    "CatalogoRepuestosView",
    "RepuestosView",
    "RepuestoDetailView",
    "RepuestosConfigView",
    "RepuestosCompraMovimientoView",
    "RepuestosMovimientosView",
    "RepuestosCambiosView",
    "RepuestosStockPermisosView",
    "RepuestosStockPermisoDetailView",
    "IngresoAccesoriosView",
    "IngresoAccesorioDetailView",
    "BuscarAccesorioPorReferenciaView",
    "IngresoAlquilerAccesoriosView",
    "IngresoAlquilerAccesorioDetailView",
    # catalogo jerarquía
    "CatalogoTiposView",
    "CatalogoModelosDeTipoView",
    "CatalogoVariantesView",
    "CatalogoMarcasPorTipoView",
    "CatalogoTiposCreateView",
    "CatalogoTipoDetailView",
    "CatalogoModelosCreateView",
    "CatalogoModeloDetailView",
    "CatalogoVariantesCreateView",
    "CatalogoVarianteDetailView",
    "ModeloTipoEquipoView",
    # marcas y modelos
    "CatalogoMarcasView",
    "CatalogoModelosView",
    "CatalogoVariantesPorMarcaView",
    "CatalogoUbicacionesView",
    "ModeloVarianteView",
    "ModelosPorMarcaView",
    "MarcaDeleteView",
    "MarcaDeleteCascadeView",
    "ModeloDeleteView",
    "ModeloTecnicoView",
    "MarcaTecnicoView",
    "MarcaAplicarTecnicoAModelosView",
    "ModelMergeView",
    "MarcaMergeView",
    "CatalogoMotivosView",
    # usuarios
    "UsuariosView",
    "UsuarioActivoView",
    "UsuarioResetPassView",
    "UsuarioRolePermView",
    "UsuarioDeleteView",
    "CatalogoRolesView",
    "CatalogoPermisosView",
    "UsuarioPermisosView",
    "UsuarioPermisosResetView",
    "CatalogoTecnicosView",
    "NotificacionesView",
    "NotificacionClickView",
    "UsuarioNotificacionesView",
    # derivaciones
    "DerivarIngresoView",
    "DerivacionesPorIngresoView",
    "DevolverDerivacionView",
    "EquiposDerivadosView",
    # devices
    "DeviceDirectCreateView",
    "DeviceIdentificadoresView",
    "DevicesListView",
    "DevicesMergeView",
    "DeviceMgVentaView",
    "DeviceMgReactivarView",
    # preventivos
    "DevicePreventivoPlanView",
    "DevicePreventivoRevisionCreateView",
    "DevicePreventivoRepuestosView",
    "DevicePreventivoRepuestoDetailView",
    "PreventivoAgendaView",
    "PreventivoClientesListView",
    "CustomerPreventivoPlanView",
    "CustomerPreventivoRevisionesView",
    "PreventivoRevisionDetailView",
    "PreventivoRevisionItemsView",
    "PreventivoRevisionItemDetailView",
    "PreventivoRevisionCerrarView",
    # proveedores
    "ProveedoresExternosView",
    # clientes
    "CustomersListView",
    "ClientesView",
    "ClienteDeleteView",
    "ClienteMergeView",
    # reportes
    "RemitoSalidaPdfView",
    "RemitoDerivacionPdfView",
    # scan lookup
    "ScanLookupView",
    # motivos (catálogo ENUM ingreso.motivo)
    "CatalogoMotivosView",
    # warranty rules
    "WarrantyRulesView",
    "WarrantyRuleDetailView",
    # trabajo operativo
    "WorkResumenView",
    "WorkObjectivesView",
    "WorkAlertRulesView",
    "GlobalSearchView",
    # Bejerman
    "BejermanJobsView",
    "BejermanJobRetryView",
    "BejermanArticleMappingsView",
]
