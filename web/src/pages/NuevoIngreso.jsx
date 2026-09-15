// web/src/pages/NuevoIngreso.jsx (UTF-8 authoring; will be re-encoded to Windows-1252)
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import {
  getClientes,
  getMarcas,
  getModelosByBrand,
  getTecnicos,
  postNuevoIngreso,
  postNuevoIngresoLote,
  getMotivos,
  checkGarantiaReparacion,
  checkGarantiaFabrica,
  getAccesoriosCatalogo,
  getTiposEquipo,
  getMarcasPorTipo,
  getVariantesPorModelo,
  lookupScan,
  bejermanLookupErrorMessage,
  getBejermanIngressCompanies,
  postRisPreflight,
  postRisPreflightCustomerFix,
  postRisPreflightArticleFix,
  postIngresoRisEmitir,
  postBejermanPdfQueueRetry,
  getSerialBarcodeBlob,
} from "@/lib/api";
import { isPendingPdfError, openPdfBlob, PDF_REPRINT_STEPS } from "@/lib/pdf";
import { MOTIVO } from "@/lib/constants";
import {
  equipmentKeyOf,
  findDuplicateEquipmentIndex,
  findStagedEquipmentIndex,
} from "@/lib/ingreso-equipos";
import {
  openRisPrintablePdf,
  openReservedRisPrintablePdf,
  reserveRisPrintWindow,
  risPrintUrlFor,
  risRemitoFrom,
  risRemitoNumbersFrom,
  waitForRisPdfBlob,
} from "@/lib/ris-print";
import { useAuth } from "@/context/AuthContext";
import { can, canAny, PERMISSION_CODES } from "@/lib/permissions";
import RisProgressModal, {
  waitForRisProgressMinimum,
  waitForRisProgressPaint,
} from "@/components/RisProgressModal";
import RisPreflightPanel from "@/components/RisPreflightPanel";
import BejermanPurchaseEntries from "./BejermanPurchaseEntries.jsx";

const FIELD_BASE_CLASS =
  "border border-gray-300 bg-white rounded p-2 w-full text-[15px] font-semibold text-gray-900 placeholder:text-gray-400";
const FIELD_LABEL_CLASS = "block text-xs font-semibold tracking-wide text-gray-600 mb-1";
const Input = ({ className = "", readOnly = false, ...p }) => (
  <input
    {...p}
    readOnly={readOnly}
    className={`${FIELD_BASE_CLASS} ${readOnly ? "bg-gray-50 text-gray-700 font-medium" : ""} ${className}`}
  />
);
const Select = ({ className = "", ...p }) => (
  <select {...p} className={`${FIELD_BASE_CLASS} ${className}`} />
);
const TextArea = ({ className = "", ...p }) => (
  <textarea {...p} className={`${FIELD_BASE_CLASS} ${className}`} />
);

const CollapsibleSection = ({ title, open, onToggle, incomplete = false, children }) => (
  <fieldset className="border rounded p-0">
    <legend className="ml-2 px-0">
      <button
        type="button"
        className="flex items-center gap-2 px-2 py-1 text-left font-semibold text-gray-900 hover:text-blue-700"
        onClick={onToggle}
        aria-expanded={open}
      >
        <span
          aria-hidden="true"
          className="inline-flex h-5 w-5 items-center justify-center rounded border border-gray-300 text-xs font-bold"
        >
          {open ? "-" : "+"}
        </span>
        <span>{title}</span>
        {!open && incomplete && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800">
            Falta completar
          </span>
        )}
      </button>
    </legend>
    {open && <div className="p-3 pt-1">{children}</div>}
  </fieldset>
);

const TIPO_INGRESO = {
  CLIENTE: "cliente",
  PARTICULAR: "particular",
};
const PROPIETARIO_VACIO = { nombre: "", contacto: "", doc: "" };
const DEFAULT_BEJERMAN_COMPANIES = [
  { key: "SEPID", label: "SEPID SA", brandingKey: "SEPID", isTest: false },
  { key: "MGBIO", label: "MG BIO", brandingKey: "MGBIO", isTest: false },
  { key: "TEST", label: "Empresa de prueba", brandingKey: "TEST", isTest: true },
];
const RENTAL_RETURN_MOTIVOS = new Set([
  MOTIVO.BAJA_ALQUILER,
  MOTIVO.REPARACION_ALQUILER,
  MOTIVO.OTROS,
]);
const MG_OWNED_BLOCKED_MOTIVOS = new Set([MOTIVO.REPARACION, MOTIVO.COTIZACION_EQUIPO]);

function scrollPageTop() {
  try {
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  } catch (_) {
    try {
      window.scrollTo(0, 0);
    } catch (_) {}
  }
}

// clone helper (fallback if structuredClone is missing)
function clone(obj) {
  try {
    return typeof structuredClone === "function" ? structuredClone(obj) : JSON.parse(JSON.stringify(obj));
  } catch (_) {
    return JSON.parse(JSON.stringify(obj));
  }
}

function cleanText(value) {
  return String(value || "").trim();
}

function rentalReturnCustomerFromPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const rental = payload.rental_return_customer || payload.rentalReturnCustomer || {};
  if (!rental || typeof rental !== "object") return null;
  const name = cleanText(
    rental.razon_social ||
      rental.customer_nombre ||
      rental.name ||
      rental.alquiler_a
  );
  if (!name) return null;
  return {
    id: rental.id || rental.customer_id || null,
    name,
    cod: cleanText(rental.cod_empresa || rental.customer_cod),
    telefono: cleanText(rental.telefono || rental.customer_telefono),
  };
}

function documentProfileFromResult(result) {
  if (!result || typeof result !== "object") return null;
  return (
    result.document_profile ||
    result.documentProfile ||
    result.preview?.document_profile ||
    result.preview?.documentProfile ||
    null
  );
}

function documentTypeFromResult(result, fallback = "remito") {
  return cleanText(documentProfileFromResult(result)?.type) || fallback;
}

function documentDisplayName(documentType, fallback = "remito") {
  const type = cleanText(documentType);
  if (!type || type.toLowerCase() === "remito") return fallback;
  return `${fallback} ${type}`;
}

function hasDocumentProfileMismatch(result) {
  return Array.isArray(result?.issues) && result.issues.some((issue) => issue?.code === "INGRESO_DOCUMENT_PROFILE_MISMATCH");
}

function remitoNumbersLabel(source) {
  const numbers = risRemitoNumbersFrom(source);
  return numbers.length > 1 ? numbers.join(" · ") : numbers[0] || "";
}

function remitoDocumentLabel(source) {
  const numbers = risRemitoNumbersFrom(source);
  if (numbers.length > 1) return `${numbers.length} remitos`;
  return numbers[0] ? `Remito ${numbers[0]}` : "";
}

