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
  getBejermanIngressCompanies,
  postRisPreflight,
  postRisPreflightCustomerFix,
  postRisPreflightArticleFix,
  postIngresoRisEmitir,
  getSerialBarcodeBlob,
} from "@/lib/api";
import { openPdfBlob } from "@/lib/pdf";
import {
  openRisPrintablePdf,
  risRemitoFrom,
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

function documentTypeFromResult(result, fallback = "RIS") {
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
  const [risMode, setRisMode] = useState("emit");
  const [manualRemitoNumber, setManualRemitoNumber] = useState("");
  const [risPreflight, setRisPreflight] = useState(null);
  const [risPreflightLoading, setRisPreflightLoading] = useState(false);
  const [risPreflightError, setRisPreflightError] = useState("");
  const [bejermanSuggestion, setBejermanSuggestion] = useState(null);
  const [barcodeLoading, setBarcodeLoading] = useState(false);
  const [dupPrompt, setDupPrompt] = useState({ open: false, ingresoId: null, fechaIngreso: null, os: "" });

  const [clientesPerm, setClientesPerm] = useState(true);
  const [garRepLoading, setGarRepLoading] = useState(false);
  const [garRepError, setGarRepError] = useState(false);
  const [autofillBy, setAutofillBy] = useState("serie");
  const [lookupRequest, setLookupRequest] = useState({ nonce: 0, source: "serie", code: "" });
  const [mgLookup, setMgLookup] = useState({ loading: false, notFound: false, checkedNs: "" });
  const [mgInactiveInfo, setMgInactiveInfo] = useState(null);
  const [nsAutofillInfo, setNsAutofillInfo] = useState("");
  const [nsAutofillClienteWarning, setNsAutofillClienteWarning] = useState("");
  const mgLookupSeqRef = useRef(0);
  const lookupRequestSeqRef = useRef(0);
  const nsAutofillSnapshotRef = useRef(null);
  const nsAutoDesiredBrandRef = useRef(null);
  const nsAutoDesiredModelRef = useRef(null);

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

  const isMgOwnerCustomer = (value) => {
    const key = normalizeCustomerName(value);
    return key.includes("mgbio");
  };

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

  const clearNsAutofillFields = () => {
    const snap = nsAutofillSnapshotRef.current;
    if (!snap) {
      setNsAutofillInfo("");
      setNsAutofillClienteWarning("");
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
  };

  const triggerLookupRequest = (source, rawValue) => {
    if (source !== autofillBy) return;
    const code = String(rawValue || "").trim();
    if (!code) {
      setMgLookup({ loading: false, notFound: false, checkedNs: "" });
      setMgInactiveInfo(null);
      clearNsAutofillFields();
      return;
    }
    if (source === "interno" && !isInternalLookupReady(code)) {
      setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
      setMgInactiveInfo(null);
      return;
    }
    const nonce = ++lookupRequestSeqRef.current;
    setLookupRequest({ nonce, source, code });
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
    setTipoIngreso(TIPO_INGRESO.CLIENTE);
    setPropietario(PROPIETARIO_VACIO);
    setTecnicoId(null);
    setEmpresaBejerman("SEPID");
    setVarianteTxt("");
    setMgLookup({ loading: false, notFound: false, checkedNs: "" });
    setMgInactiveInfo(null);
    setBejermanSuggestion(null);
    setRisPreflight(null);
    setRisPreflightError("");
    setRisMode("emit");
    setManualRemitoNumber("");
    setNsAutofillInfo("");
    setNsAutofillClienteWarning("");
    nsAutofillSnapshotRef.current = null;
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
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
    if (prefill.marca_id) setMarcaId(prefill.marca_id);
    if (prefill.marca) setMarcaTxt(prefill.marca);
    if (prefill.tipo_equipo) {
      prefillSkipTipoResetRef.current = true;
      prefillTipoAppliedRef.current = true;
      setTipoSel(prefill.tipo_equipo);
    }
    const variantePrefill = prefill.variante || prefill.equipo_variante || "";
    if (variantePrefill) setVarianteTxt(variantePrefill);
    const prefillClienteNombre = prefill.customer_nombre || prefill.alquiler_a || "";
    const prefillTienePropietario = !!(
      prefill.propietario_nombre ||
      prefill.propietario_contacto ||
      prefill.propietario_doc
    );
    const prefillEsParticular =
      isParticularCustomerName(prefillClienteNombre) || (!prefillClienteNombre && prefillTienePropietario);
    setTipoIngreso(prefillEsParticular ? TIPO_INGRESO.PARTICULAR : TIPO_INGRESO.CLIENTE);

    if (prefill.customer_nombre || prefill.alquiler_a) {
      setClienteRsInput(prefill.customer_nombre || prefill.alquiler_a || "");
      setClienteCodInput(prefill.customer_cod || "");
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
  }, [location, searchParams]);

  useEffect(() => {
    if (prefillModelAppliedRef.current) return;
    const prefill = prefillRef.current;
    if (!prefill || !prefill.model_id) return;
    if (!modelos || modelos.length === 0) return;
    const exists = modelos.some((m) => String(m.id) === String(prefill.model_id));
    if (!exists) return;
    setForm((f0) => ({ ...f0, equipo: { ...f0.equipo, modelo_id: String(prefill.model_id) } }));
    prefillModelAppliedRef.current = true;
    nsAutoDesiredModelRef.current = null;
  }, [modelos]);

  useEffect(() => {
    if (prefillClienteAppliedRef.current) return;
    const prefill = prefillRef.current;
    if (!prefill) return;
    if (!clientes || clientes.length === 0) return;
    const rsVal = (clienteRsInput || prefill.customer_nombre || prefill.alquiler_a || "").trim();
    const codVal = (clienteCodInput || prefill.customer_cod || "").trim();
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
    const lookupLabel = lookupByInterno ? "número interno" : "N/S";
    setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
    const seq = ++mgLookupSeqRef.current;
    (async () => {
      setMgLookup((s) => ({ ...s, loading: true, notFound: false, checkedNs: lookupCode }));
      try {
        const res = await lookupScan(lookupCode);
        if (mgLookupSeqRef.current !== seq) return;

        if (res?.kind === "bejerman_sale" && res?.suggestion) {
          clearNsAutofillFields();
          setMgInactiveInfo(null);
          setBejermanSuggestion(res.suggestion);
          setMgLookup({ loading: false, notFound: false, checkedNs: lookupCode });
          setNsAutofillInfo("Venta encontrada en Bejerman. Puede aplicar la sugerencia de equipo y decidir el cliente.");
          return;
        }

        if (res?.kind !== "device" || !res?.device) {
          clearNsAutofillFields();
          setMgInactiveInfo(null);
          setBejermanSuggestion(null);
          setMgLookup({ loading: false, notFound: true, checkedNs: lookupCode });
          return;
        }

        const device = res.device || {};
        const ingreso = res.ingreso || {};
        const flags = res.flags || {};
        setBejermanSuggestion(null);

        const marcaIdFromDevice = toIntOrNull(device.marca_id);
        const modelIdFromDevice = toIntOrNull(device.model_id);
        const nsFromDevice = String(device.numero_serie || "").trim();
        const tipoFromDevice = String(device.tipo_equipo || "").trim();
        const varianteFromDevice = String(device.variante || "").trim();
        const mgFromDevice = String(device.numero_interno || "").trim();
        const mgInactiveBySale = !!flags.mg_inactivo_venta;

        const alquilerA = String(ingreso.alquiler_a || "").trim();
        const esPropietarioMg = !!flags.es_propietario_mg;
        const prevAutoCliente = !!nsAutofillSnapshotRef.current?.auto_cliente;

        const deviceCustomerRs = String(device.customer_nombre || "").trim();
        const deviceCustomerCod = String(device.customer_cod || "").trim();
        const deviceCustomerTelefono = String(device.customer_telefono || "").trim();
        const customerIsMgOwner = isMgOwnerCustomer(deviceCustomerRs);
        const useAlquilerCliente = esPropietarioMg && customerIsMgOwner;

        const clienteRawRs = useAlquilerCliente ? alquilerA : deviceCustomerRs;
        const clienteRawCod = useAlquilerCliente ? "" : deviceCustomerCod;
        const clienteRawTelefono = useAlquilerCliente ? "" : deviceCustomerTelefono;

        const byRs = clienteRawRs ? findClienteByRS(clienteRawRs) : null;
        const byCod = clienteRawCod ? findClienteByCod(clienteRawCod) : null;
        let clienteMatch = byCod || byRs || null;
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
        setNsAutofillInfo(hasAutofillData ? `Datos autocompletados desde Equipos por ${lookupLabel}. Puede editarlos.` : "");
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
        const r = await checkGarantiaReparacion(ns, mg);
        setForm((f) => ({ ...f, garantia_reparacion: !!r?.within_90_days }));
        setGarRepLoading(false);
      } catch {
        setGarRepLoading(false);
        setGarRepError(true);
      }
    }, 400);
    return () => clearTimeout(h);
  }, [form.equipo.numero_serie, form.equipo.numero_interno]);

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
        });
        if (typeof r?.within_365_days !== "boolean") return;
        const enGarantia = r.within_365_days;
        setForm((f) => ({ ...f, equipo: { ...f.equipo, garantia: enGarantia } }));
      } catch {
        /* noop: no bloquear */
      }
    }, 400);
    return () => clearTimeout(h);
  }, [form.equipo.numero_serie, marcaId, form.equipo.marca_id, form.equipo.modelo_id, marcas]);

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

  const onNumeroSerieChange = (e) => {
    setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
    setBejermanSuggestion(null);
    onChange("equipo.numero_serie")(e);
  };

  const onNumeroInternoChange = (e) => {
    setMgInactiveInfo(null);
    setMgLookup((s) => (s.notFound ? { ...s, notFound: false } : s));
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

  const applyBejermanSuggestion = () => {
    const s = bejermanSuggestion || {};
    const equipment = s.equipment || {};
    const suggestedMarcaId = toIntOrNull(equipment.marca_id);
    const suggestedModeloId = toIntOrNull(equipment.modelo_id);
    const serial = String(s.serial || form.equipo.numero_serie || "").trim();
    const modelInCurrentList = suggestedModeloId
      ? (modelos || []).find((m) => Number(m.id) === suggestedModeloId)
      : null;

    setForm((prev) => {
      const next = clone(prev);
      next.equipo.numero_serie = serial;
      if (suggestedMarcaId) next.equipo.marca_id = String(suggestedMarcaId);
      next.equipo.modelo_id = modelInCurrentList?.id ? String(modelInCurrentList.id) : "";
      if (typeof s?.warranty?.garantia === "boolean") {
        next.equipo.garantia = s.warranty.garantia;
      }
      return next;
    });

    if (equipment.tipo_equipo) {
      prefillSkipTipoResetRef.current = true;
      setTipoSel(equipment.tipo_equipo);
    }
    if (suggestedMarcaId) {
      setMarcaId(suggestedMarcaId);
      setMarcaTxt(equipment.marca || "");
    }
    setVarianteTxt(equipment.variante || "");
    nsAutoDesiredBrandRef.current = suggestedMarcaId;
    nsAutoDesiredModelRef.current = modelInCurrentList?.id ? null : suggestedModeloId;
    setNsAutofillInfo("Sugerencia Bejerman aplicada. Verifique cliente, motivo y accesorios antes de guardar.");
  };

  const applyBejermanCustomerSuggestion = () => {
    const local = bejermanSuggestion?.customer?.local_customer;
    if (!local?.id) return;
    setTipoIngreso(TIPO_INGRESO.CLIENTE);
    setPropietario(PROPIETARIO_VACIO);
    setClienteCatalogo(local);
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
    const s = bejermanSuggestion;
    const serial = (form.equipo.numero_serie || "").trim();
    if (!s || !serial || normalizeSerialKey(s.serial) !== normalizeSerialKey(serial)) return null;
    return {
      serial: s.serial || serial,
      issueDate: s.document?.issueDate || s.warranty?.fecha_venta || null,
      articleCode: s.article?.code || "",
      articleDescription: s.article?.description || "",
      customerCode: s.customer?.code || "",
      customerName: s.customer?.name || "",
      documentLabel: s.document?.label || "",
    };
  };

  const validateCurrentEquipmentForSubmit = () => {
    if (!form.equipo.marca_id) return "Seleccione una marca válida de la lista.";
    if (!form.equipo.modelo_id) return "Seleccione un modelo.";
    if (!form.motivo) return "Seleccione un motivo.";
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
        error: "Para un ingreso particular es obligatorio completar nombre y CUIT del propietario.",
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
    const modelo = (modelos || []).find((m) => String(m.id) === String(form.equipo.modelo_id));
    const motivo = (motivos || []).find((m) => String(m.value) === String(form.motivo));
    const serial = (form.equipo.numero_serie || "").trim();
    const interno = (form.equipo.numero_interno || "").trim();
    return {
      serial: serial || interno || "Sin identificador",
      equipo: [tipoSel, marcaTxt, modelo?.nombre, varianteTxt].filter(Boolean).join(" - ") || "Equipo",
      motivo: motivo?.label || form.motivo || "-",
      accesorios: accItems.map((it) => it.accesorio_nombre + (it.referencia ? ` (ref: ${it.referencia})` : "")),
    };
  };

  const clearCurrentEquipmentFields = () => {
    setMarcaTxt("");
    setMarcaId(null);
    setModelos([]);
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
    setBejermanSuggestion(null);
    setNsAutofillInfo("");
    setNsAutofillClienteWarning("");
    nsAutofillSnapshotRef.current = null;
    nsAutoDesiredBrandRef.current = null;
    nsAutoDesiredModelRef.current = null;
  };

  const addCurrentEquipmentToBatch = async () => {
    if (loading || risPreflightLoading) return;
    scrollPageTop();
    setErr("");
    const equipmentError = validateCurrentEquipmentForSubmit();
    if (equipmentError) {
      setErr(equipmentError);
      return;
    }
    const shared = validateSharedForSubmit();
    if (!shared.ok) {
      setErr(shared.error);
      return;
    }
    const payload = buildIngresoPayload(shared.c, shared.propietarioPayload, { includeShared: false });
    const display = currentEquipmentDisplay();
    const nextItem = {
      id: `${Date.now()}-${batchItems.length}`,
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
        marcaTxt,
        marcaId,
        tipoSel,
        varianteTxt,
        accItems: accItems.map((it) => ({ ...it })),
        tecnicoId,
        bejermanSuggestion,
      },
    };
    const nextItems = [...batchItems, nextItem];
    setRisPreflightError("");
    const nextPreflight = await runRisPreflight({ silent: false, items: nextItems });
    if (hasDocumentProfileMismatch(nextPreflight)) {
      setErr(nextPreflight?.detail || "El lote mezcla comprobantes de ingreso incompatibles.");
      return;
    }
    setBatchItems(nextItems);
    clearCurrentEquipmentFields();
    const nextDocumentType = documentTypeFromResult(nextPreflight, risMode === "register" ? "remito" : "RIS");
    setNotice(risMode === "register" ? "Equipo agregado al remito." : `Equipo agregado al ${documentDisplayName(nextDocumentType)}.`);
    if (risMode === "register" && !manualRemitoNumber.trim()) {
      setRisPreflight(null);
      return;
    }
    if (!nextPreflight) await runRisPreflight({ silent: true, items: nextItems });
  };

  const removeBatchItem = (itemId) => {
    setRisPreflight(null);
    setRisPreflightError("");
    setBatchItems((items) => items.filter((item) => item.id !== itemId));
  };

  const editBatchItem = (itemId) => {
    const item = batchItems.find((entry) => entry.id === itemId);
    if (!item) return;
    const editor = item.editor || {};
    setForm((prev) => ({
      ...prev,
      ...(editor.form || {}),
      cliente: prev.cliente,
      remito_ingreso: prev.remito_ingreso,
      fecha_ingreso: prev.fecha_ingreso,
    }));
    setMarcaTxt(editor.marcaTxt || "");
    setMarcaId(editor.marcaId || null);
    setTipoSel(editor.tipoSel || "");
    setVarianteTxt(editor.varianteTxt || "");
    setAccItems((editor.accItems || []).map((it) => ({ ...it })));
    setTecnicoId(editor.tecnicoId || null);
    setBejermanSuggestion(editor.bejermanSuggestion || null);
    setRisPreflight(null);
    setRisPreflightError("");
    removeBatchItem(itemId);
  };

  const submit = async (e) => {
    e.preventDefault();
    scrollPageTop();
    const hasBatchItems = batchItems.length > 0;
    const isRegisterMode = risMode === "register";
    if (!hasBatchItems) {
      setErr(isRegisterMode ? "Agregue el equipo a la lista antes de registrar el remito." : "Agregue el equipo a la lista antes de emitir el remito.");
      return;
    }
    if (isRegisterMode && !manualRemitoNumber.trim()) {
      setErr("Cargue el número de remito manual.");
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
      setErr("Para un ingreso particular es obligatorio completar nombre y CUIT del propietario.");
      return;
    }

    setSubmitStage("Validando remito...");
    const preflight = await runRisPreflight({ silent: true });
    if (!preflight?.can_emit) {
      setLoading(false);
      setSubmitStage("");
      return;
    }

    try {
      if (hasBatchItems) {
        const fechaIngresoNorm = normalizeFechaIngreso(form.fecha_ingreso);
        const lotePayload = {
          cliente: { id: c.id },
          propietario: propietarioPayload,
          empresa_bejerman: (empresaBejerman || "SEPID").toUpperCase(),
          empresa_facturar: (selectedBejermanCompany?.brandingKey || "SEPID").toUpperCase(),
          ris_mode: risMode,
          manual_remito_number: isRegisterMode ? manualRemitoNumber.trim() : "",
          ...(fechaIngresoNorm ? { fecha_ingreso: fechaIngresoNorm } : {}),
          items: batchItems.map((item) => item.payload),
        };
        const risProgressStartedAt = Date.now();
        setSubmitStage(isRegisterMode ? "Creando ingresos y registrando remito..." : "Creando ingresos...");
        await waitForRisProgressPaint();
        const r = await postNuevoIngresoLote(lotePayload);
        setSubmitStage(isRegisterMode ? "Registrando remito..." : "Preparando PDF...");
        const remito = risRemitoFrom(r);
        const responseMode = r?.document_mode || r?.ris?.document_mode || risMode;
        const isRegisteredResponse = responseMode === "register";
        const count = Array.isArray(r?.ingresos) ? r.ingresos.length : batchItems.length;
        const firstIngresoId = r?.ingreso_ids?.[0] || r?.ingresos?.[0]?.ingreso_id || r?.ingresos?.[0]?.id;
        let risNotice = isRegisteredResponse
          ? (
              remito
                ? `Ingresos creados y remito ${remito} registrado en Bejerman para ${count} ${count === 1 ? "equipo" : "equipos"}.`
                : r?.detail || `Ingresos creados y remito registrado en Bejerman para ${count} ${count === 1 ? "equipo" : "equipos"}.`
            )
          : (
              remito
                ? `Ingresos creados y remito ${remito} emitido para ${count} ${count === 1 ? "equipo" : "equipos"}.`
                : r?.detail || `Ingresos creados para ${count} ${count === 1 ? "equipo" : "equipos"}. Remito pendiente de reintento.`
            );
        if (!isRegisteredResponse && remito && firstIngresoId) {
          try {
            const blob = await waitForRisPdfBlob(firstIngresoId, r, {
              onProgress: (progress) => setSubmitStage(progress?.status || "Preparando PDF del remito..."),
            });
            setSubmitStage("Abriendo impresión...");
            const opened = openRisPrintablePdf(blob, r).opened;
            if (opened) {
              risNotice += " PDF listo para imprimir.";
            } else {
              setManualRisPrint({ blob, source: r, remito });
              risNotice += " El PDF ya está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.";
            }
          } catch (pdfError) {
            risNotice += ` No se pudo abrir el PDF del remito: ${pdfError?.message || "error desconocido"}. Reintente desde una hoja de servicio.`;
          }
        }
        await waitForRisProgressMinimum(risProgressStartedAt);
        resetFormFields();
        setOut({
          lote: true,
          ingresos: r?.ingresos || [],
          ingreso_ids: r?.ingreso_ids || [],
          remito_number: remito,
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
        try {
          setSubmitStage("Emitiendo remito en Bejerman...");
          await waitForRisProgressPaint();
          const risResult = await postIngresoRisEmitir(r.ingreso_id);
          setSubmitStage("Preparando PDF...");
          const remito = risRemitoFrom(risResult);
          risNotice = remito
            ? `Ingreso creado y remito ${remito} emitido.`
            : "Ingreso creado y remito emitido.";
          const blob = await waitForRisPdfBlob(r.ingreso_id, risResult, {
            onProgress: (progress) => setSubmitStage(progress?.status || "Preparando PDF del remito..."),
          });
          setSubmitStage("Abriendo impresión...");
          const opened = openRisPrintablePdf(blob, risResult).opened;
          if (opened) {
            risNotice += " PDF listo para imprimir.";
          } else {
            setManualRisPrint({ blob, source: risResult, remito });
            risNotice += " El PDF ya está listo, pero el navegador bloqueó la ventana automática. Use Abrir e imprimir.";
          }
        } catch (risError) {
          risNotice = `Ingreso creado. No se pudo emitir o abrir el remito: ${risError?.message || "error desconocido"}. Reintente desde la hoja de servicio.`;
        } finally {
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
          numero_interno: e2?.data?.payload?.numero_interno || mgInput,
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
        const itemNumber = e2.data.item_index + 1;
        setErr(`Equipo #${itemNumber}: ${e2?.data?.detail || e2?.message || "Error creando ingreso"}`);
        return;
      }
      setErr(e2?.message || "Error creando ingreso");
    } finally {
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
      setNotice(manualRisPrint.remito ? `Remito ${manualRisPrint.remito} listo para imprimir.` : "Remito listo para imprimir.");
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
  const resolvedDocumentType = documentTypeFromResult(risPreflight, isRegisterMode ? "remito" : "RIS");
  const documentActionLabel = isRegisterMode ? "Registrar remito" : "Emitir remito";
  const documentLabel = resolvedDocumentType;
  const manualRemitoReady = !isRegisterMode || manualRemitoNumber.trim().length > 0;
  const canEmitRisBatch = batchItems.length > 0 && risPreflight?.can_emit === true;
  const submitDisabled =
    loading ||
    risPreflightLoading ||
    !canSubmitCliente ||
    !manualRemitoReady ||
    !canEmitRisBatch;
  const submitButtonText = loading
    ? submitStage || "Guardando..."
    : batchItems.length > 0
      ? documentActionLabel
      : `Agregue equipos para ${isRegisterMode ? "registrar remito" : "emitir remito"}`;
  const outIngresoCount = out?.lote
    ? (Array.isArray(out.ingresos) ? out.ingresos.length : out.ingreso_ids?.length || 0)
    : 0;
  const outFirstIngresoId = out?.lote
    ? out?.ingreso_ids?.[0] || out?.ingresos?.[0]?.ingreso_id || out?.ingresos?.[0]?.id
    : out?.ingreso_id;
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
      />
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
          <div className="text-sm">El PDF del remito está listo. Si no se abrió automáticamente, imprímalo desde este botón.</div>
          <button
            type="button"
            onClick={abrirRisManualPendiente}
            className="mt-2 rounded bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-800"
          >
            Abrir e imprimir remito
          </button>
        </div>
      )}
      {out && (
        <div className="bg-green-100 border border-green-300 text-green-700 p-2 rounded flex flex-wrap items-center gap-2">
          {out.lote ? (
            <span>
              {outIngresoCount} {outIngresoCount === 1 ? "ingreso creado" : "ingresos creados"}
              {out.remito_number ? <> con remito <b>{out.remito_number}</b></> : null}
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
        {nsAutofillInfo && (
          <div className="bg-indigo-50 border border-indigo-200 text-indigo-800 p-2 rounded text-sm">
            {nsAutofillInfo}
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
              <button
                type="button"
                className="rounded bg-sky-700 px-3 py-2 text-xs font-semibold text-white hover:bg-sky-800"
                onClick={applyBejermanSuggestion}
              >
                Aplicar equipo
              </button>
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
                {bejermanSuggestion.customer?.local_customer?.id ? (
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
        <fieldset className="border rounded p-3">
          <legend className="px-2 font-semibold">Cliente</legend>
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
              <Input
                value={clienteCodInput}
                readOnly
                tabIndex={-1}
                placeholder="Se completa al elegir cliente"
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
        </fieldset>

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
                <label className={FIELD_LABEL_CLASS}>CUIT</label>
                <Input
                  value={propietario.doc}
                  onChange={(e) => setPropietario((p) => ({ ...p, doc: e.target.value }))}
                  placeholder="CUIT"
                  required
                />
              </div>
            </div>
          </div>
        )}

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
        <fieldset className="border rounded p-3">
          <legend className="px-2 font-semibold">Equipo</legend>
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            {/* Tipo de equipo */}
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Tipo de equipo</label>
              <Select value={tipoSel} onChange={(e) => setTipoSel(e.target.value || "")}> 
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
        </fieldset>

        {/* Ingreso */}
        <fieldset className="border rounded p-3">
          <legend className="px-2 font-semibold">Ingreso</legend>
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
                {motivos.map((m) => (
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
        </fieldset>

        <div className="flex flex-col gap-3 border-t pt-4 md:flex-row md:items-center md:justify-between">
          <div className="text-sm text-gray-600">
            {batchItems.length > 0
              ? `${batchItems.length} ${batchItems.length === 1 ? "equipo cargado" : "equipos cargados"} para ${isRegisterMode ? "registrar en un solo remito" : `emitir en un solo ${documentLabel}`}.`
              : `Puede cargar varios equipos antes de ${isRegisterMode ? "registrar el remito" : `emitir el ${documentLabel}`}.`}
          </div>
          <button
            type="button"
            disabled={loading || risPreflightLoading || !canSubmitCliente}
            className={`px-4 py-2 rounded border text-sm font-semibold ${
              loading || risPreflightLoading || !canSubmitCliente
                ? "border-gray-200 bg-gray-100 text-gray-400 cursor-not-allowed"
                : "border-blue-600 text-blue-700 hover:bg-blue-50"
            }`}
            onClick={addCurrentEquipmentToBatch}
          >
            {risPreflightLoading ? `Validando ${documentLabel}...` : "Agregar equipo"}
          </button>
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
                  <div key={item.id} className="grid grid-cols-1 gap-3 px-3 py-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs font-semibold text-gray-500">#{index + 1}</span>
                        <span className="font-semibold text-gray-900">{item.display.serial}</span>
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
                    ? "Use esta opción cuando el remito manual ya fue hecho y solo debe registrarse en Bejerman."
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
                  <label className={FIELD_LABEL_CLASS}>Número de remito manual</label>
                  <Input
                    value={manualRemitoNumber}
                    onChange={(event) => updateManualRemitoNumber(event.target.value)}
                    placeholder="Ej: 26249"
                  />
                  <div className="mt-1 text-xs text-gray-500">
                    Se registrará como remito manual según la configuración de ingreso.
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
        <BejermanPurchaseEntries embedded />
      )}
    </div>
  );
}



