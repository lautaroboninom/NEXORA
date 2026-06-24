// web/src/pages/ServiceSheet.jsx (container)
import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import {
  getIngreso, getUbicaciones, patchIngreso,
  getTecnicos,
  getAccesoriosCatalogo,
  getIngresoHistorial,
  getGeneralEquipos,
  postBajaIngreso,
  postAltaIngreso,
  postIngresoConvertirPropioMg,
  postSolicitarBaja,
  postRechazarSolicitudBaja,
  getClientes,
  getClientesBasico,
  getDeviceEditable,
  postCliente,
  postDevicesMerge,
  getMotivos,
  postDeviceMgVenta,
  postDeviceMgReactivar,
  postIngresoCorreccionHistorica,
  postIngresoRisPreflight,
  postRisPreflightCustomerFix,
  postRisPreflightArticleFix,
  postIngresoRisEmitir,
  getIngresoBarcodeBlob,
} from "../lib/api";
import { openPdfBlob } from "../lib/pdf";
import {
  documentNameFromRis,
  isRisGenerated,
  isRisRegistered,
  openRisPrintablePdf,
  risRemitoFrom,
  waitForRisPdfBlob,
} from "../lib/ris-print";
import {
  getMarcas,
  getModelosByBrand,
  getVariantesPorModelo,
  checkGarantiaFabrica,
  patchModeloTipoEquipo,
} from "../lib/api";
import { useAuth } from "../context/AuthContext";
import {
  formatOS as formatOSHelper,
  formatDateTime as formatDateTimeHelper,
  resolveFechaIngreso,
  resolveFechaCreacion,
  isMotivoCotizacionEquipo,
  isMotivoRevisionTecnica,
  isMgOwned,
  isSaleTicketState,
  deviceIdentifierPartsOf,
  nsPreferInternoOf,
} from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import { canActAsTech, isJefe, ROLES } from "../lib/authz";
import { can, PERMISSION_CODES } from "../lib/permissions";
import ArchivosTab from "./ServiceSheet/tabs/ArchivosTab";
import HistorialTab from "./ServiceSheet/tabs/HistorialTab";
import PresupuestoTab from "./ServiceSheet/tabs/PresupuestoTab";
import DiagnosticoTab from "./ServiceSheet/tabs/DiagnosticoTab";
import TestTab from "./ServiceSheet/tabs/TestTab";
import PrincipalTab from "./ServiceSheet/tabs/PrincipalTab";
import DerivacionesTab from "./ServiceSheet/tabs/DerivacionesTab";
import ServiceCriticalStrip from "../components/ServiceCriticalStrip.jsx";
import DeviceIdentifier from "../components/DeviceIdentifier.jsx";
import RisProgressModal, {
  waitForRisProgressMinimum,
  waitForRisProgressPaint,
} from "../components/RisProgressModal.jsx";
import RisPreflightPanel from "../components/RisPreflightPanel.jsx";
import ReleaseOrderModal from "../components/ReleaseOrderModal.jsx";

const TAB_VALUES = ["principal", "diagnostico", "test", "presupuesto", "derivaciones", "historial", "archivos"];
const isValidTab = (value) => TAB_VALUES.includes((value || "").toString().trim());

const Tabs = ({ value, onChange, items, extraRight }) => (
  <div className="border-b mb-4 flex items-center">
    <div className="flex gap-2">
      {items.map((it) => (
        <button
          key={it.value}
          className={`px-3 py-2 rounded-t ${value === it.value ? "bg-white border border-b-0" : "text-gray-600 hover:text-black"}`}
          onClick={() => onChange(it.value)}
          type="button"
        >
          {it.label}
        </button>
      ))}
    </div>
    <div className="ml-auto">{extraRight}</div>
  </div>
);

const normalizeConflictDevice = (row) => {
  if (!row) return null;
  return {
    id: row.id ?? row.device_id ?? null,
    customer_nombre: row.customer_nombre || row.razon_social || row.last_customer_nombre || "",
    cod_empresa: row.cod_empresa || row.customer_cod_empresa || "",
    tipo_equipo: row.tipo_equipo || row.tipo_equipo_nombre || "",
    marca: row.marca || "",
    modelo: row.modelo || "",
    variante: row.variante || row.equipo_variante || "",
    numero_serie: row.numero_serie || "",
    numero_interno: row.numero_interno || "",
    ubicacion_nombre: row.ubicacion_nombre || "",
    alquilado: !!row.alquilado,
    alquiler_a: row.alquiler_a || "",
  };
};

const conflictValue = (value) => {
  const text = String(value || "").trim();
  return text || "-";
};

function ConflictDeviceCard({ title, device, tone = "gray" }) {
  const normalized = normalizeConflictDevice(device);
  const borderClass = tone === "amber" ? "border-amber-300 bg-amber-50" : "border-gray-200 bg-white";
  const Field = ({ label, value }) => (
    <div>
      <div className="text-xs font-medium text-gray-500">{label}</div>
      <div className="text-sm text-gray-900 break-words">{conflictValue(value)}</div>
    </div>
  );

  if (!normalized) {
    return (
      <div className={`border rounded p-3 ${borderClass}`}>
        <div className="font-semibold mb-2">{title}</div>
        <div className="text-sm text-gray-500">No se pudo cargar este equipo.</div>
      </div>
    );
  }

  return (
    <div className={`border rounded p-3 ${borderClass}`}>
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <div className="font-semibold">{title}</div>
          <div className="text-xs text-gray-500">Equipo #{normalized.id || "-"}</div>
        </div>
        <div className="text-xs font-semibold text-gray-700">{nsPreferInternoOf(normalized, "-")}</div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Cliente" value={normalized.customer_nombre} />
        <Field label="Código empresa" value={normalized.cod_empresa} />
        <Field label="Tipo" value={normalized.tipo_equipo} />
        <Field label="Marca" value={normalized.marca} />
        <Field label="Modelo" value={normalized.modelo} />
        <Field label="Variante" value={normalized.variante} />
        <Field label="N° serie" value={normalized.numero_serie} />
        <Field label="N° interno (MG)" value={normalized.numero_interno} />
        <Field label="Ubicación" value={normalized.ubicacion_nombre} />
        <Field label="Alquiler" value={normalized.alquilado ? (normalized.alquiler_a || "Alquilado") : "No"} />
      </div>
    </div>
  );
}

