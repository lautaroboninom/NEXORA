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
  postSolicitarBaja,
  postRechazarSolicitudBaja,
  getClientes,
  getClientesBasico,
  getMotivos,
  postDeviceMgVenta,
  postDeviceMgReactivar,
} from "../lib/api";
import { getMarcas, getModelosByBrand, getVariantesPorMarca, checkGarantiaFabrica, patchModeloTipoEquipo } from "../lib/api";
import { useAuth } from "../context/AuthContext";
import {
  formatOS as formatOSHelper,
  formatDateTime as formatDateTimeHelper,
  resolveFechaIngreso,
  resolveFechaCreacion,
  isMotivoCotizacionEquipo,
} from "../lib/ui-helpers";
import { estadoLabel } from "../lib/constants";
import { canActAsTech, ROLES } from "../lib/authz";
import { can, PERMISSION_CODES } from "../lib/permissions";
import ArchivosTab from "./ServiceSheet/tabs/ArchivosTab";
import HistorialTab from "./ServiceSheet/tabs/HistorialTab";
import PresupuestoTab from "./ServiceSheet/tabs/PresupuestoTab";
import DiagnosticoTab from "./ServiceSheet/tabs/DiagnosticoTab";
import TestTab from "./ServiceSheet/tabs/TestTab";
import PrincipalTab from "./ServiceSheet/tabs/PrincipalTab";
import DerivacionesTab from "./ServiceSheet/tabs/DerivacionesTab";

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
  const canBajaAltaPermission = can(user, PERMISSION_CODES.ACTION_INGRESO_BAJA_ALTA);
  const canManageDevices = can(user, PERMISSION_CODES.ACTION_DEVICES_PREVENTIVOS_MANAGE);

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
  useEffect(() => {
    try {
      const t = location?.state?.tab;
      if (isValidTab(t)) setTabPersisted(t);
    } catch {}
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location?.state]);

  // Leer tab/tecnico_id desde el querystring (enlaces del mail)
  useEffect(() => {
    try {
      const search = location?.search || "";
      if (!search) return;
      const sp = new URLSearchParams(search);
      const t = (sp.get("tab") || "").trim();
      if (isValidTab(t)) {
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
  }, [location?.search, canAssignTecnico]);

  // datos generales
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  // entrega
  const [entrega, setEntrega] = useState({ remito_salida: "", factura_numero: "", fecha_entrega: "", serial_confirm: "" });
  const canEditEntrega = can(user, PERMISSION_CODES.ACTION_INGRESO_EDIT_DELIVERY);
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
  const [savingBaja, setSavingBaja] = useState(false);
  const [savingAlta, setSavingAlta] = useState(false);
  const [solicitarBajaOpen, setSolicitarBajaOpen] = useState(false);
  const [solicitarBajaMotivo, setSolicitarBajaMotivo] = useState("");
  const [savingSolicitarBaja, setSavingSolicitarBaja] = useState(false);
  const [rejectingBajaRequest, setRejectingBajaRequest] = useState(false);
  const [mgModalOpen, setMgModalOpen] = useState(false);
  const [mgSaving, setMgSaving] = useState(false);
  const [mgForm, setMgForm] = useState({
    factura_numero: "",
    remito_numero: "",
    fecha_venta: "",
    observaciones: "",
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
      return (clientes || []).find((c) => normalizeClientText(c?.razon_social) === needle) || null;
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

  // PATCH helper
  async function patch(fields) {
    try {
      await patchIngreso(id, fields);
      setData((d) => ({ ...d, ...fields }));
      setErr("");
    } catch (e) {
      const conflict = e?.data?.conflict_type;
      if (conflict) {
        const detail = e?.data?.detail || "Conflicto al guardar identificadores. Debes corregirlo desde Equipos.";
        const devId = e?.data?.payload?.device_mg?.id || e?.data?.payload?.device_ns?.id || null;
        setErr(detail);
        window.alert(`${detail}\nTe redirigimos a Equipos para corregir N/S o MG.`);
        if (devId) {
          navigate(`/equipos?device_id=${devId}&from=service&ingreso_id=${id}`);
        } else {
          navigate(`/equipos?from=service&ingreso_id=${id}`);
        }
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

  // cargar historial solo cuando se selecciona la pestaña
  useEffect(() => {
    if (tab !== "historial") return;
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
  }, [tab, id]);

  // limpiar modal relacionados al cambiar id
  useEffect(() => {
    setRelatedOpen(false);
    setRelatedRows([]);
    setRelatedErr("");
    setRelatedLoading(false);
    setSolicitarBajaOpen(false);
    setSolicitarBajaMotivo("");
    setSavingSolicitarBaja(false);
    setRejectingBajaRequest(false);
    setMgModalOpen(false);
    setMgSaving(false);
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
      observaciones: "",
    });
  }, [data?.id, data?.mg_venta_fecha, data?.mg_venta_factura_numero, data?.mg_venta_remito_numero]);

  // cargar relacionados cuando se abre el modal
  useEffect(() => {
    if (!relatedOpen) return;
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
  }, [relatedOpen, data?.numero_serie, id]);

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
          } catch { setModelos([]); setModeloIdSel(null); }
          try { setVarSugeridas(await getVariantesPorMarca(marcaId)); } catch { setVarSugeridas([]); }
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
          ? arr.find((c) => normalizeClientText(c?.razon_social) === normalizeClientText(clienteRsInput))
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
      setErr("Debes seleccionar un cliente valido de la lista.");
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
      const ok = window.confirm("Dar BAJA al equipo? Esta accion marcara el ingreso como baja.");
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
      setErr("Debes indicar el motivo para solicitar la BAJA.");
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

  async function guardarVentaMgMenu() {
    if (!data?.device_id) return;
    const factura = (mgForm.factura_numero || "").trim();
    const remito = (mgForm.remito_numero || "").trim();
    if (!factura && !remito) {
      setErr("Debes informar factura o remito para marcar venta.");
      return;
    }
    try {
      setMgSaving(true);
      await postDeviceMgVenta(data.device_id, {
        factura_numero: factura || null,
        remito_numero: remito || null,
        fecha_venta: mgForm.fecha_venta || null,
        observaciones: (mgForm.observaciones || "").trim() || null,
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

  // cargar catalogos base
  useEffect(() => { (async () => { try { setAccesCatalogo(await getAccesoriosCatalogo()); } catch {} })(); }, []);
  useEffect(() => { (async () => { try { setMarcas(await getMarcas()); } catch {} })(); }, []);
  useEffect(() => { (async () => { try { const list = await getMotivos(); setMotivos(Array.isArray(list) ? list : []); } catch { setMotivos([]); } })(); }, []);
  useEffect(() => {
    if (clientesLoadedRef.current) return;
    clientesLoadedRef.current = true;
    (async () => {
      try {
        await loadClientesCatalogo();
      } catch (e) {
        setClientesPerm(false);
      }
    })();
  }, [loadClientesCatalogo]);
  useEffect(() => {
    if (!editBasics) return;
    if (!marcaIdSel) { setModelos([]); setModeloIdSel(null); setVarSugeridas([]); return; }
    (async () => {
      try { setModelos(await getModelosByBrand(marcaIdSel) || []); } catch { setModelos([]); }
      try { setVarSugeridas(await getVariantesPorMarca(marcaIdSel)); } catch { setVarSugeridas([]); }
    })();
  }, [editBasics, marcaIdSel]);
  useEffect(() => {
    if (!editBasics) return;
    const ns = (formBasics?.numero_serie || "").trim();
    const selMarca = marcas.find((m) => String(m.id) === String(marcaIdSel));
    const marcaName = (selMarca?.nombre || data?.marca || "").toString();
    if (!ns) { if (formBasics) setFormBasics((s) => ({ ...(s || {}), garantia: false })); return; }
    const h = setTimeout(async () => {
      try {
        const r = await checkGarantiaFabrica(ns, marcaName);
        const enGarantia = !!r.within_365_days;
        setFormBasics((s) => ({ ...(s || {}), garantia: enGarantia }));
      } catch {}
    }, 400);
    return () => clearTimeout(h);
  }, [editBasics, formBasics?.numero_serie, marcaIdSel, marcas, data?.marca]);

  const estadoLower = (data?.estado || "").toLowerCase();
  const isEntregadoOBaja = estadoLower === "entregado" || estadoLower === "baja";

  // carga general
  useEffect(() => {
    (async () => {
      try {
        const [ing, ubs] = await Promise.all([getIngreso(id, { strong: 1 }), getUbicaciones()]);
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
          fecha_entrega: toDatetimeLocalStr(ing?.fecha_entrega),
        });
        // tcnicos
        if (canAssignTecnico) { try { setTecnicos(await getTecnicos()); } catch {} } else { setTecnicos([]); }
      } catch (e) {
        setErr(e?.message || "Error cargando datos");
      }
    })();
  }, [id, canAssignTecnico, tecnicoIdQS]);

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
  const permiteReparacion = Boolean(data?.permite_reparacion ?? true);
  const canEditDiag = canEditDiagPermission && (canAssignTecnico || assignedToMe);
  const canManagePhotos = canEditDiag;
  const canMarkReparado = canRepairTransitions && (canAssignTecnico || assignedToMe);
  const canResolve = canRepairTransitions && (canAssignTecnico || assignedToMe);
  const canAutorizarReparar = canRepairTransitions && (canAssignTecnico || assignedToMe);
  const canDarBaja = canBajaAltaPermission;
  const hasPendingBajaRequest = Boolean(
    data?.baja_solicitada_id
      || String(data?.baja_solicitada_motivo || "").trim()
      || data?.baja_solicitada_fecha
      || String(data?.baja_solicitada_nombre || "").trim()
  );
  const showDecisionBajaModal = canDarBaja && hasPendingBajaRequest && estadoLower !== "baja";
  const canSolicitarBaja = !canDarBaja && estadoLower !== "baja";
  const canOpenSolicitarBaja = canSolicitarBaja && !hasPendingBajaRequest;
  const canEditAccesorios = canEditDiag;
  const canManageMgFromMenu = Boolean(canManageDevices && data?.es_propietario_mg);
  const hasMenuActions = Boolean(canSeeHistory || numeroSerie || canDarBaja || canSolicitarBaja || canManageMgFromMenu);

  return (
    <div className="max-w-none p-4">
      <button type="button" onClick={() => navigate(-1)} className="mb-3 inline-flex items-center gap-2 text-sm text-blue-600 hover:text-blue-800">
        Volver
      </button>
      <div className="flex items-start justify-between gap-3 mb-2">
        <h1 className="text-2xl font-bold">
          Hoja de servicio - OS: {formatOSHelper(data, id)} - NS: {data?.numero_interno || data?.numero_serie}
        </h1>
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
              <div className="absolute right-0 mt-2 w-56 rounded border bg-white shadow-lg z-20">
                <div className="py-1">
                  {numeroSerie && (
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
                  {canDarBaja && (
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
      <div className="text-sm text-gray-700 mb-3">{(data?.tipo_equipo_nombre || data?.tipo_equipo || "-").toString()} - {(data?.marca || "-").toString()} - {(data?.modelo || "-").toString()} {(data?.equipo_variante || "").toString()}</div>
      

      {err && <div className="bg-red-100 border border-red-300 text-red-700 p-2 rounded mb-4">{err}</div>}
      <Tabs
        value={tab}
        onChange={setTabPersisted}
        items={[
          { value: "principal", label: "Principal" },
          { value: "diagnostico", label: "Diagnóstico y Reparación" },
          { value: "test", label: "Tests" },
          { value: "presupuesto", label: isCotizacion ? "Cotización" : "Presupuesto" },
          { value: "derivaciones", label: "Derivaciones" },
          { value: "archivos", label: "Archivos" },
        ]}
      />
      {/* ARCHIVOS */}
      {tab === "archivos" && (<ArchivosTab id={id} canManagePhotos={canManagePhotos} />)}

      {/* PRINCIPAL */}
      {tab === "principal" && (
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
          toDatetimeLocalStr={toDatetimeLocalStr}
        />
      )}

      {/* Diagnóstico */}
      {tab === "diagnostico" && (
        <DiagnosticoTab
          id={id}
          data={data}
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
          setResolucion ={setResolucion}
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
        />
      )}

      {/* TEST */}
      {tab === "test" && (
        <TestTab
          id={id}
          setErr={setErr}
        />
      )}

      {/* PRESUPUESTO */}
      {tab === "presupuesto" && (
        <PresupuestoTab
          id={id}
          data={data}
          canManagePresupuesto={canManagePresupuesto}
          canSeeCosts={canSeeCosts}
          money={money}
          isCotizacion={isCotizacion}
          refreshIngreso={refreshIngreso}
          setErr={setErr}
        />
      )}

      {/* DERIVACIONES */}
      {tab === "derivaciones" && (
        <DerivacionesTab id={id} setErr={setErr} refreshIngreso={refreshIngreso} />
      )}

      {relatedOpen && (
        <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/50 p-4" role="dialog" aria-modal="true" onClick={() => setRelatedOpen(false)}>
          <div className="bg-white rounded shadow-xl max-w-4xl w-full max-h-[80vh] overflow-y-auto p-4" onClick={(e) => e.stopPropagation()}>
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
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="bg-white rounded shadow-xl max-w-xl w-full p-5">
            <h2 className="text-lg font-semibold">Solicitud de BAJA pendiente</h2>
            <div className="text-sm text-gray-600 mt-1">
              OS: {formatOSHelper(data, id)} - NS: {data?.numero_interno || data?.numero_serie || "-"}
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
                disabled={savingBaja || rejectingBajaRequest}
              >
                {savingBaja ? "Aceptando..." : "Aceptar"}
              </button>
            </div>
          </div>
        </div>
      )}

      {solicitarBajaOpen && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!savingSolicitarBaja) setSolicitarBajaOpen(false); }}
        >
          <div className="bg-white rounded shadow-xl max-w-xl w-full p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Solicitar BAJA</h2>
                <div className="text-sm text-gray-600">
                  OS: {formatOSHelper(data, id)} - NS: {data?.numero_interno || data?.numero_serie || "-"}
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

      {mgModalOpen && canManageMgFromMenu && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => { if (!mgSaving) setMgModalOpen(false); }}
        >
          <div className="bg-white rounded shadow-xl max-w-2xl w-full p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">Venta del equipo (MG)</h2>
                <div className="text-sm text-gray-600">
                  {data?.numero_interno || data?.numero_serie || "-"}
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
                  MG historico inactivo por venta; no operativo para nuevos ingresos.
                </div>
                <div>Fecha venta: {data?.mg_venta_fecha ? formatDateTimeHelper(data.mg_venta_fecha) : "-"}</div>
                <div>Factura venta: {data?.mg_venta_factura_numero || "-"}</div>
                <div>Remito venta: {data?.mg_venta_remito_numero || "-"}</div>
                <div>Observaciones: {data?.mg_venta_observaciones || "-"}</div>
                <label className="block">
                  <div className="text-xs text-gray-600 mb-1">Observaciones de reactivacion</div>
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
                  <label className="block md:col-span-2">
                    <div className="text-xs text-gray-600 mb-1">Fecha venta</div>
                    <input
                      type="date"
                      className="border rounded p-2 w-full"
                      value={mgForm.fecha_venta}
                      onChange={(e) => setMgForm((s) => ({ ...s, fecha_venta: e.target.value }))}
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
                  disabled={mgSaving || !((mgForm.factura_numero || "").trim() || (mgForm.remito_numero || "").trim())}
                  type="button"
                >
                  {mgSaving ? "Guardando..." : "Registrar venta"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {toastMsg && (
        <div className="fixed right-4 top-4 bg-emerald-600 text-white px-4 py-2 rounded shadow-lg" role="status">
          {toastMsg}
        </div>
      )}
      {showReparadoToast && (
        <div className="fixed right-4 top-4 bg-emerald-600 text-white px-4 py-2 rounded shadow-lg" role="status">
          Marcado como reparado
        </div>
      )}

      {/* HISTORIAL */}
      {tab === "historial" && canSeeHistory && (
        <HistorialTab hErr={hErr} hLoading={hLoading} hist={hist} />
      )}

      {/* Boton flotante para edicion basica */}
      {canEditBasics && (
        <div className="fixed bottom-4 right-4 z-20 flex gap-2">
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