export default function NuevoIngreso() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const canCreateEquipmentIngreso = canAny(user, [
    PERMISSION_CODES.ACTION_INGRESO_CREATE,
    PERMISSION_CODES.PAGE_NEW_INGRESO,
  ]);
  const canMercaderiaIngreso = can(user, PERMISSION_CODES.PAGE_BEJERMAN_PURCHASE_ENTRIES);
  const requestedIngresoTab = (searchParams.get("tab") || "").trim().toLowerCase();
  const ingresoTab =
    requestedIngresoTab === "mercaderia" && canMercaderiaIngreso
      ? "mercaderia"
      : requestedIngresoTab === "equipos" && canCreateEquipmentIngreso
        ? "equipos"
        : canCreateEquipmentIngreso
          ? "equipos"
          : "mercaderia";
  const setIngresoTab = (tab) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", tab);
    setSearchParams(next, { replace: true });
    scrollPageTop();
  };
  const canAutoOpenCreatedIngreso = canAny(user, [
    PERMISSION_CODES.PAGE_INGRESOS_HISTORY,
    PERMISSION_CODES.PAGE_WORK_QUEUES,
    PERMISSION_CODES.PAGE_BUDGET_QUEUES,
    PERMISSION_CODES.PAGE_LOGISTICS,
    PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_BASICS,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_DIAGNOSIS,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_LOCATION,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY,
    PERMISSION_CODES.ACTION_INGRESO_MANAGE_DERIVATIONS,
    PERMISSION_CODES.ACTION_INGRESO_REPAIR_TRANSITIONS,
    PERMISSION_CODES.ACTION_INGRESO_BAJA_ALTA,
  ]);
  const canViewCreatedIngreso = canAny(user, [
    PERMISSION_CODES.PAGE_SERVICE_SHEET_PRINCIPAL,
    PERMISSION_CODES.PAGE_INGRESOS_HISTORY,
    PERMISSION_CODES.PAGE_WORK_QUEUES,
    PERMISSION_CODES.PAGE_BUDGET_QUEUES,
    PERMISSION_CODES.PAGE_LOGISTICS,
    PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_BASICS,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_DIAGNOSIS,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_LOCATION,
    PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY,
    PERMISSION_CODES.ACTION_INGRESO_MANAGE_DERIVATIONS,
    PERMISSION_CODES.ACTION_INGRESO_REPAIR_TRANSITIONS,
    PERMISSION_CODES.ACTION_INGRESO_BAJA_ALTA,
  ]);
  const prefillRef = useRef(null);
  const prefillAppliedRef = useRef(false);
  const prefillModelAppliedRef = useRef(false);
  const prefillClienteAppliedRef = useRef(false);
  const prefillSkipTipoResetRef = useRef(false);
  const prefillTipoAppliedRef = useRef(false);

  // Catálogos base
  const [marcas, setMarcas] = useState([]);
  const [motivos, setMotivos] = useState([]);
  const [modelos, setModelos] = useState([]);

  // Marca y tipo
  const [marcaTxt, setMarcaTxt] = useState("");
  const [marcaId, setMarcaId] = useState(null);
  const [tiposEquipo, setTiposEquipo] = useState([]);
  const [tipoSel, setTipoSel] = useState("");
  const [marcasPorTipo, setMarcasPorTipo] = useState([]);

  // Clientes (autocompletar)
  const [clientes, setClientes] = useState([]);
  const [clienteRsInput, setClienteRsInput] = useState("");
  const [clienteCodInput, setClienteCodInput] = useState("");
  const [tipoIngreso, setTipoIngreso] = useState(TIPO_INGRESO.CLIENTE);

  // Variantes (opcional)
  const [varianteTxt, setVarianteTxt] = useState("");
  const [varianteSugeridas, setVarianteSugeridas] = useState([]);
  const [equipoAlquiladoOrigen, setEquipoAlquiladoOrigen] = useState(false);
  const [esEquipoPropioMg, setEsEquipoPropioMg] = useState(false);

  // Form principal
  const [form, setForm] = useState({
    etiq_garantia_ok: false,
    cliente: { id: null, razon_social: "", cod_empresa: "", telefono: "" },
    equipo: {
      marca_id: "",
      modelo_id: "",
      numero_serie: "",
      numero_interno: "",
      garantia: false,
    },
    motivo: "",
    informe_preliminar: "",
    comentarios: "",
    garantia_reparacion: false,
    remito_ingreso: "",
    fecha_ingreso: "",
  });

  // Accesorios
  const [accesCatalogo, setAccesCatalogo] = useState([]);
  const [nuevoAcc, setNuevoAcc] = useState({ descripcion: "", referencia: "" });
  const [accItems, setAccItems] = useState([]);
  const [batchItems, setBatchItems] = useState([]);
  const [editingBatchItemId, setEditingBatchItemId] = useState(null);
  useEffect(() => {
    batchItemsRef.current = batchItems;
  }, [batchItems]);
  useEffect(() => {
    editingBatchItemIdRef.current = editingBatchItemId;
  }, [editingBatchItemId]);

  // Propietario y técnico
  const [propietario, setPropietario] = useState(PROPIETARIO_VACIO);
  const [tecnicos, setTecnicos] = useState([]);
  const [tecnicoId, setTecnicoId] = useState(null);
  // Empresa Bejerman (SEPID por defecto)
  const [bejermanCompanies, setBejermanCompanies] = useState(DEFAULT_BEJERMAN_COMPANIES);
  const [empresaBejerman, setEmpresaBejerman] = useState("SEPID");
  const selectedBejermanCompany = useMemo(
    () => bejermanCompanies.find((item) => item.key === empresaBejerman) || DEFAULT_BEJERMAN_COMPANIES[0],
    [bejermanCompanies, empresaBejerman]
  );

  const [loading, setLoading] = useState(false);
  const [submitStage, setSubmitStage] = useState("");
  const [out, setOut] = useState(null);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [manualRisPrint, setManualRisPrint] = useState(null);
  // Espera del PDF: se puede cancelar y tiene tope. Antes giraba sin límite.
  const [pdfWait, setPdfWait] = useState({ startedAt: 0, remito: "" });
  const [pendingPdfRetry, setPendingPdfRetry] = useState(null);
  const [pdfRetryBusy, setPdfRetryBusy] = useState(false);
  const pdfWaitAbortRef = useRef(null);
  const [risMode, setRisMode] = useState("emit");
  const [manualRemitoNumber, setManualRemitoNumber] = useState("");
  const [risPreflight, setRisPreflight] = useState(null);
  const [risPreflightLoading, setRisPreflightLoading] = useState(false);
  const [risPreflightError, setRisPreflightError] = useState("");
  const [bejermanSuggestion, setBejermanSuggestion] = useState(null);
  const [appliedBejermanSale, setAppliedBejermanSale] = useState(null);
  const [barcodeLoading, setBarcodeLoading] = useState(false);
  const [dupPrompt, setDupPrompt] = useState({ open: false, ingresoId: null, fechaIngreso: null, os: "" });

  const [clientesPerm, setClientesPerm] = useState(true);
  const [garRepLoading, setGarRepLoading] = useState(false);
  const [garRepError, setGarRepError] = useState(false);
  const [autofillBy, setAutofillBy] = useState("serie");
  const [lookupRequest, setLookupRequest] = useState({ nonce: 0, source: "serie", code: "" });
  // Dos equipos con el mismo N/S (el CPAP y su calentador humidificador, que no trae serie
  // propia). Antes el backend elegía uno en silencio y el formulario se llenaba con el
  // equipo equivocado; ahora se pregunta.
  const [deviceChoice, setDeviceChoice] = useState({
    open: false,
    candidates: [],
    code: "",
    source: "serie",
    reason: "ambiguous",
    stagedIndex: -1,
  });
  const [mgLookup, setMgLookup] = useState({ loading: false, notFound: false, checkedNs: "" });
  const [mgInactiveInfo, setMgInactiveInfo] = useState(null);
  // El equipo que devolvió el autocompletado ya tiene una OS abierta de hoy: es el remito
  // cargado por segunda vez. Se guarda con la clave del equipo (serie + tipo) para que el
  // bloqueo se suelte solo apenas el operador cambie alguno de los dos.
  const [ingresoDeHoy, setIngresoDeHoy] = useState(null);
  const [nsAutofillInfo, setNsAutofillInfo] = useState("");
  const [nsAutofillClienteWarning, setNsAutofillClienteWarning] = useState("");
  // Aviso de "esto es otro equipo con el mismo número": qué equipo tenía la base con ese
  // número y qué le queda por completar al operador.
  const [otroEquipoMismoNs, setOtroEquipoMismoNs] = useState(null);
  const mgLookupSeqRef = useRef(0);
  const lookupRequestSeqRef = useRef(0);
  const nsAutofillSnapshotRef = useRef(null);
  const nsAutoDesiredBrandRef = useRef(null);
  const nsAutoDesiredModelRef = useRef(null);
  // El último código ya resuelto y el código para el que el operador declaró carga manual.
  // Sin esto, cada vez que el foco pasaba por el campo de N/S se repetía la búsqueda y el
  // autocompletado volvía a pisar lo que el operador acababa de corregir: era imposible
  // cargar el humidificador que comparte serie con su CPAP.
  const lastLookupRef = useRef({ key: "" });
  const manualEntryRef = useRef({ key: "" });
  // La lista de equipos ya cargados y el renglón en edición se leen dentro del efecto de
  // lookup, que no puede depender de ellos (cambiarlos volvería a disparar la búsqueda).
  const batchItemsRef = useRef([]);
  const editingBatchItemIdRef = useRef(null);
  const [expandedSections, setExpandedSections] = useState({
    cliente: false,
    equipo: false,
    ingreso: false,
  });
  const collapseDataSections = () =>
    setExpandedSections({ cliente: false, equipo: false, ingreso: false });
  // Tras un autocompletado (N/S, MG, escaneo o venta Bejerman) se abren los tres
  // bloques: el operador tiene que revisar lo que se cargó y completar lo que falta.
  const expandAllSections = () =>
    setExpandedSections({ cliente: true, equipo: true, ingreso: true });
  const expandSection = (section) => {
    setExpandedSections((prev) => (prev[section] ? prev : { ...prev, [section]: true }));
  };
  const toggleSection = (section) => {
    setExpandedSections((prev) => ({ ...prev, [section]: !prev[section] }));
  };

  // Helpers de clientes
  const clienteKey = (v) => String(v || "").trim().toLowerCase();
  const findClienteByRS = (v) => {
    const needle = clienteKey(v);
    if (!needle) return null;
    return (clientes || []).find((c) => clienteKey(c.razon_social) === needle || clienteKey(c.alias_interno) === needle) || null;
  };
  const findClienteByCod = (v) =>
    (clientes || []).find((c) => clienteKey(c.cod_empresa) === clienteKey(v));

  function resolveCliente(rsVal, codVal) {
    const byRs = rsVal ? findClienteByRS(rsVal) : null;
    const byCod = codVal ? findClienteByCod(codVal) : null;
    if (byRs && !codVal) return byRs;
    if (byCod && !rsVal) return byCod;
    if (byRs && byCod && byRs.id === byCod.id) return byRs;
    return null;
  }

  function syncClienteFromInputs(rsVal, codVal) {
    const c = resolveCliente(rsVal, codVal);
    if (rsVal || codVal) expandSection("cliente");
    setForm((f0) => {
      const f = clone(f0);
      f.cliente = {
        id: c?.id || null,
        razon_social: rsVal || "",
        cod_empresa: codVal || "",
        telefono: c?.telefono || "",
      };
      return f;
    });
  }

  const toIntOrNull = (v) => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  };

  const normalizeSerialKey = (value) =>
    String(value || "")
      .trim()
      .replace(/[\s\-_./]+/g, "")
      .toUpperCase();

  const normalizeCustomerName = (value) =>
    String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "");

  const isParticularCustomerName = (value) => normalizeCustomerName(value) === "particular";
  const isParticularCustomer = (customer) => isParticularCustomerName(customer?.razon_social);
  const isParticularIngreso = tipoIngreso === TIPO_INGRESO.PARTICULAR;

  const particularCliente = useMemo(
    () => (clientes || []).find((c) => isParticularCustomer(c)) || null,
    [clientes]
  );

  const clientesIngreso = useMemo(
    () =>
      (Array.isArray(clientes) ? clientes : []).filter((c) =>
        isParticularIngreso ? isParticularCustomer(c) : !isParticularCustomer(c)
      ),
    [clientes, isParticularIngreso]
  );
  const motivosIngreso = useMemo(() => {
    const options = Array.isArray(motivos) ? motivos : [];
    let filtered = options;
    if (equipoAlquiladoOrigen) {
      filtered = filtered.filter((m) => RENTAL_RETURN_MOTIVOS.has(String(m?.value || "").trim()));
    }
    if (esEquipoPropioMg) {
      filtered = filtered.filter((m) => !MG_OWNED_BLOCKED_MOTIVOS.has(String(m?.value || "").trim()));
    }
    return filtered;
  }, [motivos, equipoAlquiladoOrigen, esEquipoPropioMg]);

  useEffect(() => {
    if (!equipoAlquiladoOrigen || !form.motivo) return;
    if (RENTAL_RETURN_MOTIVOS.has(String(form.motivo || "").trim())) return;
    setForm((prev) => ({ ...prev, motivo: "" }));
  }, [equipoAlquiladoOrigen, form.motivo]);

  useEffect(() => {
    if (!esEquipoPropioMg || !form.motivo) return;
    if (!MG_OWNED_BLOCKED_MOTIVOS.has(String(form.motivo || "").trim())) return;
    setForm((prev) => ({ ...prev, motivo: "" }));
  }, [esEquipoPropioMg, form.motivo]);

  function setClienteCatalogo(c) {
    const razonSocial = c?.razon_social || "";
    const codigo = c?.cod_empresa || "";
    setClienteRsInput(razonSocial);
    setClienteCodInput(codigo);
    syncClienteFromInputs(razonSocial, codigo);
  }

  function clearClienteSeleccionado() {
    setClienteRsInput("");
    setClienteCodInput("");
    setForm((f0) => ({
      ...f0,
      cliente: { id: null, razon_social: "", cod_empresa: "", telefono: "" },
    }));
  }

  function seleccionarTipoIngreso(nextTipo) {
    const next = nextTipo === TIPO_INGRESO.PARTICULAR ? TIPO_INGRESO.PARTICULAR : TIPO_INGRESO.CLIENTE;
    setTipoIngreso(next);
    if (next === TIPO_INGRESO.PARTICULAR) {
      if (particularCliente) {
        setClienteCatalogo(particularCliente);
      } else {
        setClienteRsInput("Particular");
        setClienteCodInput("");
        syncClienteFromInputs("Particular", "");
      }
      return;
    }

    setPropietario(PROPIETARIO_VACIO);
    const clienteActual = resolveCliente(clienteRsInput, clienteCodInput);
    if (isParticularCustomer(clienteActual) || isParticularCustomerName(clienteRsInput)) {
      clearClienteSeleccionado();
    }
  }

  useEffect(() => {
    if (!isParticularIngreso || !particularCliente) return;
    const clienteActual = resolveCliente(clienteRsInput, clienteCodInput);
    if (Number(clienteActual?.id || 0) === Number(particularCliente.id)) return;
    setClienteCatalogo(particularCliente);
  }, [isParticularIngreso, particularCliente?.id]);

  useEffect(() => {
    if (isParticularIngreso) return;
    setPropietario((prev) => {
      if (!prev.nombre && !prev.contacto && !prev.doc) return prev;
      return PROPIETARIO_VACIO;
    });
  }, [isParticularIngreso]);

  const isInternalLookupReady = (value) => /^(MG|NM|NV|CE)\s*\d{4}$/i.test(String(value || "").trim());

  // Clave con la que se recuerda una búsqueda: el mismo número escrito con o sin espacios y
  // guiones es el mismo número.
  const lookupKey = (source, code) =>
    `${source}|${String(code || "").replace(/[\s-]/g, "").toUpperCase()}`;

  const forgetLookupMemory = () => {
    lastLookupRef.current = { key: "" };
    manualEntryRef.current = { key: "" };
  };

  const clearNsAutofillFields = () => {
    const snap = nsAutofillSnapshotRef.current;
    if (!snap) {
      setNsAutofillInfo("");
      setNsAutofillClienteWarning("");
      setEquipoAlquiladoOrigen(false);
      return;
    }

    setForm((prev) => {
      const next = clone(prev);
      if (snap.auto_ns) {
        next.equipo.numero_serie = "";
      }
      if (snap.auto_mg) {
        next.equipo.numero_interno = "";
      }
      if (snap.auto_marca) {
        next.equipo.marca_id = "";
        next.equipo.modelo_id = "";
      }
      if (snap.auto_modelo) {
        next.equipo.modelo_id = "";
      }
      if (snap.auto_cliente) {
        next.cliente.id = null;
        next.cliente.razon_social = "";
        next.cliente.cod_empresa = "";
        next.cliente.telefono = "";
      }
      return next;
    });

    if (snap.auto_marca) {
      setMarcaId(null);
      setMarcaTxt("");
    }
    if (snap.auto_tipo) setTipoSel("");
    if (snap.auto_variante) setVarianteTxt("");
    if (snap.auto_tecnico) setTecnicoId(null);
    if (snap.auto_cliente) {
      setClienteRsInput("");
      setClienteCodInput("");
    }
    if (snap.auto_propietario) {
      setPropietario(PROPIETARIO_VACIO);
    }

    nsAutofillSnapshotRef.current = null;
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
    setNsAutofillInfo("");
    setNsAutofillClienteWarning("");
    setEquipoAlquiladoOrigen(false);
    setEsEquipoPropioMg(false);
  };

  const triggerLookupRequest = (source, rawValue, { force = false } = {}) => {
    if (source !== autofillBy) return;
    const code = String(rawValue || "").trim();
    if (!code) {
      setMgLookup({ loading: false, notFound: false, checkedNs: "" });
      setMgInactiveInfo(null);
      clearNsAutofillFields();
      setOtroEquipoMismoNs(null);
      forgetLookupMemory();
      return;
    }
    if (source === "interno" && !isInternalLookupReady(code)) {
      setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
      setMgInactiveInfo(null);
      return;
    }
    const key = lookupKey(source, code);
    if (!force) {
      // El operador ya dijo que éste es otro equipo con el mismo número (el humidificador del
      // CPAP): traer de nuevo el equipo de la base sería pisarle todo lo que cargó a mano.
      if (manualEntryRef.current.key === key) return;
      // Mismo número que la última búsqueda: el campo pierde el foco muchas veces mientras se
      // completa el resto de la pantalla, y repetir la búsqueda revierte lo editado.
      if (lastLookupRef.current.key === key) return;
    }
    lastLookupRef.current = { key };
    const nonce = ++lookupRequestSeqRef.current;
    setLookupRequest({
      nonce,
      source,
      code,
      company_key: (empresaBejerman || "SEPID").toUpperCase(),
    });
  };

  const closeDeviceChoice = () =>
    setDeviceChoice({
      open: false,
      candidates: [],
      code: "",
      source: "serie",
      reason: "ambiguous",
      stagedIndex: -1,
    });

  const pickDeviceCandidate = (candidate) => {
    if (!candidate?.id) return;
    const { code, source } = deviceChoice;
    closeDeviceChoice();
    lastLookupRef.current = { key: lookupKey(source, code) };
    manualEntryRef.current = { key: "" };
    setOtroEquipoMismoNs(null);
    const nonce = ++lookupRequestSeqRef.current;
    // Se repite el lookup completo con el equipo elegido: así el autocompletado trae también
    // su cliente, su ingreso en curso y su venta en Bejerman, no sólo marca y modelo.
    setLookupRequest({
      nonce,
      source,
      code,
      company_key: (empresaBejerman || "SEPID").toUpperCase(),
      device_id: candidate.id,
    });
  };

  const cancelDeviceChoice = () => {
    const { code, source } = deviceChoice;
    closeDeviceChoice();
    setMgLookup({ loading: false, notFound: false, checkedNs: "" });
    // "Ninguno de estos": es un equipo que la base todavía no conoce. Queda en carga manual
    // para que el próximo blur del campo no vuelva a abrir el mismo modal.
    declararOtroEquipoMismoNs({ source, code, equipoPrevio: describeDeviceChoice() });
  };

  const describeDeviceChoice = () => {
    const candidates = deviceChoice.candidates || [];
    if (candidates.length === 1) return describeDeviceCandidate(candidates[0]);
    if (candidates.length > 1) return `${candidates.length} equipos ya cargados con ese número`;
    return "";
  };

  const describeDeviceCandidate = (candidate) =>
    [candidate?.tipo_equipo, candidate?.marca, candidate?.modelo, candidate?.variante]
      .filter(Boolean)
      .join(" - ");

  /**
   * "Es otro equipo con el mismo número": el CPAP ya está en la base y ahora entra su
   * calentador humidificador, que no trae serie propia y se carga con la del CPAP.
   *
   * Se conservan el número y el cliente —son los mismos— y se borra todo lo que identifica al
   * equipo anterior: tipo, marca, modelo, variante, técnico, número interno (el MG es de una
   * sola máquina) y los estados de alquiler/propiedad, que también son de la otra. Y el
   * autocompletado queda desactivado para ese número: si no, el próximo blur del campo volvía
   * a traer el equipo de la base y no había manera de cargar el segundo.
   */
  const declararOtroEquipoMismoNs = ({ source, code, tipo = "", equipoPrevio = "" } = {}) => {
    const lookupSource = source || autofillBy;
    const rawCode = String(
      code || (lookupSource === "interno" ? form.equipo.numero_interno : form.equipo.numero_serie) || ""
    ).trim();
    // Cortar cualquier búsqueda en vuelo: su respuesta llegaría después de limpiar y volvería
    // a llenar los campos con el equipo que acabamos de descartar.
    mgLookupSeqRef.current += 1;
    lookupRequestSeqRef.current += 1;
    setLookupRequest({ nonce: 0, source: lookupSource, code: "" });
    manualEntryRef.current = { key: lookupKey(lookupSource, rawCode) };
    lastLookupRef.current = { key: lookupKey(lookupSource, rawCode) };
    closeDeviceChoice();

    setForm((prev) => {
      const next = clone(prev);
      next.equipo.marca_id = "";
      next.equipo.modelo_id = "";
      next.equipo.garantia = false;
      if (lookupSource === "interno") {
        next.equipo.numero_serie = "";
      } else {
        next.equipo.numero_interno = "";
      }
      return next;
    });
    prefillSkipTipoResetRef.current = false;
    setTipoSel(tipo || "");
    setMarcaId(null);
    setMarcaTxt("");
    setModelos([]);
    setVarianteTxt("");
    setVarianteSugeridas([]);
    setTecnicoId(null);
    setMgLookup({ loading: false, notFound: false, checkedNs: "" });
    setMgInactiveInfo(null);
    setIngresoDeHoy(null);
    setBejermanSuggestion(null);
    setAppliedBejermanSale(null);
    setEquipoAlquiladoOrigen(false);
    setEsEquipoPropioMg(false);
    nsAutofillSnapshotRef.current = null;
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
    setNsAutofillInfo("");
    setNsAutofillClienteWarning("");
    setOtroEquipoMismoNs({
      code: rawCode,
      source: lookupSource,
      equipoPrevio,
    });
    expandSection("equipo");
  };

  const volverAlEquipoDeLaBase = () => {
    const source = otroEquipoMismoNs?.source || autofillBy;
    const code = otroEquipoMismoNs?.code || "";
    setOtroEquipoMismoNs(null);
    forgetLookupMemory();
    if (code) triggerLookupRequest(source, code, { force: true });
  };

  const formatFechaIngreso = (val) => {
    if (!val) return "-";
    const d = new Date(val);
    if (Number.isNaN(d.getTime())) return String(val);
    return d.toLocaleDateString();
  };
  const normalizeFechaIngreso = (val) => {
    const s = String(val || "").trim();
    if (!s) return "";
    const m = s.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/);
    if (m) {
      const [, dd, mm, yyyy] = m;
      return `${yyyy}-${mm.padStart(2, "0")}-${dd.padStart(2, "0")}`;
    }
    return s;
  };

  const resetFormFields = () => {
    setMarcaTxt("");
    setMarcaId(null);
    setModelos([]);
    setClienteRsInput("");
    setClienteCodInput("");
    setForm({
      etiq_garantia_ok: false,
      cliente: { id: null, razon_social: "", cod_empresa: "", telefono: "" },
      equipo: { marca_id: "", modelo_id: "", numero_serie: "", numero_interno: "", garantia: false },
      motivo: "",
      informe_preliminar: "",
      comentarios: "",
      garantia_reparacion: false,
      remito_ingreso: "",
      fecha_ingreso: "",
    });
    setAccItems([]);
    setBatchItems([]);
    setEditingBatchItemId(null);
    setTipoIngreso(TIPO_INGRESO.CLIENTE);
    setPropietario(PROPIETARIO_VACIO);
    setTecnicoId(null);
    setEmpresaBejerman("SEPID");
    setVarianteTxt("");
    setMgLookup({ loading: false, notFound: false, checkedNs: "" });
    setMgInactiveInfo(null);
    setIngresoDeHoy(null);
    setBejermanSuggestion(null);
    setAppliedBejermanSale(null);
    setRisPreflight(null);
    setRisPreflightError("");
    setRisMode("emit");
    setManualRemitoNumber("");
    setNsAutofillInfo("");
    setNsAutofillClienteWarning("");
    setOtroEquipoMismoNs(null);
    nsAutofillSnapshotRef.current = null;
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
    forgetLookupMemory();
    setEquipoAlquiladoOrigen(false);
    collapseDataSections();
  };

  useEffect(() => {
    if (prefillAppliedRef.current) return;
    const payload = location?.state?.prefill || null;
    const serieParam = (searchParams.get("serie") || "").trim();
    const rawPrefill = payload || (serieParam ? { numero_serie: serieParam } : null);
    const prefillMarcaId = rawPrefill?.marca_id || rawPrefill?.brand_id || rawPrefill?.marcaId || "";
    const prefillModelId = rawPrefill?.modelo_id || rawPrefill?.model_id || rawPrefill?.modelId || "";
    const prefill = rawPrefill
      ? {
          ...rawPrefill,
          marca_id: prefillMarcaId,
          model_id: prefillModelId,
          modelo_id: prefillModelId,
          marca: rawPrefill.marca || rawPrefill.marca_nombre || rawPrefill.brand || "",
          modelo: rawPrefill.modelo || rawPrefill.modelo_nombre || rawPrefill.model || "",
        }
      : null;
    if (!prefill) return;

    prefillAppliedRef.current = true;
    prefillModelAppliedRef.current = false;
    prefillClienteAppliedRef.current = false;
    prefillSkipTipoResetRef.current = true;
    prefillTipoAppliedRef.current = false;
    prefillRef.current = prefill;
    nsAutoDesiredBrandRef.current = prefill.marca_id || null;
    nsAutoDesiredModelRef.current = prefill.model_id || null;

    setForm((f0) => {
      const f = clone(f0);
      if (prefill.numero_serie) f.equipo.numero_serie = prefill.numero_serie;
      if (prefill.numero_interno && !prefill.mg_inactivo_venta) f.equipo.numero_interno = prefill.numero_interno;
      if (prefill.marca_id) f.equipo.marca_id = String(prefill.marca_id);
      if (prefill.model_id) f.equipo.modelo_id = String(prefill.model_id);
      return f;
    });
    setEquipoAlquiladoOrigen(
      !!prefill.alquilado || String(prefill.estado || "").trim().toLowerCase() === "alquilado"
    );
    if (prefill.marca_id) setMarcaId(prefill.marca_id);
    if (prefill.marca) setMarcaTxt(prefill.marca);
    if (prefill.tipo_equipo) {
      prefillSkipTipoResetRef.current = true;
      prefillTipoAppliedRef.current = true;
      setTipoSel(prefill.tipo_equipo);
    }
    const variantePrefill = prefill.variante || prefill.equipo_variante || "";
    if (variantePrefill) setVarianteTxt(variantePrefill);
    expandAllSections();
    const prefillRentalCustomer = rentalReturnCustomerFromPayload(prefill);
    const prefillAlquilerA = prefill.alquilado ? cleanText(prefill.alquiler_a) : "";
    const prefillClienteNombre = prefillRentalCustomer?.name || prefillAlquilerA || prefill.customer_nombre || "";
    const prefillClienteCod = prefillRentalCustomer?.cod || (prefillRentalCustomer || prefillAlquilerA ? "" : prefill.customer_cod || "");
    const prefillTienePropietario = !!(
      prefill.propietario_nombre ||
      prefill.propietario_contacto ||
      prefill.propietario_doc
    );
    const prefillEsParticular =
      isParticularCustomerName(prefillClienteNombre) || (!prefillClienteNombre && prefillTienePropietario);
    setTipoIngreso(prefillEsParticular ? TIPO_INGRESO.PARTICULAR : TIPO_INGRESO.CLIENTE);

    if (prefillClienteNombre) {
      setClienteRsInput(prefillClienteNombre);
      setClienteCodInput(prefillClienteCod);
    }
    if (prefillEsParticular && prefillTienePropietario) {
      setPropietario({
        nombre: prefill.propietario_nombre || "",
        contacto: prefill.propietario_contacto || "",
        doc: prefill.propietario_doc || "",
      });
    } else {
      setPropietario(PROPIETARIO_VACIO);
    }
    if (prefill.alquilado && prefill.alquiler_a) {
      setNotice(`Equipo alquilado a ${prefill.alquiler_a}. Completa los datos restantes.`);
    } else if (prefill.mg_inactivo_venta) {
      setMgInactiveInfo({
        numero_interno: prefill.numero_interno || "",
        msg: prefill.mg_context_msg || "MG histórico inactivo por venta; no operativo para nuevos ingresos.",
      });
      setNotice(prefill.mg_context_msg || "MG histórico inactivo por venta; no operativo para nuevos ingresos.");
    } else if (prefill.customer_nombre) {
      setNotice("Datos cargados desde lectura de código.");
    } else if (prefill.numero_serie || prefill.numero_interno) {
      setNotice("Serie cargada desde lectura de código.");
    }

    // El prefill que llega con equipo (del lector de códigos) ya es un equipo elegido: el
    // primer blur del campo no tiene que salir a buscarlo de nuevo y reemplazarlo por el otro
    // que comparte el número. El prefill que trae sólo la serie sí se busca, que es para lo
    // que está.
    const prefillCode = prefill.numero_serie || prefill.numero_interno || "";
    if (prefillCode && (prefill.marca_id || prefill.model_id || prefill.tipo_equipo)) {
      lastLookupRef.current = {
        key: lookupKey(prefill.numero_serie ? "serie" : "interno", prefillCode),
      };
    }
  }, [location, searchParams]);

  useEffect(() => {
    if (prefillModelAppliedRef.current) return;
    const prefill = prefillRef.current;
    if (!prefill || !prefill.model_id) return;
    if (!modelos || modelos.length === 0) return;
    const exists = modelos.some((m) => String(m.id) === String(prefill.model_id));
    if (!exists) return;
    setForm((f0) => ({ ...f0, equipo: { ...f0.equipo, modelo_id: String(prefill.model_id) } }));
    expandSection("equipo");
    prefillModelAppliedRef.current = true;
    nsAutoDesiredModelRef.current = null;
  }, [modelos]);

  useEffect(() => {
    if (prefillClienteAppliedRef.current) return;
    const prefill = prefillRef.current;
    if (!prefill) return;
    if (!clientes || clientes.length === 0) return;
    const prefillRentalCustomer = rentalReturnCustomerFromPayload(prefill);
    const prefillAlquilerA = prefill.alquilado ? cleanText(prefill.alquiler_a) : "";
    const rsVal = (clienteRsInput || prefillRentalCustomer?.name || prefillAlquilerA || prefill.customer_nombre || "").trim();
    const codVal = (clienteCodInput || prefillRentalCustomer?.cod || (prefillRentalCustomer || prefillAlquilerA ? "" : prefill.customer_cod || "")).trim();
    if (!rsVal && !codVal) {
      prefillClienteAppliedRef.current = true;
      return;
    }
    if (!clienteRsInput && rsVal) setClienteRsInput(rsVal);
    if (!clienteCodInput && codVal) setClienteCodInput(codVal);
    syncClienteFromInputs(rsVal, codVal);
    prefillClienteAppliedRef.current = true;
  }, [clientes]);
  // Lookup manual por N/S o número interno (Enter o blur/Tab)
  useEffect(() => {
    if (!lookupRequest?.nonce) return;
    const lookupByInterno = lookupRequest.source === "interno";
    const lookupCode = String(lookupRequest.code || "").trim();
    const lookupCompanyKey = String(lookupRequest.company_key || "SEPID").trim().toUpperCase() || "SEPID";
    const lookupLabel = lookupByInterno ? "número interno" : "N/S";
    setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
    const seq = ++mgLookupSeqRef.current;
    (async () => {
      setMgLookup((s) => ({ ...s, loading: true, notFound: false, checkedNs: lookupCode }));
      // El aviso de "ya ingresado hoy" lo vuelve a decidir esta búsqueda: arrastrar el de la
      // anterior dejaría trabado un equipo que no es.
      setIngresoDeHoy(null);
      try {
        const res = await lookupScan(lookupCode, {
          company_key: lookupCompanyKey,
          device_id: lookupRequest.device_id || "",
        });
        if (mgLookupSeqRef.current !== seq) return;

        // Si el código responde a más de un equipo, no se adivina: se corta acá y se
        // pregunta cuál. Autocompletar con el equivocado es peor que no autocompletar.
        if (res?.ambiguous && Array.isArray(res?.device_candidates) && res.device_candidates.length > 1) {
          setDeviceChoice({
            open: true,
            candidates: res.device_candidates,
            code: lookupCode,
            source: lookupRequest.source,
            reason: "ambiguous",
            stagedIndex: -1,
          });
          setMgLookup({ loading: false, notFound: false, checkedNs: lookupCode });
          return;
        }

        // El equipo que trae la base ya está en la lista de este remito: el operador está
        // cargando el segundo equipo que comparte el número (el humidificador del CPAP que
        // acaba de agregar). Autocompletar acá le llenaría la pantalla con el equipo que ya
        // cargó, y el choque recién saltaría al querer agregarlo. Se pregunta antes.
        if (res?.kind === "device" && res?.device) {
          const stagedIndex = findStagedEquipmentIndex(batchItemsRef.current, res.device, {
            skipItemId: editingBatchItemIdRef.current,
          });
          if (stagedIndex >= 0 && !lookupRequest.device_id) {
            setDeviceChoice({
              open: true,
              candidates: res.device_candidates?.length ? res.device_candidates : [res.device],
              code: lookupCode,
              source: lookupRequest.source,
              reason: "already_staged",
              stagedIndex,
            });
            setMgLookup({ loading: false, notFound: false, checkedNs: lookupCode });
            return;
          }
        }

        if (res?.kind === "bejerman_sale" && res?.suggestion) {
          clearNsAutofillFields();
          setMgInactiveInfo(null);
          const suggestion = res.suggestion;
          setBejermanSuggestion(suggestion);
          setAppliedBejermanSale(null);
          const localCustomerId = suggestion?.customer?.local_customer?.id;
          if (localCustomerId && !(clientes || []).some((c) => Number(c.id) === Number(localCustomerId))) {
            try {
              const rows = await getClientes();
              setClientes(rows || []);
            } catch {
              /* no bloquear el autocompletado */
            }
          }
          applyBejermanSuggestionPayload(suggestion, { auto: true });
          setMgLookup({ loading: false, notFound: false, checkedNs: lookupCode });
          return;
        }

        if (res?.kind !== "device" || !res?.device) {
          clearNsAutofillFields();
          setMgInactiveInfo(null);
          setBejermanSuggestion(null);
          setAppliedBejermanSale(null);
          setNsAutofillInfo(
            res?.bejerman_lookup_error
              ? bejermanLookupErrorMessage(
                  { message: res.bejerman_lookup_error },
                  "No se encontró el equipo en NEXORA",
                )
              : ""
          );
          setMgLookup({ loading: false, notFound: true, checkedNs: lookupCode });
          return;
        }

        const device = res.device || {};
        const ingreso = res.ingreso || {};
        const flags = res.flags || {};
        setOtroEquipoMismoNs(null);
        setBejermanSuggestion(res?.bejerman_sale ? { ...res.bejerman_sale, existingDeviceContext: true } : null);
        setAppliedBejermanSale(null);

        const marcaIdFromDevice = toIntOrNull(device.marca_id);
        const modelIdFromDevice = toIntOrNull(device.model_id);
        const nsFromDevice = String(device.numero_serie || "").trim();
        const tipoFromDevice = String(device.tipo_equipo || "").trim();
        const varianteFromDevice = String(device.variante || "").trim();
        const mgFromDevice = String(device.numero_interno || "").trim();
        // Éste es el freno que el operador necesita en el momento de tipear el número, y no
        // cuando ya cargó los seis equipos y le da a emitir: si el equipo tiene una OS abierta
        // de hoy, el remito que está por hacer ya salió.
        if (ingreso?.ingreso_en_curso && ingreso?.ingreso_hoy && ingreso?.id) {
          setIngresoDeHoy({
            key: equipmentKeyOf({
              numero_serie: lookupByInterno ? nsFromDevice : nsFromDevice || lookupCode,
              numero_interno: lookupByInterno ? mgFromDevice || lookupCode : mgFromDevice,
              tipo_equipo: tipoFromDevice,
            }),
            ingresoId: ingreso.id,
            os: ingreso.os || String(ingreso.id),
            fechaIngreso: ingreso.fecha_ingreso || null,
            identificador: nsFromDevice || mgFromDevice || lookupCode,
            equipo: [tipoFromDevice, device.marca, device.modelo].filter(Boolean).join(" - "),
          });
        }
        const mgInactiveBySale = !!flags.mg_inactivo_venta;
        const propioMg = !mgInactiveBySale && (
          flags.es_propietario_mg === true ||
          flags.es_cliente_mg_owner === true ||
          device.es_propietario_mg === true ||
          device.es_cliente_mg_owner === true
        );

        const ingresoAlquilado =
          !!ingreso.alquilado || String(ingreso.estado || "").trim().toLowerCase() === "alquilado";
        const deviceAlquilado = !!device.alquilado;
        const alquilerOrigen = ingresoAlquilado || deviceAlquilado;
        const alquilerA = String(ingreso.alquiler_a || device.alquiler_a || "").trim();
        const rentalCustomer =
          rentalReturnCustomerFromPayload(res) ||
          ((ingresoAlquilado || deviceAlquilado) && alquilerA
            ? { id: null, name: alquilerA, cod: "", telefono: "" }
            : null);
        const prevAutoCliente = !!nsAutofillSnapshotRef.current?.auto_cliente;

        const deviceCustomerRs = String(device.customer_nombre || "").trim();
        const deviceCustomerCod = String(device.customer_cod || "").trim();
        const deviceCustomerTelefono = String(device.customer_telefono || "").trim();
        const useAlquilerCliente = !!rentalCustomer?.name;

        const clienteRawRs = useAlquilerCliente ? rentalCustomer.name : deviceCustomerRs;
        const clienteRawCod = useAlquilerCliente ? rentalCustomer.cod : deviceCustomerCod;
        const clienteRawTelefono = useAlquilerCliente ? rentalCustomer.telefono : deviceCustomerTelefono;

        const byId = rentalCustomer?.id
          ? (clientes || []).find((c) => Number(c.id) === Number(rentalCustomer.id))
          : null;
        const byRs = clienteRawRs ? findClienteByRS(clienteRawRs) : null;
        const byCod = clienteRawCod ? findClienteByCod(clienteRawCod) : null;
        let clienteMatch = byId || byCod || byRs || null;
        if (byRs && byCod && byRs.id !== byCod.id) {
          clienteMatch = byRs;
        }

        const shouldAutofillCliente = !!(clienteRawRs || clienteRawCod);
        const clienteRs = shouldAutofillCliente ? String(clienteMatch?.razon_social || clienteRawRs) : "";
        const clienteCod = shouldAutofillCliente ? String(clienteMatch?.cod_empresa || clienteRawCod) : "";
        const clienteTelefono = shouldAutofillCliente
          ? String(clienteMatch?.telefono || clienteRawTelefono)
          : "";
        const clienteWarning = shouldAutofillCliente && !clienteMatch
          ? useAlquilerCliente
            ? `El cliente alquilado "${clienteRawRs}" no existe en el catálogo. Seleccione un cliente válido antes de guardar.`
            : `El cliente dueño "${clienteRawRs || clienteRawCod}" no existe en el catálogo. Seleccione un cliente válido antes de guardar.`
          : "";

        const propietarioNombre = String(device.propietario_nombre || "").trim();
        const propietarioContacto = String(device.propietario_contacto || "").trim();
        const propietarioDoc = String(device.propietario_doc || "").trim();
        const tienePropietarioAutofill = !!(propietarioNombre || propietarioContacto || propietarioDoc);
        const autofillEsParticular =
          isParticularCustomerName(clienteRs) || (!shouldAutofillCliente && tienePropietarioAutofill);

        const marcaFromCatalog = marcaIdFromDevice
          ? (marcas || []).find((m) => Number(m.id) === marcaIdFromDevice)
          : null;
        const marcaNombre = String(device.marca || marcaFromCatalog?.nombre || "").trim();

        const modelInCurrentList = modelIdFromDevice
          ? (modelos || []).find((m) => Number(m.id) === modelIdFromDevice)
          : null;
        let tecnicoAutoId = toIntOrNull(modelInCurrentList?.tecnico_id);
        if (!tecnicoAutoId && marcaIdFromDevice) {
          tecnicoAutoId = toIntOrNull(marcaFromCatalog?.tecnico_id);
        }

        setForm((prev) => {
          const next = clone(prev);
          if (lookupByInterno) {
            next.equipo.numero_interno = mgFromDevice || lookupCode;
            next.equipo.numero_serie = nsFromDevice || "";
          } else {
            next.equipo.numero_serie = nsFromDevice || lookupCode;
            next.equipo.numero_interno = mgInactiveBySale ? "" : mgFromDevice;
          }
          next.equipo.marca_id = marcaIdFromDevice ? String(marcaIdFromDevice) : "";
          next.equipo.modelo_id = modelInCurrentList?.id ? String(modelInCurrentList.id) : "";
          if (shouldAutofillCliente) {
            next.cliente = {
              id: clienteMatch?.id || null,
              razon_social: clienteRs,
              cod_empresa: clienteCod,
              telefono: clienteTelefono,
            };
          } else if (prevAutoCliente) {
            next.cliente = { id: null, razon_social: "", cod_empresa: "", telefono: "" };
          }
          return next;
        });
        setEquipoAlquiladoOrigen(alquilerOrigen);
        setEsEquipoPropioMg(propioMg);

        prefillSkipTipoResetRef.current = true;
        setTipoSel(tipoFromDevice || "");
        setMarcaId(marcaIdFromDevice);
        setMarcaTxt(marcaNombre);
        setVarianteTxt(varianteFromDevice);
        setTecnicoId(tecnicoAutoId);
        setTipoIngreso(autofillEsParticular ? TIPO_INGRESO.PARTICULAR : TIPO_INGRESO.CLIENTE);
        setPropietario(
          autofillEsParticular
            ? {
                nombre: propietarioNombre,
                contacto: propietarioContacto,
                doc: propietarioDoc,
              }
            : PROPIETARIO_VACIO
        );

        if (shouldAutofillCliente) {
          setClienteRsInput(clienteRs);
          setClienteCodInput(clienteCod);
        } else if (prevAutoCliente) {
          setClienteRsInput("");
          setClienteCodInput("");
        }

        if (mgInactiveBySale) {
          setMgInactiveInfo({
            numero_interno: mgFromDevice || (lookupByInterno ? lookupCode : ""),
            msg: "MG histórico inactivo por venta; no operativo para nuevos ingresos.",
          });
        } else {
          setMgInactiveInfo(null);
        }

        nsAutoDesiredBrandRef.current = marcaIdFromDevice;
        nsAutoDesiredModelRef.current = modelInCurrentList?.id ? null : modelIdFromDevice;
        nsAutofillSnapshotRef.current = {
          auto_ns: lookupByInterno && !!nsFromDevice,
          auto_mg: !lookupByInterno && !mgInactiveBySale && !!mgFromDevice,
          auto_marca: true,
          auto_modelo: !!modelIdFromDevice,
          auto_tipo: true,
          auto_variante: true,
          auto_tecnico: true,
          auto_cliente: shouldAutofillCliente,
          auto_propietario: autofillEsParticular,
        };
        const hasAutofillData = !!(
          marcaIdFromDevice ||
          modelIdFromDevice ||
          nsFromDevice ||
          tipoFromDevice ||
          varianteFromDevice ||
          mgFromDevice ||
          tecnicoAutoId ||
          shouldAutofillCliente ||
          (autofillEsParticular && propietarioNombre) ||
          (autofillEsParticular && propietarioContacto) ||
          (autofillEsParticular && propietarioDoc) ||
          mgInactiveBySale
        );
        if (hasAutofillData) {
          expandAllSections();
        }
        setNsAutofillInfo(
          hasAutofillData
            ? `Datos autocompletados desde Equipos por ${lookupLabel}. Puede editarlos.`
            : ""
        );
        setNsAutofillClienteWarning(clienteWarning);
        setMgLookup({ loading: false, notFound: false, checkedNs: lookupCode });
      } catch {
        if (mgLookupSeqRef.current !== seq) return;
        setMgLookup((s) => ({ ...s, loading: false }));
      }
    })();
  }, [lookupRequest, clientes.length, marcas.length, modelos.length]);

  // Si el modelo llega despues del lookup (carga asincronica), aplicarlo cuando exista en la lista.
  useEffect(() => {
    const desiredBrand = nsAutoDesiredBrandRef.current;
    const desiredModel = nsAutoDesiredModelRef.current;
    if (!desiredModel) return;
    if (desiredBrand && Number(marcaId || 0) !== Number(desiredBrand)) return;
    if (!Array.isArray(modelos) || modelos.length === 0) return;

    const match = modelos.find((m) => Number(m.id) === Number(desiredModel));
    if (!match) return;

    const modelIdTxt = String(match.id);
    setForm((prev) => {
      if (String(prev.equipo.modelo_id || "") === modelIdTxt) return prev;
      return { ...prev, equipo: { ...prev.equipo, modelo_id: modelIdTxt } };
    });

    const tecnicoFromModel = toIntOrNull(match?.tecnico_id);
    if (tecnicoFromModel) setTecnicoId(tecnicoFromModel);
    expandSection("equipo");
    nsAutoDesiredModelRef.current = null;
  }, [modelos, marcaId]);

  // Garantía de reparación (por N/S o MG) - debounce 400ms
  useEffect(() => {
    const ns = (form.equipo.numero_serie || "").trim();
    const mg = (form.equipo.numero_interno || "").trim();
    if (!ns && !mg) {
      setForm((f) => ({ ...f, garantia_reparacion: false }));
      setGarRepLoading(false);
      setGarRepError(false);
      return;
    }
    const h = setTimeout(async () => {
      try {
        setGarRepLoading(true);
        setGarRepError(false);
        const r = await checkGarantiaReparacion(ns, mg, tipoEquipoActual());
        setForm((f) => ({ ...f, garantia_reparacion: !!r?.within_90_days }));
        setGarRepLoading(false);
      } catch {
        setGarRepLoading(false);
        setGarRepError(true);
      }
    }, 400);
    return () => clearTimeout(h);
    // El tipo entra en el chequeo: sin él, el humidificador hereda la garantía de reparación
    // del CPAP con el que comparte la serie.
  }, [form.equipo.numero_serie, form.equipo.numero_interno, form.equipo.modelo_id, tipoSel]);

  // Garantía de fábrica por N/S (debounce 400ms)
  useEffect(() => {
    const ns = (form.equipo.numero_serie || "").trim();
    const marcaSel = (() => {
      const m = (marcas || []).find((x) => x.id === (marcaId || form.equipo.marca_id));
      return m?.nombre || "";
    })();
    if (!ns) {
      setForm((f) => ({ ...f, equipo: { ...f.equipo, garantia: false } }));
      return;
    }
    const h = setTimeout(async () => {
      try {
        const r = await checkGarantiaFabrica(ns, marcaSel, {
          brand_id: marcaId || form.equipo.marca_id || null,
          model_id: form.equipo.modelo_id || null,
          company_key: empresaBejerman || "SEPID",
        });
        if (typeof r?.within_365_days !== "boolean") {
          setForm((f) => ({ ...f, equipo: { ...f.equipo, garantia: false } }));
          return;
        }
        // Con fecha estimada (sale de un remito, no de una factura) la casilla no se
        // marca sola: la garantía la decide quien recibe el equipo, no una inferencia.
        if (r?.estimated === true) return;
        const enGarantia = r.within_365_days;
        setForm((f) => ({ ...f, equipo: { ...f.equipo, garantia: enGarantia } }));
      } catch {
        /* noop: no bloquear */
      }
    }, 400);
    return () => clearTimeout(h);
  }, [form.equipo.numero_serie, marcaId, form.equipo.marca_id, form.equipo.modelo_id, marcas, empresaBejerman]);

  const tipoEquipoSel = useMemo(() => {
    const m = (modelos || []).find((x) => x.id === Number(form.equipo.modelo_id));
    return m?.tipo_equipo || "";
  }, [modelos, form.equipo.modelo_id]);

  useEffect(() => {
    if (!prefillAppliedRef.current || prefillTipoAppliedRef.current) return;
    if (tipoSel) {
      prefillTipoAppliedRef.current = true;
      return;
    }
    if (!tipoEquipoSel) return;
    prefillSkipTipoResetRef.current = true;
    prefillTipoAppliedRef.current = true;
    setTipoSel(tipoEquipoSel);
  }, [tipoEquipoSel, tipoSel]);

  // Carga inicial por secciones (mensajes por sección)
  useEffect(() => {
    (async () => {
      const errs = [];
      try {
        const mks = await getMarcas();
        setMarcas(mks || []);
      } catch (_) {
        errs.push("Error cargando marcas");
      }
      try {
        const mts = await getMotivos();
        setMotivos(mts || []);
      } catch (_) {
        errs.push("Error cargando motivos");
      }
      try {
        const cls = await getClientes();
        setClientes(cls || []);
        setClientesPerm(true);
      } catch (e) {
        const msg = String(e?.message || "");
        if (msg.startsWith("403 ")) {
          setClientesPerm(false);
        } else if (msg.startsWith("401 ")) {
          errs.push("No autenticado");
        } else {
          errs.push("Error cargando clientes");
        }
      }
      try {
        const accs = await getAccesoriosCatalogo();
        setAccesCatalogo(accs || []);
      } catch (_) {
        errs.push("Error cargando accesorios");
      }
      try {
        const tps = await getTiposEquipo();
        const list = (tps || [])
          .map((t) => t?.nombre || t?.label || t?.name || t?.value || t)
          .map(String)
          .filter(Boolean);
        setTiposEquipo(Array.from(new Set(list)));
      } catch (_) {
        errs.push("Error cargando tipos de equipo");
      }
      try {
        const res = await getBejermanIngressCompanies();
        const items = Array.isArray(res?.items) && res.items.length ? res.items : DEFAULT_BEJERMAN_COMPANIES;
        const normalizedItems = items.map((item) => ({
          key: String(item?.key || "").trim().toUpperCase(),
          label: item?.label || item?.key || "",
          brandingKey: item?.brandingKey || item?.key || "",
          isTest: !!item?.isTest,
        })).filter((item) => item.key);
        if (normalizedItems.length) {
          const defaultKey = String(res?.defaultKey || normalizedItems[0].key || "SEPID").trim().toUpperCase();
          setBejermanCompanies(normalizedItems);
          setEmpresaBejerman((current) =>
            normalizedItems.some((item) => item.key === current) ? current : defaultKey
          );
        }
      } catch (_) {
        setBejermanCompanies(DEFAULT_BEJERMAN_COMPANIES);
        setEmpresaBejerman((current) => current || "SEPID");
      }
      try {
        const tecs = await getTecnicos();
        setTecnicos(tecs || []);
      } catch (_) {
        /* noop */
      }
      if (errs.length) setErr(errs.join(" | "));
    })();
  }, []);

  // Cambio de marca / tipo
  useEffect(() => {
    setForm((f) => ({ ...f, equipo: { ...f.equipo, marca_id: marcaId || "", modelo_id: "" } }));

    if (!marcaId) {
      setModelos([]);
      setVarianteTxt("");
      setVarianteSugeridas([]);
      return;
    }
    getModelosByBrand(marcaId)
      .then((rows) => {
        const list = rows || [];
        const desiredModel = nsAutoDesiredModelRef.current;
        if (tipoSel) {
          const norm = (s) => (s || "").toString().trim().toUpperCase();
          const filtered = list.filter((m) => norm(m.tipo_equipo) === norm(tipoSel));
          const desiredExistsInList = desiredModel
            ? list.some((m) => String(m.id) === String(desiredModel))
            : false;
          const desiredExistsInFiltered = desiredModel
            ? filtered.some((m) => String(m.id) === String(desiredModel))
            : false;
          const nextModelos = desiredExistsInList && !desiredExistsInFiltered ? list : filtered;
          setModelos(nextModelos);
          const currentId = (form?.equipo?.modelo_id ?? "").toString();
          const exists = nextModelos.some((x) => String(x.id) === currentId);
          if (!exists) setForm((f) => ({ ...f, equipo: { ...f.equipo, modelo_id: "" } }));
        } else {
          setModelos(list);
        }
        setVarianteSugeridas([]);
      })
      .catch((e) => setErr(e?.message || "Error cargando modelos"));
  }, [marcaId, tipoSel]);

  // Variantes desde catálogo según modelo interno seleccionado
  useEffect(() => {
    const modeloId = form.equipo.modelo_id;
    if (!modeloId) {
      setVarianteSugeridas([]);
      if (!varianteTxt) setVarianteTxt("");
      return;
    }
    let active = true;
    (async () => {
      try {
        const vars = await getVariantesPorModelo(modeloId);
        if (!active) return;
        const seen = new Set();
        const names = (Array.isArray(vars) ? vars : [])
          .filter((v) => v?.active !== false)
          .map((v) => v?.name || v?.nombre || v?.label || "")
          .map((value) => String(value || "").trim())
          .filter((value) => {
            const key = value.toUpperCase();
            if (!value || seen.has(key)) return false;
            seen.add(key);
            return true;
          });
        setVarianteSugeridas(names);
        if (!varianteTxt && names.length === 1) setVarianteTxt(names[0]);
      } catch {
        if (active) setVarianteSugeridas([]);
      }
    })();
    return () => {
      active = false;
    };
  }, [form.equipo.modelo_id, varianteTxt]);

  // Técnico por modelo
  useEffect(() => {
    const m = (modelos || []).find((x) => x.id === Number(form.equipo.modelo_id));
    if (m?.tecnico_id) setTecnicoId(m.tecnico_id);
  }, [form.equipo.modelo_id, modelos]);

  // Fallback: técnico por marca si el modelo no define
  useEffect(() => {
    const m = (modelos || []).find((x) => x.id === Number(form.equipo.modelo_id));
    if (m?.tecnico_id) {
      setTecnicoId(m.tecnico_id);
    } else {
      const marcaObj = (marcas || []).find((x) => x.id === marcaId);
      if (marcaObj?.tecnico_id) setTecnicoId(marcaObj.tecnico_id);
    }
  }, [form.equipo.modelo_id, modelos, marcaId, marcas]);

  const onChange = (path) => (e) => {
    const v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    setForm((prev) => {
      const copy = clone(prev);
      const parts = path.split(".");
      let obj = copy;
      for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
      obj[parts.at(-1)] = v;
      return copy;
    });
  };

  // Si el número cambia, el aviso de "otro equipo con el mismo número" deja de aplicar: es
  // otro número y el autocompletado tiene que volver a funcionar normalmente.
  const forgetOtroEquipoIfCodeChanged = (source, value) => {
    if (!otroEquipoMismoNs) return;
    if (manualEntryRef.current.key === lookupKey(source, value)) return;
    setOtroEquipoMismoNs(null);
  };

  const onNumeroSerieChange = (e) => {
    forgetOtroEquipoIfCodeChanged("serie", e.target.value);
    setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
    setBejermanSuggestion(null);
    setAppliedBejermanSale(null);
    setEquipoAlquiladoOrigen(false);
    setEsEquipoPropioMg(false);
    onChange("equipo.numero_serie")(e);
  };

  const onNumeroInternoChange = (e) => {
    forgetOtroEquipoIfCodeChanged("interno", e.target.value);
    setMgInactiveInfo(null);
    setMgLookup((s) => (s.notFound ? { ...s, notFound: false } : s));
    setAppliedBejermanSale(null);
    setEquipoAlquiladoOrigen(false);
    setEsEquipoPropioMg(false);
    onChange("equipo.numero_interno")(e);
  };

  const onLookupFieldBlur = (source) => (e) => {
    triggerLookupRequest(source, e.currentTarget.value);
  };

  const onLookupFieldKeyDown = (source) => (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    triggerLookupRequest(source, e.currentTarget.value);
  };

  // Cambiar el tipo sobre datos autocompletados sólo puede significar una cosa: el equipo que
  // el operador tiene enfrente no es el que la base tiene con ese número. Antes esto no se
  // podía hacer: quedaba pendiente el modelo del equipo autocompletado y volvía a aplicarse
  // solo en cuanto se elegía la marca, así que el CPAP se recargaba una y otra vez.
  const onTipoSelChange = (value) => {
    const next = value || "";
    if (next === tipoSel) return;
    if (nsAutofillSnapshotRef.current && next) {
      declararOtroEquipoMismoNs({ tipo: next, equipoPrevio: equipoDescripcionActual() });
      return;
    }
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
    setTipoSel(next);
  };

  // El tipo con el que este equipo va a quedar identificado. El backend lo toma del modelo
  // elegido —es el que estampa en el equipo y el que entra en el índice único junto al N/S—,
  // así que acá se usa el mismo y el selector queda sólo como respaldo para los modelos del
  // catálogo que todavía no tienen tipo cargado.
  const tipoEquipoActual = () => {
    const modelo = (modelos || []).find((m) => String(m.id) === String(form.equipo.modelo_id));
    return String(modelo?.tipo_equipo || tipoSel || "").trim();
  };

  // El aviso vale mientras en pantalla siga estando el equipo que lo disparó. Si el operador
  // corrige la serie o el tipo, ya está hablando de otro equipo y el bloqueo se cae solo.
  const ingresoDeHoyBloqueo = (() => {
    if (!ingresoDeHoy?.key) return null;
    const actual = equipmentKeyOf({
      numero_serie: form.equipo.numero_serie,
      numero_interno: form.equipo.numero_interno,
      tipo_equipo: tipoEquipoActual(),
    });
    if (!actual) return null;
    if (actual === ingresoDeHoy.key) return ingresoDeHoy;
    // Los equipos viejos pueden no tener el tipo cargado. Ahí alcanza con que sea el mismo
    // número: si no, el aviso se caería justo al elegir el modelo, que es el que le pone tipo.
    const [idActual] = actual.split("|");
    const [idBloqueo, tipoBloqueo] = ingresoDeHoy.key.split("|");
    return !tipoBloqueo && idActual === idBloqueo ? ingresoDeHoy : null;
  })();
  const ingresoDeHoyMensaje = ingresoDeHoyBloqueo
    ? `Este equipo ya se ingresó hoy en la OS ${ingresoDeHoyBloqueo.os}` +
      `${ingresoDeHoyBloqueo.identificador ? ` (${ingresoDeHoyBloqueo.identificador})` : ""}. ` +
      "No se puede ingresar dos veces el mismo día: abrí esa orden, o dala de baja si quedó " +
      "mal cargada, antes de emitir un remito nuevo."
    : "";

  // Cómo se lee el equipo que está cargado en la pantalla. Sirve para el renglón de la lista
  // y para poder decirle al operador de qué equipo se limpiaron los datos.
  const equipoDescripcionActual = () => {
    const modelo = (modelos || []).find((m) => String(m.id) === String(form.equipo.modelo_id));
    return [tipoSel, marcaTxt, modelo?.nombre, varianteTxt].filter(Boolean).join(" - ");
  };

  function onMarcaInput(val) {
    setMarcaTxt(val);
    const pool = tipoSel ? (marcasPorTipo.length ? marcasPorTipo : marcas) : marcas;
    const match = (pool || []).find((m) => (m.nombre || "").toLowerCase() === String(val || "").trim().toLowerCase());
    setMarcaId(match ? match.id : null);
  }

  // Cambio de tipo => filtra marcas
  useEffect(() => {
    if (prefillSkipTipoResetRef.current) {
      prefillSkipTipoResetRef.current = false;
      if (!tipoSel) {
        setMarcasPorTipo([]);
        return;
      }
      (async () => {
        try {
          const rows = await getMarcasPorTipo(tipoSel);
          setMarcasPorTipo(rows || []);
        } catch {
          setMarcasPorTipo([]);
        }
      })();
      return;
    }
    setMarcaTxt("");
    setMarcaId(null);
    setModelos([]);
    setVarianteTxt("");
    setVarianteSugeridas([]);
    if (!tipoSel) {
      setMarcasPorTipo([]);
      return;
    }
    (async () => {
      try {
        const rows = await getMarcasPorTipo(tipoSel);
        setMarcasPorTipo(rows || []);
      } catch {
        setMarcasPorTipo([]);
      }
    })();
  }, [tipoSel]);

  // Handlers cliente
  function onClienteRsChange(v) {
    const c = findClienteByRS(v);
    const nextRs = c?.razon_social || v;
    setClienteRsInput(nextRs);
    if (isParticularCustomer(c)) {
      setTipoIngreso(TIPO_INGRESO.PARTICULAR);
    } else if (isParticularIngreso && c && !isParticularCustomer(c)) {
      setTipoIngreso(TIPO_INGRESO.CLIENTE);
      setPropietario(PROPIETARIO_VACIO);
    }
    const nextCod = c?.cod_empresa || "";
    setClienteCodInput(nextCod);
    syncClienteFromInputs(nextRs, nextCod);
  }

  const applyBejermanCustomerPayload = (s) => {
    if (s?.existingDeviceContext) return false;
    const local = s?.customer?.local_customer || s?.customer?.resolution?.local_customer;
    if (!local?.id) return false;
    setTipoIngreso(TIPO_INGRESO.CLIENTE);
    setPropietario(PROPIETARIO_VACIO);
    setClienteRsInput(local.razon_social || "");
    setClienteCodInput(local.cod_empresa || "");
    setForm((prev) => ({
      ...prev,
      cliente: {
        id: local.id,
        razon_social: local.razon_social || "",
        cod_empresa: local.cod_empresa || "",
        telefono: local.telefono || "",
      },
    }));
    expandSection("cliente");
    return true;
  };

  const applyBejermanSuggestionPayload = (s, { auto = false } = {}) => {
    s = s || {};
    if (s.existingDeviceContext) {
      setNsAutofillInfo("Venta Bejerman disponible como referencia. Se mantiene el cliente del historial del equipo.");
      return false;
    }
    const equipment = s.equipment || {};
    const highConfidence = equipment.confidence === "high";
    const suggestedMarcaId = toIntOrNull(equipment.marca_id);
    const suggestedModeloId = toIntOrNull(equipment.modelo_id);
    const serial = String(s.serial || form.equipo.numero_serie || "").trim();
    const modelInCurrentList = suggestedModeloId
      ? (modelos || []).find((m) => Number(m.id) === suggestedModeloId)
      : null;

    setForm((prev) => {
      const next = clone(prev);
      next.equipo.numero_serie = serial;
      if (!auto || highConfidence) {
        if (suggestedMarcaId) next.equipo.marca_id = String(suggestedMarcaId);
        next.equipo.modelo_id = modelInCurrentList?.id ? String(modelInCurrentList.id) : "";
      }
      if (typeof s?.warranty?.garantia === "boolean") {
        next.equipo.garantia = s.warranty.garantia;
      }
      return next;
    });

    if ((!auto || highConfidence) && equipment.tipo_equipo) {
      prefillSkipTipoResetRef.current = true;
      setTipoSel(equipment.tipo_equipo);
    }
    if ((!auto || highConfidence) && suggestedMarcaId) {
      setMarcaId(suggestedMarcaId);
      setMarcaTxt(equipment.marca || "");
    }
    if (!auto || highConfidence) {
      setVarianteTxt(equipment.variante || "");
      nsAutoDesiredBrandRef.current = suggestedMarcaId;
      nsAutoDesiredModelRef.current = modelInCurrentList?.id ? null : suggestedModeloId;
    }
    const customerApplied = applyBejermanCustomerPayload(s);
    expandAllSections();
    const resolutionStatus = s?.customer?.resolution?.status || "";
    if (auto) {
      setNsAutofillInfo(
        highConfidence
          ? "Datos autocompletados desde venta Bejerman. Verifique motivo y accesorios antes de guardar."
          : "Venta encontrada en Bejerman. El equipo requiere revisión porque el artículo no tiene mapeo confirmado."
      );
      setNsAutofillClienteWarning(
        customerApplied || !s?.customer?.name
          ? ""
          : resolutionStatus === "conflict"
            ? "El cliente de la venta Bejerman tiene datos ambiguos. Seleccione o revise el cliente antes de guardar."
            : "El cliente de la venta Bejerman no pudo aplicarse automáticamente. Seleccione un cliente antes de guardar."
      );
    } else {
      setNsAutofillInfo("Sugerencia Bejerman aplicada. Verifique cliente, motivo y accesorios antes de guardar.");
    }
    return true;
  };

  const applyBejermanSuggestion = () => {
    const suggestion = bejermanSuggestion || {};
    const applied = applyBejermanSuggestionPayload(suggestion, { auto: false });
    if (applied) {
      setAppliedBejermanSale(suggestion);
      setBejermanSuggestion(null);
    }
  };

  const applyBejermanCustomerSuggestion = () => {
    applyBejermanCustomerPayload(bejermanSuggestion || {});
  };

  const printSerialBarcode = async () => {
    const serial = (form.equipo.numero_serie || form.equipo.numero_interno || "").trim();
    if (!serial) {
      setErr("Ingrese un número de serie o interno para imprimir la etiqueta.");
      return;
    }
    try {
      setBarcodeLoading(true);
      const blob = await getSerialBarcodeBlob(serial, { title: "" });
      openPdfBlob(blob);
    } catch (error) {
      setErr(error?.message || "No se pudo imprimir el código de barras");
    } finally {
      setBarcodeLoading(false);
    }
  };

  const bejermanSalePayloadForSubmit = () => {
    const s = bejermanSuggestion || appliedBejermanSale;
    const serial = (form.equipo.numero_serie || "").trim();
    if (!s || !serial || normalizeSerialKey(s.serial) !== normalizeSerialKey(serial)) return null;
    return {
      serial: s.serial || serial,
      issueDate: s.document?.issueDate || s.warranty?.fecha_venta || null,
      articleCode: s.article?.code || "",
      articleDescription: s.article?.description || "",
      customerCode: s.customer?.code || "",
      customerName: s.customer?.name || "",
      customerCuit: s.customer?.cuit || "",
      documentLabel: s.document?.label || "",
      companyKey: s.companyKey || "",
      comprobanteId: s.document?.comprobanteId || "",
      cacheSaleItemId: s.cacheSaleItemId || "",
      syncedAt: s.syncedAt || null,
    };
  };

  const validateCurrentEquipmentForSubmit = () => {
    if (ingresoDeHoyBloqueo) return ingresoDeHoyMensaje;
    if (!form.equipo.marca_id) return "Seleccione una marca válida de la lista.";
    if (!form.equipo.modelo_id) return "Seleccione un modelo.";
    if (!form.motivo) return "Seleccione un motivo.";
    if (equipoAlquiladoOrigen && !RENTAL_RETURN_MOTIVOS.has(String(form.motivo || "").trim())) {
      return "Para equipos alquilados, seleccione Baja alquiler, Reparación alquiler u Otros.";
    }
    return "";
  };

  const validateSharedForSubmit = () => {
    const c = resolveCliente(clienteRsInput, clienteCodInput);
    if (!c?.id) {
      return { ok: false, error: "Debe seleccionar un cliente válido de la lista." };
    }
    const clienteEsParticular = isParticularCustomer(c);
    if (isParticularIngreso && !clienteEsParticular) {
      return { ok: false, error: "Para un ingreso particular debe seleccionarse el cliente Particular." };
    }
    if (!isParticularIngreso && clienteEsParticular) {
      return { ok: false, error: "Para usar el cliente Particular, seleccione el tipo Particular." };
    }
    const propietarioPayload = isParticularIngreso
      ? {
          nombre: (propietario.nombre || "").trim(),
          contacto: (propietario.contacto || "").trim(),
          doc: (propietario.doc || "").trim(),
        }
      : { ...PROPIETARIO_VACIO };
    if (isParticularIngreso && (!propietarioPayload.nombre || !propietarioPayload.doc)) {
      return {
        ok: false,
        error: "Para un ingreso particular es obligatorio completar nombre y DNI/CUIT del propietario.",
      };
    }
    return { ok: true, c, propietarioPayload };
  };

  const buildIngresoPayload = (c, propietarioPayload, { includeShared = true } = {}) => {
    const fechaIngresoNorm = normalizeFechaIngreso(form.fecha_ingreso);
    const payload = {
      equipo: {
        marca_id: Number(form.equipo.marca_id),
        modelo_id: Number(form.equipo.modelo_id),
        numero_serie: (form.equipo.numero_serie || "").trim(),
        garantia: !!form.equipo.garantia,
        numero_interno: (form.equipo.numero_interno || "").trim(),
        tipo_equipo: tipoEquipoActual(),
        // El operador declaró que este equipo comparte el número con otro que ya está en la
        // base (el humidificador que entra con la serie del CPAP): que el backend cree uno
        // nuevo en vez de adoptar el que ya existe con ese número.
        ...(otroEquipoMismoNs ? { otro_equipo_mismo_ns: true } : {}),
      },
      equipo_variante: (varianteTxt || "").trim() || null,
      motivo: form.motivo,
      informe_preliminar: form.informe_preliminar,
      comentarios: form.comentarios,
      accesorios_items: accItems.map((it) => ({
        accesorio_id: Number(it.accesorio_id),
        referencia: (it.referencia || "").trim(),
      })),
      tecnico_id: tecnicoId ? Number(tecnicoId) : null,
      garantia_reparacion: !!form.garantia_reparacion,
      bejerman_sale: bejermanSalePayloadForSubmit(),
      // Checkbox representa "fajas abiertas" => etiq_garantia_ok debe ser la negación
      etiq_garantia_ok: !form.etiq_garantia_ok,
    };
    if (!includeShared) return payload;
    return {
      cliente: { id: c.id },
      ...payload,
      ...(fechaIngresoNorm ? { fecha_ingreso: fechaIngresoNorm } : {}),
      propietario: propietarioPayload,
      empresa_bejerman: (empresaBejerman || "SEPID").toUpperCase(),
      empresa_facturar: (selectedBejermanCompany?.brandingKey || "SEPID").toUpperCase(),
    };
  };

  const buildRisPreflightPayload = (items = batchItems) => {
    const shared = validateSharedForSubmit();
    if (!shared.ok) throw new Error(shared.error);
    const sourceItems = Array.isArray(items) ? items : [];
    if (sourceItems.length === 0) {
      throw new Error("Agregue al menos un equipo a la lista para validar el remito.");
    }
    const fechaIngresoNorm = normalizeFechaIngreso(form.fecha_ingreso);
    return {
      cliente: { id: shared.c.id },
      propietario: shared.propietarioPayload,
      empresa_bejerman: (empresaBejerman || "SEPID").toUpperCase(),
      empresa_facturar: (selectedBejermanCompany?.brandingKey || "SEPID").toUpperCase(),
      ris_mode: risMode,
      manual_remito_number: risMode === "register" ? manualRemitoNumber.trim() : "",
      ...(fechaIngresoNorm ? { fecha_ingreso: fechaIngresoNorm } : {}),
      items: sourceItems.map((item) => item.payload),
    };
  };

  const runRisPreflight = async ({ silent = false, items = batchItems } = {}) => {
    setRisPreflightLoading(true);
    setRisPreflightError("");
    try {
      const payload = buildRisPreflightPayload(items);
      const result = await postRisPreflight(payload);
      setRisPreflight(result);
      if (result?.can_emit) {
        setErr("");
      } else if (silent) {
        setErr(result?.detail || "La validación previa del remito encontró problemas.");
      }
      return result;
    } catch (error) {
      const detail = error?.data?.detail || error?.message || "No se pudo validar el remito.";
      if (error?.data?.issues) {
        setRisPreflight(error.data);
      }
      setRisPreflightError(detail);
      if (silent) setErr(detail);
      return null;
    } finally {
      setRisPreflightLoading(false);
    }
  };

  const handleApplyRisCustomer = async (payload) => {
    try {
      setRisPreflightLoading(true);
      setRisPreflightError("");
      const result = await postRisPreflightCustomerFix(payload);
      const rows = await getClientes();
      setClientes(rows || []);
      const updated = result?.customer || null;
      if (updated && Number(updated.id) === Number(payload?.customer_id || payload?.customerId)) {
        setClienteRsInput(updated.razon_social || "");
        setClienteCodInput(updated.cod_empresa || "");
        setForm((prev) => ({
          ...prev,
          cliente: {
            id: updated.id || null,
            razon_social: updated.razon_social || "",
            cod_empresa: updated.cod_empresa || "",
            telefono: updated.telefono || prev?.cliente?.telefono || "",
          },
        }));
        expandSection("cliente");
      }
      await runRisPreflight();
    } catch (error) {
      setRisPreflightError(error?.data?.detail || error?.message || "No se pudo aplicar el cliente de Bejerman.");
    } finally {
      setRisPreflightLoading(false);
    }
  };

  const handleApplyRisArticle = async (payload) => {
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
  };

  const handleEditPreflightItem = (itemIndex) => {
    const item = batchItems[itemIndex];
    if (!item) return;
    editBatchItem(item.id);
    setRisPreflight(null);
  };

  const currentEquipmentDisplay = () => {
    const motivo = (motivos || []).find((m) => String(m.value) === String(form.motivo));
    const serial = (form.equipo.numero_serie || "").trim();
    const interno = (form.equipo.numero_interno || "").trim();
    return {
      serial: serial || interno || "Sin identificador",
      equipo: equipoDescripcionActual() || "Equipo",
      motivo: motivo?.label || form.motivo || "-",
      accesorios: accItems.map((it) => it.accesorio_nombre + (it.referencia ? ` (ref: ${it.referencia})` : "")),
    };
  };

  const clearCurrentEquipmentFields = () => {
    mgLookupSeqRef.current += 1;
    lookupRequestSeqRef.current += 1;
    setMarcaTxt("");
    setMarcaId(null);
    setModelos([]);
    setVarianteSugeridas([]);
    setLookupRequest({ nonce: 0, source: autofillBy, code: "" });
    closeDeviceChoice();
    forgetLookupMemory();
    setForm((prev) => ({
      ...prev,
      etiq_garantia_ok: false,
      equipo: { marca_id: "", modelo_id: "", numero_serie: "", numero_interno: "", garantia: false },
      motivo: "",
      informe_preliminar: "",
      comentarios: "",
      garantia_reparacion: false,
    }));
    setAccItems([]);
    setNuevoAcc({ descripcion: "", referencia: "" });
    setTipoSel("");
    setTecnicoId(null);
    setVarianteTxt("");
    setMgLookup({ loading: false, notFound: false, checkedNs: "" });
    setMgInactiveInfo(null);
    setIngresoDeHoy(null);
    setBejermanSuggestion(null);
    setAppliedBejermanSale(null);
    setNsAutofillInfo("");
    setNsAutofillClienteWarning("");
    setOtroEquipoMismoNs(null);
    nsAutofillSnapshotRef.current = null;
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
    setEquipoAlquiladoOrigen(false);
  };

  const hasCurrentEquipmentDraft = () => {
    const equipment = form.equipo || {};
    const textValues = [
      equipment.numero_serie,
      equipment.numero_interno,
      equipment.marca_id,
      equipment.modelo_id,
      tipoSel,
      marcaTxt,
      varianteTxt,
      form.motivo,
      form.informe_preliminar,
      form.comentarios,
    ];
    return (
      textValues.some((value) => String(value || "").trim()) ||
      !!equipment.garantia ||
      !!form.etiq_garantia_ok ||
      !!form.garantia_reparacion ||
      !!tecnicoId ||
      accItems.length > 0 ||
      !!(bejermanSuggestion || appliedBejermanSale) ||
      !!equipoAlquiladoOrigen
    );
  };

  const stageCurrentEquipment = async ({
    silent = false,
    commitOnPreflightIssues = true,
    successNotice,
  } = {}) => {
    setErr("");
    const equipmentError = validateCurrentEquipmentForSubmit();
    if (equipmentError) {
      setErr(equipmentError);
      // Abrir el bloque donde está el dato que falta: si está plegado, el error
      // solo se ve arriba y no queda claro qué campo hay que completar.
      expandSection(!form.equipo.marca_id || !form.equipo.modelo_id ? "equipo" : "ingreso");
      return { ok: false, error: equipmentError, items: batchItems, preflight: null };
    }
    const shared = validateSharedForSubmit();
    if (!shared.ok) {
      setErr(shared.error);
      expandSection("cliente");
      return { ok: false, error: shared.error, items: batchItems, preflight: null };
    }
    const payload = buildIngresoPayload(shared.c, shared.propietarioPayload, { includeShared: false });
    const display = currentEquipmentDisplay();
    const editingItem = batchItems.find((entry) => entry.id === editingBatchItemId) || null;
    const nextItem = {
      id: editingItem?.id || `${Date.now()}-${batchItems.length}`,
      payload,
      display,
      editor: {
        form: {
          etiq_garantia_ok: form.etiq_garantia_ok,
          equipo: { ...form.equipo },
          motivo: form.motivo,
          informe_preliminar: form.informe_preliminar,
          comentarios: form.comentarios,
          garantia_reparacion: form.garantia_reparacion,
        },
        equipoAlquiladoOrigen,
        esEquipoPropioMg,
        marcaTxt,
        marcaId,
        tipoSel,
        varianteTxt,
        accItems: accItems.map((it) => ({ ...it })),
        tecnicoId,
        bejermanSuggestion,
        appliedBejermanSale,
        otroEquipoMismoNs,
      },
    };
    // Éste es el chequeo que evita cargar dos veces el mismo equipo, que era lo que el
    // control de "ingreso abierto" intentaba cubrir desde el backend y terminaba frenando
    // remitos enteros. Acá es barato, inmediato y dice exactamente cuál choca con cuál.
    const clashIndex = findDuplicateEquipmentIndex(batchItems, nextItem);
    if (clashIndex >= 0) {
      const clash = batchItems[clashIndex];
      const error =
        `Este equipo ya está en la lista como #${clashIndex + 1} ` +
        `(${clash.display?.equipo || "equipo"} - ${clash.display?.serial || "sin identificador"}). ` +
        "Si son dos equipos distintos, revise el tipo de equipo o el número de serie.";
      setErr(error);
      expandSection("equipo");
      return { ok: false, error, items: batchItems, preflight: null };
    }

    const nextItems = editingItem
      ? batchItems.map((item) => (item.id === editingItem.id ? nextItem : item))
      : [...batchItems, nextItem];
    setRisPreflightError("");
    const nextPreflight = await runRisPreflight({ silent, items: nextItems });
    if (hasDocumentProfileMismatch(nextPreflight)) {
      const error = nextPreflight?.detail || "El lote mezcla comprobantes de ingreso incompatibles.";
      setErr(error);
      return { ok: false, error, items: batchItems, preflight: nextPreflight };
    }
    if (!commitOnPreflightIssues && nextPreflight?.can_emit !== true) {
      const error = nextPreflight?.detail || "Revise la validación del remito antes de emitir.";
      setErr(error);
      return { ok: false, error, items: batchItems, preflight: nextPreflight };
    }
    setBatchItems(nextItems);
    setEditingBatchItemId(null);
    clearCurrentEquipmentFields();
    const nextDocumentType = documentTypeFromResult(nextPreflight);
    if (successNotice !== null) {
      setNotice(
        successNotice ||
          (editingItem
            ? "Equipo actualizado en la lista."
            : risMode === "register"
              ? "Equipo agregado al remito."
              : `Equipo agregado al ${documentDisplayName(nextDocumentType)}.`)
      );
    }
    if (risMode === "register" && !manualRemitoNumber.trim()) {
      setRisPreflight(null);
      return { ok: true, items: nextItems, preflight: nextPreflight };
    }
    if (!nextPreflight) await runRisPreflight({ silent: true, items: nextItems });
    return { ok: true, items: nextItems, preflight: nextPreflight };
  };

  const addCurrentEquipmentToBatch = async () => {
    if (loading || risPreflightLoading) return;
    scrollPageTop();
    await stageCurrentEquipment();
  };

  const removeBatchItem = (itemId) => {
    setRisPreflight(null);
    setRisPreflightError("");
    setBatchItems((items) => items.filter((item) => item.id !== itemId));
    if (editingBatchItemId === itemId) {
      setEditingBatchItemId(null);
      clearCurrentEquipmentFields();
    }
  };

  const editBatchItem = (itemId) => {
    const item = batchItems.find((entry) => entry.id === itemId);
    if (!item) return;
    const editor = item.editor || {};
    const editorEquipo = editor.form?.equipo || {};
    const editorMarcaId = toIntOrNull(editor.marcaId ?? editorEquipo.marca_id);
    const editorModeloId = toIntOrNull(editorEquipo.modelo_id);
    const editorTipoSel = editor.tipoSel || "";
    // Reponer el equipo dispara los efectos de "cambio de tipo" y "cambio de
    // marca", que limpian marca y modelo recien restaurados. Se reusa el mismo
    // mecanismo del prefill: saltear el reset por tipo y dejar anotado el modelo
    // deseado para reaplicarlo cuando termine de cargar la lista de modelos.
    if (editorTipoSel !== tipoSel) prefillSkipTipoResetRef.current = true;
    nsAutoDesiredBrandRef.current = editorMarcaId;
    nsAutoDesiredModelRef.current = editorModeloId;
    setForm((prev) => ({
      ...prev,
      ...(editor.form || {}),
      cliente: prev.cliente,
      remito_ingreso: prev.remito_ingreso,
      fecha_ingreso: prev.fecha_ingreso,
    }));
    setMarcaTxt(editor.marcaTxt || "");
    setMarcaId(editorMarcaId);
    setTipoSel(editorTipoSel);
    setVarianteTxt(editor.varianteTxt || "");
    setAccItems((editor.accItems || []).map((it) => ({ ...it })));
    setTecnicoId(editor.tecnicoId || null);
    setBejermanSuggestion(editor.bejermanSuggestion || null);
    setAppliedBejermanSale(editor.appliedBejermanSale || null);
    setEquipoAlquiladoOrigen(!!editor.equipoAlquiladoOrigen);
    setEsEquipoPropioMg(!!editor.esEquipoPropioMg);
    // Si este renglón se cargó como "otro equipo con el mismo número", el autocompletado
    // tiene que seguir apagado mientras se lo edita: volver a traer el equipo de la base le
    // pisaría los datos apenas el foco pase por el campo del número.
    setOtroEquipoMismoNs(editor.otroEquipoMismoNs || null);
    if (editor.otroEquipoMismoNs?.code) {
      const key = lookupKey(
        editor.otroEquipoMismoNs.source || autofillBy,
        editor.otroEquipoMismoNs.code
      );
      manualEntryRef.current = { key };
      lastLookupRef.current = { key };
    }
    setEditingBatchItemId(itemId);
    setRisPreflight(null);
    setRisPreflightError("");
    expandSection("equipo");
    expandSection("ingreso");
    setNotice("Editando equipo de la lista. Guarde los cambios para actualizarlo.");
  };

  const cancelBatchItemEdit = () => {
    setEditingBatchItemId(null);
    clearCurrentEquipmentFields();
    setRisPreflight(null);
    setRisPreflightError("");
    setNotice("");
  };

  // La espera del PDF es lo único cancelable del flujo: para cuando arranca, el
  // remito ya está emitido en Bejerman y sólo falta el archivo para imprimir.
  const beginPdfWait = (remito) => {
    const controller = new AbortController();
    pdfWaitAbortRef.current = controller;
    setPdfWait({ startedAt: Date.now(), remito: remito || "" });
    return controller;
  };

  const endPdfWait = () => {
    pdfWaitAbortRef.current = null;
    setPdfWait({ startedAt: 0, remito: "" });
  };

  const cancelPdfWait = () => {
    pdfWaitAbortRef.current?.abort();
  };

  const retryPendingPdf = async () => {
    if (!pendingPdfRetry || pdfRetryBusy) return;
    const { documentKey, ingresoId, source, remito, remitoNumbers = [] } = pendingPdfRetry;
    const documentLabel = remitoNumbers.length > 1 ? `${remitoNumbers.length} remitos` : remito ? `Remito ${remito}` : "";
    const reservedPrintWindow = reserveRisPrintWindow({
      title: "Remito - NEXORA",
      heading: "Reintentando el PDF del remito",
      message: "NEXORA está volviendo a pedir el PDF",
      detail: "El remito ya está emitido en Bejerman; sólo falta el archivo para imprimir.",
      steps: PDF_REPRINT_STEPS,
    });
    let printWindowUsed = false;
    setPdfRetryBusy(true);
    setErr("");
    try {
      if (documentKey) await postBejermanPdfQueueRetry(documentKey);
      const controller = beginPdfWait(documentLabel || remito);
      setLoading(true);
      setSubmitStage("Reintentando el PDF del remito...");
      reservedPrintWindow.update({ step: "pdf", documentLabel });
      const blob = await waitForRisPdfBlob(ingresoId, source || {}, {
        signal: controller.signal,
        reservedWindow: reservedPrintWindow,
        onProgress: (progress) => setSubmitStage(progress?.status || "Preparando PDF del remito..."),
      });
      const opened = openReservedRisPrintablePdf(reservedPrintWindow, blob, source || {}).opened;
      printWindowUsed = opened;
      if (opened) {
        setPendingPdfRetry(null);
        setNotice(
          remitoNumbers.length > 1
            ? `${remitoNumbers.length} remitos listos para imprimir.`
            : remito
              ? `Remito ${remito} listo para imprimir.`
              : "Remito listo para imprimir.",
        );
      } else {
        setManualRisPrint({ blob, source: source || {}, remito, remitoNumbers });
        setPendingPdfRetry(null);
        setNotice("El PDF está listo, pero el navegador bloqueó la ventana. Use Abrir e imprimir.");
      }
    } catch (error) {
      setNotice(
        isPendingPdfError(error)
          ? `${error?.message || "Bejerman todavía no devolvió el PDF."} El remito sigue emitido y se puede reimprimir más tarde.`
          : `No se pudo obtener el PDF: ${error?.message || "error desconocido"}.`,
      );
    } finally {
      if (!printWindowUsed) reservedPrintWindow.close();
      endPdfWait();
      setLoading(false);
      setSubmitStage("");
      setPdfRetryBusy(false);
    }
  };

  const pdfWaitNotice = (error, ingresoId, source) => {
    if (!isPendingPdfError(error)) {
      return `No se pudo abrir el PDF del remito: ${error?.message || "error desconocido"}. Se puede reimprimir desde la hoja de servicio.`;
    }
    const remitoNumbers = risRemitoNumbersFrom(source || {});
    setPendingPdfRetry({
      documentKey: error?.documentKey || "",
      ingresoId: ingresoId || null,
      source: source || null,
      remito: risRemitoFrom(source || {}),
      remitoNumbers,
    });
    return `${error?.message || "Bejerman todavía no devolvió el PDF."} El remito ya quedó registrado.`;
  };

  const submit = async (e) => {
    e.preventDefault();
    scrollPageTop();
    const hasDraft = hasCurrentEquipmentDraft();
    const hasBatchItems = batchItems.length > 0;
    const isRegisterMode = risMode === "register";
    if (editingBatchItemId) {
      setErr("Guarde los cambios del equipo en edición antes de emitir el remito.");
      return;
    }
    if (!hasBatchItems && !hasDraft) {
      setErr(isRegisterMode ? "Agregue el equipo a la lista antes de registrar el remito." : "Agregue el equipo a la lista antes de emitir el remito.");
      return;
    }
    if (isRegisterMode && !manualRemitoNumber.trim()) {
      setErr("Cargue el número completo del remito.");
      return;
    }
    setLoading(true);
    setSubmitStage("Creando ingresos...");
    setErr("");
    setOut(null);
    setNotice("");
    setManualRisPrint(null);
    setDupPrompt({ open: false, ingresoId: null, fechaIngreso: null, os: "" });

    const c = resolveCliente(clienteRsInput, clienteCodInput);
    if (!c?.id) {
      setLoading(false);
      setErr("Debe seleccionar un cliente válido de la lista.");
      return;
    }
    const clienteEsParticular = isParticularCustomer(c);
    if (isParticularIngreso && !clienteEsParticular) {
      setLoading(false);
      setErr("Para un ingreso particular debe seleccionarse el cliente Particular.");
      return;
    }
    if (!isParticularIngreso && clienteEsParticular) {
      setLoading(false);
      setErr("Para usar el cliente Particular, seleccione el tipo Particular.");
      return;
    }

    const propietarioPayload = isParticularIngreso
      ? {
          nombre: (propietario.nombre || "").trim(),
          contacto: (propietario.contacto || "").trim(),
          doc: (propietario.doc || "").trim(),
        }
      : { ...PROPIETARIO_VACIO };
    if (isParticularIngreso && (!propietarioPayload.nombre || !propietarioPayload.doc)) {
      setLoading(false);
      setErr("Para un ingreso particular es obligatorio completar nombre y DNI/CUIT del propietario.");
      return;
    }

    // Se reserva antes de cualquier espera (preflight incluido), todavía dentro
    // del gesto de emitir. Así el navegador no bloquea la impresión final.
    const reservedPrintWindow = isRegisterMode
      ? null
      : reserveRisPrintWindow({
          title: "Remito - NEXORA",
          heading: "Emitiendo el remito de ingreso",
          message: "Creando el ingreso en NEXORA",
          detail: "Esta pestaña abre la impresión del remito cuando el PDF esté listo.",
        });
    // El modal de progreso y la pestaña de impresión van sincronizados: el
    // operador mira una o la otra según dónde tenga el foco.
    const avisarEtapa = (status, patch = {}) => {
      setSubmitStage(status);
      reservedPrintWindow?.update({ status, ...patch });
    };
    let printWindowUsed = false;

    let effectiveBatchItems = batchItems;
    try {
      if (hasDraft) {
        avisarEtapa("Agregando equipo pendiente...", {
          step: "emit",
          detail: "NEXORA está sumando el equipo cargado a la lista antes de emitir.",
        });
        let staged;
        try {
          staged = await stageCurrentEquipment({
            silent: true,
            commitOnPreflightIssues: false,
            successNotice: null,
          });
        } catch (stageErr) {
          setErr(stageErr?.message || "No se pudo agregar el equipo pendiente.");
          return;
        }
        if (!staged.ok) {
          return;
        }
        effectiveBatchItems = staged.items;
      }
      if (!effectiveBatchItems.length) {
        setErr(isRegisterMode ? "Agregue el equipo a la lista antes de registrar el remito." : "Agregue el equipo a la lista antes de emitir el remito.");
        return;
      }

      avisarEtapa("Validando remito...", {
        step: "emit",
        detail: "NEXORA está revisando cliente, artículos y partidas antes de emitir.",
      });
      let preflight;
      try {
        preflight = await runRisPreflight({ silent: true, items: effectiveBatchItems });
      } catch (preflightError) {
        setErr(preflightError?.message || "No se pudo validar el remito antes de emitirlo.");
        return;
      }
      if (!preflight?.can_emit) {
        return;
      }
      if (effectiveBatchItems.length) {
        const fechaIngresoNorm = normalizeFechaIngreso(form.fecha_ingreso);
        const lotePayload = {
          cliente: { id: c.id },
          propietario: propietarioPayload,
          empresa_bejerman: (empresaBejerman || "SEPID").toUpperCase(),
          empresa_facturar: (selectedBejermanCompany?.brandingKey || "SEPID").toUpperCase(),
          ris_mode: risMode,
          manual_remito_number: isRegisterMode ? manualRemitoNumber.trim() : "",
          ...(fechaIngresoNorm ? { fecha_ingreso: fechaIngresoNorm } : {}),
          items: effectiveBatchItems.map((item) => item.payload),
        };
        const risProgressStartedAt = Date.now();
        avisarEtapa(isRegisterMode ? "Creando ingresos y registrando remito..." : "Creando ingresos...", {
          step: "emit",
          detail: "NEXORA está creando los ingresos y emitiendo el remito en Bejerman.",
        });
        await waitForRisProgressPaint();
        const r = await postNuevoIngresoLote(lotePayload);
        const remito = risRemitoFrom(r);
        const remitoNumbers = risRemitoNumbersFrom(r);
        const remitoLabel = remitoNumbersLabel(r);
        const documentLabel = remitoDocumentLabel(r);
        avisarEtapa(isRegisterMode ? "Registrando remito..." : "Preparando PDF...", {
          step: "pdf",
          documentLabel,
          detail: isRegisterMode
            ? "NEXORA está registrando el remito."
            : "El comprobante ya fue emitido por Bejerman. Obteniendo el archivo para imprimir.",
        });
        const responseMode = r?.document_mode || r?.ris?.document_mode || risMode;
        const isRegisteredResponse = responseMode === "register";
        const count = Array.isArray(r?.ingresos) ? r.ingresos.length : effectiveBatchItems.length;
        const firstIngresoId = r?.ingreso_ids?.[0] || r?.ingresos?.[0]?.ingreso_id || r?.ingresos?.[0]?.id;
        let risNotice = isRegisteredResponse
          ? (
              remito
                ? `Ingresos creados y remito ${remito} registrado en Bejerman para ${count} ${count === 1 ? "equipo" : "equipos"}.`
                : r?.detail || `Ingresos creados y remito registrado en Bejerman para ${count} ${count === 1 ? "equipo" : "equipos"}.`
            )
          : (
              remitoNumbers.length > 1
                ? `Ingresos creados y ${remitoNumbers.length} remitos emitidos para ${count} equipos: ${remitoLabel}.`
                : remito
                  ? `Ingresos creados y remito ${remito} emitido para ${count} ${count === 1 ? "equipo" : "equipos"}.`
                : r?.detail || `Ingresos creados para ${count} ${count === 1 ? "equipo" : "equipos"}. Remito pendiente de reintento.`
            );
        if (!isRegisteredResponse && remitoNumbers.length > 0 && firstIngresoId) {
          const controller = beginPdfWait(remitoLabel);
          try {
            const blob = await waitForRisPdfBlob(firstIngresoId, r, {
              signal: controller.signal,
              reservedWindow: reservedPrintWindow,
              onProgress: (progress) => setSubmitStage(progress?.status || "Preparando PDF del remito..."),
            });
            avisarEtapa("Abriendo impresión...", { step: "print", state: "ready" });
            await waitForRisProgressPaint();
            const opened = reservedPrintWindow
              ? openReservedRisPrintablePdf(reservedPrintWindow, blob, r).opened
              : openRisPrintablePdf(blob, r).opened;
            printWindowUsed = opened;
            if (opened) {
              risNotice += remitoNumbers.length > 1 ? " PDF conjunto listo para imprimir." : " PDF listo para imprimir.";
            } else {
              setManualRisPrint({ blob, source: r, remito, remitoNumbers });
              risNotice += " El PDF ya está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.";
            }
          } catch (pdfError) {
            reservedPrintWindow?.update({
              state: "error",
              status: "El comprobante fue emitido pero no se pudo obtener el PDF.",
              detail: "El comprobante ya quedó registrado en Bejerman. Puede reimprimirlo más tarde desde la hoja de servicio.",
            });
            printWindowUsed = true;
            risNotice += ` ${pdfWaitNotice(pdfError, firstIngresoId, r)}`;
          } finally {
            endPdfWait();
          }
        }
        await waitForRisProgressMinimum(risProgressStartedAt);
        resetFormFields();
        setOut({
          lote: true,
          ingresos: r?.ingresos || [],
          ingreso_ids: r?.ingreso_ids || [],
          remito_number: remito,
          remito_numbers: remitoNumbers,
          remitos: r?.remitos || [],
          document_mode: responseMode,
        });
        setNotice(risNotice);
        return;
      }

      const fechaIngresoNorm = normalizeFechaIngreso(form.fecha_ingreso);
      const payload = {
        cliente: { id: c.id },
        equipo: {
          marca_id: Number(form.equipo.marca_id),
          modelo_id: Number(form.equipo.modelo_id),
          numero_serie: (form.equipo.numero_serie || "").trim(),
          garantia: !!form.equipo.garantia,
          numero_interno: (form.equipo.numero_interno || "").trim(),
        },
        equipo_variante: (varianteTxt || "").trim() || null,
        motivo: form.motivo,
        informe_preliminar: form.informe_preliminar,
        comentarios: form.comentarios,
        remito_ingreso: (form.remito_ingreso || "").trim(),
        ...(fechaIngresoNorm ? { fecha_ingreso: fechaIngresoNorm } : {}),
        accesorios_items: accItems.map((it) => ({
          accesorio_id: Number(it.accesorio_id),
          referencia: (it.referencia || "").trim(),
        })),
        tecnico_id: tecnicoId ? Number(tecnicoId) : null,
        garantia_reparacion: !!form.garantia_reparacion,
        propietario: propietarioPayload,
        empresa_bejerman: (empresaBejerman || "SEPID").toUpperCase(),
        empresa_facturar: (selectedBejermanCompany?.brandingKey || "SEPID").toUpperCase(),
        bejerman_sale: bejermanSalePayloadForSubmit(),
        // Checkbox representa "fajas abiertas" => etiq_garantia_ok debe ser la negación
        etiq_garantia_ok: !form.etiq_garantia_ok,
      };

      const r = await postNuevoIngreso(payload);
      if (r?.existing === true) {
        setDupPrompt({
          open: true,
          ingresoId: r.ingreso_id || null,
          fechaIngreso: r.fecha_ingreso || null,
          os: r.os || "",
        });
        return;
      }
      let risNotice = "Ingreso creado.";
      if (r?.ingreso_id) {
        const risProgressStartedAt = Date.now();
        let risResult = null;
        try {
          avisarEtapa("Emitiendo remito en Bejerman...", {
            step: "emit",
            detail: "NEXORA está enviando el remito de ingreso a Bejerman.",
          });
          await waitForRisProgressPaint();
          risResult = await postIngresoRisEmitir(r.ingreso_id);
          const remito = risRemitoFrom(risResult);
          const documentLabel = remito ? `Remito ${remito}` : "";
          avisarEtapa("Preparando PDF...", {
            step: "pdf",
            documentLabel,
            detail: "El comprobante ya fue emitido por Bejerman. Obteniendo el archivo para imprimir.",
          });
          risNotice = remito
            ? `Ingreso creado y remito ${remito} emitido.`
            : "Ingreso creado y remito emitido.";
          const controller = beginPdfWait(remito);
          const blob = await waitForRisPdfBlob(r.ingreso_id, risResult, {
            signal: controller.signal,
            reservedWindow: reservedPrintWindow,
            onProgress: (progress) => setSubmitStage(progress?.status || "Preparando PDF del remito..."),
          });
          avisarEtapa("Abriendo impresión...", { step: "print", state: "ready" });
          await waitForRisProgressPaint();
          const opened = reservedPrintWindow
            ? openReservedRisPrintablePdf(reservedPrintWindow, blob, risResult).opened
            : openRisPrintablePdf(blob, risResult).opened;
          printWindowUsed = opened;
          if (opened) {
            risNotice += " PDF listo para imprimir.";
          } else {
            setManualRisPrint({ blob, source: risResult, remito });
            risNotice += " El PDF ya está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.";
          }
        } catch (risError) {
          if (risResult) {
            reservedPrintWindow?.update({
              state: "error",
              status: "El comprobante fue emitido pero no se pudo obtener el PDF.",
              detail: "El comprobante ya quedó registrado en Bejerman. Puede reimprimirlo más tarde desde la hoja de servicio.",
            });
            printWindowUsed = true;
          }
          risNotice = isPendingPdfError(risError)
            ? `${risNotice} ${pdfWaitNotice(risError, r.ingreso_id, risResult)}`
            : `Ingreso creado. No se pudo emitir o abrir el remito: ${risError?.message || "error desconocido"}. Reintente desde la hoja de servicio.`;
        } finally {
          endPdfWait();
          await waitForRisProgressMinimum(risProgressStartedAt);
        }
      }
      resetFormFields();
      setOut(r);
      setNotice(risNotice);
      if (r?.ingreso_id && canAutoOpenCreatedIngreso) navigate(`/ingresos/${r.ingreso_id}`);
    } catch (e2) {
      if (e2?.data?.conflict_type === "MG_INACTIVO_VENTA") {
        setMgInactiveInfo({
          numero_interno: e2?.data?.payload?.numero_interno || (form.equipo.numero_interno || "").trim(),
          msg: "MG histórico inactivo por venta; no operativo para nuevos ingresos.",
        });
        setErr("MG histórico inactivo por venta; no operativo para nuevos ingresos.");
        return;
      }
      if (Array.isArray(e2?.data?.issues)) {
        setRisPreflight(e2.data);
        setErr(e2?.data?.detail || "La validación previa del remito encontró problemas.");
        return;
      }
      if (Number.isInteger(e2?.data?.item_index)) {
        // "Equipo #2" no le dice nada a quien está en el mostrador con seis equipos sobre la
        // mesa: hay que nombrar el que falló con su tipo y su número de serie.
        const itemNumber = e2.data.item_index + 1;
        const fallado = effectiveBatchItems?.[e2.data.item_index];
        const etiqueta = fallado?.display
          ? `${fallado.display.equipo || "Equipo"} - ${fallado.display.serial || "sin identificador"}`
          : "";
        const encabezado = etiqueta ? `Equipo #${itemNumber} (${etiqueta})` : `Equipo #${itemNumber}`;
        setErr(`${encabezado}: ${e2?.data?.detail || e2?.message || "Error creando ingreso"}`);
        return;
      }
      setErr(e2?.message || "Error creando ingreso");
    } finally {
      if (!printWindowUsed) reservedPrintWindow?.close();
      setLoading(false);
      setSubmitStage("");
    }
  };

  const abrirRisManualPendiente = () => {
    if (!manualRisPrint?.blob) return;
    const opened = openRisPrintablePdf(manualRisPrint.blob, manualRisPrint.source || {}).opened;
    if (opened) {
      setManualRisPrint(null);
      setErr("");
      const count = manualRisPrint.remitoNumbers?.length || 0;
      setNotice(
        count > 1
          ? `${count} remitos listos para imprimir.`
          : manualRisPrint.remito
            ? `Remito ${manualRisPrint.remito} listo para imprimir.`
            : "Remito listo para imprimir.",
      );
    } else {
      setErr("El navegador volvió a bloquear la ventana. Habilite ventanas emergentes para NEXORA y reintente.");
    }
  };

  const rsMatch = clienteRsInput ? findClienteByRS(clienteRsInput) : null;
  const codMatch = clienteCodInput ? findClienteByCod(clienteCodInput) : null;
  const clienteMismatch = rsMatch && codMatch && rsMatch.id !== codMatch.id;
  const clienteResuelto = resolveCliente(clienteRsInput, clienteCodInput);
  const clienteResueltoEsParticular = isParticularCustomer(clienteResuelto);
  const propietarioCompleto =
    !isParticularIngreso || (!!(propietario.nombre || "").trim() && !!(propietario.doc || "").trim());
  const canSubmitCliente =
    !!clienteResuelto?.id &&
    propietarioCompleto &&
    (isParticularIngreso ? clienteResueltoEsParticular : !clienteResueltoEsParticular);
  const closeDupPrompt = () => setDupPrompt({ open: false, ingresoId: null, fechaIngreso: null, os: "" });
  const confirmDupPrompt = () => {
    if (dupPrompt.ingresoId) navigate(`/ingresos/${dupPrompt.ingresoId}`);
    closeDupPrompt();
  };
  const risProgressOpen = loading && !!submitStage;
  const risProgressStatus = submitStage || "Emitiendo remito en Bejerman";
  const risProgressTitle = risProgressStatus.toLowerCase().includes("validando")
    ? "Validando remito"
    : risProgressStatus.toLowerCase().includes("guardando") || risProgressStatus.toLowerCase().includes("creando")
      ? "Creando ingreso"
      : "Emitiendo remito";
  const isRegisterMode = risMode === "register";
  const editingBatchItem = batchItems.find((item) => item.id === editingBatchItemId) || null;
  const currentEquipmentDraft = hasCurrentEquipmentDraft();
  const resolvedDocumentType = documentTypeFromResult(risPreflight);
  const documentActionLabel = isRegisterMode ? "Registrar remito" : "Emitir remito";
  const documentLabel = resolvedDocumentType;
  const manualRemitoReady = !isRegisterMode || manualRemitoNumber.trim().length > 0;
  const canEmitRisBatch = batchItems.length > 0 && risPreflight?.can_emit === true;
  const canSubmitWithDraft = currentEquipmentDraft && !editingBatchItem;
  const canSubmitRemito = canEmitRisBatch || canSubmitWithDraft;
  // Aviso en el encabezado de los bloques plegados que todavía tienen datos
  // obligatorios sin cargar. Sólo cuando el ingreso ya arrancó, para no marcar
  // en rojo un formulario recién abierto.
  const ingresoIniciado = !!(
    (form.equipo.numero_serie || "").trim() ||
    (form.equipo.numero_interno || "").trim() ||
    currentEquipmentDraft ||
    editingBatchItem ||
    batchItems.length > 0
  );
  const clienteIncompleto = ingresoIniciado && !canSubmitCliente;
  const equipoIncompleto =
    ingresoIniciado && (!form.equipo.marca_id || !form.equipo.modelo_id);
  const ingresoIncompleto = ingresoIniciado && !form.motivo;
  const submitDisabled =
    loading ||
    risPreflightLoading ||
    !canSubmitCliente ||
    !!editingBatchItem ||
    // El equipo que está en pantalla se agrega solo al emitir, así que un bloqueo activo
    // frena todo el remito hasta que se corrija o se borre el número.
    !!ingresoDeHoyBloqueo ||
    !manualRemitoReady ||
    !canSubmitRemito;
  const submitButtonText = loading
    ? submitStage || "Guardando..."
    : ingresoDeHoyBloqueo
      ? `Equipo ya ingresado hoy (OS ${ingresoDeHoyBloqueo.os})`
    : editingBatchItem
      ? "Guarde los cambios del equipo"
    : currentEquipmentDraft
      ? isRegisterMode
        ? "Agregar equipo y registrar remito"
        : "Agregar equipo y emitir remito"
    : batchItems.length > 0
      ? documentActionLabel
      : `Agregue equipos para ${isRegisterMode ? "registrar remito" : "emitir remito"}`;
  const outIngresoCount = out?.lote
    ? (Array.isArray(out.ingresos) ? out.ingresos.length : out.ingreso_ids?.length || 0)
    : 0;
  const outFirstIngresoId = out?.lote
    ? out?.ingreso_ids?.[0] || out?.ingresos?.[0]?.ingreso_id || out?.ingresos?.[0]?.id
    : out?.ingreso_id;
  const outRemitoNumbers = out?.lote ? risRemitoNumbersFrom(out) : [];
  const updateRisMode = (nextMode) => {
    setRisMode(nextMode);
    setRisPreflight(null);
    setRisPreflightError("");
  };
  const updateManualRemitoNumber = (value) => {
    setManualRemitoNumber(value);
    setRisPreflight(null);
    setRisPreflightError("");
  };

  return (
    <div className="mx-auto max-w-[1600px] space-y-4 p-2 sm:p-3 md:p-4 lg:mx-0">
      <RisProgressModal
        open={risProgressOpen}
        title={risProgressTitle}
        status={risProgressStatus}
        documentLabel={pdfWait.remito ? `Remito ${pdfWait.remito}` : ""}
        waitStartedAt={pdfWait.startedAt}
        onCancel={pdfWait.startedAt ? cancelPdfWait : undefined}
      />
      {pendingPdfRetry && (
        <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">
            {pendingPdfRetry.remito ? `Remito ${pendingPdfRetry.remito} emitido` : "Remito emitido"}, pero
            Bejerman todavía no devolvió el PDF.
          </p>
          <p className="mt-1">
            El remito ya está registrado. Podés pedir el PDF de nuevo, o reimprimirlo más tarde desde la
            hoja de servicio o desde Remitos.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={pdfRetryBusy}
              onClick={retryPendingPdf}
              className="rounded bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-800 disabled:opacity-60"
            >
              {pdfRetryBusy ? "Reintentando..." : "Reintentar PDF"}
            </button>
            {pendingPdfRetry.ingresoId && (
              <a
                href={risPrintUrlFor(pendingPdfRetry.ingresoId, pendingPdfRetry.source || {})}
                target="_blank"
                rel="noreferrer"
                className="rounded border border-amber-400 px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-100"
              >
                Abrir PDF en otra pestaña
              </a>
            )}
            <button
              type="button"
              onClick={() => setPendingPdfRetry(null)}
              className="rounded px-3 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-100"
            >
              Descartar
            </button>
          </div>
        </div>
      )}
      <h1 className="text-2xl font-bold">Nuevo ingreso</h1>

      {(canCreateEquipmentIngreso || canMercaderiaIngreso) && (
        <div className="flex flex-wrap gap-2 border-b border-gray-200">
          {canCreateEquipmentIngreso && (
            <button
              type="button"
              className={`border-b-2 px-4 py-2 text-sm font-semibold ${
                ingresoTab === "equipos"
                  ? "border-blue-600 text-blue-700"
                  : "border-transparent text-gray-600 hover:text-gray-900"
              }`}
              onClick={() => setIngresoTab("equipos")}
            >
              Equipos
            </button>
          )}
          {canMercaderiaIngreso && (
            <button
              type="button"
              className={`border-b-2 px-4 py-2 text-sm font-semibold ${
                ingresoTab === "mercaderia"
                  ? "border-emerald-600 text-emerald-700"
                  : "border-transparent text-gray-600 hover:text-gray-900"
              }`}
              onClick={() => setIngresoTab("mercaderia")}
            >
              Mercadería
            </button>
          )}
        </div>
      )}

      {ingresoTab === "equipos" && canCreateEquipmentIngreso ? (
        <>

      {deviceChoice.open && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded shadow-lg p-5 w-full max-w-lg">
            <div className="text-lg font-semibold mb-1">
              {deviceChoice.reason === "already_staged"
                ? "Ese equipo ya está en la lista"
                : "Hay dos equipos con ese número"}
            </div>
            <p className="text-sm text-gray-700 mb-4">
              {deviceChoice.reason === "already_staged" ? (
                <>
                  El número <b>{deviceChoice.code}</b> ya está cargado en este remito
                  {deviceChoice.stagedIndex >= 0 ? ` como #${deviceChoice.stagedIndex + 1}` : ""}. Si
                  está ingresando el accesorio que comparte la serie (el humidificador que viene con
                  el CPAP), cárguelo a mano: es otro equipo.
                </>
              ) : (
                <>
                  El número <b>{deviceChoice.code}</b> corresponde a más de un equipo. Suele pasar con
                  los accesorios que no traen serie propia y se cargan con la del equipo principal.
                  Elija cuál está ingresando.
                </>
              )}
            </p>
            <div className="space-y-2">
              {deviceChoice.candidates.map((candidate) => {
                const equipo = describeDeviceCandidate(candidate);
                return (
                  <button
                    key={candidate.id}
                    type="button"
                    className="w-full rounded border border-gray-300 bg-white p-3 text-left hover:border-blue-500 hover:bg-blue-50"
                    onClick={() => pickDeviceCandidate(candidate)}
                  >
                    <div className="text-sm font-semibold text-gray-900">{equipo || "Equipo sin descripción"}</div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-600">
                      {candidate.numero_serie && <span>N/S: {candidate.numero_serie}</span>}
                      {candidate.numero_interno && <span>Interno: {candidate.numero_interno}</span>}
                      {candidate.customer_nombre && <span>Cliente: {candidate.customer_nombre}</span>}
                    </div>
                  </button>
                );
              })}
            </div>
            <div className="flex justify-end gap-2 pt-4">
              <button
                type="button"
                className="px-3 py-2 rounded border border-gray-300 text-gray-700 hover:bg-gray-100"
                onClick={cancelDeviceChoice}
              >
                {deviceChoice.reason === "already_staged"
                  ? "Es otro equipo, cargo los datos a mano"
                  : "Ninguno, cargo los datos a mano"}
              </button>
            </div>
          </div>
        </div>
      )}

      {dupPrompt.open && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded shadow-lg p-5 w-full max-w-md">
            <div className="text-lg font-semibold mb-2">Ingreso duplicado</div>
            <p className="text-sm text-gray-700 mb-1">
              Equipo ya ingresado el <b>{formatFechaIngreso(dupPrompt.fechaIngreso)}</b>: redirigir a su Hoja de servicio?
            </p>
            {dupPrompt.os && (
              <div className="text-xs text-gray-500 mb-4">OS {dupPrompt.os}</div>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                className="px-3 py-2 rounded border border-gray-300 text-gray-700 hover:bg-gray-100"
                onClick={closeDupPrompt}
              >
                Cancelar
              </button>
              <button
                type="button"
                className="px-3 py-2 rounded bg-blue-600 text-white hover:bg-blue-700"
                onClick={confirmDupPrompt}
              >
                Aceptar
              </button>
            </div>
          </div>
        </div>
      )}

      {notice && (
        <div className="bg-blue-100 border border-blue-300 text-blue-700 p-2 rounded">{notice}</div>
      )}
      {err && (
        <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded">{err}</div>
      )}
      {manualRisPrint?.blob && (
        <div className="rounded border border-amber-300 bg-amber-50 p-2 text-amber-900">
          <div className="text-sm">
            {manualRisPrint.remitoNumbers?.length > 1 ? "El PDF conjunto de los remitos está listo." : "El PDF del remito está listo."}
            {" Si no se abrió automáticamente, imprímalo desde este botón."}
          </div>
          <button
            type="button"
            onClick={abrirRisManualPendiente}
            className="mt-2 rounded bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-800"
          >
            {manualRisPrint.remitoNumbers?.length > 1
              ? `Abrir e imprimir los ${manualRisPrint.remitoNumbers.length} remitos`
              : "Abrir e imprimir remito"}
          </button>
        </div>
      )}
      {out && (
        <div className="bg-green-100 border border-green-300 text-green-700 p-2 rounded flex flex-wrap items-center gap-2">
          {out.lote ? (
            <span>
              {outIngresoCount} {outIngresoCount === 1 ? "ingreso creado" : "ingresos creados"}
              {outRemitoNumbers.length > 1 ? (
                <> con {outRemitoNumbers.length} remitos: <b>{outRemitoNumbers.join(" · ")}</b></>
              ) : out.remito_number ? (
                <> con remito <b>{out.remito_number}</b></>
              ) : null}
            </span>
          ) : (
            <span>
              Ingreso creado: <b>{out.os}</b> (ID: {out.ingreso_id})
            </span>
          )}
          {outFirstIngresoId && canViewCreatedIngreso && (
            <Link
              to={`/ingresos/${outFirstIngresoId}`}
              className="font-semibold underline underline-offset-2 hover:text-green-900"
            >
              {out.lote ? "Ver primer ingreso" : "Ver ingreso"}
            </Link>
          )}
        </div>
      )}

      <form onSubmit={submit} className="grid gap-4 2xl:grid-cols-[minmax(0,920px)_420px] 2xl:items-start">
        <div className="min-w-0 space-y-6">
        {ingresoDeHoyBloqueo && (
          <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
            <div className="font-semibold">Este equipo ya se ingresó hoy</div>
            <div className="mt-1">
              {ingresoDeHoyBloqueo.identificador ? <b>{ingresoDeHoyBloqueo.identificador}</b> : "El equipo"}
              {ingresoDeHoyBloqueo.equipo ? ` (${ingresoDeHoyBloqueo.equipo})` : ""} está en la{" "}
              <b>OS {ingresoDeHoyBloqueo.os}</b>, abierta desde hoy. No se puede ingresar dos veces el
              mismo día: si el remito anterior no salió bien, reimprimilo desde esa orden; si quedó mal
              cargada, dala de baja antes de emitir uno nuevo.
            </div>
            {canViewCreatedIngreso && (
              <Link
                to={`/ingresos/${ingresoDeHoyBloqueo.ingresoId}`}
                className="mt-2 inline-block font-semibold underline underline-offset-2 hover:text-red-950"
              >
                Abrir la OS {ingresoDeHoyBloqueo.os}
              </Link>
            )}
          </div>
        )}
        {nsAutofillInfo && (
          <div className="bg-indigo-50 border border-indigo-200 text-indigo-800 p-2 rounded text-sm">
            <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
              <span>{nsAutofillInfo}</span>
              {/* Con dos equipos ya cargados el modal pregunta cuál es. Cuando la base
                  conoce sólo uno —el CPAP, y el que entra es su humidificador— no hay nada
                  que preguntar: este botón es la forma de decir que es el otro. */}
              <button
                type="button"
                className="shrink-0 rounded border border-indigo-300 bg-white px-3 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-100"
                onClick={() => declararOtroEquipoMismoNs({ equipoPrevio: equipoDescripcionActual() })}
              >
                Es otro equipo con el mismo número
              </button>
            </div>
          </div>
        )}
        {otroEquipoMismoNs && (
          <div className="bg-amber-50 border border-amber-200 text-amber-900 p-2 rounded text-sm">
            <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
              <span>
                Cargando otro equipo con el número <b>{otroEquipoMismoNs.code}</b>
                {otroEquipoMismoNs.equipoPrevio ? ` (en la base ese número es ${otroEquipoMismoNs.equipoPrevio})` : ""}.
                Complete tipo, marca y modelo: no se va a autocompletar solo.
              </span>
              <button
                type="button"
                className="shrink-0 rounded border border-amber-300 bg-white px-3 py-1.5 text-xs font-semibold text-amber-800 hover:bg-amber-100"
                onClick={volverAlEquipoDeLaBase}
              >
                Volver a autocompletar
              </button>
            </div>
          </div>
        )}
        {nsAutofillClienteWarning && (
          <div className="bg-amber-50 border border-amber-200 text-amber-800 p-2 rounded text-sm">
            {nsAutofillClienteWarning}
          </div>
        )}
        {bejermanSuggestion && (
          <div className="border border-sky-200 bg-sky-50 rounded p-3 text-sm text-sky-950">
            <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
              <div>
                <div className="font-semibold">Venta encontrada en Bejerman</div>
                <div className="text-xs text-sky-800">
                  {bejermanSuggestion.document?.label || "Comprobante sin número"} ·{" "}
                  {bejermanSuggestion.document?.issueDate || "sin fecha"} · Serie{" "}
                  {bejermanSuggestion.serial || "-"}
                </div>
              </div>
              {!bejermanSuggestion.existingDeviceContext && (
                <button
                  type="button"
                  className="rounded bg-sky-700 px-3 py-2 text-xs font-semibold text-white hover:bg-sky-800"
                  onClick={applyBejermanSuggestion}
                >
                  Aplicar equipo
                </button>
              )}
            </div>
            <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-3">
              <div>
                <div className="text-[11px] font-semibold uppercase text-sky-700">Artículo</div>
                <div className="font-semibold">{bejermanSuggestion.article?.code || "-"}</div>
                <div className="text-xs text-sky-800">{bejermanSuggestion.article?.description || "-"}</div>
              </div>
              <div>
                <div className="text-[11px] font-semibold uppercase text-sky-700">Cliente facturado sugerido</div>
                <div className="font-semibold">{bejermanSuggestion.customer?.name || "-"}</div>
                <div className="text-xs text-sky-800">{bejermanSuggestion.customer?.code || "-"}</div>
                {bejermanSuggestion.customer?.local_customer?.id && !bejermanSuggestion.existingDeviceContext ? (
                  <button
                    type="button"
                    className="mt-1 rounded border border-sky-300 bg-white px-2 py-1 text-xs font-semibold text-sky-800 hover:bg-sky-100"
                    onClick={applyBejermanCustomerSuggestion}
                  >
                    Usar cliente sugerido
                  </button>
                ) : (
                  <div className="mt-1 text-xs text-amber-700">No se fuerza: elija el cliente que dejó el equipo.</div>
                )}
              </div>
              <div>
                <div className="text-[11px] font-semibold uppercase text-sky-700">Equipo sugerido</div>
                <div className="font-semibold">
                  {[bejermanSuggestion.equipment?.tipo_equipo, bejermanSuggestion.equipment?.marca, bejermanSuggestion.equipment?.modelo]
                    .filter(Boolean)
                    .join(" · ") || "-"}
                </div>
                <div className="text-xs text-sky-800">
                  {bejermanSuggestion.equipment?.variante || "Sin variante"} ·{" "}
                  {bejermanSuggestion.warranty?.garantia === true
                    ? `En garantía hasta ${bejermanSuggestion.warranty?.vence_el || "-"}`
                    : bejermanSuggestion.warranty?.garantia === false
                      ? `Sin garantía vigente desde venta ${bejermanSuggestion.warranty?.fecha_venta || "-"}`
                      : "Garantía sin fecha de venta"}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Equipo - datos de identificación y garantías */}
        <fieldset className="border rounded p-3">
          <div className="mb-3 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <h3 className="font-semibold">Equipo - Identificación</h3>
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-700">Autocompletado por:</label>
              <Select
                className="w-full md:w-52 text-xs font-medium"
                value={autofillBy}
                onChange={(e) => {
                  const mode = e.target.value === "interno" ? "interno" : "serie";
                  setAutofillBy(mode);
                  setMgInactiveInfo(null);
                  setMgLookup({ loading: false, notFound: false, checkedNs: "" });
                }}
              >
                <option value="serie">Número de serie</option>
                <option value="interno">Número interno</option>
              </Select>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className={`md:col-span-7 ${autofillBy === "serie" ? "md:order-1" : "md:order-2"}`}>
              <label className={FIELD_LABEL_CLASS}>Número de serie</label>
              <div className="flex gap-2">
                <Input
                  value={form.equipo.numero_serie}
                  onChange={onNumeroSerieChange}
                  onBlur={onLookupFieldBlur("serie")}
                  onKeyDown={onLookupFieldKeyDown("serie")}
                />
                <button
                  type="button"
                  className="shrink-0 rounded border border-gray-300 bg-white px-3 py-2 text-xs font-semibold text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={printSerialBarcode}
                  disabled={barcodeLoading || !(form.equipo.numero_serie || form.equipo.numero_interno || "").trim()}
                  title="Imprimir código de barras"
                >
                  {barcodeLoading ? "..." : "Código"}
                </button>
              </div>
            </div>

            <div className={`md:col-span-5 ${autofillBy === "serie" ? "md:order-2" : "md:order-1"}`}>
              <div className="flex items-center justify-between gap-2">
                <label className={FIELD_LABEL_CLASS}>Número interno</label>
                {mgLookup.notFound && (
                  <span className="text-xs text-amber-600">
                    Equipo no encontrado por {autofillBy === "serie" ? "N/S" : "número interno"}
                  </span>
                )}
              </div>
              <Input
                value={form.equipo.numero_interno}
                onChange={onNumeroInternoChange}
                onBlur={onLookupFieldBlur("interno")}
                onKeyDown={onLookupFieldKeyDown("interno")}
                placeholder="MG/NM/NV/CE ..."
              />
              {autofillBy === "interno" && (form.equipo.numero_interno || "").trim() && !isInternalLookupReady(form.equipo.numero_interno) && (
                <div className="text-xs text-gray-500 mt-1">
                  Para autocompletar, ingrese el código completo (ej. MG 7293).
                </div>
              )}
              {mgInactiveInfo?.msg && (
                <div className="text-xs text-amber-700 mt-1">
                  {mgInactiveInfo.msg}
                  {mgInactiveInfo?.numero_interno ? ` (${mgInactiveInfo.numero_interno})` : ""}
                </div>
              )}
            </div>
            <div className="md:col-span-12 md:order-3 text-[11px] text-gray-500 mt-1">
              {autofillBy === "serie"
                ? "Para autocompletar por N/S, presione Enter o Tab."
                : "Para autocompletar por número interno, presione Enter o Tab."}
            </div>

            <div className="flex items-center gap-2 md:col-span-4 md:order-4">
              <input id="gar" type="checkbox" checked={form.equipo.garantia} onChange={onChange("equipo.garantia")} />
              <label htmlFor="gar" className="text-sm font-medium text-gray-800">En garantía</label>
            </div>
            <div className="flex items-center gap-2 md:col-span-4 md:order-5">
              <input type="checkbox" checked={form.garantia_reparacion} onChange={(e) => setForm((f) => ({ ...f, garantia_reparacion: e.target.checked }))} />
              <span className="text-sm">Garantía de reparación</span>
              {garRepLoading && <span className="text-xs text-gray-500">...</span>}
              {!garRepLoading && garRepError && <span className="text-xs text-gray-400">No disponible</span>}
            </div>

            <div className="md:col-span-4 md:order-6 flex items-center gap-2">
              <input id="etiqok" type="checkbox" checked={!!form.etiq_garantia_ok} onChange={(e) => setForm((f) => ({ ...f, etiq_garantia_ok: !!e.target.checked }))} />
              <label htmlFor="etiqok" className="text-sm font-medium text-gray-800">Faja abierta</label>
            </div>
            <div className="md:col-span-12 md:order-7 text-xs text-gray-500 mt-1">
              Marque si al ingresar el equipo la faja o las etiquetas estaban en mal estado.
            </div>
          </div>
        </fieldset>

        {/* Cliente */}
        <CollapsibleSection
          title="Cliente"
          open={expandedSections.cliente}
          onToggle={() => toggleSection("cliente")}
          incomplete={clienteIncompleto}
        >
          {!clientesPerm && (
            <div className="text-xs text-gray-600 mb-2">No tiene permisos para listar clientes</div>
          )}
          <div className="mb-3">
            <label className={FIELD_LABEL_CLASS}>Tipo</label>
            <div className="inline-flex overflow-hidden rounded border border-gray-300 bg-white">
              <button
                type="button"
                aria-pressed={!isParticularIngreso}
                onClick={() => seleccionarTipoIngreso(TIPO_INGRESO.CLIENTE)}
                className={`px-4 py-2 text-sm font-semibold ${
                  !isParticularIngreso
                    ? "bg-blue-600 text-white"
                    : "text-gray-700 hover:bg-gray-50"
                }`}
              >
                Cliente
              </button>
              <button
                type="button"
                aria-pressed={isParticularIngreso}
                onClick={() => seleccionarTipoIngreso(TIPO_INGRESO.PARTICULAR)}
                className={`border-l border-gray-300 px-4 py-2 text-sm font-semibold ${
                  isParticularIngreso
                    ? "bg-blue-600 text-white"
                    : "text-gray-700 hover:bg-gray-50"
                }`}
              >
                Particular
              </button>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className="md:col-span-6">
              <label className={FIELD_LABEL_CLASS}>Razón social</label>
              <Input
                list={clientesPerm && !isParticularIngreso ? "clientes_rs" : undefined}
                value={clienteRsInput}
                onChange={(e) => onClienteRsChange(e.target.value)}
                readOnly={isParticularIngreso}
                placeholder={isParticularIngreso ? "Particular" : "Escriba y elija de la lista"}
                required
              />
              {clientesPerm && (
                <datalist id="clientes_rs">
                  {clientesIngreso.map((c) => (
                    <option key={`rs-${c.id}`} value={c.razon_social} label={[c.alias_interno, c.cod_empresa].filter(Boolean).join(" - ")} />
                  ))}
                  {clientesIngreso.filter((c) => c.alias_interno).map((c) => (
                    <option key={`alias-${c.id}`} value={c.alias_interno} label={c.razon_social} />
                  ))}
                </datalist>
              )}
              {!isParticularIngreso && clienteRsInput && !rsMatch && (
                <div className="text-xs text-red-600 mt-1">Debe seleccionar de la lista</div>
              )}
              {isParticularIngreso && clienteRsInput && !rsMatch && (
                <div className="text-xs text-red-600 mt-1">No se encontró el cliente Particular en el catálogo.</div>
              )}
            </div>
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Código empresa</label>
              {/* Un particular no tiene código de cliente en Bejerman: se emite como cliente
                  ocasional con los datos del propietario. Mostrar el código del placeholder local
                  hacía creer que el equipo entraba a nombre de un cliente real. */}
              <Input
                value={isParticularIngreso ? "" : clienteCodInput}
                readOnly
                tabIndex={-1}
                placeholder={isParticularIngreso ? "No aplica (cliente ocasional)" : "Se completa al elegir cliente"}
              />
            </div>
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Teléfono</label>
              <Input value={form.cliente.telefono} readOnly placeholder="-" />
            </div>
          </div>
          {clienteMismatch && (
            <div className="text-xs text-red-600 mt-2">
              El código no corresponde a la razón social seleccionada.
            </div>
          )}
          {isParticularIngreso && (
            <div className="mt-4 border rounded p-3">
              <h3 className="font-semibold mb-2">Propietario</h3>
              <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
                <div className="md:col-span-4">
                  <label className={FIELD_LABEL_CLASS}>Nombre</label>
                  <Input
                    value={propietario.nombre}
                    onChange={(e) => setPropietario((p) => ({ ...p, nombre: e.target.value }))}
                    placeholder="Nombre del propietario"
                    required
                  />
                </div>
                <div className="md:col-span-4">
                  <label className={FIELD_LABEL_CLASS}>Contacto</label>
                  <Input
                    value={propietario.contacto}
                    onChange={(e) => setPropietario((p) => ({ ...p, contacto: e.target.value }))}
                    placeholder="Contacto (opcional)"
                  />
                </div>
                <div className="md:col-span-4">
                  <label className={FIELD_LABEL_CLASS}>DNI o CUIT</label>
                  <Input
                    value={propietario.doc}
                    onChange={(e) => setPropietario((p) => ({ ...p, doc: e.target.value }))}
                    placeholder="DNI o CUIT"
                    required
                  />
                </div>
              </div>
            </div>
          )}
        </CollapsibleSection>

        {/* Empresa Bejerman */}
        <div className="border rounded p-3">
          <label className={FIELD_LABEL_CLASS}>Empresa Bejerman</label>
          <Select
            className="md:max-w-sm"
            value={empresaBejerman}
            onChange={(e) => setEmpresaBejerman((e.target.value || "SEPID").toUpperCase())}
          >
            {bejermanCompanies.map((company) => (
              <option key={company.key} value={company.key}>
                {company.label}
              </option>
            ))}
          </Select>
        </div>

        {/* Equipo */}
        <CollapsibleSection
          title="Equipo"
          open={expandedSections.equipo}
          onToggle={() => toggleSection("equipo")}
          incomplete={equipoIncompleto}
        >
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            {/* Tipo de equipo */}
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Tipo de equipo</label>
              <Select value={tipoSel} onChange={(e) => onTipoSelChange(e.target.value)}>
                <option value="">-- Seleccionar --</option>
                {(Array.isArray(tiposEquipo) ? tiposEquipo : []).map((t, i) => (
                  <option key={i} value={t}>
                    {t}
                  </option>
                ))}
              </Select>
            </div>

            {/* Marca (filtrada por tipo) */}
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Marca</label>
              <Input list="marcas-list" value={marcaTxt} placeholder="Marca" onChange={(e) => onMarcaInput(e.target.value)} />
              <datalist id="marcas-list">
                {(tipoSel && marcasPorTipo.length ? marcasPorTipo : marcas).map((m) => (
                  <option key={m.id} value={m.nombre} />
                ))}
              </datalist>
              {marcaTxt && !marcaId && (
                <div className="text-xs text-red-600 mt-1">Elija una marca de las sugeridas.</div>
              )}
            </div>

            {/* Modelo */}
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Modelo</label>
              <Select value={form.equipo.modelo_id} onChange={onChange("equipo.modelo_id")} disabled={!marcaId || !modelos.length}>
                <option value="">{!marcaId ? "Elija una marca primero" : "Seleccione modelo"}</option>
                {modelos.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.nombre}
                  </option>
                ))}
              </Select>
            </div>

            {/* Variante (opcional) */}
            <div className="md:col-span-12 md:order-5">
              <label className={FIELD_LABEL_CLASS}>Variante (opcional)</label>
              <Input list="variantes_sugeridas" value={varianteTxt} onChange={(e) => setVarianteTxt(e.target.value)} placeholder="Ej: 25, 25T, V30BT, etc." />
              <datalist id="variantes_sugeridas">
                {(varianteSugeridas || []).map((v, i) => (
                  <option key={i} value={v} />
                ))}
              </datalist>
            </div>

            {/* Técnico asignado */}
            <div className="md:col-span-3 md:order-4">
              <label className={FIELD_LABEL_CLASS}>Técnico asignado</label>
              <Select value={tecnicoId ?? ""} onChange={(e) => setTecnicoId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">-- Seleccionar técnico --</option>
                {(tecnicos || []).map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.nombre || t.email || t.id}
                  </option>
                ))}
              </Select>
            </div>
          </div>
        </CollapsibleSection>

        {/* Ingreso */}
        <CollapsibleSection
          title="Ingreso"
          open={expandedSections.ingreso}
          onToggle={() => toggleSection("ingreso")}
          incomplete={ingresoIncompleto}
        >
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Fecha de ingreso</label>
              <Input type="date" value={form.fecha_ingreso} onChange={onChange("fecha_ingreso")} />
              <div className="text-xs text-gray-500 mt-1">Si se deja vacío, se usa la fecha de hoy.</div>
            </div>
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Motivo</label>
              <Select value={form.motivo} onChange={onChange("motivo")}>
                <option value="">Seleccione motivo</option>
                {motivosIngreso.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </Select>
              {!form.motivo && <div className="text-xs text-gray-600 mt-1">Seleccione un motivo</div>}
            </div>
            <div className="md:col-span-3 text-sm text-gray-600 self-end">
              Ubicación inicial: <b>Taller</b> (se puede modificar desde la hoja de servicio)
            </div>
            <div className="md:col-span-6">
              <label className={FIELD_LABEL_CLASS}>Informe preliminar</label>
              <TextArea rows={3} value={form.informe_preliminar} onChange={onChange("informe_preliminar")} />
            </div>
            <div className="md:col-span-6">
              <label className={FIELD_LABEL_CLASS}>Comentarios</label>
              <TextArea rows={3} value={form.comentarios} onChange={onChange("comentarios")} placeholder="Notas internas u observaciones del ingreso" />
            </div>

            {/* Accesorios */}
            <div className="md:col-span-12">
              <label className="block text-xs font-semibold tracking-wide text-gray-600 mb-1">Accesorios</label>
              <div className="grid grid-cols-1 md:grid-cols-12 items-end gap-3 mb-2">
                <div className="md:col-span-6">
                  <label className={FIELD_LABEL_CLASS}>Descripción</label>
                  <Input list="accesorios_catalogo" value={nuevoAcc.descripcion} onChange={(e) => setNuevoAcc((s) => ({ ...s, descripcion: e.target.value }))} placeholder="Escriba y elija de la lista" />
                  <datalist id="accesorios_catalogo">
                    {(Array.isArray(accesCatalogo) ? accesCatalogo : []).map((a) => (
                      <option key={a.id} value={a.nombre} />
                    ))}
                  </datalist>
                </div>
                <div className="md:col-span-4">
                  <label className={FIELD_LABEL_CLASS}>Número de referencia</label>
                  <Input value={nuevoAcc.referencia} onChange={(e) => setNuevoAcc((s) => ({ ...s, referencia: e.target.value }))} placeholder="Opcional" />
                </div>
                <div className="md:col-span-2">
                  <button type="button" className="bg-blue-600 text-white px-3 py-2 rounded w-full" onClick={() => {
                  const d = (nuevoAcc.descripcion || "").trim().toLowerCase();
                  if (!d) return;
                  const acc = (accesCatalogo || []).find((a) => (a.nombre || "").trim().toLowerCase() === d);
                  if (!acc) {
                    setErr("Elija una descripción válida de la lista");
                    return;
                  }
                  setAccItems((list) => [
                    ...list,
                    { accesorio_id: acc.id, referencia: (nuevoAcc.referencia || "").trim(), accesorio_nombre: acc.nombre },
                  ]);
                  setNuevoAcc({ descripcion: "", referencia: "" });
                  }}>Agregar</button>
                </div>
              </div>
              {accItems.length > 0 && (
                <ul className="list-disc pl-5 text-sm text-gray-700">
                  {accItems.map((it, i) => (
                    <li key={i}>{it.accesorio_nombre}{it.referencia ? ` (ref: ${it.referencia})` : ""}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </CollapsibleSection>

        <div className="flex flex-col gap-3 border-t pt-4 md:flex-row md:items-center md:justify-between">
          <div className="text-sm text-gray-600">
            {editingBatchItem
              ? "Editando un equipo ya cargado. Los demás equipos se mantienen en la lista."
              : currentEquipmentDraft
              ? "Hay datos de un equipo sin agregar. Si emite ahora, se agregará automáticamente a la lista."
              : batchItems.length > 0
              ? `${batchItems.length} ${batchItems.length === 1 ? "equipo cargado" : "equipos cargados"} para ${isRegisterMode ? "registrar en un solo remito" : `emitir en un solo ${documentLabel}`}.`
              : `Puede cargar varios equipos antes de ${isRegisterMode ? "registrar el remito" : `emitir el ${documentLabel}`}.`}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            {editingBatchItem && (
              <button
                type="button"
                disabled={loading || risPreflightLoading}
                className={`px-4 py-2 rounded border text-sm font-semibold ${
                  loading || risPreflightLoading
                    ? "border-gray-200 bg-gray-100 text-gray-400 cursor-not-allowed"
                    : "border-gray-300 text-gray-700 hover:bg-gray-50"
                }`}
                onClick={cancelBatchItemEdit}
              >
                Cancelar edición
              </button>
            )}
            <button
              type="button"
              disabled={loading || risPreflightLoading || !canSubmitCliente || !!ingresoDeHoyBloqueo}
              className={`px-4 py-2 rounded border text-sm font-semibold ${
                loading || risPreflightLoading || !canSubmitCliente || ingresoDeHoyBloqueo
                  ? "border-gray-200 bg-gray-100 text-gray-400 cursor-not-allowed"
                  : "border-blue-600 text-blue-700 hover:bg-blue-50"
              }`}
              onClick={addCurrentEquipmentToBatch}
            >
              {ingresoDeHoyBloqueo
                ? "Ya ingresado hoy"
                : risPreflightLoading
                ? `Validando ${documentLabel}...`
                : editingBatchItem
                  ? "Guardar cambios"
                  : "Agregar equipo"}
            </button>
          </div>
        </div>
        </div>

        <aside className="space-y-3 rounded border border-gray-200 bg-white p-3 shadow-sm 2xl:sticky 2xl:top-4">
          <section className="rounded border border-gray-200 bg-white">
            <div className="flex items-center justify-between border-b border-gray-200 px-3 py-2">
              <h2 className="text-sm font-semibold text-gray-800">
                {isRegisterMode ? "Equipos en remito" : `Equipos en ${documentLabel}`}
              </h2>
              <span className="text-xs font-semibold text-gray-500">
                {batchItems.length} {batchItems.length === 1 ? "equipo" : "equipos"}
              </span>
            </div>
            {batchItems.length === 0 ? (
              <div className="px-3 py-4 text-sm text-gray-500">
                Agregue al menos un equipo a la lista para habilitar {isRegisterMode ? "la registración del remito" : "la emisión del remito"}.
              </div>
            ) : (
              <div className="max-h-[420px] divide-y divide-gray-100 overflow-y-auto">
                {batchItems.map((item, index) => (
                  <div
                    key={item.id}
                    className={`grid grid-cols-1 gap-3 px-3 py-3 ${
                      editingBatchItemId === item.id ? "bg-blue-50" : ""
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs font-semibold text-gray-500">#{index + 1}</span>
                        <span className="font-semibold text-gray-900">{item.display.serial}</span>
                        {editingBatchItemId === item.id && (
                          <span className="rounded bg-blue-100 px-2 py-0.5 text-xs font-semibold text-blue-700">
                            En edición
                          </span>
                        )}
                      </div>
                      <div className="mt-1 text-sm text-gray-700">{item.display.equipo}</div>
                      <div className="mt-1 text-sm text-gray-600">
                        Motivo: {item.display.motivo}
                      </div>
                      <div className="mt-1 text-xs text-gray-500">
                        Accesorios: {item.display.accesorios.length > 0 ? item.display.accesorios.join(", ") : "sin accesorios"}
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="rounded border border-gray-300 px-3 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50"
                        onClick={() => editBatchItem(item.id)}
                      >
                        Editar
                      </button>
                      <button
                        type="button"
                        className="rounded border border-red-200 px-3 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50"
                        onClick={() => removeBatchItem(item.id)}
                      >
                        Quitar
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="rounded border border-gray-200 bg-white p-3">
            <div className="flex flex-col gap-3">
              <div>
                <h2 className="text-sm font-semibold text-gray-800">Remito de ingreso</h2>
                <div className="mt-1 text-xs text-gray-600">
                  {isRegisterMode
                    ? "Use esta opción cuando el remito ya fue hecho y solo debe asociarse al ingreso."
                    : "NEXORA emitirá el remito correspondiente en Bejerman y abrirá el PDF para imprimir."}
                </div>
              </div>
              <div className="grid grid-cols-2 overflow-hidden rounded border border-gray-300">
                <button
                  type="button"
                  className={`px-3 py-2 text-sm font-semibold ${
                    !isRegisterMode ? "bg-blue-700 text-white" : "bg-white text-gray-700 hover:bg-gray-50"
                  }`}
                  onClick={() => updateRisMode("emit")}
                  aria-pressed={!isRegisterMode}
                >
                  Emitir remito
                </button>
                <button
                  type="button"
                  className={`border-l border-gray-300 px-3 py-2 text-sm font-semibold ${
                    isRegisterMode ? "bg-blue-700 text-white" : "bg-white text-gray-700 hover:bg-gray-50"
                  }`}
                  onClick={() => updateRisMode("register")}
                  aria-pressed={isRegisterMode}
                >
                  Registrar remito
                </button>
              </div>
              {isRegisterMode && (
                <div>
                  <label className={FIELD_LABEL_CLASS}>Número completo del remito</label>
                  <Input
                    value={manualRemitoNumber}
                    onChange={(event) => updateManualRemitoNumber(event.target.value)}
                    placeholder="Ej: RIS R 00004-00026249"
                  />
                  <div className="mt-1 text-xs text-gray-500">
                    Cargá tipo, letra, punto de venta y número. Puede ser manual 00001 o digital 00004/00007.
                  </div>
                </div>
              )}
            </div>
          </section>

          <RisPreflightPanel
            result={risPreflight}
            loading={risPreflightLoading}
            error={risPreflightError}
            onValidate={() => runRisPreflight()}
            onApplyCustomer={handleApplyRisCustomer}
            onApplyArticle={handleApplyRisArticle}
            onEditItem={handleEditPreflightItem}
            onConflictResolved={() => runRisPreflight({ silent: true })}
            disabled={loading || batchItems.length === 0}
            documentLabel={documentLabel}
            actionLabel={documentActionLabel}
          />

          <button
            type="submit"
            disabled={submitDisabled}
            className={`w-full rounded px-4 py-2 text-white ${
              submitDisabled
                ? "bg-blue-400 cursor-not-allowed"
                : "bg-blue-600 hover:bg-blue-700"
            }`}
          >
            {submitButtonText}
          </button>
        </aside>
      </form>
        </>
      ) : (
        <BejermanPurchaseEntries embedded companyKey={empresaBejerman} />
      )}
    </div>
  );
}