function IdentifierConflictModal({
  conflict,
  canMerge,
  onClose,
  onEditDevice,
  onMerge,
  onChange,
}) {
  if (!conflict) return null;

  const current = normalizeConflictDevice(conflict.currentDevice);
  const other = normalizeConflictDevice(conflict.conflictDevice);
  const finalNs = conflict.nsChoice === "conflict" ? (other?.numero_serie || "") : (current?.numero_serie || "");
  const finalMg = conflict.mgChoice === "conflict" ? (other?.numero_interno || "") : (current?.numero_interno || "");
  const target = conflict.targetChoice === "conflict" ? other : current;
  const source = conflict.targetChoice === "conflict" ? current : other;
  const canSubmitMerge = canMerge && !!target?.id && !!source?.id && (String(finalNs || "").trim() || String(finalMg || "").trim()) && !conflict.mergeSaving;
  const conflictLabel = conflict.conflictType === "NS_DUPLICATE" ? "N/S" : "MG";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-2 sm:p-4"
      role="dialog"
      aria-modal="true"
      onClick={() => { if (!conflict.mergeSaving) onClose(); }}
    >
      <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-6xl overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3 mb-3">
          <div>
            <h2 className="text-lg font-semibold">Conflicto de identificadores</h2>
            <div className="text-sm text-gray-600">
              El {conflictLabel} que intentaste guardar ya pertenece a otro equipo.
            </div>
          </div>
          <button
            type="button"
            className="px-3 py-1.5 rounded border text-sm hover:bg-gray-50 disabled:opacity-60"
            onClick={onClose}
            disabled={conflict.mergeSaving}
          >
            Cerrar
          </button>
        </div>

        {conflict.detail && (
          <div className="bg-amber-50 border border-amber-200 text-amber-900 rounded p-2 mb-3 text-sm">
            {conflict.detail}
          </div>
        )}
        {conflict.loadErr && (
          <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3 text-sm">
            {conflict.loadErr}
          </div>
        )}
        {conflict.mergeErr && (
          <div className="bg-red-100 border border-red-300 text-red-800 rounded p-2 mb-3 text-sm">
            {conflict.mergeErr}
          </div>
        )}

        {conflict.loading ? (
          <div className="text-sm text-gray-500 py-6">Cargando equipos...</div>
        ) : (
          <>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <ConflictDeviceCard title="Equipo de esta OS" device={current} />
              <ConflictDeviceCard title="Equipo en conflicto" device={other} tone="amber" />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 mt-4 text-sm">
              <div className="border rounded p-3">
                <div className="font-medium mb-2">Equipo a conservar</div>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="conflict-target"
                    value="current"
                    checked={conflict.targetChoice !== "conflict"}
                    onChange={() => onChange({ targetChoice: "current" })}
                  />
                  Equipo de esta OS
                </label>
                <label className="flex items-center gap-2 mt-2">
                  <input
                    type="radio"
                    name="conflict-target"
                    value="conflict"
                    checked={conflict.targetChoice === "conflict"}
                    onChange={() => onChange({ targetChoice: "conflict" })}
                  />
                  Equipo en conflicto
                </label>
              </div>

              <div className="border rounded p-3">
                <div className="font-medium mb-2">N/S final</div>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="conflict-ns"
                    value="current"
                    checked={conflict.nsChoice !== "conflict"}
                    onChange={() => onChange({ nsChoice: "current" })}
                  />
                  Esta OS: {conflictValue(current?.numero_serie)}
                </label>
                <label className="flex items-center gap-2 mt-2">
                  <input
                    type="radio"
                    name="conflict-ns"
                    value="conflict"
                    checked={conflict.nsChoice === "conflict"}
                    onChange={() => onChange({ nsChoice: "conflict" })}
                  />
                  Conflicto: {conflictValue(other?.numero_serie)}
                </label>
              </div>

              <div className="border rounded p-3">
                <div className="font-medium mb-2">MG final</div>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="conflict-mg"
                    value="current"
                    checked={conflict.mgChoice !== "conflict"}
                    onChange={() => onChange({ mgChoice: "current" })}
                  />
                  Esta OS: {conflictValue(current?.numero_interno)}
                </label>
                <label className="flex items-center gap-2 mt-2">
                  <input
                    type="radio"
                    name="conflict-mg"
                    value="conflict"
                    checked={conflict.mgChoice === "conflict"}
                    onChange={() => onChange({ mgChoice: "conflict" })}
                  />
                  Conflicto: {conflictValue(other?.numero_interno)}
                </label>
              </div>
            </div>
          </>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 mt-4">
          <div className="flex flex-wrap gap-2">
            {current?.id && (
              <button
                type="button"
                className="px-3 py-1.5 rounded border text-sm hover:bg-gray-50"
                onClick={() => onEditDevice(current.id)}
                disabled={conflict.mergeSaving}
              >
                Editar equipo de esta OS
              </button>
            )}
            {other?.id && (
              <button
                type="button"
                className="px-3 py-1.5 rounded border text-sm hover:bg-gray-50"
                onClick={() => onEditDevice(other.id)}
                disabled={conflict.mergeSaving}
              >
                Editar equipo en conflicto
              </button>
            )}
          </div>
          <button
            type="button"
            className="px-3 py-1.5 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60"
            disabled={!canSubmitMerge}
            onClick={() => onMerge({ targetId: target.id, sourceId: source.id, numeroSerie: finalNs, numeroInterno: finalMg })}
          >
            {conflict.mergeSaving ? "Unificando..." : "Unificar equipos"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function ServiceSheet() {
  const { id } = useParams();
  const location = useLocation();
  const { user } = useAuth();
  const navigate = useNavigate();

  const isTech = user?.rol === ROLES.TECNICO;

  const actAsTech = canActAsTech(user);
  const canEditBasics = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_BASICS);
  const canAssignTecnico = can(user, PERMISSION_CODES.ACTION_INGRESO_CHANGE_ASSIGNMENT);
  const canManagePresupuesto = can(user, PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE);
  const canSeeCosts = can(user, PERMISSION_CODES.ACTION_PRESUPUESTO_VIEW_COSTS);
  const canSeeHistory = can(user, PERMISSION_CODES.PAGE_INGRESOS_HISTORY);
  const canEditDiagPermission = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_DIAGNOSIS);
  const canRepairTransitions = can(user, PERMISSION_CODES.ACTION_INGRESO_REPAIR_TRANSITIONS);
  const canReleaseOrder = can(user, PERMISSION_CODES.ACTION_INGRESO_PRINT_EXIT_ORDER);
  const canEmitIngressOrder = can(user, PERMISSION_CODES.ACTION_INGRESO_EMIT_INGRESS_ORDER);
  const canPrintBarcode = can(user, PERMISSION_CODES.ACTION_INGRESO_PRINT_BARCODE);
  const canBajaAltaPermission = can(user, PERMISSION_CODES.ACTION_INGRESO_BAJA_ALTA);
  const canForceHistorical = can(user, PERMISSION_CODES.ACTION_INGRESO_FORCE_HISTORICAL);
  const canManageDevices = can(user, PERMISSION_CODES.ACTION_DEVICES_PREVENTIVOS_MANAGE);
  const canEditLocation = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_LOCATION);
  const canManageDerivations = can(user, PERMISSION_CODES.ACTION_INGRESO_MANAGE_DERIVATIONS);
  const canViewLogistics = can(user, PERMISSION_CODES.PAGE_LOGISTICS);
  const canViewDiagnosticoTab = canEditDiagPermission || canRepairTransitions;
  const canViewTestTab = canEditDiagPermission;
  const canViewPresupuestoTab = canManagePresupuesto || canSeeCosts;
  const canViewDerivacionesTab = canManageDerivations || canViewLogistics;
  const canViewArchivosTab = canEditDiagPermission;
  const canViewHistorialTab = canSeeHistory;

  // pestañas
  const [tab, setTab] = useState("principal");
  const setTabPersisted = useCallback((nextTab) => {
    const t = (nextTab || "").toString().trim();
    if (!isValidTab(t)) return;
    setTab(t);
    try {
      const sp = new URLSearchParams(location?.search || "");
      if (sp.get("tab") === t) return;
      sp.set("tab", t);
      const nextSearch = sp.toString();
      navigate(
        { pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : "" },
        { replace: true, state: location.state },
      );
    } catch {}
  }, [location?.pathname, location?.search, location?.state, navigate]);
  const isTabAllowed = useCallback(
    (value) => {
      const t = (value || "").toString().trim();
      if (t === "principal") return true;
      if (t === "diagnostico") return canViewDiagnosticoTab;
      if (t === "test") return canViewTestTab;
      if (t === "presupuesto") return canViewPresupuestoTab;
      if (t === "derivaciones") return canViewDerivacionesTab;
      if (t === "archivos") return canViewArchivosTab;
      if (t === "historial") return canViewHistorialTab;
      return false;
    },
    [
      canViewArchivosTab,
      canViewDerivacionesTab,
      canViewDiagnosticoTab,
      canViewHistorialTab,
      canViewPresupuestoTab,
      canViewTestTab,
    ],
  );
  useEffect(() => {
    if (!isTabAllowed(tab)) setTabPersisted("principal");
  }, [isTabAllowed, setTabPersisted, tab]);
  useEffect(() => {
    try {
      const t = location?.state?.tab;
      if (isValidTab(t) && isTabAllowed(t)) setTabPersisted(t);
    } catch {}
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location?.state, isTabAllowed]);

  // Leer tab/tecnico_id desde el querystring (enlaces del mail)
  useEffect(() => {
    try {
      const search = location?.search || "";
      if (!search) return;
      const sp = new URLSearchParams(search);
      const t = (sp.get("tab") || "").trim();
      if (isValidTab(t) && isTabAllowed(t)) {
        setTab(t);
      }
      if (canAssignTecnico) {
        const tid = (sp.get("tecnico_id") || "").trim();
        if (tid) {
          const n = Number(tid);
          if (!Number.isNaN(n)) { setTecnicoIdQS(n); setTecnicoId(n); }
        }
      }
    } catch {}
  }, [location?.search, canAssignTecnico, isTabAllowed]);

  // datos generales
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [identifierConflict, setIdentifierConflict] = useState(null);
  const [releaseOrderRow, setReleaseOrderRow] = useState(null);
  const ingressDocumentName = useMemo(() => documentNameFromRis(data, "RIS"), [data]);
  const ingressDocumentNameFor = useCallback(
    (source) => documentNameFromRis(source || data, ingressDocumentName),
    [data, ingressDocumentName],
  );

  // entrega
  const [entrega, setEntrega] = useState({ remito_salida: "", factura_numero: "", fecha_entrega: "", serial_confirm: "" });
  const canEditEntrega = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY);
  const canEditAlquiler = canEditEntrega || canEditLocation || canManageDevices;
  const [editEntrega, setEditEntrega] = useState(false);
  const [savingEntrega, setSavingEntrega] = useState(false);

  // ubicaciones
  const [ubicaciones, setUbicaciones] = useState([]);
  const [ubicacionId, setUbicacionId] = useState("");

  // clientes (autocompletar)
  const [clientes, setClientes] = useState([]);
  const [clientesPerm, setClientesPerm] = useState(true);
  const [clienteRsInput, setClienteRsInput] = useState("");
  const [clienteCodInput, setClienteCodInput] = useState("");
  const clientesLoadedRef = useRef(false);

  // tcnicos
  const [tecnicos, setTecnicos] = useState([]);
  const [tecnicoId, setTecnicoId] = useState(null);
  const [tecnicoIdQS, setTecnicoIdQS] = useState(null);

  // accesorios
  const [accesCatalogo, setAccesCatalogo] = useState([]);
  const [nuevoAcc, setNuevoAcc] = useState({ descripcion: "", referencia: "" });

  // Diagnóstico (texto/fecha) mantenidos en el contenedor
  const [descripcion, setDescripcion] = useState("");
  const [trabajos, setTrabajos] = useState("");
  const [resolucion, setResolucion] = useState("");
  const [fechaServStr, setFechaServStr] = useState("");
  const [toastMsg, setToastMsg] = useState("");
  const [showReparadoToast, setShowReparadoToast] = useState(false);
  const [savingDiag, setSavingDiag] = useState(false);
  const toDatetimeLocalStr = (isoOrDate) => {
    if (!isoOrDate) return "";
    const d = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const toDateInputStr = (isoOrDate) => {
    if (!isoOrDate) return "";
    if (!(isoOrDate instanceof Date)) {
      const raw = String(isoOrDate).trim();
      const match = raw.match(/^(\d{4}-\d{2}-\d{2})/);
      if (match) return match[1];
    }
    const d = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
    if (Number.isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };
  const maxLocalNow = toDatetimeLocalStr(new Date());

  // historial de cambios
  const [hist, setHist] = useState([]);
  const [hLoading, setHLoading] = useState(false);
  const [hErr, setHErr] = useState("");

  // ingresos relacionados por N/S
  const [relatedOpen, setRelatedOpen] = useState(false);
  const [relatedLoading, setRelatedLoading] = useState(false);
  const [relatedErr, setRelatedErr] = useState("");
  const [relatedRows, setRelatedRows] = useState([]);

  // Catalogo de Equipo (para editar Marca/Modelo/Variante)
  const [marcas, setMarcas] = useState([]);
  const [marcaIdSel, setMarcaIdSel] = useState(null);
  const [modelos, setModelos] = useState([]);
  const [modeloIdSel, setModeloIdSel] = useState(null);
  const [tipoSel, setTipoSel] = useState("");
  const [varSugeridas, setVarSugeridas] = useState([]);
  const [motivos, setMotivos] = useState([]);

  // edicion basica
  const [editBasics, setEditBasics] = useState(false);
  const [formBasics, setFormBasics] = useState(null);
  const [savingBasics, setSavingBasics] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsMenuRef = useRef(null);
  const [risBusy, setRisBusy] = useState(false);
  const [risProgressStatus, setRisProgressStatus] = useState("");
  const [manualRisPrint, setManualRisPrint] = useState(null);
  const [risPreflight, setRisPreflight] = useState(null);
  const [risPreflightLoading, setRisPreflightLoading] = useState(false);
  const [risPreflightError, setRisPreflightError] = useState("");
  const [barcodeBusy, setBarcodeBusy] = useState(false);
  const [savingBaja, setSavingBaja] = useState(false);
  const [savingAlta, setSavingAlta] = useState(false);
  const [solicitarBajaOpen, setSolicitarBajaOpen] = useState(false);
  const [solicitarBajaMotivo, setSolicitarBajaMotivo] = useState("");
  const [savingSolicitarBaja, setSavingSolicitarBaja] = useState(false);
  const [rejectingBajaRequest, setRejectingBajaRequest] = useState(false);
  const [mgModalOpen, setMgModalOpen] = useState(false);
  const [mgSaving, setMgSaving] = useState(false);
  const [propioMgModalOpen, setPropioMgModalOpen] = useState(false);
  const [propioMgSaving, setPropioMgSaving] = useState(false);
  const [mgAddCustomerOpen, setMgAddCustomerOpen] = useState(false);
  const [mgAddCustomerSaving, setMgAddCustomerSaving] = useState(false);
  const [mgAddCustomerErr, setMgAddCustomerErr] = useState("");
  const [mgAddCustomerForm, setMgAddCustomerForm] = useState({
    razon_social: "",
    cod_empresa: "",
    telefono: "",
    telefono_2: "",
    email: "",
  });
  const [histModalOpen, setHistModalOpen] = useState(false);
  const [histSaving, setHistSaving] = useState(false);
  const [histForm, setHistForm] = useState({
    accion: "entrega",
    fecha_efectiva: toDateInputStr(new Date()),
    motivo: "",
    notificar: true,
    remito_salida: "",
    factura_numero: "",
    fecha_entrega: "",
    alquiler_a: "",
    alquiler_remito: "",
    alquiler_fecha: "",
  });
  const [mgForm, setMgForm] = useState({
    factura_numero: "",
    remito_numero: "",
    fecha_venta: "",
    observaciones: "",
    venta_customer_id: "",
    venta_numero_alternativo: "",
  });
  const [propioMgForm, setPropioMgForm] = useState({
    numero_interno: "",
  });

  // Helpers de clientes (validación de selección)
  const normalizeClientText = useCallback(
    (val) =>
      String(val || "")
        .trim()
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .replace(/\s+/g, " "),
    [],
  );
  const hasClienteCodCatalog = useMemo(
    () => (clientes || []).some((c) => String(c?.cod_empresa || "").trim() !== ""),
    [clientes],
  );
  const findClienteByRS = useCallback(
    (v) => {
      const needle = normalizeClientText(v);
      if (!needle) return null;
      return (clientes || []).find((c) => (
        normalizeClientText(c?.razon_social) === needle
        || normalizeClientText(c?.alias_interno) === needle
      )) || null;
    },
    [clientes, normalizeClientText],
  );
  const findClienteByCod = useCallback(
    (v) => {
      if (!hasClienteCodCatalog) return null;
      const needle = normalizeClientText(v);
      if (!needle) return null;
      return (clientes || []).find((c) => normalizeClientText(c?.cod_empresa) === needle) || null;
    },
    [clientes, hasClienteCodCatalog, normalizeClientText],
  );
  const resolveCliente = useCallback(
    (rsVal, codVal) => {
      const byRs = rsVal ? findClienteByRS(rsVal) : null;
      const byCod = codVal ? findClienteByCod(codVal) : null;
      if (byRs && (!codVal || !hasClienteCodCatalog)) return byRs;
      if (byCod && !rsVal) return byCod;
      if (byRs && byCod && byRs.id === byCod.id) return byRs;
      return null;
    },
    [findClienteByCod, findClienteByRS, hasClienteCodCatalog]
  );
  const syncClienteFromInputs = useCallback(
    (rsVal, codVal) => {
      const c = resolveCliente(rsVal, codVal);
      setFormBasics((f0) => {
        const f = { ...(f0 || {}) };
        f.razon_social = rsVal || "";
        f.cod_empresa = codVal || "";
        if (c) {
          f.telefono = c.telefono || f.telefono || "";
        }
        return f;
      });
      return c;
    },
    [resolveCliente]
  );

  const loadClientesCatalogo = useCallback(async () => {
    try {
      const full = await getClientes();
      const rows = Array.isArray(full) ? full : [];
      setClientes(rows);
      setClientesPerm(true);
      return rows;
    } catch (_) {
      try {
        const basic = await getClientesBasico();
        const rows = Array.isArray(basic) ? basic : [];
        setClientes(rows);
        setClientesPerm(true);
        return rows;
      } catch (e) {
        setClientesPerm(false);
        setClientes([]);
        throw e;
      }
    }
  }, []);

  function money(n) {
    if (n == null) return "-";
    const num = Number(n);
    if (Number.isNaN(num)) return String(n);
    return num.toLocaleString("es-AR", { style: "currency", currency: "ARS", minimumFractionDigits: 2 });
  }

  function currentDeviceForConflict(deviceId = null) {
    return normalizeConflictDevice({
      ...(data || {}),
      id: deviceId || data?.device_id || null,
      customer_nombre: data?.razon_social || "",
      variante: data?.equipo_variante || "",
    });
  }

  async function openIdentifierConflict(error, fields = {}) {
    const payload = error?.data || {};
    const conflictType = payload?.conflict_type || "";
    const detail = payload?.detail || "Conflicto al guardar identificadores.";
    const currentDeviceId = payload?.current_device_id || data?.device_id || null;
    const embeddedConflictDevice =
      payload?.conflict_device ||
      payload?.payload?.device_mg ||
      payload?.payload?.device_ns ||
      null;
    const conflictDeviceId =
      payload?.conflict_device_id ||
      embeddedConflictDevice?.id ||
      null;

    const currentFallback = currentDeviceForConflict(currentDeviceId);
    const conflictFallback = normalizeConflictDevice(embeddedConflictDevice);
    const nsChoice = conflictType === "NS_DUPLICATE" ? "conflict" : "current";
    const mgChoice = conflictType === "MG_DUPLICATE" || conflictType === "MG_UNIQUE_CONSTRAINT" ? "conflict" : "current";

    setIdentifierConflict({
      loading: true,
      loadErr: "",
      mergeErr: "",
      mergeSaving: false,
      detail,
      conflictType,
      currentDeviceId,
      conflictDeviceId,
      attemptedNumeroSerie: payload?.numero_serie_input || fields?.numero_serie || formBasics?.numero_serie || "",
      attemptedNumeroInterno: payload?.numero_interno_input || fields?.numero_interno || formBasics?.numero_interno || "",
      currentDevice: currentFallback,
      conflictDevice: conflictFallback,
      targetChoice: "current",
      nsChoice,
      mgChoice,
    });

    if (!currentDeviceId && !conflictDeviceId) {
      setIdentifierConflict((prev) => prev ? ({
        ...prev,
        loading: false,
        loadErr: "No se pudieron determinar los equipos involucrados en el conflicto.",
      }) : prev);
      return;
    }

    try {
      const [currentRes, conflictRes] = await Promise.all([
        currentDeviceId ? getDeviceEditable(currentDeviceId) : Promise.resolve(null),
        conflictDeviceId ? getDeviceEditable(conflictDeviceId) : Promise.resolve(null),
      ]);
      setIdentifierConflict((prev) => prev ? ({
        ...prev,
        loading: false,
        currentDevice: normalizeConflictDevice(currentRes?.device) || currentFallback,
        conflictDevice: normalizeConflictDevice(conflictRes?.device) || conflictFallback,
      }) : prev);
    } catch (fetchError) {
      setIdentifierConflict((prev) => prev ? ({
        ...prev,
        loading: false,
        loadErr: fetchError?.message || "No se pudieron cargar los equipos en conflicto.",
        currentDevice: prev.currentDevice || currentFallback,
        conflictDevice: prev.conflictDevice || conflictFallback,
      }) : prev);
    }
  }

  const updateIdentifierConflict = (patchFields) => {
    setIdentifierConflict((prev) => prev ? ({ ...prev, ...patchFields }) : prev);
  };

  const closeIdentifierConflict = () => {
    setIdentifierConflict((prev) => (prev?.mergeSaving ? prev : null));
  };

  const editConflictDevice = (deviceId) => {
    if (!deviceId) return;
    const sp = new URLSearchParams();
    sp.set("tab", "equipos");
    sp.set("edit_device_id", String(deviceId));
    sp.set("device_id", String(deviceId));
    sp.set("from", "service");
    sp.set("ingreso_id", String(id));
    navigate(`/equipos?${sp.toString()}`);
  };

  async function mergeIdentifierConflict({ targetId, sourceId, numeroSerie, numeroInterno }) {
    if (!targetId || !sourceId || targetId === sourceId) return;
    setIdentifierConflict((prev) => prev ? ({ ...prev, mergeSaving: true, mergeErr: "" }) : prev);
    try {
      await postDevicesMerge({
        target_id: Number(targetId),
        source_id: Number(sourceId),
        numero_serie: String(numeroSerie || "").trim(),
        numero_interno: String(numeroInterno || "").trim(),
      });
      setIdentifierConflict(null);
      setEditBasics(false);
      setFormBasics(null);
      setErr("");
      await refreshIngreso();
    } catch (e) {
      setIdentifierConflict((prev) => prev ? ({
        ...prev,
        mergeSaving: false,
        mergeErr: e?.message || "No se pudieron unificar los equipos.",
      }) : prev);
    }
  }

  // PATCH helper
  async function patch(fields) {
    try {
      await patchIngreso(id, fields);
      setData((d) => ({ ...d, ...fields }));
      setErr("");
    } catch (e) {
      const conflict = e?.data?.conflict_type;
      if (conflict) {
        const detail = e?.data?.detail || "Conflicto al guardar identificadores.";
        setErr(detail);
        await openIdentifierConflict(e, fields);
        return;
      }
      setErr(e?.message || "No se pudo guardar");
    }
  }

  async function refreshIngreso(params) {
    try {
      const ing = await getIngreso(id, params || undefined);
      setData(ing);
    } catch (e) {
      setErr(e?.message || "No se pudo refrescar el ingreso");
    }
  }

  async function runRisPreflight({ silent = false } = {}) {
    setRisPreflightLoading(true);
    setRisPreflightError("");
    try {
      const result = await postIngresoRisPreflight(id);
      setRisPreflight(result);
      if (!result?.can_emit && silent) {
        setErr(result?.detail || `La validación previa del ${ingressDocumentNameFor(result)} encontró problemas.`);
      }
      return result;
    } catch (error) {
      const detail = error?.data?.detail || error?.message || `No se pudo validar el ${ingressDocumentName}.`;
      if (Array.isArray(error?.data?.issues)) {
        setRisPreflight(error.data);
      }
      setRisPreflightError(detail);
      if (silent) setErr(detail);
      return null;
    } finally {
      setRisPreflightLoading(false);
    }
  }

  async function applyRisCustomerFix(payload) {
    try {
      setRisPreflightLoading(true);
      setRisPreflightError("");
      await postRisPreflightCustomerFix(payload);
      await loadClientesCatalogo();
      await refreshIngreso({ strong: 1 });
      await runRisPreflight();
    } catch (error) {
      setRisPreflightError(error?.data?.detail || error?.message || "No se pudo aplicar el cliente de Bejerman.");
    } finally {
      setRisPreflightLoading(false);
    }
  }

  async function applyRisArticleFix(payload) {
    try {
      setRisPreflightLoading(true);
      setRisPreflightError("");
      await postRisPreflightArticleFix(payload);
      await runRisPreflight();
    } catch (error) {
      setRisPreflightError(error?.data?.detail || error?.message || "No se pudo aplicar el artículo.");
    } finally {
      setRisPreflightLoading(false);
    }
  }

  async function emitirRisIngreso() {
    if (risBusy) return;
    setActionsOpen(false);
    setErr("");
    setManualRisPrint(null);
    if (isRisRegistered(data)) {
      const remito = risRemitoFrom(data);
      setToastMsg(remito ? `Remito ${remito} registrado en Bejerman.` : "Remito registrado en Bejerman.");
      setTimeout(() => setToastMsg(""), 2500);
      return;
    }
    const progressStartedAt = Date.now();
    try {
      setRisBusy(true);
      await waitForRisProgressPaint();
      let risSource = data;
      if (!isRisGenerated(data)) {
        setRisProgressStatus(`Validando ${ingressDocumentName}...`);
        const preflight = await runRisPreflight({ silent: true });
        if (!preflight?.can_emit) return;
        const preflightDocumentName = ingressDocumentNameFor(preflight);
        setRisProgressStatus(`Emitiendo ${preflightDocumentName} en Bejerman...`);
        await waitForRisProgressPaint();
        risSource = await postIngresoRisEmitir(id);
        await refreshIngreso({ strong: 1 });
      }
      const risDocumentName = ingressDocumentNameFor(risSource);
      const remito = risRemitoFrom(risSource) || risRemitoFrom(data);
      const blob = await waitForRisPdfBlob(id, risSource, {
        onProgress: (progress) => setRisProgressStatus(progress?.status || `Preparando PDF del ${risDocumentName}...`),
      });
      setRisProgressStatus("Abriendo impresión...");
      const opened = openRisPrintablePdf(blob, risSource).opened;
      if (opened) {
        setToastMsg(remito ? `${risDocumentName} ${remito} listo para imprimir.` : `${risDocumentName} listo para imprimir.`);
        setTimeout(() => setToastMsg(""), 2500);
      } else {
        setManualRisPrint({ blob, source: risSource, remito });
        setErr(`El PDF del ${risDocumentName} ya está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.`);
      }
    } catch (e) {
      if (Array.isArray(e?.data?.issues)) {
        setRisPreflight(e.data);
        setErr(e?.data?.detail || `La validación previa del ${ingressDocumentNameFor(e.data)} encontró problemas.`);
        return;
      }
      setErr(e?.message || `No se pudo emitir o reimprimir el ${ingressDocumentName}`);
    } finally {
      await waitForRisProgressMinimum(progressStartedAt);
      setRisBusy(false);
      setRisProgressStatus("");
    }
  }

  function abrirRisManualPendiente() {
    if (!manualRisPrint?.blob) return;
    const opened = openRisPrintablePdf(manualRisPrint.blob, manualRisPrint.source || data).opened;
    if (opened) {
      const remito = manualRisPrint.remito || risRemitoFrom(manualRisPrint.source) || risRemitoFrom(data);
      const risDocumentName = ingressDocumentNameFor(manualRisPrint.source);
      setManualRisPrint(null);
      setErr("");
      setToastMsg(remito ? `${risDocumentName} ${remito} listo para imprimir.` : `${risDocumentName} listo para imprimir.`);
      setTimeout(() => setToastMsg(""), 2500);
    } else {
      setErr("El navegador volvió a bloquear la ventana. Habilite ventanas emergentes para NEXORA y reintente.");
    }
  }

  async function imprimirCodigoBarrasIngreso() {
    if (barcodeBusy) return;
    try {
      setBarcodeBusy(true);
      setActionsOpen(false);
      const blob = await getIngresoBarcodeBlob(id);
      openPdfBlob(blob);
    } catch (e) {
      setErr(e?.message || "No se pudo imprimir el código de barras");
    } finally {
      setBarcodeBusy(false);
    }
  }

  // cargar historial solo cuando se selecciona la pestaña
  useEffect(() => {
    if (tab !== "historial" || !canViewHistorialTab) return;
    (async () => {
      try {
        setHErr(""); setHLoading(true);
        const rows = await getIngresoHistorial(id);
        setHist(Array.isArray(rows) ? rows : []);
      } catch (e) {
        setHErr(e?.message || "No se pudo cargar el historial");
        setHist([]);
      } finally {
        setHLoading(false);
      }
    })();
  }, [tab, id, canViewHistorialTab]);

  // limpiar modal relacionados al cambiar id
  useEffect(() => {
    setRelatedOpen(false);
    setRelatedRows([]);
    setRelatedErr("");
    setRelatedLoading(false);
    setManualRisPrint(null);
    setSolicitarBajaOpen(false);
    setSolicitarBajaMotivo("");
    setSavingSolicitarBaja(false);
    setRejectingBajaRequest(false);
    setHistModalOpen(false);
    setHistSaving(false);
    setHistForm({
      accion: "entrega",
      fecha_efectiva: toDateInputStr(new Date()),
      motivo: "",
      notificar: true,
      remito_salida: "",
      factura_numero: "",
      fecha_entrega: "",
      alquiler_a: "",
      alquiler_remito: "",
      alquiler_fecha: "",
    });
    setMgModalOpen(false);
    setMgSaving(false);
    setMgAddCustomerOpen(false);
    setMgAddCustomerSaving(false);
    setMgAddCustomerErr("");
    setMgAddCustomerForm({
      razon_social: "",
      cod_empresa: "",
      telefono: "",
      telefono_2: "",
      email: "",
    });
  }, [id]);

  useEffect(() => {
    const defaultFecha = (() => {
      if (data?.mg_venta_fecha) {
        const s = String(data.mg_venta_fecha);
        return s.includes("T") ? s.slice(0, 10) : s.slice(0, 10);
      }
      return new Date().toISOString().slice(0, 10);
    })();
    setMgForm({
      factura_numero: data?.mg_venta_factura_numero || "",
      remito_numero: data?.mg_venta_remito_numero || "",
      fecha_venta: defaultFecha,
      observaciones: data?.mg_venta_observaciones || "",
      venta_customer_id: data?.mg_venta_customer_id ? String(data.mg_venta_customer_id) : "",
      venta_numero_alternativo: data?.mg_venta_numero_alternativo || "",
    });
  }, [
    data?.id,
    data?.mg_venta_fecha,
    data?.mg_venta_factura_numero,
    data?.mg_venta_remito_numero,
    data?.mg_venta_observaciones,
    data?.mg_venta_customer_id,
    data?.mg_venta_numero_alternativo,
  ]);

  // cargar relacionados cuando se abre el modal
  useEffect(() => {
    if (!relatedOpen || !canSeeHistory) return;
    const serie = (data?.numero_serie || "").trim();
    if (!serie) {
      setRelatedErr("Este equipo no tiene Número de serie registrado.");
      setRelatedRows([]);
      setRelatedLoading(false);
      return;
    }
    let cancelled = false;
    setRelatedLoading(true);
    setRelatedErr("");
    (async () => {
      try {
        const rows = await getGeneralEquipos({ q: serie });
        if (cancelled) return;
        const safe = Array.isArray(rows) ? rows : [];
        const normalized = serie.toLowerCase();
        const toTs = (row) => {
          const raw = resolveFechaCreacion(row);
          if (!raw) return 0;
          const ts = new Date(raw).getTime();
          return Number.isNaN(ts) ? 0 : ts;
        };
        const filtered = safe
          .filter((row) => String(row?.numero_serie || "").trim().toLowerCase() === normalized)
          .sort((a, b) => toTs(b) - toTs(a));
        setRelatedRows(filtered);
      } catch (e) {
        if (cancelled) return;
        setRelatedErr(e?.message || "No se pudieron cargar los ingresos del equipo.");
        setRelatedRows([]);
      } finally {
        if (!cancelled) setRelatedLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [relatedOpen, data?.numero_serie, id, canSeeHistory]);

  // close modal with Escape
  useEffect(() => {
    if (!relatedOpen) return;
    const handler = (ev) => { if (ev.key === "Escape") setRelatedOpen(false); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [relatedOpen]);

  // Cerrar menú de acciones al hacer click fuera
  useEffect(() => {
    const handleClickOutside = (ev) => {
      if (!actionsOpen) return;
      if (actionsMenuRef.current && !actionsMenuRef.current.contains(ev.target)) {
        setActionsOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [actionsOpen]);

  // Activar edicion basica
  function startEditBasics() {
    setFormBasics({
      razon_social: data?.razon_social || "",
      cod_empresa: data?.cod_empresa || "",
      telefono: data?.telefono || "",
      propietario_nombre: data?.propietario_nombre || "",
      propietario_contacto: data?.propietario_contacto || "",
      propietario_doc: data?.propietario_doc || "",
      numero_serie: data?.numero_serie || "",
      numero_interno: data?.numero_interno || "",
      remito_ingreso: data?.remito_ingreso || "",
      informe_preliminar: data?.informe_preliminar || "",
      comentarios: data?.comentarios || "",
      motivo: data?.motivo || "",
      garantia_reparacion: !!data?.garantia_reparacion,
      equipo_variante: data?.equipo_variante || "",
      garantia: !!data?.garantia,
    });
    setEditBasics(true);
    setVarSugeridas([]);
    setClienteRsInput(data?.razon_social || "");
    setClienteCodInput(data?.cod_empresa || "");
    (async () => {
      try {
        if (!clientes.length) {
          await loadClientesCatalogo();
        }
      } catch (e) {
        setClientesPerm(false);
      } finally {
        syncClienteFromInputs(data?.razon_social || "", data?.cod_empresa || "");
      }
    })();
    (async () => {
      try {
        const norm = (s) => (s || "").toString().trim().toLowerCase();
        let marcasList = Array.isArray(marcas) ? marcas : [];
        if (!marcasList.length) {
          try {
            const loaded = await getMarcas();
            marcasList = Array.isArray(loaded) ? loaded : [];
            setMarcas(marcasList);
          } catch {
            marcasList = [];
          }
        }
        const marcaIdData = data?.marca_id != null ? Number(data.marca_id) : null;
        const marcaById = marcasList.find((m) => Number(m?.id) === Number(marcaIdData));
        const curMarcaName = norm(data?.marca);
        const marcaByName = curMarcaName ? marcasList.find((m) => norm(m?.nombre) === curMarcaName) : null;
        const marcaId = marcaById?.id ?? marcaByName?.id ?? null;
        setMarcaIdSel(marcaId);
        const tipoActual = (data?.tipo_equipo_nombre || data?.tipo_equipo || "").toString().trim().toUpperCase();
        setTipoSel(tipoActual);
        if (marcaId) {
          try {
            const list = await getModelosByBrand(marcaId);
            const modelosList = Array.isArray(list) ? list : [];
            setModelos(modelosList);
            const modeloIdData = data?.model_id != null ? Number(data.model_id) : null;
            const modeloById = modelosList.find((x) => Number(x?.id) === Number(modeloIdData));
            const curModeloName = norm(data?.modelo);
            const modeloByName = curModeloName ? modelosList.find((x) => norm(x?.nombre) === curModeloName) : null;
            setModeloIdSel(modeloById?.id ?? modeloByName?.id ?? null);
            setVarSugeridas([]);
          } catch { setModelos([]); setModeloIdSel(null); setVarSugeridas([]); }
        } else {
          setModelos([]); setModeloIdSel(null); setVarSugeridas([]);
        }
      } catch {}
    })();
  }

  async function saveEditBasics() {
    if (!formBasics) { setEditBasics(false); return; }
    let clienteSel = resolveCliente(clienteRsInput, clienteCodInput);
    let hasCodCatalogRuntime = hasClienteCodCatalog;
    if (!clienteSel) {
      try {
        const list = await loadClientesCatalogo();
        const arr = Array.isArray(list) ? list : [];
        const hasCodInArr = arr.some((c) => String(c?.cod_empresa || "").trim() !== "");
        hasCodCatalogRuntime = hasCodInArr;
        const byRs = clienteRsInput
          ? arr.find((c) => (
              normalizeClientText(c?.razon_social) === normalizeClientText(clienteRsInput)
              || normalizeClientText(c?.alias_interno) === normalizeClientText(clienteRsInput)
            ))
          : null;
        const byCod = hasCodInArr && clienteCodInput
          ? arr.find((c) => normalizeClientText(c?.cod_empresa) === normalizeClientText(clienteCodInput))
          : null;
        if (byRs && (!clienteCodInput || !hasCodInArr)) clienteSel = byRs;
        else if (byCod && !clienteRsInput) clienteSel = byCod;
        else if (byRs && byCod && byRs.id === byCod.id) clienteSel = byRs;
      } catch (e) {
        setClientesPerm(false);
      }
    }
    const rsInputNorm = normalizeClientText(clienteRsInput);
    const rsCurrentNorm = normalizeClientText(data?.razon_social);
    const codInputNorm = normalizeClientText(clienteCodInput);
    const codCurrentNorm = normalizeClientText(data?.cod_empresa);
    const clienteInputsUnchanged =
      rsInputNorm === rsCurrentNorm &&
      (!hasCodCatalogRuntime || codInputNorm === codCurrentNorm);
    if (!clienteSel && clienteInputsUnchanged) {
      clienteSel = {
        id: data?.customer_id ?? data?.cliente_id ?? data?.customerId ?? null,
        razon_social: data?.razon_social || clienteRsInput || "",
        cod_empresa: data?.cod_empresa || clienteCodInput || "",
      };
    }
    if (!clienteSel) {
      setErr("Debe seleccionar un cliente válido de la lista.");
      return;
    }
    const diff = {};
    const cmp = (a, b) => (a ?? "") !== (b ?? "");
    if (cmp(clienteSel.razon_social, data?.razon_social)) diff.razon_social = clienteSel.razon_social;
    if (hasCodCatalogRuntime && cmp(clienteSel.cod_empresa, data?.cod_empresa)) {
      diff.cod_empresa = clienteSel.cod_empresa;
    }
    const currentCid = data?.customer_id ?? data?.cliente_id ?? data?.customerId ?? null;
    if (clienteSel.id && Number(clienteSel.id) !== Number(currentCid)) diff.customer_id = Number(clienteSel.id);
    const telefonoNuevo = (formBasics.telefono || "").trim();
    if (cmp(telefonoNuevo, data?.telefono)) diff.telefono = telefonoNuevo;
    if (cmp(formBasics.propietario_nombre, data?.propietario_nombre)) diff.propietario_nombre = formBasics.propietario_nombre;
    if (cmp(formBasics.propietario_contacto, data?.propietario_contacto)) diff.propietario_contacto = formBasics.propietario_contacto;
    if (cmp(formBasics.propietario_doc, data?.propietario_doc)) diff.propietario_doc = formBasics.propietario_doc;
    if (cmp(formBasics.numero_serie, data?.numero_serie)) diff.numero_serie = formBasics.numero_serie;
    if (cmp(formBasics.numero_interno, data?.numero_interno)) diff.numero_interno = formBasics.numero_interno;
    const remitoNuevo = (formBasics.remito_ingreso || "").trim();
    const remitoActual = (data?.remito_ingreso || "").trim();
    if (remitoNuevo !== remitoActual) diff.remito_ingreso = remitoNuevo;
    if (cmp(formBasics.informe_preliminar, data?.informe_preliminar)) diff.informe_preliminar = formBasics.informe_preliminar;
    if (cmp(formBasics.comentarios, data?.comentarios)) diff.comentarios = formBasics.comentarios;
    const motivoNuevo = (formBasics?.motivo || "").trim();
    const motivoActual = (data?.motivo || "").trim();
    if (motivoNuevo && motivoNuevo !== motivoActual) diff.motivo = motivoNuevo;
    if ((formBasics.garantia_reparacion ? 1 : 0) !== (data?.garantia_reparacion ? 1 : 0)) diff.garantia_reparacion = !!formBasics.garantia_reparacion;
    try {
      const norm = (s) => (s || "").toString().trim().toLowerCase();
      const selMarca = marcas.find((m) => String(m.id) === String(marcaIdSel));
      if (selMarca && norm(selMarca?.nombre) !== norm(data?.marca)) diff.marca_id = Number(selMarca.id);
      const selModelo = modelos.find((m) => String(m.id) === String(modeloIdSel));
      if (selModelo && norm(selModelo?.nombre) !== norm(data?.modelo)) diff.modelo_id = Number(selModelo.id);
      const varNew = (formBasics?.equipo_variante || "").trim();
      const varOld = (data?.equipo_variante || "").trim();
      if (varNew !== varOld) diff.equipo_variante = varNew || null;
      const garNew = !!formBasics?.garantia;
      const garOld = !!data?.garantia;
      if (garNew !== garOld) diff.garantia = garNew;
    } catch {}
    try {
      setSavingBasics(true);
      if (Object.keys(diff).length > 0) {
        await patch(diff);
      }

      // Persistir Tipo de equipo en el modelo asociado si cambi
      try {
        const tipoNuevo = (tipoSel || "").toString().trim();
        const tipoActual = (data?.tipo_equipo_nombre || data?.tipo_equipo || "").toString().trim();
        // Determinar modelo/marca efectivos (nuevo seleccionado o actuales)
        const selModelo = modelos.find((m) => String(m.id) === String(modeloIdSel));
        const modeloIdEfectivo = selModelo ? Number(selModelo.id) : (data?.model_id != null ? Number(data.model_id) : null);
        const marcaIdEfectivo = marcaIdSel != null ? Number(marcaIdSel) : (data?.marca_id != null ? Number(data.marca_id) : null);
        if (modeloIdEfectivo && marcaIdEfectivo && (tipoNuevo || tipoActual) && tipoNuevo.toUpperCase() !== (tipoActual || "").toUpperCase()) {
          await patchModeloTipoEquipo(marcaIdEfectivo, modeloIdEfectivo, { tipo_equipo: tipoNuevo });
        }
      } catch {}

      // Refrescar si hubo cambios relevantes o si pudo haber cambiado el tipo
      if (
        Object.keys(diff).length > 0 ||
        (tipoSel || "").toString().trim().toUpperCase() !== (data?.tipo_equipo || "").toString().trim().toUpperCase()
      ) {
        await refreshIngreso();
      }
      setEditBasics(false);
      setFormBasics(null);
    } finally {
      setSavingBasics(false);
    }
  }

  async function ejecutarBaja({ askConfirm = true } = {}) {
    if (savingBaja || savingAlta) return;
    if (askConfirm) {
      const ok = window.confirm("¿Dar de baja el equipo? Esto deja el ingreso como baja.");
      if (!ok) return;
    }
    try {
      setSavingBaja(true);
      await postBajaIngreso(id);
      setActionsOpen(false);
      await refreshIngreso({ strong: 1 });
      setTabPersisted("principal");
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo marcar la baja");
    } finally {
      setSavingBaja(false);
    }
  }

  async function marcarBaja() {
    await ejecutarBaja({ askConfirm: true });
  }

  async function aceptarSolicitudBaja() {
    await ejecutarBaja({ askConfirm: false });
  }

  async function rechazarSolicitudBaja() {
    if (rejectingBajaRequest || savingBaja || savingAlta) return;
    try {
      setRejectingBajaRequest(true);
      await postRechazarSolicitudBaja(id);
      setActionsOpen(false);
      await refreshIngreso({ strong: 1 });
      setTabPersisted("principal");
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo rechazar la solicitud de baja");
    } finally {
      setRejectingBajaRequest(false);
    }
  }

  async function marcarAlta() {
    if (savingAlta || savingBaja) return;
    const ok = window.confirm("Dar ALTA al equipo? Esta acción cambiará el ingreso a estado ingresado.");
    if (!ok) return;
    try {
      setSavingAlta(true);
      await postAltaIngreso(id);
      setActionsOpen(false);
      await refreshIngreso({ strong: 1 });
      setTabPersisted("principal");
      setErr("");
    } catch (e) {
      setErr(e?.message || "No se pudo marcar el alta");
    } finally {
      setSavingAlta(false);
    }
  }

  async function enviarSolicitudBaja() {
    if (savingSolicitarBaja) return;
    const motivo = (solicitarBajaMotivo || "").trim();
    if (!motivo) {
      setErr("Debe indicar el motivo para solicitar la BAJA.");
      return;
    }
    try {
      setSavingSolicitarBaja(true);
      const resp = await postSolicitarBaja(id, { motivo });
      if (resp?.already_pending) {
        setErr("Ya existe una solicitud de BAJA pendiente para este ingreso.");
      } else if (resp?.already_baja) {
        setErr("El ingreso ya está en estado BAJA.");
      } else if (resp && resp.ok && resp.email_sent === false) {
        setErr("Solicitud de BAJA registrada; no se pudo enviar el correo.");
      } else {
        setErr("");
      }
      setSolicitarBajaOpen(false);
      setSolicitarBajaMotivo("");
      setActionsOpen(false);
      await refreshIngreso({ strong: 1 });
      setTabPersisted("principal");
    } catch (e) {
      setErr(e?.message || "No se pudo solicitar la baja");
    } finally {
      setSavingSolicitarBaja(false);
    }
  }

  function resetHistForm(nextAccion = "entrega") {
    setHistForm({
      accion: nextAccion,
      fecha_efectiva: toDateInputStr(new Date()),
      motivo: "",
      notificar: true,
      remito_salida: "",
      factura_numero: "",
      fecha_entrega: "",
      alquiler_a: "",
      alquiler_remito: "",
      alquiler_fecha: "",
    });
  }

  function abrirCorreccionHistorica() {
    if (!canForceHistorical) return;
    resetHistForm("entrega");
    setHistModalOpen(true);
    setActionsOpen(false);
  }

  async function guardarCorreccionHistorica() {
    if (!id || histSaving) return;
    const accion = String(histForm.accion || "").trim();
    const motivo = String(histForm.motivo || "").trim();
    const fechaEfectiva = String(histForm.fecha_efectiva || "").trim();
    if (!accion) {
      setErr("Debe seleccionar una acción.");
      return;
    }
    if (!fechaEfectiva) {
      setErr("Debe indicar la fecha efectiva.");
      return;
    }
    if (!motivo) {
      setErr("Debe indicar el motivo.");
      return;
    }

    const payload = {
      accion,
      fecha_efectiva: fechaEfectiva,
      motivo,
      notificar: !!histForm.notificar,
    };
    if (accion === "entrega") {
      payload.remito_salida = String(histForm.remito_salida || "").trim() || null;
      payload.factura_numero = String(histForm.factura_numero || "").trim() || null;
      payload.fecha_entrega = String(histForm.fecha_entrega || "").trim() || null;
    } else if (accion === "alta_alquiler") {
      payload.alquiler_a = String(histForm.alquiler_a || "").trim() || null;
      payload.alquiler_remito = String(histForm.alquiler_remito || "").trim() || null;
      payload.alquiler_fecha = String(histForm.alquiler_fecha || "").trim() || null;
    }

    try {
      setHistSaving(true);
      await postIngresoCorreccionHistorica(id, payload);
      await refreshIngreso({ strong: 1 });
      setErr("");
      setHistModalOpen(false);
    } catch (e) {
      setErr(e?.message || "No se pudo registrar la corrección histórica.");
    } finally {
      setHistSaving(false);
    }
  }

  function abrirAltaRapidaClienteMg() {
    setMgAddCustomerErr("");
    setMgAddCustomerForm({
      razon_social: "",
      cod_empresa: "",
      telefono: "",
      telefono_2: "",
      email: "",
    });
    setMgAddCustomerOpen(true);
  }

  async function crearClienteRapidoMg() {
    if (mgAddCustomerSaving) return;
    const razon_social = String(mgAddCustomerForm.razon_social || "").trim();
    const cod_empresa = String(mgAddCustomerForm.cod_empresa || "").trim();
    if (!razon_social || !cod_empresa) {
      setMgAddCustomerErr("Razón social y código de empresa son obligatorios.");
      return;
    }
    try {
      setMgAddCustomerSaving(true);
      setMgAddCustomerErr("");
      await postCliente({
        razon_social,
        cod_empresa,
        telefono: String(mgAddCustomerForm.telefono || "").trim() || null,
        telefono_2: String(mgAddCustomerForm.telefono_2 || "").trim() || null,
        email: String(mgAddCustomerForm.email || "").trim() || null,
      });
      const rows = await loadClientesCatalogo();
      const match = (rows || []).find(
        (c) => String(c?.razon_social || "").trim().toLowerCase() === razon_social.toLowerCase()
      );
      if (match?.id) {
        setMgForm((s) => ({ ...s, venta_customer_id: String(match.id) }));
      }
      setMgAddCustomerOpen(false);
    } catch (e) {
      setMgAddCustomerErr(e?.message || "No se pudo crear el cliente.");
    } finally {
      setMgAddCustomerSaving(false);
    }
  }

  async function guardarVentaMgMenu() {
    if (!data?.device_id) return;
    const factura = (mgForm.factura_numero || "").trim();
    const remito = (mgForm.remito_numero || "").trim();
    const ventaCustomerId = Number(mgForm.venta_customer_id || 0);
    if (!factura && !remito) {
      setErr("Debe informar factura o remito para marcar venta.");
      return;
    }
    if (!ventaCustomerId) {
      setErr("Debe seleccionar a quién se vendió.");
      return;
    }
    try {
      setMgSaving(true);
      await postDeviceMgVenta(data.device_id, {
        factura_numero: factura || null,
        remito_numero: remito || null,
        fecha_venta: mgForm.fecha_venta || null,
        observaciones: (mgForm.observaciones || "").trim() || null,
        venta_customer_id: ventaCustomerId,
        venta_numero_alternativo: (mgForm.venta_numero_alternativo || "").trim() || null,
        ingreso_id: Number(id),
        source: "service_sheet",
      });
      await refreshIngreso({ strong: 1 });
      setErr("");
      setMgModalOpen(false);
    } catch (e) {
      setErr(e?.message || "No se pudo registrar la venta del MG.");
    } finally {
      setMgSaving(false);
    }
  }

  async function reactivarMgMenu() {
    if (!data?.device_id) return;
    try {
      setMgSaving(true);
      await postDeviceMgReactivar(data.device_id, {
        observaciones: (mgForm.observaciones || "").trim() || null,
        ingreso_id: Number(id),
        source: "service_sheet",
      });
      await refreshIngreso({ strong: 1 });
      setErr("");
      setMgModalOpen(false);
    } catch (e) {
      setErr(e?.message || "No se pudo reactivar el MG.");
    } finally {
      setMgSaving(false);
    }
  }

  function abrirAltaMg() {
    setErr("");
    setPropioMgForm({
      numero_interno: String(data?.numero_interno || "").trim(),
    });
    setPropioMgModalOpen(true);
    setActionsOpen(false);
  }

  async function guardarAltaMg() {
    if (!id || propioMgSaving) return;
    const numeroInterno = String(propioMgForm.numero_interno || "").trim();
    if (!numeroInterno) {
      setErr("Debe indicar el número MG para dar de alta el equipo como propio.");
      return;
    }
    try {
      setPropioMgSaving(true);
      await postIngresoConvertirPropioMg(id, { numero_interno: numeroInterno });
      await refreshIngreso({ strong: 1 });
      setErr("");
      setPropioMgModalOpen(false);
    } catch (e) {
      setErr(e?.message || "No se pudo dar de alta el MG.");
    } finally {
      setPropioMgSaving(false);
    }
  }

  // cargar catálogos base
  useEffect(() => {
    if (!canEditDiagPermission && !canEditAlquiler) return;
    (async () => { try { setAccesCatalogo(await getAccesoriosCatalogo()); } catch {} })();
  }, [canEditAlquiler, canEditDiagPermission]);
  useEffect(() => {
    if (!canEditBasics) return;
    (async () => { try { setMarcas(await getMarcas()); } catch {} })();
  }, [canEditBasics]);
  useEffect(() => {
    if (!canEditBasics) return;
    (async () => { try { const list = await getMotivos(); setMotivos(Array.isArray(list) ? list : []); } catch { setMotivos([]); } })();
  }, [canEditBasics]);
  useEffect(() => {
    if (!canEditBasics && !canEditAlquiler) return;
    if (clientesLoadedRef.current) return;
    clientesLoadedRef.current = true;
    (async () => {
      try {
        await loadClientesCatalogo();
      } catch (e) {
        setClientesPerm(false);
      }
    })();
  }, [canEditAlquiler, canEditBasics, loadClientesCatalogo]);
  useEffect(() => {
    if (!editBasics) return;
    if (!marcaIdSel) { setModelos([]); setModeloIdSel(null); setVarSugeridas([]); return; }
    let active = true;
    (async () => {
      try {
        const list = await getModelosByBrand(marcaIdSel);
        if (active) setModelos(Array.isArray(list) ? list : []);
      } catch {
        if (active) setModelos([]);
      }
    })();
    return () => { active = false; };
  }, [editBasics, marcaIdSel]);
  useEffect(() => {
    if (!editBasics) return;
    if (!marcaIdSel || !modeloIdSel) { setVarSugeridas([]); return; }
    let active = true;
    (async () => {
      try {
        const rows = await getVariantesPorModelo(modeloIdSel);
        const seen = new Set();
        const variantes = (Array.isArray(rows) ? rows : [])
          .filter((item) => item?.active !== false)
          .map((item) => item?.name || item?.nombre || item?.label || "")
          .map((value) => String(value || "").trim())
          .filter((value) => {
            const key = value.toUpperCase();
            if (!value || seen.has(key)) return false;
            seen.add(key);
            return true;
          });
        if (active) setVarSugeridas(variantes);
      } catch {
        if (active) setVarSugeridas([]);
      }
    })();
    return () => { active = false; };
  }, [editBasics, marcaIdSel, modeloIdSel]);
  useEffect(() => {
    if (!editBasics) return;
    const ns = (formBasics?.numero_serie || "").trim();
    const selMarca = marcas.find((m) => String(m.id) === String(marcaIdSel));
    const marcaName = (selMarca?.nombre || data?.marca || "").toString();
    if (!ns) { if (formBasics) setFormBasics((s) => ({ ...(s || {}), garantia: false })); return; }
    const h = setTimeout(async () => {
      try {
        const r = await checkGarantiaFabrica(ns, marcaName, {
          brand_id: marcaIdSel || null,
          model_id: modeloIdSel || null,
        });
        if (typeof r?.within_365_days !== "boolean") return;
        const enGarantia = r.within_365_days;
        setFormBasics((s) => ({ ...(s || {}), garantia: enGarantia }));
      } catch {}
    }, 400);
    return () => clearTimeout(h);
  }, [editBasics, formBasics?.numero_serie, marcaIdSel, modeloIdSel, marcas, data?.marca]);

  const estadoLower = (data?.estado || "").toLowerCase();
  const isEntregadoOBaja = estadoLower === "entregado" || estadoLower === "baja" || isSaleTicketState(estadoLower);

  // carga general
  useEffect(() => {
    (async () => {
      try {
        const [ing, ubs] = await Promise.all([
          getIngreso(id, { strong: 1 }),
          canEditLocation ? getUbicaciones() : Promise.resolve([]),
        ]);
        setData(ing);
        setUbicaciones(ubs);
        setUbicacionId(ing?.ubicacion_id != null ? String(ing.ubicacion_id) : "");
        if (canAssignTecnico) {
          if (tecnicoIdQS != null) {
            setTecnicoId(Number(tecnicoIdQS));
          } else if (ing?.tecnico_solicitado_id && ing?.tecnico_solicitado_id !== (ing?.asignado_a ?? null)) {
            setTecnicoId(ing.tecnico_solicitado_id);
          } else {
            setTecnicoId(ing?.asignado_a ?? null);
          }
        } else {
          setTecnicoId(ing?.asignado_a ?? null);
        }
        // inicializar campos de tcnico
        setDescripcion(ing?.descripcion_problema ?? "");
        setTrabajos(ing?.trabajos_realizados ?? "");
        setResolucion(ing?.resolucion ?? "");
        setFechaServStr(toDatetimeLocalStr(ing?.fecha_servicio));
        // entrega
        setEntrega({
          remito_salida: ing?.remito_salida || "",
          factura_numero: ing?.factura_numero || "",
          fecha_entrega: toDateInputStr(ing?.fecha_entrega),
        });
        // tcnicos
        if (canAssignTecnico) { try { setTecnicos(await getTecnicos()); } catch {} } else { setTecnicos([]); }
      } catch (e) {
        setErr(e?.message || "Error cargando datos");
      }
    })();
  }, [id, canAssignTecnico, canEditLocation, tecnicoIdQS]);

  // Auto-guardado de diagnóstico y trabajos (con debounce)
  useEffect(() => {
    if (!data) return;
    // respetar permisos de edición
    const userId = Number(user?.id || 0);
    const assignedToMe = userId && data?.asignado_a === userId;
    const canEditDiagLocal = canEditDiagPermission && (canAssignTecnico || assignedToMe);
    if (!canEditDiagLocal) return;
    if (isEntregadoOBaja) return;

    const curDesc = data?.descripcion_problema ?? "";
    const curTrab = data?.trabajos_realizados ?? "";
    const curFechaStr = toDatetimeLocalStr(data?.fecha_servicio);

    const payload = {};
    if (descripcion !== curDesc) payload.descripcion_problema = descripcion;
    if (trabajos !== curTrab) payload.trabajos_realizados = trabajos;
    if ((fechaServStr || "") !== (curFechaStr || "")) payload.fecha_servicio = (fechaServStr || "").trim() || null;

    if (Object.keys(payload).length === 0) return;

    const h = setTimeout(async () => {
      try {
        setSavingDiag(true);
        await patch(payload);
        setErr("");
      } catch (e) {
        setErr(e?.message || "No se pudo guardar");
      } finally {
        setSavingDiag(false);
      }
    }, 700);

    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [descripcion, trabajos, fechaServStr, data, user]);

  if (!data) return <div className="p-4">Cargando...</div>;
  const numeroSerie = (data?.numero_serie || "").trim();
  const userId = Number(user?.id || 0);
  const assignedToMe = userId && data?.asignado_a === userId;
  const isCotizacion = isMotivoCotizacionEquipo(data?.motivo);
  const isOwnedByMgCustomer = isMgOwned(data);
  const permiteReparacion = Boolean(data?.permite_reparacion ?? true);
  const canEditDiag = canEditDiagPermission && (canAssignTecnico || assignedToMe);
  const canManagePhotos = canEditDiag;
  const canMarkReparado = canRepairTransitions && (canAssignTecnico || assignedToMe);
  const canResolve = canRepairTransitions && isJefe(user);
  const canAutorizarReparar = canRepairTransitions && (canAssignTecnico || assignedToMe);
  const canDarBaja = Boolean(canBajaAltaPermission && isOwnedByMgCustomer);
  const canDarAlta = Boolean(canBajaAltaPermission && estadoLower === "baja");
  const canRequestBaja = Boolean(
    isOwnedByMgCustomer && (
      canEditBasics
        || canEditDiagPermission
        || canEditLocation
        || canEditEntrega
        || canManageDerivations
        || canRepairTransitions
      || canManagePresupuesto
    )
  );
  const openReleaseOrderModal = (row = data) => {
    if (!canReleaseOrder || !row) return;
    setErr("");
    setReleaseOrderRow(row);
  };
  const handleReleaseOrderDone = async ({ opened } = {}) => {
    await refreshIngreso({ strong: 1 });
    if (opened) {
      setToastMsg("Orden de salida lista para imprimir.");
      setTimeout(() => setToastMsg(""), 2500);
    }
  };
  const hasPendingBajaRequest = Boolean(
    data?.baja_solicitada_id
      || String(data?.baja_solicitada_motivo || "").trim()
      || data?.baja_solicitada_fecha
      || String(data?.baja_solicitada_nombre || "").trim()
  );
  const showDecisionBajaModal = canBajaAltaPermission && hasPendingBajaRequest && estadoLower !== "baja";
  const canSolicitarBaja = !canDarBaja && canRequestBaja && estadoLower !== "baja";
  const canOpenSolicitarBaja = canSolicitarBaja && !hasPendingBajaRequest;
  const canEditAccesorios = canEditDiag;
  const canManageMgFromMenu = Boolean(canManageDevices && isOwnedByMgCustomer);
  const canConvertToOwnMg = Boolean((canManageDevices || canBajaAltaPermission) && data?.device_id && !isOwnedByMgCustomer);
  const isRegisteredRis = isRisRegistered(data);
  const canShowRisAction = Boolean(canEmitIngressOrder && !isRegisteredRis);
  const canPrintIngresoBarcode = Boolean(canPrintBarcode && (data?.numero_serie || data?.numero_interno));
  const hasGeneratedRis = Boolean(!isRegisteredRis && data?.ris?.status === "generated" && data?.ris?.remito_number);
  const showRisErrorBanner = Boolean(!isRegisteredRis && data?.ris?.available && (data?.ris?.status === "failed" || data?.ris?.pdf_status === "failed"));
  const risBannerTitle = hasGeneratedRis && data?.ris?.pdf_status === "failed"
    ? `${ingressDocumentName} ${data.ris.remito_number} emitido; PDF no disponible`
    : `${ingressDocumentName} pendiente`;
  const risBannerMessage = data?.ris?.last_error || (
    hasGeneratedRis
      ? `El comprobante está emitido, pero no se pudo recuperar el PDF desde Bejerman. Use Ver ${ingressDocumentName} para buscar el PDF existente.`
      : "no se pudo completar la emisión o el PDF."
  );
  const risPreflightIssues = Array.isArray(risPreflight?.issues) ? risPreflight.issues : [];
  const showRisPreflightPanel = Boolean(
    canShowRisAction
      && (
        risPreflightError
        || (risPreflight && (!risPreflight.can_emit || risPreflightIssues.length > 0))
      )
  );
  const risPreflightPanelResult = risPreflight || (
    risPreflightError ? { can_emit: false, detail: risPreflightError, issues: [] } : null
  );
  const maxDateOnly = maxLocalNow.slice(0, 10);
  const hasMenuActions = Boolean(
    (canSeeHistory && numeroSerie)
      || canShowRisAction
      || canPrintIngresoBarcode
      || canDarBaja
      || canDarAlta
      || canSolicitarBaja
      || canManageMgFromMenu
      || canConvertToOwnMg
      || canForceHistorical
  );
  const serialIdentifier = deviceIdentifierPartsOf(data, "-");
  const serialLabel = serialIdentifier.primary;
  const activeTab = isTabAllowed(tab) ? tab : "principal";
  const serviceTabItems = [
    { value: "principal", label: "Principal" },
    ...(canViewDiagnosticoTab ? [{ value: "diagnostico", label: "Diagnóstico y Reparación" }] : []),
    ...(canViewTestTab ? [{ value: "test", label: "Tests" }] : []),
    ...(canViewPresupuestoTab ? [{ value: "presupuesto", label: isCotizacion ? "Cotización" : "Presupuesto" }] : []),
    ...(canViewDerivacionesTab ? [{ value: "derivaciones", label: "Derivaciones" }] : []),
    ...(canViewArchivosTab ? [{ value: "archivos", label: "Archivos" }] : []),
    ...(canViewHistorialTab ? [{ value: "historial", label: "Historial" }] : []),
  ];

  return (
    <div className="max-w-none p-4">
      <RisProgressModal
        open={risBusy}
        title={risProgressStatus.toLowerCase().includes("validando") ? `Validando ${ingressDocumentName}` : `Emitiendo ${ingressDocumentName}`}
        status={risProgressStatus || `Emitiendo ${ingressDocumentName} en Bejerman...`}
      />
      <ReleaseOrderModal
        open={Boolean(releaseOrderRow)}
        row={releaseOrderRow}
        canManageResolution={canResolve}
        onClose={() => setReleaseOrderRow(null)}
        onReleased={handleReleaseOrderDone}
      />
      <button type="button" onClick={() => navigate(-1)} className="mb-3 inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800">
        Volver
      </button>
      <div className="flex items-start justify-between gap-3 mb-2">
        <div>
          <h1 className="text-2xl font-bold">
            Hoja de servicio - OS: {formatOSHelper(data, id)} - NS/MG: {serialLabel}
          </h1>
        </div>
        {hasMenuActions && (
          <div className="relative" ref={actionsMenuRef}>
            <button
              type="button"
              onClick={() => setActionsOpen((v) => !v)}
              className="p-2 rounded border text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-400"
              aria-haspopup="menu"
              aria-expanded={actionsOpen ? "true" : "false"}
              aria-label="Acciones"
            >
              <span aria-hidden className="text-xl leading-none">{"\u22EE"}</span>
            </button>
            {actionsOpen && (
              <div className="absolute right-0 mt-2 w-56 rounded border bg-white shadow-lg z-50">
                <div className="py-1">
                  {canSeeHistory && numeroSerie && (
                    <button
                      type="button"
                      onClick={() => { setRelatedOpen(true); setActionsOpen(false); }}
                      className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
                    >
                      Ingresos del equipo
                    </button>
                  )}
                  {canSeeHistory && (
                    <button
                      type="button"
                      onClick={() => { setTabPersisted("historial"); setActionsOpen(false); }}
                      className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
                    >
                      Historial de cambios
                    </button>
                  )}
                  {canShowRisAction && (
                    <button
                      type="button"
                      onClick={emitirRisIngreso}
                      disabled={risBusy}
                      className="w-full text-left px-3 py-2 text-sm text-sky-700 hover:bg-sky-50 disabled:opacity-60 disabled:cursor-not-allowed"
                    >
                      {risBusy
                        ? `Preparando ${ingressDocumentName}...`
                        : data?.ris?.remito_number
                          ? `Ver ${ingressDocumentName}`
                          : `Emitir ${ingressDocumentName}`}
                    </button>
                  )}
                  {canPrintIngresoBarcode && (
                    <button
                      type="button"
                      onClick={imprimirCodigoBarrasIngreso}
                      disabled={barcodeBusy}
                      className="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-60 disabled:cursor-not-allowed"
                    >
                      {barcodeBusy ? "Preparando etiqueta..." : "Imprimir código de barras"}
                    </button>
                  )}
                  {canForceHistorical && (
                    <button
                      type="button"
                      onClick={abrirCorreccionHistorica}
                      className="w-full text-left px-3 py-2 text-sm text-indigo-700 hover:bg-indigo-50"
                    >
                      Corrección histórica
                    </button>
                  )}
                  {canManageMgFromMenu && (
                    <button
                      type="button"
                      onClick={() => {
                        setMgModalOpen(true);
                        setActionsOpen(false);
                      }}
                      className="w-full text-left px-3 py-2 text-sm text-amber-700 hover:bg-amber-50"
                    >
                      {data?.mg_inactivo_venta ? "Venta MG / Reactivar" : "Venta del equipo (MG)"}
                    </button>
                  )}
                  {canConvertToOwnMg && (
                    <button
                      type="button"
                      onClick={abrirAltaMg}
                      className="w-full text-left px-3 py-2 text-sm text-emerald-700 hover:bg-emerald-50"
                    >
                      Alta de MG
                    </button>
                  )}
                  {(canDarBaja || canDarAlta) && (
                    estadoLower === "baja" ? (
                      <button
                        type="button"
                        onClick={marcarAlta}
                        disabled={savingAlta}
                        className="w-full text-left px-3 py-2 text-sm text-emerald-700 hover:bg-emerald-50 disabled:opacity-60 disabled:cursor-not-allowed"
                      >
                        {savingAlta ? "Marcando alta..." : "Dar ALTA al equipo"}
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={marcarBaja}
                        disabled={savingBaja}
                        className="w-full text-left px-3 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-60 disabled:cursor-not-allowed"
                      >
                        {savingBaja ? "Marcando baja..." : "Dar BAJA al equipo"}
                      </button>
                    )
                  )}
                  {!canDarBaja && canSolicitarBaja && (
                    <button
                      type="button"
                      onClick={() => {
                        if (!canOpenSolicitarBaja) return;
                        setErr("");
                        setSolicitarBajaMotivo("");
                        setSolicitarBajaOpen(true);
                        setActionsOpen(false);
                      }}
                      disabled={!canOpenSolicitarBaja || savingSolicitarBaja}
                      className={`w-full text-left px-3 py-2 text-sm disabled:opacity-60 disabled:cursor-not-allowed ${
                        hasPendingBajaRequest
                          ? "text-amber-700 bg-amber-50"
                          : "text-red-700 hover:bg-red-50"
                      }`}
                    >
                      {hasPendingBajaRequest
                        ? "Solicitud de BAJA pendiente"
                        : (savingSolicitarBaja ? "Enviando solicitud..." : "Solicitar BAJA")}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
      <ServiceCriticalStrip data={data} isCotizacion={isCotizacion} />
      {showRisErrorBanner && (
        <div className="mb-3 rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-800">
          <span className="font-semibold">{risBannerTitle}:</span> {risBannerMessage}
        </div>
      )}
      

      {showRisPreflightPanel && (
        <div className="mb-4">
          <RisPreflightPanel
            result={risPreflightPanelResult}
            loading={risPreflightLoading}
            error={risPreflightError}
            onApplyCustomer={applyRisCustomerFix}
            onApplyArticle={applyRisArticleFix}
            documentLabel={documentNameFromRis(risPreflightPanelResult || data, ingressDocumentName)}
            actionLabel={`Emitir ${documentNameFromRis(risPreflightPanelResult || data, ingressDocumentName)}`}
          />
        </div>
      )}

      {err && (
        <div className="mb-4 rounded border border-red-300 bg-red-100 p-2 text-red-700">
          <div>{err}</div>
          {manualRisPrint?.blob && (
            <button
              type="button"
              onClick={abrirRisManualPendiente}
              className="mt-2 rounded bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-800"
            >
              Abrir e imprimir {ingressDocumentName}
            </button>
          )}
        </div>
      )}
      <Tabs
        value={activeTab}
        onChange={setTabPersisted}
        items={serviceTabItems}
      />
      {/* ARCHIVOS */}
      {activeTab === "archivos" && canViewArchivosTab && (<ArchivosTab id={id} canManagePhotos={canManagePhotos} />)}

      {/* PRINCIPAL */}
      {activeTab === "principal" && (
        <PrincipalTab
          id={id}
          data={data}
          user={user}
          release={canReleaseOrder}
          numeroSerie={numeroSerie}
          editBasics={editBasics}
          formBasics={formBasics}
          setFormBasics={setFormBasics}
          clientes={clientes}
          clientesPerm={clientesPerm}
          clienteRsInput={clienteRsInput}
          setClienteRsInput={setClienteRsInput}
          clienteCodInput={clienteCodInput}
          setClienteCodInput={setClienteCodInput}
          syncClienteFromInputs={syncClienteFromInputs}
          marcas={marcas}
          marcaIdSel={marcaIdSel}
          setMarcaIdSel={setMarcaIdSel}
          modelos={modelos}
          modeloIdSel={modeloIdSel}
          setModeloIdSel={setModeloIdSel}
          tipoSel={tipoSel}
          setTipoSel={setTipoSel}
          variantes={varSugeridas}
          motivos={motivos}
          ubicaciones={ubicaciones}
          ubicacionId={ubicacionId}
          setUbicacionId={setUbicacionId}
          canEditLocation={canEditLocation}
          canEditAlquiler={canEditAlquiler}
          canManageReleaseResolution={canResolve}
          canShowRisAction={canShowRisAction}
          ingressDocumentName={ingressDocumentName}
          risBusy={risBusy}
          onEmitRis={emitirRisIngreso}
          onOpenReleaseModal={openReleaseOrderModal}
          tecnicos={tecnicos}
          tecnicoId={tecnicoId}
          setTecnicoId={setTecnicoId}
          canAssignTecnico={canAssignTecnico}
          isTech={isTech}
          userId={userId}
          canEditEntrega={canEditEntrega}
          editEntrega={editEntrega}
          setEditEntrega={setEditEntrega}
          entrega={entrega}
          setEntrega={setEntrega}
          savingEntrega={savingEntrega}
          setSavingEntrega={setSavingEntrega}
          patch={patch}
          refreshIngreso={refreshIngreso}
          setErr={setErr}
          setRelatedOpen={setRelatedOpen}
          toDateInputStr={toDateInputStr}
          maxDateOnly={maxDateOnly}
        />
      )}

      {/* Diagnóstico */}
      {activeTab === "diagnostico" && canViewDiagnosticoTab && (
        <DiagnosticoTab
          id={id}
          data={data}
          money={money}
          canEditAcc={canEditAccesorios && !isEntregadoOBaja}
          accesCatalogo={accesCatalogo}
          nuevoAcc={nuevoAcc}
          setNuevoAcc={setNuevoAcc}
          descripcion={descripcion}
          setDescripcion={setDescripcion}
          trabajos={trabajos}
          setTrabajos={setTrabajos}
          fechaServStr={fechaServStr}
          setFechaServStr={setFechaServStr}
          maxLocalNow={maxLocalNow}
          canResolve={canResolve}
          resolucion={resolucion}
          setResolucion={setResolucion}
          canAutorizarReparar={canAutorizarReparar}
          isCotizacion={isCotizacion}
          permiteReparacion={permiteReparacion}
          canHabilitarReparacionCotizacion={canAutorizarReparar}
          actAsTech={actAsTech}
          canEditDiag={canEditDiag}
          canMarkReparado={canMarkReparado}
          patch={patch}
          setErr={setErr}
          refreshIngreso={refreshIngreso}
          setToastMsg={setToastMsg}
          setShowReparadoToast={setShowReparadoToast}
          savingDiag={savingDiag}
          canManagePhotos={canManagePhotos}
          onOpenReleaseModal={openReleaseOrderModal}
        />
      )}

      {/* TEST */}
      {activeTab === "test" && canViewTestTab && (
        <TestTab
          id={id}
          setErr={setErr}
        />
      )}

      {/* PRESUPUESTO */}
      {activeTab === "presupuesto" && canViewPresupuestoTab && (
        <PresupuestoTab
          id={id}
          data={data}
          canManagePresupuesto={canManagePresupuesto}
          canSeeCosts={canSeeCosts}
          money={money}
          isCotizacion={isCotizacion}
          refreshIngreso={refreshIngreso}
          setErr={setErr}
          onOpenReleaseModal={canReleaseOrder ? openReleaseOrderModal : undefined}
        />
      )}

      {/* DERIVACIONES */}
      {activeTab === "derivaciones" && canViewDerivacionesTab && (
        <DerivacionesTab id={id} canManage={canManageDerivations} setErr={setErr} refreshIngreso={refreshIngreso} />
      )}

      {relatedOpen && (
        <div className="fixed inset-0 z-30 flex items-start justify-center overflow-y-auto bg-black/50 p-2 sm:p-4" role="dialog" aria-modal="true" onClick={() => setRelatedOpen(false)}>
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-4xl overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Ingresos del equipo</h2>
                <div className="text-sm text-gray-600">Número de serie: <span className="font-semibold">{numeroSerie || "-"}</span></div>
              </div>
              <button type="button" className="text-sm text-gray-500 hover:text-gray-900" onClick={() => setRelatedOpen(false)} aria-label="Cerrar historial de ingresos">Cerrar</button>
            </div>
            {relatedLoading ? (
              <div className="text-sm text-gray-500">Cargando ingresos relacionados...</div>
            ) : relatedErr ? (
              <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded">{relatedErr}</div>
            ) : relatedRows.length === 0 ? (
              <div className="text-sm text-gray-500">No se encontraron otros ingresos con este Número de serie.</div>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="min-w-full text-sm">
                    <thead>
                      <tr className="text-left">
                        <th className="p-2">OS</th>
                        <th className="p-2">Identificación</th>
                        <th className="p-2">Estado</th>
                        <th className="p-2">Presupuesto</th>
                        <th className="p-2">Fecha ingreso</th>
                        <th className="p-2">Ubicación</th>
                      </tr>
                    </thead>
                    <tbody>
                      {relatedRows.map((r) => {
                        const ingresoId = r?.id ?? r?.ingreso_id;
                        const isCurrent = ingresoId === data.id;
                        if (!ingresoId) return null;
                        return (
                          <tr key={ingresoId} className={`border-t hover:bg-gray-50 cursor-pointer ${isCurrent ? 'bg-blue-50' : ''}`} onClick={() => { setRelatedOpen(false); if (ingresoId) navigate(`/ingresos/${ingresoId}`); }}>
                            <td className="p-2 underline">{formatOSHelper(ingresoId)}</td>
                            <td className="p-2"><DeviceIdentifier row={r} /></td>
                            <td className="p-2">{estadoLabel(r?.estado) || '-'}</td>
                            <td className="p-2">{(() => {
                              const v = r?.presupuesto_estado;
                              if (!v) return '-';
                              if (v === 'presupuestado') return 'Presupuestado';
                              if (v === 'no_aplica') return 'No aplica';
                              try { const s = String(v); return s.charAt(0).toUpperCase() + s.slice(1); } catch { return String(v); }
                            })()}</td>
                            <td className="p-2 whitespace-nowrap">{formatDateTimeHelper(resolveFechaIngreso(r))}</td>
                            <td className="p-2">{r?.ubicacion_nombre || '-'}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <div className="text-xs text-gray-500 mt-2">Mostrando {relatedRows.length} ingreso(s).</div>
              </>
            )}
          </div>
        </div>
      )}

      {showDecisionBajaModal && (
        <div
          className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/60 p-2 sm:p-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-xl overflow-y-auto rounded bg-white p-4 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-5">
            <h2 className="text-lg font-semibold">Solicitud de BAJA pendiente</h2>
            <div className="text-sm text-gray-600 mt-1">
              OS: {formatOSHelper(data, id)} - NS: {serialLabel}
            </div>
            <div className="mt-4">
              <div className="text-xs text-gray-600 mb-1">Motivo de la solicitud</div>
              <div className="border rounded p-3 bg-amber-50 text-sm text-gray-800 whitespace-pre-wrap">
                {String(data?.baja_solicitada_motivo || "").trim() || "Sin motivo informado."}
              </div>
            </div>
            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                type="button"
                className="px-3 py-2 rounded border bg-white hover:bg-gray-50 disabled:opacity-60"
                onClick={rechazarSolicitudBaja}
                disabled={rejectingBajaRequest || savingBaja}
              >
                {rejectingBajaRequest ? "Rechazando..." : "Rechazar"}
              </button>
              <button
                type="button"
                className="px-3 py-2 rounded bg-red-700 text-white hover:bg-red-800 disabled:opacity-60"
                onClick={aceptarSolicitudBaja}
                disabled={savingBaja || rejectingBajaRequest || !isOwnedByMgCustomer}
              >
                {savingBaja ? "Aceptando..." : "Aceptar"}
              </button>
            </div>
            {!isOwnedByMgCustomer && (
              <div className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                Solo se puede aceptar la baja para equipos propios MG.
              </div>
            )}
          </div>
        </div>
      )}

      {solicitarBajaOpen && (
        <div
          className="fixed inset-0 z-30 flex items-start justify-center overflow-y-auto bg-black/50 p-2 sm:p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!savingSolicitarBaja) setSolicitarBajaOpen(false); }}
        >
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-xl overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Solicitar BAJA</h2>
                <div className="text-sm text-gray-600">
                  OS: {formatOSHelper(data, id)} - NS: {serialLabel}
                </div>
              </div>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-60"
                onClick={() => setSolicitarBajaOpen(false)}
                disabled={savingSolicitarBaja}
              >
                Cerrar
              </button>
            </div>

            <label className="block">
              <div className="text-xs text-gray-600 mb-1">Motivo (obligatorio)</div>
              <textarea
                className="border rounded p-2 w-full min-h-[120px]"
                value={solicitarBajaMotivo}
                onChange={(e) => setSolicitarBajaMotivo(e.target.value)}
                placeholder="Describí por qué se solicita la BAJA del equipo"
                disabled={savingSolicitarBaja}
              />
            </label>

            {hasPendingBajaRequest && (
              <div className="mt-2 text-xs text-amber-700">
                Ya hay una solicitud pendiente
                {data?.baja_solicitada_nombre ? ` de ${data.baja_solicitada_nombre}` : ""}
                {data?.baja_solicitada_fecha ? ` (${formatDateTimeHelper(data.baja_solicitada_fecha)})` : ""}.
              </div>
            )}

            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                type="button"
                className="px-3 py-2 rounded border bg-white hover:bg-gray-50 disabled:opacity-60"
                onClick={() => setSolicitarBajaOpen(false)}
                disabled={savingSolicitarBaja}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="px-3 py-2 rounded bg-red-700 text-white hover:bg-red-800 disabled:opacity-60"
                onClick={enviarSolicitudBaja}
                disabled={savingSolicitarBaja || !String(solicitarBajaMotivo || "").trim() || hasPendingBajaRequest}
              >
                {savingSolicitarBaja ? "Enviando..." : "Enviar solicitud"}
              </button>
            </div>
          </div>
        </div>
      )}

      {histModalOpen && canForceHistorical && (
        <div
          className="fixed inset-0 z-30 flex items-start justify-center overflow-y-auto bg-black/50 p-2 sm:p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!histSaving) setHistModalOpen(false); }}
        >
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-2xl overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Corrección histórica</h2>
                <div className="text-sm text-gray-600">
                  OS: {formatOSHelper(data, id)} - NS: {serialLabel}
                </div>
              </div>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-60"
                onClick={() => setHistModalOpen(false)}
                disabled={histSaving}
              >
                Cerrar
              </button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
              <label className="block">
                <div className="text-xs text-gray-600 mb-1">Acción</div>
                <select
                  className="border rounded p-2 w-full"
                  value={histForm.accion}
                  onChange={(e) => {
                    const next = e.target.value;
                    setHistForm((s) => ({ ...s, accion: next, notificar: true }));
                  }}
                  disabled={histSaving}
                >
                  <option value="entrega">Entrega</option>
                  <option value="alta_alquiler">Alta de alquiler</option>
                  <option value="baja_alquiler">Baja de alquiler</option>
                  <option value="baja_ingreso">Baja de ingreso</option>
                  <option value="alta_ingreso">Alta de ingreso</option>
                </select>
              </label>

              <label className="block">
                <div className="text-xs text-gray-600 mb-1">Fecha efectiva</div>
                <input
                  type="date"
                  className="border rounded p-2 w-full"
                  value={histForm.fecha_efectiva}
                  onChange={(e) => setHistForm((s) => ({ ...s, fecha_efectiva: e.target.value }))}
                  max={maxDateOnly}
                  disabled={histSaving}
                />
              </label>

              {histForm.accion === "entrega" && (
                <>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Remito salida</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={histForm.remito_salida}
                      onChange={(e) => setHistForm((s) => ({ ...s, remito_salida: e.target.value }))}
                      disabled={histSaving}
                    />
                  </label>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Factura</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={histForm.factura_numero}
                      onChange={(e) => setHistForm((s) => ({ ...s, factura_numero: e.target.value }))}
                      disabled={histSaving}
                    />
                  </label>
                  <label className="block md:col-span-2">
                    <div className="text-xs text-gray-600 mb-1">Fecha entrega (opcional)</div>
                    <input
                      type="date"
                      className="border rounded p-2 w-full"
                      value={histForm.fecha_entrega}
                      onChange={(e) => setHistForm((s) => ({ ...s, fecha_entrega: e.target.value }))}
                      max={maxDateOnly}
                      disabled={histSaving}
                    />
                  </label>
                </>
              )}

              {histForm.accion === "alta_alquiler" && (
                <>
                  <label className="block md:col-span-2">
                    <div className="text-xs text-gray-600 mb-1">Alquiler a</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={histForm.alquiler_a}
                      onChange={(e) => setHistForm((s) => ({ ...s, alquiler_a: e.target.value }))}
                      disabled={histSaving}
                    />
                  </label>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Remito alquiler</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={histForm.alquiler_remito}
                      onChange={(e) => setHistForm((s) => ({ ...s, alquiler_remito: e.target.value }))}
                      disabled={histSaving}
                    />
                  </label>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Fecha alquiler</div>
                    <input
                      type="date"
                      className="border rounded p-2 w-full"
                      value={histForm.alquiler_fecha}
                      onChange={(e) => setHistForm((s) => ({ ...s, alquiler_fecha: e.target.value }))}
                      max={maxDateOnly}
                      disabled={histSaving}
                    />
                  </label>
                </>
              )}

              {(histForm.accion === "baja_ingreso" || histForm.accion === "alta_ingreso") && (
                <label className="inline-flex items-center gap-2 md:col-span-2">
                  <input
                    type="checkbox"
                    checked={!!histForm.notificar}
                    onChange={(e) => setHistForm((s) => ({ ...s, notificar: e.target.checked }))}
                    disabled={histSaving}
                  />
                  <span>Enviar notificación por correo</span>
                </label>
              )}

              <label className="block md:col-span-2">
                <div className="text-xs text-gray-600 mb-1">Motivo (obligatorio)</div>
                <textarea
                  className="border rounded p-2 w-full min-h-[90px]"
                  value={histForm.motivo}
                  onChange={(e) => setHistForm((s) => ({ ...s, motivo: e.target.value }))}
                  disabled={histSaving}
                />
              </label>
            </div>

            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                type="button"
                className="px-3 py-2 rounded border bg-white hover:bg-gray-50 disabled:opacity-60"
                onClick={() => setHistModalOpen(false)}
                disabled={histSaving}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="px-3 py-2 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-60"
                onClick={guardarCorreccionHistorica}
                disabled={histSaving || !String(histForm.motivo || "").trim() || !String(histForm.fecha_efectiva || "").trim()}
              >
                {histSaving ? "Guardando..." : "Guardar corrección"}
              </button>
            </div>
          </div>
        </div>
      )}

      {mgModalOpen && canManageMgFromMenu && (
        <div
          className="fixed inset-0 z-30 flex items-start justify-center overflow-y-auto bg-black/50 p-2 sm:p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!mgSaving) setMgModalOpen(false); }}
        >
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-2xl overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Venta del equipo (MG)</h2>
                <div className="text-sm text-gray-600">
                  {serialLabel}
                </div>
              </div>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-60"
                onClick={() => setMgModalOpen(false)}
                disabled={mgSaving}
              >
                Cerrar
              </button>
            </div>

            {data?.mg_inactivo_venta ? (
              <div className="space-y-3 text-sm">
                <div className="text-amber-700">
                  MG histórico inactivo por venta; no operativo para nuevos ingresos.
                </div>
                <div>Fecha venta: {data?.mg_venta_fecha ? formatDateTimeHelper(data.mg_venta_fecha) : "-"}</div>
                <div>Factura venta: {data?.mg_venta_factura_numero || "-"}</div>
                <div>Remito venta: {data?.mg_venta_remito_numero || "-"}</div>
                <div>Vendido a: {data?.mg_venta_customer_nombre || "-"}</div>
                <div>Número alternativo: {data?.mg_venta_numero_alternativo || "-"}</div>
                <div>Observaciones: {data?.mg_venta_observaciones || "-"}</div>
                <label className="block">
                  <div className="text-xs text-gray-600 mb-1">Observaciones de reactivación</div>
                  <textarea
                    className="border rounded p-2 w-full min-h-[80px]"
                    value={mgForm.observaciones}
                    onChange={(e) => setMgForm((s) => ({ ...s, observaciones: e.target.value }))}
                    disabled={mgSaving}
                  />
                </label>
                <button
                  className="px-3 py-2 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60"
                  onClick={reactivarMgMenu}
                  disabled={mgSaving}
                  type="button"
                >
                  {mgSaving ? "Guardando..." : "Reactivar MG"}
                </button>
              </div>
            ) : (
              <div className="space-y-3 text-sm">
                <div className="text-gray-600">Registrar venta del equipo para desactivar MG operativo.</div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <label className="block md:col-span-2">
                    <div className="text-xs text-gray-600 mb-1">Vendido a (cliente)</div>
                    <div className="flex gap-2">
                      <select
                        className="border rounded p-2 w-full"
                        value={mgForm.venta_customer_id}
                        onChange={(e) => setMgForm((s) => ({ ...s, venta_customer_id: e.target.value }))}
                        disabled={mgSaving}
                      >
                        <option value="">Seleccionar cliente</option>
                        {(clientes || []).map((c) => (
                          <option key={c.id} value={String(c.id)}>
                            {c.razon_social}{c.alias_interno ? ` [${c.alias_interno}]` : ""}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        className="px-3 py-2 rounded border whitespace-nowrap hover:bg-gray-50 disabled:opacity-60"
                        onClick={abrirAltaRapidaClienteMg}
                        disabled={mgSaving}
                      >
                        Alta rápida
                      </button>
                    </div>
                  </label>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Factura venta</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={mgForm.factura_numero}
                      onChange={(e) => setMgForm((s) => ({ ...s, factura_numero: e.target.value }))}
                      disabled={mgSaving}
                    />
                  </label>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Remito venta</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={mgForm.remito_numero}
                      onChange={(e) => setMgForm((s) => ({ ...s, remito_numero: e.target.value }))}
                      disabled={mgSaving}
                    />
                  </label>
                  <label className="block">
                    <div className="text-xs text-gray-600 mb-1">Número alternativo</div>
                    <input
                      className="border rounded p-2 w-full"
                      value={mgForm.venta_numero_alternativo}
                      onChange={(e) => setMgForm((s) => ({ ...s, venta_numero_alternativo: e.target.value }))}
                      disabled={mgSaving}
                    />
                  </label>
                  <label className="block md:col-span-2">
                    <div className="text-xs text-gray-600 mb-1">Fecha venta</div>
                    <input
                      type="date"
                      className="border rounded p-2 w-full"
                      value={mgForm.fecha_venta}
                      onChange={(e) => setMgForm((s) => ({ ...s, fecha_venta: e.target.value }))}
                      max={maxDateOnly}
                      disabled={mgSaving}
                    />
                  </label>
                  <label className="block md:col-span-2">
                    <div className="text-xs text-gray-600 mb-1">Observaciones</div>
                    <textarea
                      className="border rounded p-2 w-full min-h-[80px]"
                      value={mgForm.observaciones}
                      onChange={(e) => setMgForm((s) => ({ ...s, observaciones: e.target.value }))}
                      disabled={mgSaving}
                    />
                  </label>
                </div>
                <div className="text-xs text-gray-500">Se requiere factura o remito.</div>
                <button
                  className="px-3 py-2 rounded bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-60"
                  onClick={guardarVentaMgMenu}
                  disabled={
                    mgSaving
                    || !Number(mgForm.venta_customer_id || 0)
                    || !((mgForm.factura_numero || "").trim() || (mgForm.remito_numero || "").trim())
                  }
                  type="button"
                >
                  {mgSaving ? "Guardando..." : "Registrar venta"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {mgAddCustomerOpen && (
        <div
          className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/60 p-2 sm:p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!mgAddCustomerSaving) setMgAddCustomerOpen(false); }}
        >
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-lg overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <h3 className="text-lg font-semibold">Alta rápida de cliente</h3>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-60"
                onClick={() => setMgAddCustomerOpen(false)}
                disabled={mgAddCustomerSaving}
              >
                Cerrar
              </button>
            </div>
            {mgAddCustomerErr && (
              <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-3 text-sm">
                {mgAddCustomerErr}
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
              <label className="block md:col-span-2">
                <div className="text-xs text-gray-600 mb-1">Razón social</div>
                <input
                  className="border rounded p-2 w-full"
                  value={mgAddCustomerForm.razon_social}
                  onChange={(e) => setMgAddCustomerForm((s) => ({ ...s, razon_social: e.target.value }))}
                  disabled={mgAddCustomerSaving}
                />
              </label>
              <label className="block md:col-span-2">
                <div className="text-xs text-gray-600 mb-1">Código de empresa</div>
                <input
                  className="border rounded p-2 w-full"
                  value={mgAddCustomerForm.cod_empresa}
                  onChange={(e) => setMgAddCustomerForm((s) => ({ ...s, cod_empresa: e.target.value }))}
                  disabled={mgAddCustomerSaving}
                />
              </label>
              <label className="block">
                <div className="text-xs text-gray-600 mb-1">Teléfono</div>
                <input
                  className="border rounded p-2 w-full"
                  value={mgAddCustomerForm.telefono}
                  onChange={(e) => setMgAddCustomerForm((s) => ({ ...s, telefono: e.target.value }))}
                  disabled={mgAddCustomerSaving}
                />
              </label>
              <label className="block">
                <div className="text-xs text-gray-600 mb-1">Teléfono 2</div>
                <input
                  className="border rounded p-2 w-full"
                  value={mgAddCustomerForm.telefono_2}
                  onChange={(e) => setMgAddCustomerForm((s) => ({ ...s, telefono_2: e.target.value }))}
                  disabled={mgAddCustomerSaving}
                />
              </label>
              <label className="block md:col-span-2">
                <div className="text-xs text-gray-600 mb-1">Email</div>
                <input
                  type="email"
                  className="border rounded p-2 w-full"
                  value={mgAddCustomerForm.email}
                  onChange={(e) => setMgAddCustomerForm((s) => ({ ...s, email: e.target.value }))}
                  disabled={mgAddCustomerSaving}
                />
              </label>
            </div>
            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                type="button"
                className="px-3 py-2 rounded border bg-white hover:bg-gray-50 disabled:opacity-60"
                onClick={() => setMgAddCustomerOpen(false)}
                disabled={mgAddCustomerSaving}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="px-3 py-2 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60"
                onClick={crearClienteRapidoMg}
                disabled={
                  mgAddCustomerSaving
                  || !String(mgAddCustomerForm.razon_social || "").trim()
                  || !String(mgAddCustomerForm.cod_empresa || "").trim()
                }
              >
                {mgAddCustomerSaving ? "Guardando..." : "Crear cliente"}
              </button>
            </div>
          </div>
        </div>
      )}

      {propioMgModalOpen && canConvertToOwnMg && (
        <div
          className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/60 p-2 sm:p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!propioMgSaving) setPropioMgModalOpen(false); }}
        >
          <div className="my-2 max-h-[calc(100dvh-1rem)] w-full max-w-xl overflow-y-auto rounded bg-white p-3 shadow-xl sm:my-4 sm:max-h-[calc(100dvh-2rem)] sm:p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Alta de MG</h2>
                <div className="text-sm text-gray-600">
                  Este equipo pasará a pertenecer a MG BIO como equipo propio y conservará el mismo historial.
                </div>
              </div>
              <button
                type="button"
                className="text-sm text-gray-500 hover:text-gray-900 disabled:opacity-60"
                onClick={() => setPropioMgModalOpen(false)}
                disabled={propioMgSaving}
              >
                Cerrar
              </button>
            </div>

            <div className="space-y-3 text-sm">
              <div className="rounded border border-emerald-200 bg-emerald-50 p-3">
                <div><span className="font-medium">Equipo:</span> {data?.marca || "-"} {data?.modelo || ""}</div>
                <div><span className="font-medium">Cliente actual:</span> {data?.razon_social || "-"}</div>
                <div><span className="font-medium">N/S actual:</span> {(data?.numero_serie || "").trim() || "-"}</div>
                <div><span className="font-medium">Motivo:</span> {data?.motivo || "-"}</div>
              </div>

              <label className="block">
                <div className="text-xs text-gray-600 mb-1">Número interno MG</div>
                <input
                  className="border rounded p-2 w-full"
                  placeholder="MG 0123"
                  value={propioMgForm.numero_interno}
                  onChange={(e) => setPropioMgForm((s) => ({ ...s, numero_interno: e.target.value }))}
                  disabled={propioMgSaving}
                />
              </label>

              <div className="text-xs text-gray-500">
                Se validará que el MG no exista en otro equipo antes de guardar.
              </div>

              <div className="flex items-center justify-end gap-2 pt-1">
                <button
                  type="button"
                  className="px-3 py-2 rounded border bg-white hover:bg-gray-50 disabled:opacity-60"
                  onClick={() => setPropioMgModalOpen(false)}
                  disabled={propioMgSaving}
                >
                  Cancelar
                </button>
                <button
                  type="button"
                  className="px-3 py-2 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60"
                  onClick={guardarAltaMg}
                  disabled={propioMgSaving || !String(propioMgForm.numero_interno || "").trim()}
                >
                  {propioMgSaving ? "Guardando..." : "Dar de alta MG"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <IdentifierConflictModal
        conflict={identifierConflict}
        canMerge={canManageDevices}
        onClose={closeIdentifierConflict}
        onEditDevice={editConflictDevice}
        onMerge={mergeIdentifierConflict}
        onChange={updateIdentifierConflict}
      />

      {toastMsg && (
        <div className="fixed left-3 right-3 top-3 bg-emerald-600 text-white px-4 py-2 rounded shadow-lg sm:left-auto sm:right-4 sm:top-4" role="status">
          {toastMsg}
        </div>
      )}
      {showReparadoToast && (
        <div className="fixed left-3 right-3 top-3 bg-emerald-600 text-white px-4 py-2 rounded shadow-lg sm:left-auto sm:right-4 sm:top-4" role="status">
          Marcado como reparado
        </div>
      )}

      {/* HISTORIAL */}
      {activeTab === "historial" && canViewHistorialTab && (
        <HistorialTab hErr={hErr} hLoading={hLoading} hist={hist} />
      )}

      {/* Botón flotante para edición básica */}
      {canEditBasics && (
        <div className="fixed bottom-3 left-3 right-3 z-20 flex justify-end gap-2 sm:left-auto sm:right-4">
          {!editBasics ? (
            <button className="text-xs px-3 py-2 rounded shadow bg-neutral-800 text-white hover:bg-neutral-700" onClick={startEditBasics} type="button" title="Habilitar edición de datos">
              Editar datos
            </button>
          ) : (
            <>
              <button className="text-xs px-3 py-2 rounded shadow bg-amber-600 text-white disabled:opacity-60" onClick={saveEditBasics} disabled={savingBasics} type="button" title="Cerrar edición y guardar cambios">
                {savingBasics ? "Guardando..." : "Cerrar edición"}
              </button>
              <button className="text-xs px-3 py-2 rounded shadow bg-gray-200 hover:bg-gray-300" onClick={() => { setEditBasics(false); setFormBasics(null); }} type="button" title="Cancelar edición">
                Cancelar
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
