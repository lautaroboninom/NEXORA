// web/src/pages/NuevoIngreso.jsx (UTF-8 authoring; will be re-encoded to Windows-1252)
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import {
  getClientes,
  getMarcas,
  getModelosByBrand,
  getTecnicos,
  postNuevoIngreso,
  getMotivos,
  checkGarantiaReparacion,
  checkGarantiaFabrica,
  getAccesoriosCatalogo,
  getTiposEquipo,
  getMarcasPorTipo,
  getCatalogTipos,
  getCatalogModelos,
  getCatalogVariantes,
  lookupScan,
} from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { canAny, PERMISSION_CODES } from "@/lib/permissions";

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

export default function NuevoIngreso() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  const [searchParams] = useSearchParams();
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
  const [selectedCliente, setSelectedCliente] = useState(null);
  const [clienteRsInput, setClienteRsInput] = useState("");
  const [clienteCodInput, setClienteCodInput] = useState("");

  // Variantes (opcional)
  const [varianteTxt, setVarianteTxt] = useState("");
  const [varianteSugeridas, setVarianteSugeridas] = useState([]);
  const [catTipoId, setCatTipoId] = useState(null);
  const [catModelos, setCatModelos] = useState([]);

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

  // Propietario y técnico
  const [propietario, setPropietario] = useState({ nombre: "", contacto: "", doc: "" });
  const [tecnicos, setTecnicos] = useState([]);
  const [tecnicoId, setTecnicoId] = useState(null);
  // Empresa a facturar (SEPID por defecto)
  const [empresaFact, setEmpresaFact] = useState("SEPID");

  const [loading, setLoading] = useState(false);
  const [out, setOut] = useState(null);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
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
  const findClienteByRS = (v) =>
    (clientes || []).find((c) => (c.razon_social || "").toLowerCase() === String(v || "").trim().toLowerCase());
  const findClienteByCod = (v) =>
    (clientes || []).find((c) => String(c.cod_empresa || "").toLowerCase() === String(v || "").trim().toLowerCase());

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
    setSelectedCliente(c);
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
      setSelectedCliente(null);
    }
    if (snap.auto_propietario) {
      setPropietario({ nombre: "", contacto: "", doc: "" });
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
    setSelectedCliente(null);
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
    setPropietario({ nombre: "", contacto: "", doc: "" });
    setTecnicoId(null);
    setEmpresaFact("SEPID");
    setVarianteTxt("");
    setMgLookup({ loading: false, notFound: false, checkedNs: "" });
    setMgInactiveInfo(null);
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
    const prefill = payload || (serieParam ? { numero_serie: serieParam } : null);
    if (!prefill) return;

    prefillAppliedRef.current = true;
    prefillModelAppliedRef.current = false;
    prefillClienteAppliedRef.current = false;
    prefillSkipTipoResetRef.current = true;
    prefillTipoAppliedRef.current = false;
    prefillRef.current = prefill;

    setForm((f0) => {
      const f = clone(f0);
      if (prefill.numero_serie) f.equipo.numero_serie = prefill.numero_serie;
      if (prefill.numero_interno && !prefill.mg_inactivo_venta) f.equipo.numero_interno = prefill.numero_interno;
      if (prefill.marca_id) f.equipo.marca_id = prefill.marca_id;
      if (prefill.model_id) f.equipo.modelo_id = prefill.model_id;
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
    if (prefill.customer_nombre || prefill.alquiler_a) {
      setClienteRsInput(prefill.customer_nombre || prefill.alquiler_a || "");
      setClienteCodInput(prefill.customer_cod || "");
    }
    if (prefill.propietario_nombre || prefill.propietario_contacto || prefill.propietario_doc) {
      setPropietario({
        nombre: prefill.propietario_nombre || "",
        contacto: prefill.propietario_contacto || "",
        doc: prefill.propietario_doc || "",
      });
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
    setForm((f0) => ({ ...f0, equipo: { ...f0.equipo, modelo_id: prefill.model_id } }));
    prefillModelAppliedRef.current = true;
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
  // Lookup manual por N/S o Numero interno (Enter o blur/Tab)
  useEffect(() => {
    if (!lookupRequest?.nonce) return;
    const lookupByInterno = lookupRequest.source === "interno";
    const lookupCode = String(lookupRequest.code || "").trim();
    const lookupLabel = lookupByInterno ? "Numero interno" : "N/S";
    setMgLookup((s) => (s.notFound || s.loading ? { ...s, notFound: false, loading: false } : s));
    const seq = ++mgLookupSeqRef.current;
    (async () => {
      setMgLookup((s) => ({ ...s, loading: true, notFound: false, checkedNs: lookupCode }));
      try {
        const res = await lookupScan(lookupCode);
        if (mgLookupSeqRef.current !== seq) return;

        if (res?.kind !== "device" || !res?.device) {
          clearNsAutofillFields();
          setMgInactiveInfo(null);
          setMgLookup({ loading: false, notFound: true, checkedNs: lookupCode });
          return;
        }

        const device = res.device || {};
        const ingreso = res.ingreso || {};
        const flags = res.flags || {};

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
            ? `El cliente alquilado "${clienteRawRs}" no existe en el catalogo. Seleccionalo manualmente antes de guardar.`
            : `El cliente dueno "${clienteRawRs || clienteRawCod}" no existe en el catalogo. Seleccionalo manualmente antes de guardar.`
          : "";

        const propietarioNombre = String(device.propietario_nombre || "").trim();
        const propietarioContacto = String(device.propietario_contacto || "").trim();
        const propietarioDoc = String(device.propietario_doc || "").trim();

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
        setPropietario({
          nombre: propietarioNombre,
          contacto: propietarioContacto,
          doc: propietarioDoc,
        });

        if (shouldAutofillCliente) {
          setClienteRsInput(clienteRs);
          setClienteCodInput(clienteCod);
          setSelectedCliente(clienteMatch || null);
        } else if (prevAutoCliente) {
          setClienteRsInput("");
          setClienteCodInput("");
          setSelectedCliente(null);
        }

        if (mgInactiveBySale) {
          setMgInactiveInfo({
            numero_interno: mgFromDevice || (lookupByInterno ? lookupCode : ""),
            msg: "MG historico inactivo por venta; no operativo para nuevos ingresos.",
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
          auto_propietario: true,
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
          propietarioNombre ||
          propietarioContacto ||
          propietarioDoc ||
          mgInactiveBySale
        );
        setNsAutofillInfo(hasAutofillData ? `Datos autocompletados desde Equipos por ${lookupLabel}. Podes editarlos.` : "");
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
        if (tipoSel) {
          const norm = (s) => (s || "").toString().trim().toUpperCase();
          const filtered = list.filter((m) => norm(m.tipo_equipo) === norm(tipoSel));
          setModelos(filtered);
          const currentId = (form?.equipo?.modelo_id ?? "").toString();
          const exists = filtered.some((x) => String(x.id) === currentId);
          if (!exists) setForm((f) => ({ ...f, equipo: { ...f.equipo, modelo_id: "" } }));
        } else {
          setModelos(list);
        }
        (async () => {
          setCatTipoId(null);
          setCatModelos([]);
          setVarianteSugeridas([]);
          if (!tipoSel) return;
          try {
            const tiposBrand = await getCatalogTipos(marcaId);
            const match = (tiposBrand || []).find(
              (t) => (t.name || "").trim().toUpperCase() === (tipoSel || "").trim().toUpperCase()
            );
            const tId = match?.id ?? null;
            setCatTipoId(tId);
            if (tId) {
              const mods = await getCatalogModelos(marcaId, tId);
              setCatModelos(mods || []);
            }
          } catch {
            setCatTipoId(null);
            setCatModelos([]);
          }
        })();
      })
      .catch((e) => setErr(e?.message || "Error cargando modelos"));
  }, [marcaId, tipoSel]);

  // Variantes desde catálogo según modelo interno seleccionado
  useEffect(() => {
    const m = (modelos || []).find((x) => x.id === Number(form.equipo.modelo_id));
    if (!m || !marcaId || !catTipoId) {
      setVarianteSugeridas([]);
      if (!varianteTxt) setVarianteTxt("");
      return;
    }
    const needle = (m.nombre || "").trim().toUpperCase();
    const cmatch = (catModelos || []).filter((cm) => {
      const a = (cm.name || "").trim().toUpperCase();
      const alias = (cm.alias || "").trim().toUpperCase();
      return a === needle || a.includes(needle) || needle.includes(a) || (alias && (alias === needle || needle.includes(alias) || alias.includes(needle)));
    });
    if (cmatch.length !== 1) {
      setVarianteSugeridas([]);
      if (!varianteTxt) setVarianteTxt("");
      return;
    }
    const cm = cmatch[0];
    (async () => {
      try {
        const vars = await getCatalogVariantes(marcaId, catTipoId, cm.id);
        const names = (vars || []).filter((v) => v && v.name).map((v) => v.name);
        setVarianteSugeridas(names);
        if (!varianteTxt && names.length === 1) setVarianteTxt(names[0]);
      } catch {
        setVarianteSugeridas([]);
      }
    })();
  }, [form.equipo.modelo_id, modelos, marcaId, catTipoId, catModelos, varianteTxt]);

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
    setClienteRsInput(v);
    const c = findClienteByRS(v);
    const nextCod = c?.cod_empresa || "";
    setClienteCodInput(nextCod);
    syncClienteFromInputs(v, nextCod);
  }

  const submit = async (e) => {
    e.preventDefault();
    scrollPageTop();
    setLoading(true);
    setErr("");
    setOut(null);
    setNotice("");
    setDupPrompt({ open: false, ingresoId: null, fechaIngreso: null, os: "" });

    if (!form.equipo.marca_id) {
      setLoading(false);
      setErr("Seleccion? una marca v?lida de la lista.");
      return;
    }
    if (!form.equipo.modelo_id) {
      setLoading(false);
      setErr("Seleccion? un modelo.");
      return;
    }
    if (!form.motivo) {
      setLoading(false);
      setErr("Seleccion? un motivo.");
      return;
    }

    const c = resolveCliente(clienteRsInput, clienteCodInput);
    if (!c?.id) {
      setLoading(false);
      setErr("Deb?s seleccionar un cliente v?lido de la lista.");
      return;
    }

    const mgInput = (form.equipo.numero_interno || "").trim();
    if (mgInput) {
      try {
        const scanMg = await lookupScan(mgInput);
        if (scanMg?.kind === "device" && scanMg?.flags?.mg_inactivo_venta) {
          setMgInactiveInfo({
            numero_interno: scanMg?.device?.numero_interno || mgInput,
            msg: "MG histórico inactivo por venta; no operativo para nuevos ingresos.",
          });
          setLoading(false);
          setErr("MG histórico inactivo por venta; no operativo para nuevos ingresos.");
          return;
        }
      } catch {
        // Si falla la validación online, backend vuelve a validar en el submit.
      }
    }

    try {
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
        propietario: {
          nombre: propietario.nombre || "",
          contacto: propietario.contacto || "",
          doc: propietario.doc || "",
        },
        empresa_facturar: (empresaFact || "SEPID").toUpperCase(),
        // Checkbox representa "fajas abiertas" => etiq_garantia_ok debe ser la negaci?n
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
      resetFormFields();
      setOut(r);
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
      setErr(e2?.message || "Error creando ingreso");
    } finally {
      setLoading(false);
    }
  };

  const rsMatch = clienteRsInput ? findClienteByRS(clienteRsInput) : null;
  const codMatch = clienteCodInput ? findClienteByCod(clienteCodInput) : null;
  const clienteMismatch = rsMatch && codMatch && rsMatch.id !== codMatch.id;
  const canSubmitCliente = !!resolveCliente(clienteRsInput, clienteCodInput)?.id;
  const closeDupPrompt = () => setDupPrompt({ open: false, ingresoId: null, fechaIngreso: null, os: "" });
  const confirmDupPrompt = () => {
    if (dupPrompt.ingresoId) navigate(`/ingresos/${dupPrompt.ingresoId}`);
    closeDupPrompt();
  };

  return (
    <div className="max-w-5xl mx-auto lg:mx-0 p-4 space-y-4">
      <h1 className="text-2xl font-bold">Nuevo Ingreso (Orden de Servicio)</h1>

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
      {out && (
        <div className="bg-green-100 border border-green-300 text-green-700 p-2 rounded flex flex-wrap items-center gap-2">
          <span>
            Ingreso creado: <b>{out.os}</b> (ID: {out.ingreso_id})
          </span>
          {out.ingreso_id && canViewCreatedIngreso && (
            <Link
              to={`/ingresos/${out.ingreso_id}`}
              className="font-semibold underline underline-offset-2 hover:text-green-900"
            >
              Ver ingreso
            </Link>
          )}
        </div>
      )}

      <form onSubmit={submit} className="space-y-6">
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

        {/* Equipo - datos de identificacion y garantias */}
        <fieldset className="border rounded p-3">
          <div className="mb-3 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <h3 className="font-semibold">Equipo - Identificacion</h3>
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
                <option value="serie">Numero de serie</option>
                <option value="interno">Numero interno</option>
              </Select>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className={`md:col-span-7 ${autofillBy === "serie" ? "md:order-1" : "md:order-2"}`}>
              <label className={FIELD_LABEL_CLASS}>Numero de serie</label>
              <Input
                value={form.equipo.numero_serie}
                onChange={onNumeroSerieChange}
                onBlur={onLookupFieldBlur("serie")}
                onKeyDown={onLookupFieldKeyDown("serie")}
              />
            </div>

            <div className={`md:col-span-5 ${autofillBy === "serie" ? "md:order-2" : "md:order-1"}`}>
              <div className="flex items-center justify-between gap-2">
                <label className={FIELD_LABEL_CLASS}>Numero Interno</label>
                {mgLookup.notFound && (
                  <span className="text-xs text-amber-600">
                    Equipo no encontrado por {autofillBy === "serie" ? "N/S" : "Numero interno"}
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
                  Para autocompletar, ingresa el codigo completo (ej. MG 7293).
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
                ? "Para autocompletar por N/S, presiona Enter o Tab."
                : "Para autocompletar por numero interno, presiona Enter o Tab."}
            </div>

            <div className="flex items-center gap-2 md:col-span-4 md:order-4">
              <input id="gar" type="checkbox" checked={form.equipo.garantia} onChange={onChange("equipo.garantia")} />
              <label htmlFor="gar" className="text-sm font-medium text-gray-800">En Garantia</label>
            </div>
            <div className="flex items-center gap-2 md:col-span-4 md:order-5">
              <input type="checkbox" checked={form.garantia_reparacion} onChange={(e) => setForm((f) => ({ ...f, garantia_reparacion: e.target.checked }))} />
              <span className="text-sm">Garantia de reparacion</span>
              {garRepLoading && <span className="text-xs text-gray-500">...</span>}
              {!garRepLoading && garRepError && <span className="text-xs text-gray-400">No disponible</span>}
            </div>

            <div className="md:col-span-4 md:order-6 flex items-center gap-2">
              <input id="etiqok" type="checkbox" checked={!!form.etiq_garantia_ok} onChange={(e) => setForm((f) => ({ ...f, etiq_garantia_ok: !!e.target.checked }))} />
              <label htmlFor="etiqok" className="text-sm font-medium text-gray-800">Faja de garantia abiertas</label>
            </div>
            <div className="md:col-span-12 md:order-7 text-xs text-gray-500 mt-1">
              Marca si al ingresar el equipo la faja/etiquetas estaban en mal estado.
            </div>
          </div>
        </fieldset>

        {/* Cliente */}
        <fieldset className="border rounded p-3">
          <legend className="px-2 font-semibold">Cliente</legend>
          {!clientesPerm && (
            <div className="text-xs text-gray-600 mb-2">No tenés permisos para listar clientes</div>
          )}
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className="md:col-span-6">
              <label className={FIELD_LABEL_CLASS}>Razón social</label>
              <Input
                list={clientesPerm ? "clientes_rs" : undefined}
                value={clienteRsInput}
                onChange={(e) => onClienteRsChange(e.target.value)}
                placeholder="Escribí y elegí de la lista"
                required
              />
              {clientesPerm && (
                <datalist id="clientes_rs">
                  {(Array.isArray(clientes) ? clientes : []).map((c) => (
                    <option key={c.id} value={c.razon_social} />
                  ))}
                </datalist>
              )}
              {clienteRsInput && !rsMatch && (
                <div className="text-xs text-red-600 mt-1">Debés seleccionar de la lista</div>
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

        {/* Propietario: requerido si cliente es Particular */}
        <div className="mt-4 border rounded p-3">
          <h3 className="font-semibold mb-2">Propietario</h3>
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className="md:col-span-4">
              <label className={FIELD_LABEL_CLASS}>Nombre</label>
              <Input
                value={propietario.nombre}
                onChange={(e) => setPropietario((p) => ({ ...p, nombre: e.target.value }))}
                placeholder="Nombre del propietario"
                required={selectedCliente?.razon_social?.trim().toLowerCase() === 'particular'}
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
                required={selectedCliente?.razon_social?.trim().toLowerCase() === 'particular'}
              />
            </div>
          </div>
          <div className="text-xs text-gray-500 mt-1">
            Obligatorio cuando el cliente es "Particular".
          </div>
        </div>

        {/* Empresa a facturar */}
        <div className="border rounded p-3">
          <label className={FIELD_LABEL_CLASS}>Empresa a facturar</label>
          <Select className="md:max-w-sm" value={empresaFact} onChange={(e) => setEmpresaFact((e.target.value || "SEPID").toUpperCase())}>
            <option value="SEPID">SEPID SA</option>
            <option value="MGBIO">MG BIO</option>
          </Select>
          <div className="text-xs text-gray-500 mt-1">Por defecto: SEPID SA</div>
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
                <div className="text-xs text-red-600 mt-1">Elegí una marca de las sugeridas.</div>
              )}
            </div>

            {/* Modelo */}
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Modelo</label>
              <Select value={form.equipo.modelo_id} onChange={onChange("equipo.modelo_id")} disabled={!marcaId || !modelos.length}>
                <option value="">{!marcaId ? "Elegí marca primero" : "Seleccioná modelo"}</option>
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
              <label className={FIELD_LABEL_CLASS}>Número de remito</label>
              <Input value={form.remito_ingreso} onChange={onChange("remito_ingreso")} placeholder="Opcional" />
            </div>
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Fecha de ingreso</label>
              <Input type="date" value={form.fecha_ingreso} onChange={onChange("fecha_ingreso")} />
              <div className="text-xs text-gray-500 mt-1">Si se deja vacío, se usa la fecha de hoy.</div>
            </div>
            <div className="md:col-span-3">
              <label className={FIELD_LABEL_CLASS}>Motivo</label>
              <Select value={form.motivo} onChange={onChange("motivo")} required>
                <option value="">Seleccioná motivo</option>
                {motivos.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </Select>
              {!form.motivo && <div className="text-xs text-gray-600 mt-1">Seleccioná un motivo</div>}
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
                  <Input list="accesorios_catalogo" value={nuevoAcc.descripcion} onChange={(e) => setNuevoAcc((s) => ({ ...s, descripcion: e.target.value }))} placeholder="Escribí y elegí de la lista" />
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
                    setErr("Elegí una descripción válida de la lista");
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

        <div className="flex justify-end gap-3">
          <button
            disabled={loading || !canSubmitCliente || !marcaId || !form.equipo.modelo_id || !form.motivo}
            className={`px-4 py-2 rounded text-white ${
              loading || !canSubmitCliente || !marcaId || !form.equipo.modelo_id || !form.motivo
                ? "bg-blue-400 cursor-not-allowed"
                : "bg-blue-600"
            }`}
          >
            {loading ? "Guardando..." : "Crear Ingreso"}
          </button>
        </div>
      </form>
    </div>
  );
}



