import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "../context/AuthContext";
import { normalizeRole } from "../lib/authz";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { formatMoney, norm } from "../lib/ui-helpers";
import * as XLSX from "xlsx";
import {
  getRepuestos,
  getRepuestosConfig,
  getRepuestoDetalle,
  postRepuesto,
  patchRepuesto,
  patchRepuestosConfig,
  postRepuestosMovimientoCompra,
  getRepuestosMovimientos,
  getRepuestosCambios,
  getRepuestosSubrubros,
  postRepuestosSubrubro,
  patchRepuestosSubrubro,
  deleteRepuestosSubrubro,
  getRepuestosStockPermisos,
  postRepuestosStockPermiso,
  patchRepuestosStockPermiso,
  getMarcas,
  getModelos,
  getCatalogTipos,
  getCatalogModelos,
  getCatalogVariantes,
  getTecnicos,
  getProveedoresExternos,
  deleteRepuesto,
} from "../lib/api";

const Input = (p) => <input {...p} className="border rounded p-2 w-full" />;

const Chip = ({ children, tone = "gray" }) => {
  const tones = {
    gray: "bg-gray-100 text-gray-700 border-gray-200",
    yellow: "bg-yellow-100 text-yellow-800 border-yellow-200",
    red: "bg-red-100 text-red-700 border-red-200",
    green: "bg-green-100 text-green-700 border-green-200",
    blue: "bg-blue-100 text-blue-700 border-blue-200",
  };
  const cls = tones[tone] || tones.gray;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs border ${cls}`}>
      {children}
    </span>
  );
};

const Tabs = ({ value, onChange, items }) => (
  <div className="border-b mb-3 flex flex-wrap gap-2">
    {items.map((it) => (
      <button
        key={it.value}
        className={`px-3 py-1.5 rounded-t ${value === it.value ? "bg-white border border-b-0" : "text-gray-600 hover:text-black"}`}
        onClick={() => onChange(it.value)}
        type="button"
      >
        {it.label}
      </button>
    ))}
  </div>
);

const formatTs = (s) => (s ? new Date(s).toLocaleString("es-AR") : "-");
const formatDate = (s) => {
  if (!s) return "-";
  const parts = String(s).split("-");
  if (parts.length !== 3) return String(s);
  return `${parts[2]}/${parts[1]}/${parts[0]}`;
};
const todayLocalISO = () => {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 10);
};

const normalizeEquipoToken = (value) =>
  norm(value).replace(/\s+/g, " ").trim();

const canonEquipoName = (value) => norm(value).replace(/\s+/g, " ").trim();
const typeKey = (value) =>
  (value ?? "").toString().toLowerCase().replace(/\s+/g, " ").trim();

const splitEquipoParts = (value) =>
  (value ?? "")
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean);

const parseEquipoLines = (value) =>
  (value ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

const isPrefixParts = (shortParts, longParts) => {
  if (shortParts.length > longParts.length) return false;
  for (let i = 0; i < shortParts.length; i += 1) {
    const shortPart = normalizeEquipoToken(shortParts[i]);
    const longPart = normalizeEquipoToken(longParts[i]);
    if (!longPart.startsWith(shortPart)) return false;
  }
  return true;
};

const matchEquipoEntry = (filterRaw, entryRaw) => {
  const filter = (filterRaw || "").trim();
  if (!filter) return true;
  const entryParts = splitEquipoParts(entryRaw);
  if (!entryParts.length) return false;
  if (!filter.includes("|")) {
    const needle = normalizeEquipoToken(filter);
    return entryParts.some((part) => normalizeEquipoToken(part).includes(needle));
  }
  const filterParts = splitEquipoParts(filter);
  if (!filterParts.length) return false;
  return isPrefixParts(filterParts, entryParts) || isPrefixParts(entryParts, filterParts);
};

const matchEquipoFilter = (filterRaw, estadoRaw) => {
  const filter = (filterRaw || "").trim();
  if (!filter) return true;
  const entries = parseEquipoLines(estadoRaw);
  if (!entries.length) return false;
  return entries.some((entry) => matchEquipoEntry(filter, entry));
};

const DETAIL_FIELDS = [
  "nombre",
  "tipo_articulo",
  "categoria",
  "unidad_medida",
  "marca_fabricante",
  "nro_parte",
  "ubicacion_deposito",
  "estado",
  "notas",
  "fecha_ultima_compra",
  "fecha_ultimo_conteo",
  "fecha_vencimiento",
];

const EMPTY_ADD_FORM = {
  subrubro_codigo: "",
  nombre: "",
  tipo_articulo: "",
  categoria: "",
  nro_parte: "",
  ubicacion_deposito: "",
  fecha_vencimiento: "",
  estado: "",
  stock_on_hand: "",
  stock_min: "",
  multiplicador: "",
  costo_valor: "",
  costo_moneda: "USD",
};

const EMPTY_SUBRUBRO_FORM = {
  codigo: "",
  nombre: "",
};

const COST_CURRENCY_OPTIONS = [
  { value: "USD", label: "U$D" },
  { value: "ARS", label: "$" },
];

const buildEmptyCompraForm = () => ({
  repuesto_input: "",
  repuesto_id: "",
  cantidad: "1",
  fecha_compra: todayLocalISO(),
  proveedor_input: "",
  nota: "",
});


/**
 * Repuestos component - Manages spare parts inventory system
 * 
 * Provides comprehensive spare parts (repuestos) management including:
 * - Stock tracking with alert levels and negative stock detection
 * - Dynamic pricing based on USD exchange rate and multipliers
 * - Permission system for technicians to edit stock
 * - Supplier management with lead times and priorities
 * - Movement history and change tracking
 * - Export to CSV and Excel
 * - Configuration management for exchange rates and general multipliers
 * 
 * Role-based access control:
 * - Jefes (managers) and JefeVeedores: Full management access
 * - Tecnicos (technicians): Stock edit access if permitted (24h permissions)
 * - Admins: Cost visibility
 * 
 * Features:
 * - Search filtering by code or name
 * - Inline editing for stock, minimum stock, and multipliers
 * - Detailed view with tabs for specifications, suppliers, and movements
 * - Batch loading with pagination (500 items per page)
 * - Real-time validation and change tracking
 * - Temporary permission system (24-hour duration)
 * - Movement history per item
 * - Audit trail of name changes
 * 
 * @component
 * @returns {JSX.Element} The complete repuestos management interface
 * 
 * @requires useAuth - Authentication context hook
 * @requires useRef - React reference hook
 * @requires useState - React state management
 * @requires useEffect - React side effects
 * @requires useMemo - React memoization
 * @requires Fragment - React fragment for grouping
 * 
 * @example
 * import Repuestos from '@/pages/Repuestos';
 * 
 * export default function App() {
 *   return <Repuestos />;
 * }
 */
export default function Repuestos() {
  const { user } = useAuth();
  const role = normalizeRole(user?.rol);
  const isTech = role === "tecnico";
  const canManage = can(user, PERMISSION_CODES.ACTION_SPARE_PARTS_MANAGE);
  const canManage24hPermissions = can(
    user,
    PERMISSION_CODES.ACTION_SPARE_PARTS_MANAGE_24H_PERMISSIONS,
  );
  const canSeeCosts = canManage || can(user, PERMISSION_CODES.ACTION_PRESUPUESTO_VIEW_COSTS);
  const canEditCost = canManage;
  const canManageSubrubros = canManage;
  const canViewCambios = canManage;

  const [items, setItems] = useState([]);
  const [q, setQ] = useState("");
  const [equipo, setEquipo] = useState("");
  const [orderBy, setOrderBy] = useState("codigo");
  const [orderDir, setOrderDir] = useState("desc");
  const [config, setConfig] = useState(null);
  const [history, setHistory] = useState([]);
  const [cfgForm, setCfgForm] = useState({ dolar_ars: "", multiplicador_general: "" });
  const [drafts, setDrafts] = useState({});
  const [detalles, setDetalles] = useState({});
  const [openId, setOpenId] = useState(null);
  const [movimientos, setMovimientos] = useState([]);
  const [movimientosOpen, setMovimientosOpen] = useState(false);
  const [movimientosLoading, setMovimientosLoading] = useState(false);
  const [cambios, setCambios] = useState([]);
  const [cambiosOpen, setCambiosOpen] = useState(false);
  const [cambiosLoading, setCambiosLoading] = useState(false);
  const [cambiosErr, setCambiosErr] = useState("");
  const [permisos, setPermisos] = useState([]);
  const [permisosLoading, setPermisosLoading] = useState(false);
  const [permisosErr, setPermisosErr] = useState("");
  const [permSaving, setPermSaving] = useState(false);
  const [permForm, setPermForm] = useState({ tecnico_id: "" });
  const [tecnicos, setTecnicos] = useState([]);
  const [proveedores, setProveedores] = useState([]);
  const [subrubros, setSubrubros] = useState([]);
  const [subrubrosLoading, setSubrubrosLoading] = useState(false);
  const [subrubrosErr, setSubrubrosErr] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [cfgLoading, setCfgLoading] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [compraOpen, setCompraOpen] = useState(false);
  const [compraSaving, setCompraSaving] = useState(false);
  const [compraForm, setCompraForm] = useState(() => buildEmptyCompraForm());
  const [compraRepuestoOpen, setCompraRepuestoOpen] = useState(false);
  const [subrubrosOpen, setSubrubrosOpen] = useState(false);
  const [subrubroForm, setSubrubroForm] = useState(() => ({ ...EMPTY_SUBRUBRO_FORM }));
  const [subrubroEditCodigo, setSubrubroEditCodigo] = useState("");
  const [subrubroSaving, setSubrubroSaving] = useState(false);
  const [subrubroDeletingCodigo, setSubrubroDeletingCodigo] = useState("");
  const [addSaving, setAddSaving] = useState(false);
  const [addForm, setAddForm] = useState(() => ({ ...EMPTY_ADD_FORM }));
  const [addEquipoInput, setAddEquipoInput] = useState("");
  const [addEquipoErr, setAddEquipoErr] = useState("");
  const menuRef = useRef(null);
  const [equipOptionsAll, setEquipOptionsAll] = useState([]);
  const [equipOptionsLoading, setEquipOptionsLoading] = useState(false);
  const [equipOptionsErr, setEquipOptionsErr] = useState("");
  const [equipOpenId, setEquipOpenId] = useState(null);

  const filtered = useMemo(() => {
    const needle = norm(q);
    const equipoNeedle = (equipo || "").trim();
    if (!needle && !equipoNeedle) return items;
    return (items || []).filter((it) => {
      if (
        needle &&
        !norm(it?.codigo || "").includes(needle) &&
        !norm(it?.nombre || "").includes(needle)
      ) {
        return false;
      }
      if (!equipoNeedle) return true;
      return matchEquipoFilter(equipoNeedle, it?.estado || "");
    });
  }, [q, equipo, items]);

  const equipOptionsIndex = useMemo(() => {
    return (equipOptionsAll || []).map((opt) => ({
      raw: opt,
      norm: normalizeEquipoToken(opt),
    }));
  }, [equipOptionsAll]);

  const equipOptionsMap = useMemo(() => {
    const map = new Map();
    equipOptionsIndex.forEach((opt) => {
      if (!opt.norm) return;
      if (!map.has(opt.norm)) {
        map.set(opt.norm, opt.raw);
      }
    });
    return map;
  }, [equipOptionsIndex]);

  const equipOptionsLoadingUi =
    equipOptionsLoading || (!equipOptionsAll.length && !equipOptionsErr);

  function filterEquipOptions(input) {
    const needle = normalizeEquipoToken(input);
    if (!needle) return equipOptionsIndex.map((opt) => opt.raw);
    return equipOptionsIndex
      .filter((opt) => opt.norm.includes(needle))
      .map((opt) => opt.raw);
  }

  const addEquipDropdownOpen = equipOpenId === "add";
  const addEquipoSuggestions = addEquipDropdownOpen
    ? filterEquipOptions(addEquipoInput)
    : [];
  const addEquipoSuggestionsTop = addEquipoSuggestions.slice(0, 200);
  const addEquipoMatch = addEquipoInput
    ? equipOptionsMap.get(normalizeEquipoToken(addEquipoInput))
    : null;
  const addEquipoLines = parseEquipoLines(addForm.estado);

  const activePerm = useMemo(() => {
    if (!isTech) return null;
    const uid = Number(user?.id || 0);
    if (!uid) return null;
    return (permisos || []).find((p) => Number(p?.tecnico_id) === uid) || null;
  }, [permisos, isTech, user?.id]);

  const canEditStock = canManage || !!activePerm;
  const canEditDetalle = canEditStock;
  const canDelete = canEditStock;
  const canAdd = canManage || (isTech && !!activePerm);
  const colCount = 6;
  const compraSelectedRepuesto = useMemo(() => {
    const byInput = findRepuestoCompraMatch(compraForm.repuesto_input);
    if (byInput?.id) return byInput;
    const repId = Number(compraForm.repuesto_id || 0);
    if (!repId) return null;
    return (items || []).find((it) => Number(it?.id || 0) === repId) || null;
  }, [compraForm.repuesto_input, compraForm.repuesto_id, items]);
  const compraRepuestoSuggestions = useMemo(() => {
    if (!compraRepuestoOpen) return [];
    const needle = norm(compraForm.repuesto_input || "");
    const rows = (items || []).filter((it) => {
      if (!needle) return true;
      return (
        norm(it?.codigo || "").includes(needle) ||
        norm(it?.nombre || "").includes(needle) ||
        norm(repuestoOptionLabel(it)).includes(needle)
      );
    });
    return rows.slice(0, 200);
  }, [compraRepuestoOpen, compraForm.repuesto_input, items]);

  async function loadConfig() {
    try {
      setErr("");
      const data = await getRepuestosConfig();
      const cfg = data?.config || null;
      setConfig(cfg);
      setHistory(data?.history || []);
      setCfgForm({
        dolar_ars: cfg?.dolar_ars != null ? String(cfg.dolar_ars) : "",
        multiplicador_general:
          cfg?.multiplicador_general != null
            ? String(cfg.multiplicador_general)
            : "",
      });
    } catch (e) {
      setErr(e?.message || "No se pudo cargar la configuracion");
    }
  }

  async function loadRepuestos() {
    try {
      setErr("");
      setLoading(true);
      const pageSize = 500;
      const all = [];
      let offset = 0;
      for (let i = 0; i < 50; i++) {
        const rows = await getRepuestos({
          limit: pageSize,
          offset,
          order: orderBy,
          dir: orderDir,
        });
        const batch = Array.isArray(rows) ? rows : [];
        all.push(...batch);
        if (batch.length < pageSize) break;
        offset += batch.length;
      }
      setItems(all);
    } catch (e) {
      setErr(e?.message || "No se pudo cargar repuestos");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }

  async function loadPermisos() {
    try {
      setPermisosErr("");
      setPermisosLoading(true);
      const rows = await getRepuestosStockPermisos();
      setPermisos(rows || []);
    } catch (e) {
      setPermisosErr(e?.message || "No se pudieron cargar permisos");
      setPermisos([]);
    } finally {
      setPermisosLoading(false);
    }
  }

  async function loadTecnicos() {
    if (!canManage24hPermissions) return;
    try {
      const rows = await getTecnicos();
      setTecnicos(rows || []);
    } catch {
      setTecnicos([]);
    }
  }

  async function loadProveedores() {
    if (!canEditStock) return;
    try {
      const rows = await getProveedoresExternos();
      setProveedores(rows || []);
    } catch {
      setProveedores([]);
    }
  }

  async function loadSubrubros() {
    if (!canAdd && !canManageSubrubros) return;
    try {
      setSubrubrosErr("");
      setSubrubrosLoading(true);
      const rows = await getRepuestosSubrubros();
      setSubrubros(rows || []);
    } catch (e) {
      setSubrubrosErr(e?.message || "No se pudieron cargar subrubros");
      setSubrubros([]);
    } finally {
      setSubrubrosLoading(false);
    }
  }

  async function loadEquipOptionsAll() {
    if (equipOptionsLoading) return;
    if ((equipOptionsAll || []).length) return;
    try {
      setEquipOptionsErr("");
      setEquipOptionsLoading(true);
      const marcas = await getMarcas();
      const allOptions = new Map();
      const addOption = (value) => {
        const cleaned = (value || "").trim();
        if (!cleaned) return;
        const key = normalizeEquipoToken(cleaned);
        if (!key || allOptions.has(key)) return;
        allOptions.set(key, cleaned);
      };

      for (const marca of marcas || []) {
        const marcaNombre = (marca?.nombre || marca?.name || "").trim();
        if (!marcaNombre) continue;
        addOption(marcaNombre);
        const modelos = await getModelos(marca.id);
        const modelosArr = Array.isArray(modelos) ? modelos : [];

        const tipos = await getCatalogTipos(marca.id);
        const tiposArr = Array.isArray(tipos) ? tipos : [];
        const tipoByKey = new Map();
        tiposArr.forEach((tipo) => {
          const keyName = typeKey(tipo?.name || "");
          const keyLabel = typeKey(tipo?.label || tipo?.name || "");
          if (keyName) tipoByKey.set(keyName, tipo);
          if (keyLabel) tipoByKey.set(keyLabel, tipo);
        });

        const catalogModelIndexByTipo = new Map();
        const getCatalogModelIndex = async (tipoId) => {
          if (catalogModelIndexByTipo.has(tipoId)) {
            return catalogModelIndexByTipo.get(tipoId);
          }
          const modelosCat = await getCatalogModelos(marca.id, tipoId);
          const index = new Map();
          (modelosCat || []).forEach((modelo) => {
            if (modelo?.active === false) return;
            const nameKey = canonEquipoName(modelo?.name || "");
            const aliasKey = canonEquipoName(modelo?.alias || "");
            if (nameKey) index.set(nameKey, modelo);
            if (aliasKey) index.set(aliasKey, modelo);
          });
          catalogModelIndexByTipo.set(tipoId, index);
          return index;
        };

        const variantesCache = new Map();
        const getVariantesBySerie = async (serieId) => {
          if (variantesCache.has(serieId)) return variantesCache.get(serieId);
          const rows = await getCatalogVariantes(marca.id, null, serieId);
          const variantes = (rows || []).filter((v) => v?.active !== false);
          variantesCache.set(serieId, variantes);
          return variantes;
        };

        for (const modelo of modelosArr) {
          const modeloNombre = (modelo?.nombre || modelo?.name || "").trim();
          if (!modeloNombre) continue;
          addOption(`${marcaNombre} | ${modeloNombre}`);

          const varianteSimple = (modelo?.variante || "").trim();
          if (varianteSimple) {
            addOption(`${marcaNombre} | ${modeloNombre} | ${varianteSimple}`);
          }

          const tipoName = (modelo?.tipo_equipo || "").trim();
          if (!tipoName) continue;
          const tipo = tipoByKey.get(typeKey(tipoName));
          if (!tipo) continue;

          const index = await getCatalogModelIndex(tipo.id);
          const serie = index.get(canonEquipoName(modeloNombre));
          if (!serie) continue;

          const variantes = await getVariantesBySerie(serie.id);
          variantes.forEach((variante) => {
            const varNombre = (variante?.name || variante?.nombre || "").trim();
            if (!varNombre) return;
            addOption(`${marcaNombre} | ${modeloNombre} | ${varNombre}`);
          });
        }
      }

      const sorted = Array.from(allOptions.values()).sort((a, b) =>
        a.localeCompare(b, "es", { sensitivity: "base" })
      );
      setEquipOptionsAll(sorted);
    } catch (e) {
      setEquipOptionsErr(e?.message || "No se pudieron cargar equipos");
    } finally {
      setEquipOptionsLoading(false);
    }
  }

  function applyItemUpdate(updated) {
    if (!updated?.id) return;
    setItems((prev) =>
      prev.map((row) => {
        if (row.id !== updated.id) return row;
        const next = { ...row, ...updated };
        const stockOnHand = Number(next.stock_on_hand ?? 0);
        const stockMin = Number(next.stock_min ?? 0);
        next.stock_alerta = stockOnHand <= stockMin;
        next.stock_negativo = stockOnHand < 0;
        const multGeneral = Number(config?.multiplicador_general ?? 0);
        if (next.multiplicador != null && String(next.multiplicador) !== "") {
          next.multiplicador_aplicado = next.multiplicador;
        } else if (multGeneral) {
          next.multiplicador_aplicado = multGeneral;
        }
        return next;
      })
    );
  }

  function updateDetalleState(id, patch) {
    setDetalles((prev) => ({
      ...prev,
      [id]: {
        ...(prev[id] || {}),
        ...patch,
      },
    }));
  }

  function buildDetalleDraft(data) {
    const out = {};
    DETAIL_FIELDS.forEach((field) => {
      out[field] = data?.[field] ?? "";
    });
    out.costo_valor = data?.costo_valor != null ? String(data.costo_valor) : "";
    out.costo_moneda = data?.costo_moneda || "USD";
    out.multiplicador = data?.multiplicador != null ? String(data.multiplicador) : "";
    return out;
  }

  function buildProveedoresDraft(data) {
    return (data?.proveedores || []).map((p) => ({
      proveedor_id: p?.proveedor_id ?? "",
      proveedor_nombre: p?.proveedor_nombre ?? "",
      sku_proveedor: p?.sku_proveedor ?? "",
      lead_time_dias: p?.lead_time_dias ?? "",
      prioridad: p?.prioridad ?? "",
      ultima_compra: p?.ultima_compra ?? "",
    }));
  }

  async function loadDetalle(id) {
    updateDetalleState(id, { loading: true, error: "" });
    try {
      const data = await getRepuestoDetalle(id);
      updateDetalleState(id, {
        loading: false,
        data,
        draft: buildDetalleDraft(data),
        proveedoresDraft: buildProveedoresDraft(data),
        tab: "detalle",
        equipoInput: "",
        equipoErr: "",
        movs: [],
        movsLoading: false,
        movsError: "",
      });
    } catch (e) {
      updateDetalleState(id, {
        loading: false,
        error: e?.message || "No se pudo cargar el detalle",
      });
    }
  }

  function toggleDetalle(id) {
    setEquipOpenId(null);
    setOpenId((prev) => (prev === id ? null : id));
    if (!detalles[id]?.data && !detalles[id]?.loading) {
      loadDetalle(id);
    }
  }

  async function saveDetalle(id) {
    if (!canEditDetalle && !canEditCost && !canManage) return;
    const st = detalles[id];
    if (!st?.data || !st?.draft) return;
    const payload = {};
    DETAIL_FIELDS.forEach((field) => {
      const prevVal = st.data?.[field] ?? "";
      const nextRaw = st.draft?.[field] ?? "";
      const nextVal = field === "nombre" ? String(nextRaw).trim() : nextRaw;
      if (String(prevVal ?? "") !== String(nextVal ?? "")) {
        payload[field] = nextVal === "" ? null : nextVal;
      }
    });
    if (canEditCost) {
      const prevCost = st.data?.costo_valor ?? "";
      const nextCost = st.draft?.costo_valor ?? "";
      const prevCurrency = st.data?.costo_moneda || "USD";
      const nextCurrency = st.draft?.costo_moneda || "USD";
      if (
        String(prevCost ?? "") !== String(nextCost ?? "") ||
        String(prevCurrency) !== String(nextCurrency)
      ) {
        payload.costo_valor = nextCost === "" ? null : nextCost;
        payload.costo_moneda = nextCurrency;
      }
    }
    if (canManage) {
      const prevMult = st.data?.multiplicador ?? "";
      const nextMult = st.draft?.multiplicador ?? "";
      if (String(prevMult ?? "") !== String(nextMult ?? "")) {
        payload.multiplicador = nextMult === "" ? null : nextMult;
      }
    }
    if (!Object.keys(payload).length) return;
    updateDetalleState(id, { savingDetalle: true });
    try {
      const updated = await patchRepuesto(id, payload);
      applyItemUpdate(updated);
      updateDetalleState(id, {
        savingDetalle: false,
        data: updated,
        draft: buildDetalleDraft(updated),
        proveedoresDraft: buildProveedoresDraft(updated),
      });
      setMsg("Repuesto actualizado");
    } catch (e) {
      updateDetalleState(id, { savingDetalle: false });
      setErr(e?.message || "No se pudo actualizar");
    }
  }

  async function saveProveedores(id) {
    if (!canManage) return;
    const st = detalles[id];
    const proveedoresDraft = st?.proveedoresDraft || [];
    const parseNum = (v) => (v === "" || v == null ? null : Number(v));
    const payload = {
      proveedores: proveedoresDraft
        .map((p) => ({
          proveedor_id: p.proveedor_id ? Number(p.proveedor_id) : undefined,
          proveedor_nombre: (p.proveedor_nombre || "").trim() || undefined,
          sku_proveedor: (p.sku_proveedor || "").trim() || undefined,
          lead_time_dias: parseNum(p.lead_time_dias),
          prioridad: parseNum(p.prioridad),
          ultima_compra: p.ultima_compra || undefined,
        }))
        .filter((p) => p.proveedor_id || p.proveedor_nombre),
    };
    updateDetalleState(id, { savingProveedores: true });
    try {
      const updated = await patchRepuesto(id, payload);
      applyItemUpdate(updated);
      updateDetalleState(id, {
        savingProveedores: false,
        data: updated,
        draft: buildDetalleDraft(updated),
        proveedoresDraft: buildProveedoresDraft(updated),
      });
      setMsg("Proveedores actualizados");
    } catch (e) {
      updateDetalleState(id, { savingProveedores: false });
      setErr(e?.message || "No se pudieron actualizar proveedores");
    }
  }

  async function loadMovimientosDetalle(id) {
    updateDetalleState(id, { movsLoading: true, movsError: "" });
    try {
      const rows = await getRepuestosMovimientos({ repuesto_id: id, limit: 50 });
      updateDetalleState(id, { movs: rows || [], movsLoading: false });
    } catch (e) {
      updateDetalleState(id, {
        movs: [],
        movsLoading: false,
        movsError: e?.message || "No se pudieron cargar movimientos",
      });
    }
  }

  async function enablePermiso() {
    if (!canManage24hPermissions) return;
    if (!permForm.tecnico_id) {
      setPermisosErr("Seleccione un técnico");
      return;
    }
    try {
      setPermSaving(true);
      setPermisosErr("");
      await postRepuestosStockPermiso({ tecnico_id: permForm.tecnico_id });
      setPermForm({ tecnico_id: "" });
      await loadPermisos();
      setMsg("Permiso habilitado por 24h");
    } catch (e) {
      setPermisosErr(e?.message || "No se pudo habilitar permiso");
    } finally {
      setPermSaving(false);
    }
  }

  async function revokePermiso(id) {
    if (!canManage24hPermissions) return;
    try {
      setPermSaving(true);
      await patchRepuestosStockPermiso(id, { revoked: true });
      await loadPermisos();
      setMsg("Permiso revocado");
    } catch (e) {
      setPermisosErr(e?.message || "No se pudo revocar permiso");
    } finally {
      setPermSaving(false);
    }
  }

  useEffect(() => {
    loadConfig();
  }, []);

  useEffect(() => {
    loadRepuestos();
  }, [orderBy, orderDir]);

  useEffect(() => {
    loadPermisos();
    if (canManage24hPermissions) {
      loadTecnicos();
    }
    if (canEditStock) {
      loadProveedores();
    }
  }, [canManage24hPermissions, canEditStock, user?.id]);

  useEffect(() => {
    if (canAdd || canManageSubrubros) {
      loadSubrubros();
    }
  }, [canAdd, canManageSubrubros]);

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (event) => {
      if (!menuRef.current) return;
      if (!menuRef.current.contains(event.target)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [menuOpen]);

  useEffect(() => {
    if (!equipOpenId) return;
    loadEquipOptionsAll();
  }, [equipOpenId]);

  function resetAddForm() {
    setAddForm({ ...EMPTY_ADD_FORM });
    setAddEquipoInput("");
    setAddEquipoErr("");
    setEquipOpenId(null);
  }

  function resetCompraForm() {
    setCompraForm(buildEmptyCompraForm());
    setCompraRepuestoOpen(false);
  }

  function resetSubrubroForm() {
    setSubrubroForm({ ...EMPTY_SUBRUBRO_FORM });
    setSubrubroEditCodigo("");
  }

  function repuestoOptionLabel(it) {
    const code = String(it?.codigo || "").trim();
    const name = String(it?.nombre || "").trim();
    if (code && name) return `${code} - ${name}`;
    return code || name || "";
  }

  function findRepuestoCompraMatch(value) {
    const raw = String(value || "").trim();
    if (!raw) return null;
    const needle = norm(raw);
    let byCode = null;
    let byLabel = null;
    let byName = null;
    for (const it of items || []) {
      if (!byCode && norm(it?.codigo || "") === needle) {
        byCode = it;
      }
      if (!byLabel && norm(repuestoOptionLabel(it)) === needle) {
        byLabel = it;
      }
      if (!byName && norm(it?.nombre || "") === needle) {
        byName = it;
      }
      if (byCode || byLabel || byName) {
        return byCode || byLabel || byName;
      }
    }
    return null;
  }

  function updateCompraRepuestoInput(value) {
    const match = findRepuestoCompraMatch(value);
    setCompraForm((prev) => ({
      ...prev,
      repuesto_input: value,
      repuesto_id: match?.id ? String(match.id) : "",
    }));
  }

  function selectCompraRepuesto(item) {
    if (!item?.id) return;
    setCompraForm((prev) => ({
      ...prev,
      repuesto_input: repuestoOptionLabel(item),
      repuesto_id: String(item.id),
    }));
    setCompraRepuestoOpen(false);
  }

  function normalizeSubrubroCodeInput(value) {
    return String(value ?? "").replace(/\D/g, "").slice(0, 4);
  }

  function normalizeIntInput(value) {
    const raw = String(value ?? "");
    if (!raw) return "";
    const cleaned = raw.replace(/[^\d-]/g, "");
    if (!cleaned) return "";
    let sign = "";
    let rest = cleaned;
    if (cleaned.startsWith("-")) {
      sign = "-";
      rest = cleaned.slice(1);
    }
    rest = rest.replace(/-/g, "");
    return `${sign}${rest}`;
  }

  function downloadCSV(filename, rows) {
    const csv = rows
      .map((r) =>
        r
          .map((v) => {
            const s = String(v ?? "");
            if (s.includes('"') || s.includes(",") || s.includes("\n")) {
              return `"${s.replace(/"/g, '""')}"`;
            }
            return s;
          })
          .join(",")
      )
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function downloadXLSX(filename, rows, sheetName) {
    const workbook = XLSX.utils.book_new();
    const worksheet = XLSX.utils.aoa_to_sheet(rows);
    XLSX.utils.book_append_sheet(workbook, worksheet, sheetName || "Hoja1");
    const data = XLSX.write(workbook, { bookType: "xlsx", type: "array" });
    const blob = new Blob([data], {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function setCodigoOrder() {
    if (orderBy === "codigo" && orderDir === "desc") return;
    setOrderBy("codigo");
    setOrderDir("desc");
  }

  function toggleStockOrder() {
    if (orderBy !== "stock") {
      setOrderBy("stock");
      setOrderDir("desc");
      return;
    }
    setOrderDir((prev) => (prev === "desc" ? "asc" : "desc"));
  }

  function exportCambiosCSV() {
    const rows = [
      ["fecha", "accion", "codigo", "nombre_prev", "nombre_new", "usuario"],
      ...(cambios || []).map((c) => [
        formatTs(c.created_at),
        c.accion,
        c.codigo,
        c.nombre_prev,
        c.nombre_new,
        c.created_by_nombre || "",
      ]),
    ];
    downloadCSV("repuestos_cambios.csv", rows);
  }

  function exportRepuestosXLSX() {
    const numOrBlank = (value) => {
      if (value == null || value === "") return "";
      const num = Number(value);
      return Number.isNaN(num) ? value : num;
    };
    const rows = [
      [
        "codigo",
        "nombre",
        "multiplicador",
        "stock",
        "stock_min",
        ...(canSeeCosts ? ["costo_valor", "costo_moneda"] : []),
      ],
      ...(filtered || []).map((it) => [
        it.codigo || "",
        it.nombre || "",
        numOrBlank(it.multiplicador_aplicado ?? it.multiplicador ?? ""),
        numOrBlank(it.stock_on_hand ?? ""),
        numOrBlank(it.stock_min ?? ""),
        ...(canSeeCosts ? [numOrBlank(it.costo_valor ?? ""), it.costo_moneda || ""] : []),
      ]),
    ];
    downloadXLSX("repuestos_filtrados.xlsx", rows, "Repuestos");
  }

  function addEquipoLine(repuestoId, value) {
    const nextValue = (value || "").trim();
    if (!nextValue) return;
    setDetalles((prev) => {
      const st = prev[repuestoId] || {};
      const draft = { ...(st.draft || {}) };
      const lines = parseEquipoLines(draft.estado);
      const normalizedNext = normalizeEquipoToken(nextValue);
      if (!lines.some((line) => normalizeEquipoToken(line) === normalizedNext)) {
        lines.push(nextValue);
      }
      draft.estado = lines.join("\n");
      return {
        ...prev,
        [repuestoId]: { ...st, draft, equipoInput: "", equipoErr: "" },
      };
    });
  }

  function removeEquipoLine(repuestoId, value) {
    const normalized = normalizeEquipoToken(value);
    setDetalles((prev) => {
      const st = prev[repuestoId] || {};
      const draft = { ...(st.draft || {}) };
      const lines = parseEquipoLines(draft.estado).filter(
        (line) => normalizeEquipoToken(line) !== normalized
      );
      draft.estado = lines.join("\n");
      return { ...prev, [repuestoId]: { ...st, draft } };
    });
  }

  function addEquipoLineToAdd(value) {
    const nextValue = (value || "").trim();
    if (!nextValue) return;
    setAddForm((prev) => {
      const lines = parseEquipoLines(prev.estado);
      const normalizedNext = normalizeEquipoToken(nextValue);
      if (!lines.some((line) => normalizeEquipoToken(line) === normalizedNext)) {
        lines.push(nextValue);
      }
      return { ...prev, estado: lines.join("\n") };
    });
    setAddEquipoInput("");
    setAddEquipoErr("");
  }

  function removeAddEquipoLine(value) {
    const normalized = normalizeEquipoToken(value);
    setAddForm((prev) => {
      const lines = parseEquipoLines(prev.estado).filter(
        (line) => normalizeEquipoToken(line) !== normalized
      );
      return { ...prev, estado: lines.join("\n") };
    });
  }

  function updateDraft(id, key, value) {
    setDrafts((prev) => ({
      ...prev,
      [id]: {
        ...(prev[id] || {}),
        [key]: value,
      },
    }));
  }

  function stopRowClick(event) {
    event.stopPropagation();
  }

  function updateDetalleDraft(id, key, value) {
    setDetalles((prev) => {
      const st = prev[id] || {};
      const draft = { ...(st.draft || {}) };
      draft[key] = value;
      return { ...prev, [id]: { ...st, draft } };
    });
  }

  function updateProveedorDraft(id, index, key, value) {
    setDetalles((prev) => {
      const st = prev[id] || {};
      const list = [...(st.proveedoresDraft || [])];
      const cur = list[index] || {};
      if (key === "proveedor_nombre") {
        list[index] = { ...cur, proveedor_id: "", proveedor_nombre: value };
      } else {
        list[index] = { ...cur, [key]: value };
      }
      return { ...prev, [id]: { ...st, proveedoresDraft: list } };
    });
  }

  function addProveedorDraft(id) {
    setDetalles((prev) => {
      const st = prev[id] || {};
      const list = [...(st.proveedoresDraft || [])];
      list.push({
        proveedor_id: "",
        proveedor_nombre: "",
        sku_proveedor: "",
        lead_time_dias: "",
        prioridad: "",
        ultima_compra: "",
      });
      return { ...prev, [id]: { ...st, proveedoresDraft: list } };
    });
  }

  function removeProveedorDraft(id, index) {
    setDetalles((prev) => {
      const st = prev[id] || {};
      const list = [...(st.proveedoresDraft || [])];
      list.splice(index, 1);
      return { ...prev, [id]: { ...st, proveedoresDraft: list } };
    });
  }

  function hasChanges(it) {
    const d = drafts[it.id];
    if (!d) return false;
    const cmp = (a, b) => String(a ?? "") !== String(b ?? "");
    if (canEditStock && "stock_on_hand" in d && cmp(d.stock_on_hand, it.stock_on_hand)) return true;
    if (canManage && "stock_min" in d && cmp(d.stock_min, it.stock_min)) return true;
    if (canManage && "multiplicador" in d && cmp(d.multiplicador, it.multiplicador)) return true;
    return false;
  }

  async function saveConfig() {
    if (!canManage) return;
    try {
      setCfgLoading(true);
      setErr("");
      setMsg("");
      await patchRepuestosConfig({
        dolar_ars: cfgForm.dolar_ars,
        multiplicador_general: cfgForm.multiplicador_general,
      });
      await loadConfig();
      await loadRepuestos();
      setMsg("Configuracion actualizada");
    } catch (e) {
      setErr(e?.message || "No se pudo actualizar configuracion");
    } finally {
      setCfgLoading(false);
    }
  }

  async function saveRow(it) {
    if (!canEditStock && !canManage) return;
    const d = drafts[it.id] || {};
    const payload = {};
    if (canEditStock && "stock_on_hand" in d) payload.stock_on_hand = d.stock_on_hand;
    if (canManage && "stock_min" in d) payload.stock_min = d.stock_min;
    if (canManage && "multiplicador" in d) {
      payload.multiplicador = d.multiplicador === "" ? null : d.multiplicador;
    }
    if (!Object.keys(payload).length) return;

    try {
      setErr("");
      setMsg("");
      const updated = await patchRepuesto(it.id, payload);
      applyItemUpdate(updated);
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[it.id];
        return next;
      });
      if (detalles[it.id]?.data) {
        updateDetalleState(it.id, {
          data: updated,
          draft: buildDetalleDraft(updated),
          proveedoresDraft: buildProveedoresDraft(updated),
        });
      }
      setMsg("Repuesto actualizado");
    } catch (e) {
      setErr(e?.message || "No se pudo actualizar");
    }
  }

  async function saveAdd() {
    if (!canAdd) return;
    const subrubroCodigo = (addForm.subrubro_codigo || "").trim();
    const nombre = (addForm.nombre || "").trim();
    if (!subrubroCodigo || !nombre) {
      setErr("Subrubro y nombre requeridos");
      return;
    }
    const payload = { subrubro_codigo: subrubroCodigo, nombre };
    if (addForm.stock_on_hand !== "") payload.stock_on_hand = addForm.stock_on_hand;
    if (canManage) {
      if (addForm.stock_min !== "") payload.stock_min = addForm.stock_min;
      if (addForm.multiplicador !== "") payload.multiplicador = addForm.multiplicador;
      if (addForm.tipo_articulo.trim()) payload.tipo_articulo = addForm.tipo_articulo.trim();
      if (addForm.categoria.trim()) payload.categoria = addForm.categoria.trim();
      if (addForm.nro_parte.trim()) payload.nro_parte = addForm.nro_parte.trim();
      if (addForm.ubicacion_deposito.trim()) {
        payload.ubicacion_deposito = addForm.ubicacion_deposito.trim();
      }
      if (addForm.fecha_vencimiento) payload.fecha_vencimiento = addForm.fecha_vencimiento;
      const equipos = parseEquipoLines(addForm.estado);
      if (equipos.length) payload.estado = equipos.join("\n");
    }
    if (canEditCost && addForm.costo_valor !== "") {
      payload.costo_valor = addForm.costo_valor;
      payload.costo_moneda = addForm.costo_moneda || "USD";
    }

    try {
      setAddSaving(true);
      setErr("");
      setMsg("");
      const created = await postRepuesto(payload);
      resetAddForm();
      setAddOpen(false);
      setMenuOpen(false);
      await loadRepuestos();
      if (created?.id) {
        setOpenId(created.id);
        updateDetalleState(created.id, {
          data: created,
          draft: buildDetalleDraft(created),
          proveedoresDraft: buildProveedoresDraft(created),
          tab: "detalle",
          equipoInput: "",
          equipoErr: "",
          movs: [],
          movsLoading: false,
          movsError: "",
        });
      }
      setMsg("Repuesto creado");
    } catch (e) {
      setErr(e?.message || "No se pudo crear repuesto");
    } finally {
      setAddSaving(false);
    }
  }

  function startEditSubrubro(subrubro) {
    setSubrubroEditCodigo(subrubro.codigo || "");
    setSubrubroForm({
      codigo: subrubro.codigo || "",
      nombre: subrubro.nombre || "",
    });
    setSubrubrosErr("");
  }

  async function saveSubrubro() {
    if (!canManageSubrubros) return;
    const code = normalizeSubrubroCodeInput(subrubroForm.codigo);
    const nombre = (subrubroForm.nombre || "").trim();
    if (!nombre) {
      setSubrubrosErr("Nombre de subrubro requerido");
      return;
    }
    if (!subrubroEditCodigo && code.length !== 4) {
      setSubrubrosErr("El codigo del subrubro debe tener 4 digitos");
      return;
    }
    try {
      setSubrubroSaving(true);
      setSubrubrosErr("");
      if (subrubroEditCodigo) {
        await patchRepuestosSubrubro(subrubroEditCodigo, { nombre });
        setMsg("Subrubro actualizado");
      } else {
        await postRepuestosSubrubro({ codigo: code, nombre });
        setMsg("Subrubro creado");
      }
      await loadSubrubros();
      resetSubrubroForm();
    } catch (e) {
      setSubrubrosErr(e?.message || "No se pudo guardar subrubro");
    } finally {
      setSubrubroSaving(false);
    }
  }

  async function handleDeleteSubrubro(codigo) {
    if (!canManageSubrubros) return;
    const row = (subrubros || []).find((s) => s.codigo === codigo);
    const ok = window.confirm(
      `¿Eliminar subrubro ${codigo}${row?.nombre ? ` - ${row.nombre}` : ""}?`
    );
    if (!ok) return;
    try {
      setSubrubroDeletingCodigo(codigo);
      setSubrubrosErr("");
      await deleteRepuestosSubrubro(codigo);
      await loadSubrubros();
      if (addForm.subrubro_codigo === codigo) {
        setAddForm((prev) => ({ ...prev, subrubro_codigo: "" }));
      }
      if (subrubroEditCodigo === codigo) {
        resetSubrubroForm();
      }
      setMsg("Subrubro eliminado");
    } catch (e) {
      setSubrubrosErr(e?.message || "No se pudo eliminar subrubro");
    } finally {
      setSubrubroDeletingCodigo("");
    }
  }

  async function saveCompra() {
    if (!canEditStock) return;
    const repuestoInput = (compraForm.repuesto_input || "").trim();
    const repuestoMatch = findRepuestoCompraMatch(repuestoInput);
    const repuestoId = repuestoMatch?.id
      ? Number(repuestoMatch.id)
      : Number(compraForm.repuesto_id || 0);
    if (!repuestoId) {
      setErr("Selecciona un repuesto valido");
      return;
    }

    const qtyRaw = String(compraForm.cantidad ?? "").trim();
    if (!/^\d+$/.test(qtyRaw)) {
      setErr("La cantidad debe ser un entero mayor a 0");
      return;
    }
    const cantidad = Number(qtyRaw);
    if (!Number.isInteger(cantidad) || cantidad <= 0) {
      setErr("La cantidad debe ser un entero mayor a 0");
      return;
    }

    const fechaCompra = (compraForm.fecha_compra || "").trim();
    if (!fechaCompra) {
      setErr("Fecha de compra requerida");
      return;
    }

    const payload = {
      repuesto_id: repuestoId,
      cantidad,
      fecha_compra: fechaCompra,
    };
    const proveedorInput = (compraForm.proveedor_input || "").trim();
    if (proveedorInput) {
      const provMatch = (proveedores || []).find(
        (p) => norm(p?.nombre || "") === norm(proveedorInput)
      );
      if (provMatch?.id) {
        payload.proveedor_id = Number(provMatch.id);
      } else {
        payload.proveedor_nombre = proveedorInput;
      }
    }
    const nota = (compraForm.nota || "").trim();
    if (nota) payload.nota = nota;

    try {
      setCompraSaving(true);
      setErr("");
      setMsg("");
      const res = await postRepuestosMovimientoCompra(payload);
      const repuestoUpdated = res?.repuesto || null;
      const movimiento = res?.movimiento || null;

      if (repuestoUpdated?.id) {
        applyItemUpdate(repuestoUpdated);
        setDetalles((prev) => {
          const st = prev[repuestoUpdated.id];
          if (!st) return prev;
          const next = {
            ...st,
            data: repuestoUpdated,
            draft: buildDetalleDraft(repuestoUpdated),
            proveedoresDraft: buildProveedoresDraft(repuestoUpdated),
          };
          if (movimiento) {
            const current = Array.isArray(st.movs) ? st.movs : [];
            const merged = [movimiento, ...current.filter((m) => m?.id !== movimiento.id)];
            next.movs = merged.slice(0, 50);
          }
          return { ...prev, [repuestoUpdated.id]: next };
        });
      } else {
        await loadRepuestos();
      }

      if (movimiento) {
        setMovimientos((prev) => {
          const current = Array.isArray(prev) ? prev : [];
          const merged = [movimiento, ...current.filter((m) => m?.id !== movimiento.id)];
          return merged.slice(0, 100);
        });
      }

      if (payload.proveedor_nombre) {
        await loadProveedores();
      }

      setCompraOpen(false);
      resetCompraForm();
      setMsg("Compra registrada");
    } catch (e) {
      setErr(e?.message || "No se pudo registrar compra");
    } finally {
      setCompraSaving(false);
    }
  }

  async function loadMovimientos() {
    try {
      setErr("");
      setMovimientosLoading(true);
      setMovimientosOpen(true);
      const rows = await getRepuestosMovimientos({ limit: 100 });
      setMovimientos(rows || []);
    } catch (e) {
      setErr(e?.message || "No se pudo cargar movimientos");
      setMovimientos([]);
    } finally {
      setMovimientosLoading(false);
    }
  }

  async function loadCambios() {
    if (!canViewCambios) return;
    try {
      setCambiosErr("");
      setCambiosLoading(true);
      setCambiosOpen(true);
      const rows = await getRepuestosCambios({ limit: 500 });
      setCambios(rows || []);
    } catch (e) {
      setCambiosErr(e?.message || "No se pudieron cargar cambios");
      setCambios([]);
    } finally {
      setCambiosLoading(false);
    }
  }

  async function handleDeleteRepuesto(id) {
    if (!canDelete) return;
    const ok = window.confirm(
      "¿Eliminar este repuesto? Esta acción lo eliminará del catálogo y liberará el código para reutilizarlo."
    );
    if (!ok) return;
    try {
      setErr("");
      setMsg("");
      await deleteRepuesto(id);
      setOpenId(null);
      await loadRepuestos();
      setMsg("Repuesto eliminado");
    } catch (e) {
      setErr(e?.message || "No se pudo eliminar repuesto");
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between gap-3 mb-2">
        <h1 className="text-2xl font-bold">Repuestos</h1>
        <div className="relative" ref={menuRef}>
          <button
            className="px-3 py-2 border rounded text-sm"
            type="button"
            onClick={() => setMenuOpen((prev) => !prev)}
          >
            Menú
          </button>
          {menuOpen && (
            <div className="absolute right-0 mt-2 w-56 bg-white border rounded shadow z-10">
              <button
                className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                type="button"
                onClick={() => {
                  setMenuOpen(false);
                  loadMovimientos();
                }}
              >
                Movimientos
              </button>
              {canEditStock && (
                <button
                  className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                  type="button"
                  onClick={() => {
                    resetCompraForm();
                    setCompraOpen(true);
                    setAddOpen(false);
                    setSubrubrosOpen(false);
                    setMenuOpen(false);
                  }}
                >
                  Registrar compra
                </button>
              )}
              {canAdd && (
                <button
                  className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                  type="button"
                  onClick={() => {
                    setAddOpen(true);
                    setCompraOpen(false);
                    setSubrubrosOpen(false);
                    setMenuOpen(false);
                  }}
                >
                  Agregar repuesto
                </button>
              )}
              {canManageSubrubros && (
                <button
                  className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                  type="button"
                  onClick={() => {
                    setSubrubrosOpen(true);
                    resetSubrubroForm();
                    setAddOpen(false);
                    setCompraOpen(false);
                    setSubrubrosErr("");
                    setMenuOpen(false);
                    loadSubrubros();
                  }}
                >
                  Gestionar subrubros
                </button>
              )}
              {canViewCambios && (
                <button
                  className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    loadCambios();
                  }}
                >
                  Cambios
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      <p className="text-sm text-gray-600 mb-4">
        Stock con alerta y precios de venta basados en costo normalizado a ARS *
        multiplicador. El stock puede quedar negativo.
      </p>

      {err && <div className="bg-red-100 text-red-700 p-2 rounded mb-2">{err}</div>}
      {msg && <div className="bg-green-100 text-green-700 p-2 rounded mb-2">{msg}</div>}

      <div className="border rounded p-3 mb-4">
        <div className="flex flex-col md:flex-row md:items-end gap-3">
          <div className="flex-1">
            <label className="text-sm block mb-1">Dolar (ARS)</label>
            <Input
              type="number"
              step="1"
              value={cfgForm.dolar_ars}
              onChange={(e) =>
                setCfgForm((s) => ({ ...s, dolar_ars: e.target.value }))
              }
              disabled={!canManage}
            />
          </div>
          <div className="flex-1">
            <label className="text-sm block mb-1">Multiplicador general</label>
            <Input
              type="number"
              step="0.01"
              value={cfgForm.multiplicador_general}
              onChange={(e) =>
                setCfgForm((s) => ({
                  ...s,
                  multiplicador_general: e.target.value,
                }))
              }
              disabled={!canManage}
            />
          </div>
          {canManage && (
            <button
              className="px-4 py-2 rounded bg-blue-600 text-white disabled:opacity-60"
              onClick={saveConfig}
              disabled={cfgLoading}
            >
              {cfgLoading ? "Guardando..." : "Guardar"}
            </button>
          )}
        </div>
        <div className="text-xs text-gray-500 mt-2">
          Última actualización: {formatTs(config?.updated_at)}
          {config?.updated_by_nombre ? ` por ${config.updated_by_nombre}` : ""}
        </div>
        {history?.length ? (
          <div className="mt-2 text-xs text-gray-500">
            Cambios recientes: {history.slice(0, 3).map((h) => (
              <span key={h.id} className="mr-3">
                {formatTs(h.changed_at)} - {h.changed_by_nombre || "-"}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      <div className="border rounded p-3 mb-4">
        <div className="flex items-start justify-between gap-3 mb-2">
          <div className="font-medium">Permisos de conteo</div>
          {isTech && activePerm && (
            <Chip tone="green">Edición habilitada hasta {formatTs(activePerm.expires_at)}</Chip>
          )}
        </div>
        {permisosErr && (
          <div className="bg-red-100 text-red-700 p-2 rounded mb-2">
            {permisosErr}
          </div>
        )}
        {canManage24hPermissions ? (
          <div className="space-y-3">
            <div className="flex flex-col md:flex-row md:items-end gap-3">
              <div className="flex-1">
                <label className="text-sm block mb-1">Técnico</label>
                <select
                  className="border rounded p-2 w-full"
                  value={permForm.tecnico_id}
                  onChange={(e) =>
                    setPermForm((s) => ({ ...s, tecnico_id: e.target.value }))
                  }
                >
                  <option value="">Seleccionar técnico</option>
                  {tecnicos.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.nombre || t.email || t.id}
                    </option>
                  ))}
                </select>
              </div>
              <button
                className="px-4 py-2 rounded bg-blue-600 text-white disabled:opacity-60"
                onClick={enablePermiso}
                disabled={permSaving || !permForm.tecnico_id}
                type="button"
              >
                {permSaving ? "Guardando..." : "Habilitar 24h"}
              </button>
            </div>
            <div className="text-xs text-gray-500">
              {permisosLoading
                ? "Cargando permisos..."
                : `${permisos.length} permiso(s) activos`}
            </div>
            <div className="overflow-auto">
              <table className="min-w-full text-xs">
                <thead>
                  <tr className="text-left">
                    <th className="p-2">Técnico</th>
                    <th className="p-2">Habilitado por</th>
                    <th className="p-2">Desde</th>
                    <th className="p-2">Vence</th>
                    <th className="p-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {permisos.map((p) => (
                    <tr key={p.id} className="border-t">
                      <td className="p-2">{p.tecnico_nombre || "-"}</td>
                      <td className="p-2">{p.enabled_by_nombre || "-"}</td>
                      <td className="p-2">{formatTs(p.created_at)}</td>
                      <td className="p-2">{formatTs(p.expires_at)}</td>
                      <td className="p-2 text-right">
                        <button
                          className="px-2 py-1 border rounded text-xs disabled:opacity-60"
                          type="button"
                          onClick={() => revokePermiso(p.id)}
                          disabled={permSaving}
                        >
                          Revocar
                        </button>
                      </td>
                    </tr>
                  ))}
                  {!permisosLoading && !permisos.length && (
                    <tr>
                      <td className="p-2 text-gray-500" colSpan={5}>
                        Sin permisos activos
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <div className="text-sm text-gray-700">
            {activePerm ? (
              <div className="flex items-center gap-2">
                <Chip tone="green">Edición habilitada</Chip>
                <span>Vence {formatTs(activePerm.expires_at)}</span>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <Chip>Edición de stock no habilitada</Chip>
                <span>Contacta a un jefe para habilitar conteo.</span>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="border rounded p-3">
        <div className="flex flex-col md:flex-row md:items-center gap-3 mb-3">
          <div className="flex-1 space-y-2">
            <Input
              placeholder="Buscar por codigo o nombre"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <Input
              placeholder="Equipo (Marca | Modelo | Variante)"
              value={equipo}
              onChange={(e) => setEquipo(e.target.value)}
            />
          </div>
          <div className="text-xs text-gray-500">
            {loading ? "Cargando..." : `${filtered.length} repuestos`}
          </div>
          <div className="flex items-center gap-2 md:ml-auto">
            <button
              className="px-3 py-2 border rounded text-sm"
              type="button"
              onClick={exportRepuestosXLSX}
              disabled={!filtered.length}
            >
              Exportar Excel
            </button>
          </div>
        </div>

        {canManageSubrubros && subrubrosOpen && (
          <div className="border rounded p-3 bg-gray-50 mb-3">
            <div className="flex items-center justify-between mb-3">
              <div className="font-medium text-sm">Gestionar subrubros</div>
              <button
                className="text-xs text-gray-500 underline"
                type="button"
                onClick={() => {
                  setSubrubrosOpen(false);
                  resetSubrubroForm();
                }}
              >
                Cerrar
              </button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
              <div>
                <label className="text-xs text-gray-500">Código *</label>
                <Input
                  placeholder="4 digitos"
                  value={subrubroForm.codigo}
                  onChange={(e) =>
                    setSubrubroForm((prev) => ({
                      ...prev,
                      codigo: normalizeSubrubroCodeInput(e.target.value),
                    }))
                  }
                  disabled={!!subrubroEditCodigo}
                />
              </div>
              <div className="md:col-span-2">
                <label className="text-xs text-gray-500">Nombre *</label>
                <Input
                  value={subrubroForm.nombre}
                  onChange={(e) =>
                    setSubrubroForm((prev) => ({ ...prev, nombre: e.target.value }))
                  }
                />
              </div>
            </div>

            {subrubrosErr && (
              <div className="bg-red-100 text-red-700 p-2 rounded mb-2">{subrubrosErr}</div>
            )}

            <div className="flex justify-end gap-2 mb-3">
              {subrubroEditCodigo && (
                <button
                  className="px-3 py-2 border rounded text-sm"
                  type="button"
                  onClick={resetSubrubroForm}
                  disabled={subrubroSaving}
                >
                  Cancelar edición
                </button>
              )}
              <button
                className="px-3 py-2 rounded bg-blue-600 text-white text-sm disabled:opacity-60"
                type="button"
                onClick={saveSubrubro}
                disabled={subrubroSaving}
              >
                {subrubroSaving
                  ? "Guardando..."
                  : subrubroEditCodigo
                  ? "Guardar cambios"
                  : "Agregar subrubro"}
              </button>
            </div>

            <div className="overflow-auto">
              <table className="min-w-full text-xs">
                <thead>
                  <tr className="text-left">
                    <th className="p-2">Código</th>
                    <th className="p-2">Nombre</th>
                    <th className="p-2 text-right">Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {subrubros.map((s) => (
                    <tr key={s.codigo} className="border-t bg-white">
                      <td className="p-2">{s.codigo}</td>
                      <td className="p-2">{s.nombre}</td>
                      <td className="p-2">
                        <div className="flex items-center justify-end gap-3">
                          <button
                            className="text-blue-700 underline disabled:opacity-60"
                            type="button"
                            onClick={() => startEditSubrubro(s)}
                            disabled={subrubroSaving || subrubroDeletingCodigo === s.codigo}
                          >
                            Editar
                          </button>
                          <button
                            className="text-red-600 underline disabled:opacity-60"
                            type="button"
                            onClick={() => handleDeleteSubrubro(s.codigo)}
                            disabled={subrubroSaving || subrubroDeletingCodigo === s.codigo}
                          >
                            {subrubroDeletingCodigo === s.codigo ? "Eliminando..." : "Borrar"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!subrubrosLoading && !subrubros.length && (
                    <tr>
                      <td className="p-2 text-gray-500" colSpan={3}>
                        Sin subrubros
                      </td>
                    </tr>
                  )}
                  {subrubrosLoading && (
                    <tr>
                      <td className="p-2 text-gray-500" colSpan={3}>
                        Cargando subrubros...
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {canAdd && addOpen && (
          <div className="border rounded p-3 bg-gray-50 mb-3">
            <div className="flex items-center justify-between mb-3">
              <div className="font-medium text-sm">Agregar repuesto</div>
              <button
                className="text-xs text-gray-500 underline"
                type="button"
                onClick={() => {
                  setAddOpen(false);
                  resetAddForm();
                }}
              >
                Cerrar
              </button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div>
                <label className="text-xs text-gray-500">Subrubro *</label>
                <select
                  className="border rounded p-2 w-full"
                  value={addForm.subrubro_codigo}
                  onChange={(e) =>
                    setAddForm((s) => ({ ...s, subrubro_codigo: e.target.value }))
                  }
                >
                  <option value="">Seleccionar subrubro</option>
                  {subrubros.map((s) => (
                    <option key={s.codigo} value={s.codigo}>
                      {s.codigo} - {s.nombre}
                    </option>
                  ))}
                </select>
                <div className="text-xs text-gray-500 mt-1">
                  El código se asigna automáticamente con el próximo disponible.
                </div>
                {subrubrosLoading && (
                  <div className="text-xs text-gray-500 mt-1">Cargando subrubros...</div>
                )}
                {subrubrosErr && (
                  <div className="text-xs text-red-600 mt-1">{subrubrosErr}</div>
                )}
              </div>
              <div className="md:col-span-2">
                <label className="text-xs text-gray-500">Nombre *</label>
                <Input
                  value={addForm.nombre}
                  onChange={(e) =>
                    setAddForm((s) => ({ ...s, nombre: e.target.value }))
                  }
                />
              </div>
              <div>
                <label className="text-xs text-gray-500">Stock</label>
                <input
                  type="number"
                  step="1"
                  className="border rounded p-2 w-full"
                  value={addForm.stock_on_hand}
                  onChange={(e) =>
                    setAddForm((s) => ({
                      ...s,
                      stock_on_hand: normalizeIntInput(e.target.value),
                    }))
                  }
                />
              </div>
              {canEditCost && (
                <div>
                  <label className="text-xs text-gray-500">Costo (opcional)</label>
                  <div className="flex gap-2">
                    <select
                      className="border rounded p-2"
                      value={addForm.costo_moneda}
                      onChange={(e) =>
                        setAddForm((s) => ({ ...s, costo_moneda: e.target.value }))
                      }
                    >
                      {COST_CURRENCY_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                    <input
                      type="number"
                      step="0.01"
                      className="border rounded p-2 w-full"
                      value={addForm.costo_valor}
                      onChange={(e) =>
                        setAddForm((s) => ({ ...s, costo_valor: e.target.value }))
                      }
                    />
                  </div>
                </div>
              )}
              {canManage && (
                <>
                  <div>
                    <label className="text-xs text-gray-500">Stock min</label>
                    <input
                      type="number"
                      step="1"
                      className="border rounded p-2 w-full"
                      value={addForm.stock_min}
                      onChange={(e) =>
                        setAddForm((s) => ({
                          ...s,
                          stock_min: normalizeIntInput(e.target.value),
                        }))
                      }
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500">Multiplicador</label>
                    <input
                      type="number"
                      step="0.01"
                      className="border rounded p-2 w-full"
                      value={addForm.multiplicador}
                      onChange={(e) =>
                        setAddForm((s) => ({ ...s, multiplicador: e.target.value }))
                      }
                      placeholder={
                        config?.multiplicador_general != null
                          ? String(config.multiplicador_general)
                          : ""
                      }
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500">Tipo de articulo</label>
                    <Input
                      value={addForm.tipo_articulo}
                      onChange={(e) =>
                        setAddForm((s) => ({ ...s, tipo_articulo: e.target.value }))
                      }
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500">Categoria</label>
                    <Input
                      value={addForm.categoria}
                      onChange={(e) =>
                        setAddForm((s) => ({ ...s, categoria: e.target.value }))
                      }
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500">Nro parte</label>
                    <Input
                      value={addForm.nro_parte}
                      onChange={(e) =>
                        setAddForm((s) => ({ ...s, nro_parte: e.target.value }))
                      }
                    />
                  </div>
                  <div className="md:col-span-3">
                    <label className="text-xs text-gray-500">
                      Equipo (Marca | Modelo | Variante)
                    </label>
                    <div className="relative">
                      <div className="flex flex-wrap gap-2 items-center">
                        <input
                          className="border rounded p-2 flex-1 min-w-[220px]"
                          placeholder="Escribir para buscar..."
                          value={addEquipoInput}
                          onFocus={() => {
                            setEquipOpenId("add");
                          }}
                          onBlur={() => {
                            setTimeout(() => {
                              setEquipOpenId((prev) => (prev === "add" ? null : prev));
                            }, 150);
                          }}
                          onChange={(e) => {
                            const value = e.target.value;
                            setAddEquipoInput(value);
                            setAddEquipoErr("");
                          }}
                          onKeyDown={(e) => {
                            if (e.key !== "Enter") return;
                            e.preventDefault();
                            if (!addEquipoInput.trim()) return;
                            if (!addEquipoMatch) {
                              setAddEquipoErr("Selecciona un equipo de la lista.");
                              return;
                            }
                            addEquipoLineToAdd(addEquipoMatch);
                            setEquipOpenId(null);
                          }}
                        />
                        <button
                          className="px-3 py-2 border rounded text-xs disabled:opacity-60"
                          type="button"
                          onClick={() => {
                            if (!addEquipoInput.trim()) return;
                            if (!addEquipoMatch) {
                              setAddEquipoErr("Selecciona un equipo de la lista.");
                              return;
                            }
                            addEquipoLineToAdd(addEquipoMatch);
                            setEquipOpenId(null);
                          }}
                        >
                          Agregar
                        </button>
                      </div>
                      {addEquipDropdownOpen && (
                        <div
                          className="absolute left-0 right-0 mt-1 bg-white border rounded shadow-lg z-20 max-h-56 overflow-auto"
                          onMouseDown={(e) => e.preventDefault()}
                        >
                          {equipOptionsLoadingUi && (
                            <div className="p-2 text-xs text-gray-500">
                              Cargando equipos...
                            </div>
                          )}
                          {!equipOptionsLoadingUi && equipOptionsErr && (
                            <div className="p-2 text-xs text-red-600">
                              {equipOptionsErr}
                            </div>
                          )}
                          {!equipOptionsLoadingUi &&
                            !equipOptionsErr &&
                            !addEquipoSuggestionsTop.length && (
                              <div className="p-2 text-xs text-gray-500">
                                Sin resultados
                              </div>
                            )}
                          {!equipOptionsLoadingUi &&
                            !equipOptionsErr &&
                            addEquipoSuggestionsTop.map((opt) => (
                              <button
                                key={opt}
                                type="button"
                                className="w-full text-left px-3 py-2 text-sm hover:bg-gray-100"
                                onClick={() => {
                                  addEquipoLineToAdd(opt);
                                  setEquipOpenId(null);
                                }}
                              >
                                {opt}
                              </button>
                            ))}
                        </div>
                      )}
                    </div>
                    {addEquipoErr && (
                      <div className="text-xs text-red-600 mt-1">{addEquipoErr}</div>
                    )}
                    {equipOptionsErr && !equipOptionsLoadingUi && (
                      <div className="text-xs text-red-600 mt-1">{equipOptionsErr}</div>
                    )}
                    <div className="flex flex-wrap gap-2 mt-2">
                      {addEquipoLines.map((line) => (
                        <div
                          key={line}
                          className="flex items-center gap-2 border rounded px-2 py-1 text-xs bg-white"
                        >
                          <span>{line}</span>
                          <button
                            type="button"
                            className="text-red-600 underline"
                            onClick={() => removeAddEquipoLine(line)}
                          >
                            Quitar
                          </button>
                        </div>
                      ))}
                      {!addEquipoLines.length && (
                        <span className="text-xs text-gray-500">
                          Sin equipos asignados
                        </span>
                      )}
                    </div>
                  </div>
                  <div>
                    <label className="text-xs text-gray-500">Ubicación depósito</label>
                    <Input
                      value={addForm.ubicacion_deposito}
                      onChange={(e) =>
                        setAddForm((s) => ({
                          ...s,
                          ubicacion_deposito: e.target.value,
                        }))
                      }
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-500">Fecha vencimiento</label>
                    <input
                      type="date"
                      className="border rounded p-2 w-full"
                      value={addForm.fecha_vencimiento}
                      onChange={(e) =>
                        setAddForm((s) => ({
                          ...s,
                          fecha_vencimiento: e.target.value,
                        }))
                      }
                    />
                  </div>
                </>
              )}
            </div>
            <div className="flex justify-end gap-2 mt-3">
              <button
                className="px-3 py-2 border rounded text-sm"
                type="button"
                onClick={() => {
                  setAddOpen(false);
                  resetAddForm();
                }}
              >
                Cancelar
              </button>
              <button
                className="px-3 py-2 rounded bg-blue-600 text-white text-sm disabled:opacity-60"
                type="button"
                onClick={saveAdd}
                disabled={addSaving}
              >
                {addSaving ? "Guardando..." : "Crear repuesto"}
              </button>
            </div>
          </div>
        )}

        <div className="overflow-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="p-2 w-20 whitespace-nowrap">
                  <button
                    type="button"
                    className="hover:underline"
                    onClick={setCodigoOrder}
                  >
                    Código
                  </button>
                </th>
                <th className="p-2">Nombre</th>
                <th className="p-2">Mul.</th>
                <th className="p-2">
                  <button
                    type="button"
                    className="hover:underline"
                    onClick={toggleStockOrder}
                  >
                    Stock
                  </button>
                </th>
                <th className="p-2">Stock min</th>
                <th className="p-2 text-right">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((it) => {
                const draft = drafts[it.id] || {};
                const rowClass = it.stock_negativo
                  ? "bg-red-50"
                  : it.stock_alerta
                  ? "bg-yellow-50"
                  : "";
                const isOpen = openId === it.id;
                const detail = detalles[it.id] || {};
                const detailTab = detail.tab || "detalle";
                const detailDraft = detail.draft || {};
                const proveedoresDraft = detail.proveedoresDraft || [];
                const equipoInput = detail.equipoInput || "";
                const equipDropdownOpen = equipOpenId === it.id;
                const equipoSuggestions = equipDropdownOpen
                  ? filterEquipOptions(equipoInput)
                  : [];
                const equipoSuggestionsTop = equipoSuggestions.slice(0, 200);
                const equipoMatch = equipoInput
                  ? equipOptionsMap.get(normalizeEquipoToken(equipoInput))
                  : null;
                const equipoLines = parseEquipoLines(detailDraft.estado);
                const detailFieldsToCheck = canEditDetalle ? DETAIL_FIELDS : [];
                const detailHasChanges =
                  (canEditDetalle &&
                    detailFieldsToCheck.some((field) => {
                      const prevVal = String(detail.data?.[field] ?? "");
                      const nextRaw = String(detailDraft?.[field] ?? "");
                      const nextVal = field === "nombre" ? nextRaw.trim() : nextRaw;
                      return prevVal !== nextVal;
                    })) ||
                  (canEditCost &&
                    (
                      String(detail.data?.costo_valor ?? "") !== String(detailDraft?.costo_valor ?? "") ||
                      String(detail.data?.costo_moneda || "USD") !== String(detailDraft?.costo_moneda || "USD")
                    )) ||
                  (canManage &&
                    String(detail.data?.multiplicador ?? "") !== String(detailDraft?.multiplicador ?? ""));
                const setTab = (tab) => {
                  updateDetalleState(it.id, { tab });
                  if (
                    tab === "movimientos" &&
                    !detail.movsLoading &&
                    !(detail.movs || []).length
                  ) {
                    loadMovimientosDetalle(it.id);
                  }
                };
                return (
                  <Fragment key={it.id}>
                    <tr
                      className={`border-t ${rowClass} cursor-pointer`}
                      onClick={() => toggleDetalle(it.id)}
                    >
                      <td className="p-2 font-medium whitespace-nowrap">{it.codigo}</td>
                      <td className="p-2">
                        <div className="font-medium">{it.nombre}</div>
                        <div className="flex flex-wrap gap-1 mt-1">
                          {it.stock_negativo && (
                            <Chip tone="red">Stock negativo</Chip>
                          )}
                          {!it.stock_negativo && it.stock_alerta && (
                            <Chip tone="yellow">Stock bajo</Chip>
                          )}
                        </div>
                      </td>
                      <td className="p-2">
                        {canManage ? (
                          <input
                            type="number"
                            step="0.01"
                            className="border rounded p-1 w-28 text-right"
                            value={
                              draft.multiplicador ??
                              (it.multiplicador ?? "")
                            }
                            onChange={(e) =>
                              updateDraft(it.id, "multiplicador", e.target.value)
                            }
                            onClick={stopRowClick}
                            placeholder={
                              it.multiplicador_aplicado != null
                                ? String(it.multiplicador_aplicado)
                                : ""
                            }
                          />
                        ) : (
                          <span>
                            {it.multiplicador_aplicado != null
                              ? it.multiplicador_aplicado
                              : "-"}
                          </span>
                        )}
                      </td>
                      <td className="p-2">
                        {canEditStock ? (
                          <input
                            type="number"
                            step="1"
                            className="border rounded p-1 w-28 text-right"
                            value={
                              draft.stock_on_hand ??
                              (it.stock_on_hand ?? "")
                            }
                            onChange={(e) =>
                              updateDraft(
                                it.id,
                                "stock_on_hand",
                                normalizeIntInput(e.target.value)
                              )
                            }
                            onClick={stopRowClick}
                          />
                        ) : (
                          <span>{it.stock_on_hand ?? "-"}</span>
                        )}
                      </td>
                      <td className="p-2">
                        {canManage ? (
                          <input
                            type="number"
                            step="1"
                            className="border rounded p-1 w-28 text-right"
                            value={draft.stock_min ?? (it.stock_min ?? "")}
                            onChange={(e) =>
                              updateDraft(
                                it.id,
                                "stock_min",
                                normalizeIntInput(e.target.value)
                              )
                            }
                            onClick={stopRowClick}
                          />
                        ) : (
                          <span>{it.stock_min ?? "-"}</span>
                        )}
                      </td>
                      <td className="p-2 text-right">
                        <div className="flex flex-wrap justify-end gap-2">
                          {(canManage || canEditStock) && (
                            <button
                              className="px-2 py-1 border rounded text-xs disabled:opacity-50"
                              onClick={(e) => {
                                stopRowClick(e);
                                saveRow(it);
                              }}
                              disabled={!hasChanges(it)}
                              type="button"
                            >
                              Guardar
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="border-t">
                        <td className="p-3 bg-gray-50" colSpan={colCount}>
                          {detail.loading ? (
                            <div className="text-sm text-gray-500">Cargando detalle...</div>
                          ) : detail.error ? (
                            <div className="bg-red-100 text-red-700 p-2 rounded">{detail.error}</div>
                          ) : detail.data ? (
                            <div>
                              <div className="flex items-start justify-between gap-3 mb-3">
                                <div>
                                  <div className="text-xs text-gray-500">
                                    Código:{" "}
                                    <span className="font-medium text-gray-800">
                                      {detail.data.codigo || it.codigo}
                                    </span>
                                  </div>
                                  <div className="text-lg font-semibold">
                                    {detail.data.nombre || it.nombre}
                                  </div>
                                  <div className="flex flex-wrap gap-1 mt-2">
                                    {detail.data.stock_negativo && (
                                      <Chip tone="red">Stock negativo</Chip>
                                    )}
                                    {!detail.data.stock_negativo &&
                                      detail.data.stock_alerta && (
                                        <Chip tone="yellow">Stock bajo</Chip>
                                      )}
                                    {detail.data.stock_permiso?.activo && (
                                      <Chip tone="green">Conteo habilitado</Chip>
                                    )}
                                  </div>
                                </div>
                                <div className="flex items-center gap-3">
                                  {canDelete && (
                                    <button
                                      className="text-xs text-red-600 underline"
                                      type="button"
                                      onClick={() => handleDeleteRepuesto(it.id)}
                                    >
                                      Eliminar
                                    </button>
                                  )}
                                  <button
                                    className="text-xs text-gray-500 underline"
                                    type="button"
                                    onClick={() => toggleDetalle(it.id)}
                                  >
                                    Cerrar
                                  </button>
                                </div>
                              </div>
                              <Tabs
                                value={detailTab}
                                onChange={setTab}
                                items={[
                                  { value: "detalle", label: "Detalle" },
                                  { value: "proveedores", label: "Proveedores" },
                                  { value: "movimientos", label: "Movimientos" },
                                ]}
                              />
                              {detailTab === "detalle" && (
                                <div className="space-y-3">
                                  {canEditDetalle && (
                                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                                      <div className="md:col-span-2">
                                        <label className="text-xs text-gray-500">Nombre</label>
                                        <Input
                                          value={detailDraft.nombre || ""}
                                          onChange={(e) =>
                                            updateDetalleDraft(it.id, "nombre", e.target.value)
                                          }
                                          disabled={!canEditDetalle}
                                        />
                                      </div>
                                    </div>
                                  )}
                                  {canSeeCosts && (
                                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                                      <div className="border rounded p-2 bg-white">
                                        <div className="text-xs text-gray-500">Costo</div>
                                        {canEditCost ? (
                                          <div className="space-y-2">
                                            <div className="flex gap-2">
                                              <select
                                                className="border rounded p-1"
                                                value={detailDraft.costo_moneda || "USD"}
                                                onChange={(e) =>
                                                  updateDetalleDraft(it.id, "costo_moneda", e.target.value)
                                                }
                                              >
                                                {COST_CURRENCY_OPTIONS.map((opt) => (
                                                  <option key={opt.value} value={opt.value}>
                                                    {opt.label}
                                                  </option>
                                                ))}
                                              </select>
                                              <input
                                                type="number"
                                                step="0.01"
                                                className="border rounded p-1 w-full text-right"
                                                value={detailDraft.costo_valor || ""}
                                                onChange={(e) =>
                                                  updateDetalleDraft(it.id, "costo_valor", e.target.value)
                                                }
                                              />
                                            </div>
                                            <div className="text-xs text-gray-400">
                                              Equiv. ARS: {formatMoney(detail.data?.costo_ars)}
                                            </div>
                                          </div>
                                        ) : (
                                          <div className="space-y-1">
                                            <div className="font-medium">
                                              {formatMoney(detail.data?.costo_valor, detail.data?.costo_moneda || "USD")}
                                            </div>
                                            <div className="text-xs text-gray-400">
                                              Equiv. ARS: {formatMoney(detail.data?.costo_ars)}
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                      <div className="border rounded p-2 bg-white">
                                        <div className="text-xs text-gray-500">Multiplicador</div>
                                        {canManage ? (
                                          <div className="space-y-2">
                                            <input
                                              type="number"
                                              step="0.0001"
                                              className="border rounded p-1 w-full text-right"
                                              value={detailDraft.multiplicador || ""}
                                              placeholder={
                                                config?.multiplicador_general != null
                                                  ? String(config.multiplicador_general)
                                                  : ""
                                              }
                                              onChange={(e) =>
                                                updateDetalleDraft(it.id, "multiplicador", e.target.value)
                                              }
                                            />
                                            <div className="text-xs text-gray-400">
                                              Aplicado: {detail.data?.multiplicador_aplicado ?? "-"}
                                            </div>
                                          </div>
                                        ) : (
                                          <div className="font-medium">
                                            {detail.data?.multiplicador_aplicado ?? "-"}
                                          </div>
                                        )}
                                      </div>
                                      <div className="border rounded p-2 bg-white">
                                        <div className="text-xs text-gray-500">Precio venta</div>
                                        <div className="font-medium">
                                          {formatMoney(detail.data?.precio_venta)}
                                        </div>
                                      </div>
                                    </div>
                                  )}
                                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    <div className="md:col-span-2">
                                      <label className="text-xs text-gray-500">Tipo de articulo</label>
                                      <Input
                                        value={detailDraft.tipo_articulo || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "tipo_articulo", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Categoria</label>
                                      <Input
                                        value={detailDraft.categoria || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "categoria", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">
                                        Equipo (Marca | Modelo | Variante)
                                      </label>
                                      <div className="relative">
                                        <div className="flex flex-wrap gap-2 items-center">
                                          <input
                                            className="border rounded p-2 flex-1 min-w-[220px]"
                                            placeholder="Escribir para buscar..."
                                            value={equipoInput}
                                            onFocus={() => {
                                              setEquipOpenId(it.id);
                                            }}
                                            onBlur={() => {
                                              setTimeout(() => {
                                                setEquipOpenId((prev) => (prev === it.id ? null : prev));
                                              }, 150);
                                            }}
                                            onChange={(e) => {
                                              const value = e.target.value;
                                              updateDetalleState(it.id, {
                                                equipoInput: value,
                                                equipoErr: "",
                                              });
                                            }}
                                            onKeyDown={(e) => {
                                              if (e.key !== "Enter") return;
                                              e.preventDefault();
                                              if (!equipoInput.trim()) return;
                                              if (!equipoMatch) {
                                                updateDetalleState(it.id, {
                                                  equipoErr: "Selecciona un equipo de la lista.",
                                                });
                                                return;
                                              }
                                              addEquipoLine(it.id, equipoMatch);
                                              setEquipOpenId(null);
                                            }}
                                            disabled={!canEditDetalle}
                                          />
                                          <button
                                            className="px-3 py-2 border rounded text-xs disabled:opacity-60"
                                            type="button"
                                            onClick={() => {
                                              if (!equipoInput.trim()) return;
                                              if (!equipoMatch) {
                                                updateDetalleState(it.id, {
                                                  equipoErr: "Selecciona un equipo de la lista.",
                                                });
                                                return;
                                              }
                                              addEquipoLine(it.id, equipoMatch);
                                              setEquipOpenId(null);
                                            }}
                                            disabled={!canEditDetalle}
                                          >
                                            Agregar
                                          </button>
                                        </div>
                                        {equipDropdownOpen && (
                                          <div
                                            className="absolute left-0 right-0 mt-1 bg-white border rounded shadow-lg z-20 max-h-56 overflow-auto"
                                            onMouseDown={(e) => e.preventDefault()}
                                          >
                                            {equipOptionsLoadingUi && (
                                              <div className="p-2 text-xs text-gray-500">
                                                Cargando equipos...
                                              </div>
                                            )}
                                            {!equipOptionsLoadingUi && equipOptionsErr && (
                                              <div className="p-2 text-xs text-red-600">
                                                {equipOptionsErr}
                                              </div>
                                            )}
                                            {!equipOptionsLoadingUi &&
                                              !equipOptionsErr &&
                                              !equipoSuggestionsTop.length && (
                                                <div className="p-2 text-xs text-gray-500">
                                                  Sin resultados
                                                </div>
                                              )}
                                            {!equipOptionsLoadingUi &&
                                              !equipOptionsErr &&
                                              equipoSuggestionsTop.map((opt) => (
                                                <button
                                                  key={opt}
                                                  type="button"
                                                  className="w-full text-left px-3 py-2 text-sm hover:bg-gray-100"
                                                  onClick={() => {
                                                    addEquipoLine(it.id, opt);
                                                    setEquipOpenId(null);
                                                  }}
                                                >
                                                  {opt}
                                                </button>
                                              ))}
                                          </div>
                                        )}
                                      </div>
                                      {detail.equipoErr && (
                                        <div className="text-xs text-red-600 mt-1">
                                          {detail.equipoErr}
                                        </div>
                                      )}
                                      <div className="flex flex-wrap gap-2 mt-2">
                                        {equipoLines.map((line) => (
                                          <div
                                            key={line}
                                            className="flex items-center gap-2 border rounded px-2 py-1 text-xs bg-white"
                                          >
                                            <span>{line}</span>
                                            {canEditDetalle && (
                                              <button
                                                type="button"
                                                className="text-red-600 underline"
                                                onClick={() => removeEquipoLine(it.id, line)}
                                              >
                                                Quitar
                                              </button>
                                            )}
                                          </div>
                                        ))}
                                        {!equipoLines.length && (
                                          <span className="text-xs text-gray-500">
                                            Sin equipos asignados
                                          </span>
                                        )}
                                      </div>
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Unidad de medida</label>
                                      <Input
                                        value={detailDraft.unidad_medida || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "unidad_medida", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Marca / fabricante</label>
                                      <Input
                                        value={detailDraft.marca_fabricante || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "marca_fabricante", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Nro parte</label>
                                      <Input
                                        value={detailDraft.nro_parte || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "nro_parte", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Ubicación depósito</label>
                                      <Input
                                        value={detailDraft.ubicacion_deposito || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "ubicacion_deposito", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Fecha última compra</label>
                                      <input
                                        type="date"
                                        className="border rounded p-2 w-full"
                                        value={detailDraft.fecha_ultima_compra || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "fecha_ultima_compra", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Fecha ultimo conteo</label>
                                      <input
                                        type="date"
                                        className="border rounded p-2 w-full"
                                        value={detailDraft.fecha_ultimo_conteo || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "fecha_ultimo_conteo", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div>
                                      <label className="text-xs text-gray-500">Fecha vencimiento</label>
                                      <input
                                        type="date"
                                        className="border rounded p-2 w-full"
                                        value={detailDraft.fecha_vencimiento || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "fecha_vencimiento", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                    <div className="md:col-span-2">
                                      <label className="text-xs text-gray-500">Notas</label>
                                      <textarea
                                        className="border rounded p-2 w-full min-h-[80px]"
                                        value={detailDraft.notas || ""}
                                        onChange={(e) =>
                                          updateDetalleDraft(it.id, "notas", e.target.value)
                                        }
                                        disabled={!canEditDetalle}
                                      />
                                    </div>
                                  </div>
                                  {(canEditDetalle || canEditCost) && (
                                    <div className="flex justify-end">
                                      <button
                                        className="px-3 py-2 border rounded text-sm disabled:opacity-60"
                                        onClick={() => saveDetalle(it.id)}
                                        disabled={!detailHasChanges || detail.savingDetalle}
                                        type="button"
                                      >
                                        {detail.savingDetalle ? "Guardando..." : "Guardar detalle"}
                                      </button>
                                    </div>
                                  )}
                                </div>
                              )}
                              {detailTab === "proveedores" && (
                                <div className="space-y-3">
                                  {!canManage && (
                                    <div className="flex flex-wrap gap-2">
                                      {(detail.data?.proveedores || []).map((p) => (
                                        <Chip key={p.id} tone="blue">{p.proveedor_nombre}</Chip>
                                      ))}
                                      {!detail.data?.proveedores?.length && (
                                        <span className="text-xs text-gray-500">Sin proveedores</span>
                                      )}
                                    </div>
                                  )}
                                  {canManage && (
                                    <>
                                      <datalist id={`prov-list-${it.id}`}>
                                        {proveedores.map((p) => (
                                          <option key={p.id} value={p.nombre} />
                                        ))}
                                      </datalist>
                                      <div className="space-y-2">
                                        {proveedoresDraft.map((p, idx) => (
                                          <div key={`${p.proveedor_id || "n"}-${idx}`} className="grid grid-cols-1 md:grid-cols-6 gap-2 items-end">
                                            <div className="md:col-span-2">
                                              <label className="text-xs text-gray-500">Proveedor</label>
                                              <input
                                                className="border rounded p-2 w-full"
                                                list={`prov-list-${it.id}`}
                                                value={p.proveedor_nombre || ""}
                                                onChange={(e) =>
                                                  updateProveedorDraft(it.id, idx, "proveedor_nombre", e.target.value)
                                                }
                                              />
                                            </div>
                                            <div>
                                              <label className="text-xs text-gray-500">SKU</label>
                                              <input
                                                className="border rounded p-2 w-full"
                                                value={p.sku_proveedor || ""}
                                                onChange={(e) =>
                                                  updateProveedorDraft(it.id, idx, "sku_proveedor", e.target.value)
                                                }
                                              />
                                            </div>
                                            <div>
                                              <label className="text-xs text-gray-500">Lead time</label>
                                              <input
                                                type="number"
                                                className="border rounded p-2 w-full"
                                                value={p.lead_time_dias || ""}
                                                onChange={(e) =>
                                                  updateProveedorDraft(it.id, idx, "lead_time_dias", e.target.value)
                                                }
                                              />
                                            </div>
                                            <div>
                                              <label className="text-xs text-gray-500">Prioridad</label>
                                              <input
                                                type="number"
                                                className="border rounded p-2 w-full"
                                                value={p.prioridad || ""}
                                                onChange={(e) =>
                                                  updateProveedorDraft(it.id, idx, "prioridad", e.target.value)
                                                }
                                              />
                                            </div>
                                            <div>
                                              <label className="text-xs text-gray-500">Última compra</label>
                                              <input
                                                type="date"
                                                className="border rounded p-2 w-full"
                                                value={p.ultima_compra || ""}
                                                onChange={(e) =>
                                                  updateProveedorDraft(it.id, idx, "ultima_compra", e.target.value)
                                                }
                                              />
                                            </div>
                                            <div className="md:col-span-6 flex justify-end">
                                              <button
                                                className="text-xs text-red-600 underline"
                                                type="button"
                                                onClick={() => removeProveedorDraft(it.id, idx)}
                                              >
                                                Quitar
                                              </button>
                                            </div>
                                          </div>
                                        ))}
                                        {!proveedoresDraft.length && (
                                          <div className="text-xs text-gray-500">Sin proveedores</div>
                                        )}
                                      </div>
                                      <div className="flex flex-wrap justify-between gap-2">
                                        <button
                                          className="px-3 py-2 border rounded text-sm"
                                          type="button"
                                          onClick={() => addProveedorDraft(it.id)}
                                        >
                                          Agregar proveedor
                                        </button>
                                        <button
                                          className="px-3 py-2 border rounded text-sm disabled:opacity-60"
                                          type="button"
                                          onClick={() => saveProveedores(it.id)}
                                          disabled={detail.savingProveedores}
                                        >
                                          {detail.savingProveedores ? "Guardando..." : "Guardar proveedores"}
                                        </button>
                                      </div>
                                    </>
                                  )}
                                </div>
                              )}
                              {detailTab === "movimientos" && (
                                <div>
                                  {detail.movsLoading ? (
                                    <div className="text-sm text-gray-500">Cargando movimientos...</div>
                                  ) : detail.movsError ? (
                                    <div className="bg-red-100 text-red-700 p-2 rounded">{detail.movsError}</div>
                                  ) : (
                                    <div className="overflow-auto">
                                      <table className="min-w-full text-xs">
                                        <thead>
                                          <tr className="text-left">
                                            <th className="p-2">Fecha</th>
                                            <th className="p-2">Fecha compra</th>
                                            <th className="p-2">Usuario</th>
                                            <th className="p-2">Proveedor</th>
                                            <th className="p-2">Tipo</th>
                                            <th className="p-2">Qty</th>
                                            <th className="p-2">Stock prev</th>
                                            <th className="p-2">Stock nuevo</th>
                                            <th className="p-2">Nota</th>
                                          </tr>
                                        </thead>
                                        <tbody>
                                          {(detail.movs || []).map((m) => (
                                            <tr key={m.id} className="border-t">
                                              <td className="p-2">{formatTs(m.created_at)}</td>
                                              <td className="p-2">{formatDate(m.fecha_compra)}</td>
                                              <td className="p-2">{m.created_by_nombre || "-"}</td>
                                              <td className="p-2">{m.proveedor_nombre || "-"}</td>
                                              <td className="p-2">{m.tipo}</td>
                                              <td className="p-2">{m.qty}</td>
                                              <td className="p-2">{m.stock_prev}</td>
                                              <td className="p-2">{m.stock_new}</td>
                                              <td className="p-2">{m.nota || "-"}</td>
                                            </tr>
                                          ))}
                                          {!detail.movs?.length && (
                                            <tr>
                                              <td className="p-3 text-gray-500" colSpan={9}>
                                                Sin movimientos
                                              </td>
                                            </tr>
                                          )}
                                        </tbody>
                                      </table>
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          ) : (
                            <div className="text-sm text-gray-500">Sin detalle</div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {!filtered.length && (
                <tr>
                  <td className="p-3 text-gray-500" colSpan={colCount}>
                    Sin repuestos
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {compraOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => {
            setCompraOpen(false);
            resetCompraForm();
          }}
        >
          <div
            className="bg-white rounded shadow-xl w-full max-w-2xl max-h-[85vh] overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b p-3">
              <div className="font-medium">Registrar compra</div>
              <button
                className="text-xs text-gray-500 underline"
                type="button"
                onClick={() => {
                  setCompraOpen(false);
                  resetCompraForm();
                }}
              >
                Cerrar
              </button>
            </div>
            <div className="p-3 space-y-3 overflow-auto max-h-[75vh]">
              <div>
                <label className="text-xs text-gray-500">Repuesto *</label>
                <div className="relative">
                  <input
                    className="border rounded p-2 w-full text-sm"
                    placeholder="Código o nombre"
                    value={compraForm.repuesto_input}
                    onFocus={() => setCompraRepuestoOpen(true)}
                    onBlur={() => {
                      setTimeout(() => setCompraRepuestoOpen(false), 150);
                    }}
                    onChange={(e) => updateCompraRepuestoInput(e.target.value)}
                  />
                  {compraRepuestoOpen && (
                    <div
                      className="absolute left-0 right-0 mt-1 bg-white border rounded shadow-lg z-20 max-h-56 overflow-auto"
                      onMouseDown={(e) => e.preventDefault()}
                    >
                      {!compraRepuestoSuggestions.length && (
                        <div className="px-3 py-2 text-xs text-gray-500">Sin resultados</div>
                      )}
                      {compraRepuestoSuggestions.map((it) => (
                        <button
                          key={it.id}
                          type="button"
                          className="w-full text-left px-3 py-2 text-xs hover:bg-gray-100"
                          onClick={() => selectCompraRepuesto(it)}
                        >
                          {repuestoOptionLabel(it)}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div className="text-xs text-gray-500 mt-1">
                  {compraSelectedRepuesto
                    ? `Seleccionado: ${repuestoOptionLabel(compraSelectedRepuesto)} | Stock actual: ${
                        compraSelectedRepuesto.stock_on_hand ?? "-"
                      }`
                    : "Selecciona un repuesto del listado"}
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500">Cantidad *</label>
                  <input
                    className="border rounded p-2 w-full"
                    type="number"
                    min="1"
                    step="1"
                    value={compraForm.cantidad}
                    onChange={(e) =>
                      setCompraForm((prev) => ({
                        ...prev,
                        cantidad: normalizeIntInput(e.target.value).replace("-", ""),
                      }))
                    }
                  />
                </div>
                <div>
                  <label className="text-xs text-gray-500">Fecha compra *</label>
                  <input
                    className="border rounded p-2 w-full"
                    type="date"
                    value={compraForm.fecha_compra}
                    onChange={(e) =>
                      setCompraForm((prev) => ({ ...prev, fecha_compra: e.target.value }))
                    }
                  />
                </div>
              </div>

              <div>
                <label className="text-xs text-gray-500">Proveedor (opcional)</label>
                <input
                  className="border rounded p-2 w-full"
                  list="proveedores-compra-list"
                  placeholder="Seleccionar o escribir proveedor nuevo"
                  value={compraForm.proveedor_input}
                  onChange={(e) =>
                    setCompraForm((prev) => ({ ...prev, proveedor_input: e.target.value }))
                  }
                />
                <datalist id="proveedores-compra-list">
                  {(proveedores || []).map((p) => (
                    <option key={p.id} value={p.nombre || ""} />
                  ))}
                </datalist>
              </div>

              <div>
                <label className="text-xs text-gray-500">Nota (opcional)</label>
                <textarea
                  className="border rounded p-2 w-full min-h-[90px]"
                  value={compraForm.nota}
                  onChange={(e) =>
                    setCompraForm((prev) => ({ ...prev, nota: e.target.value }))
                  }
                />
              </div>

              <div className="flex justify-end gap-2">
                <button
                  className="px-3 py-2 border rounded text-sm"
                  type="button"
                  onClick={() => {
                    setCompraOpen(false);
                    resetCompraForm();
                  }}
                  disabled={compraSaving}
                >
                  Cancelar
                </button>
                <button
                  className="px-3 py-2 rounded bg-blue-600 text-white text-sm disabled:opacity-60"
                  type="button"
                  onClick={saveCompra}
                  disabled={compraSaving}
                >
                  {compraSaving ? "Guardando..." : "Registrar compra"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {movimientosOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => {
            setMovimientosOpen(false);
            setMovimientos([]);
          }}
        >
          <div
            className="bg-white rounded shadow-xl w-full max-w-6xl max-h-[85vh] overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b p-3">
              <div className="font-medium">Movimientos de repuestos</div>
              <button
                className="text-xs text-gray-500 underline"
                onClick={() => {
                  setMovimientosOpen(false);
                  setMovimientos([]);
                }}
                type="button"
              >
                Cerrar
              </button>
            </div>
            <div className="overflow-auto max-h-[75vh] p-3">
              <table className="min-w-full text-xs">
                <thead>
                  <tr className="text-left">
                    <th className="p-2">Repuesto</th>
                    <th className="p-2">Fecha</th>
                    <th className="p-2">Fecha compra</th>
                    <th className="p-2">Usuario</th>
                    <th className="p-2">Proveedor</th>
                    <th className="p-2">Tipo</th>
                    <th className="p-2">Qty</th>
                    <th className="p-2">Stock prev</th>
                    <th className="p-2">Stock nuevo</th>
                    <th className="p-2">Nota</th>
                  </tr>
                </thead>
                <tbody>
                  {movimientos.map((m) => (
                    <tr key={m.id} className="border-t">
                      <td className="p-2">
                        <div className="font-medium">{m.codigo || "-"}</div>
                        <div className="text-gray-500">{m.nombre || "-"}</div>
                      </td>
                      <td className="p-2">{formatTs(m.created_at)}</td>
                      <td className="p-2">{formatDate(m.fecha_compra)}</td>
                      <td className="p-2">{m.created_by_nombre || "-"}</td>
                      <td className="p-2">{m.proveedor_nombre || "-"}</td>
                      <td className="p-2">{m.tipo}</td>
                      <td className="p-2">{m.qty}</td>
                      <td className="p-2">{m.stock_prev}</td>
                      <td className="p-2">{m.stock_new}</td>
                      <td className="p-2">{m.nota || "-"}</td>
                    </tr>
                  ))}
                  {movimientosLoading && (
                    <tr>
                      <td className="p-3 text-gray-500" colSpan={10}>
                        Cargando...
                      </td>
                    </tr>
                  )}
                  {!movimientosLoading && !movimientos.length && (
                    <tr>
                      <td className="p-3 text-gray-500" colSpan={10}>
                        Sin movimientos
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {cambiosOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => {
            setCambiosOpen(false);
            setCambios([]);
          }}
        >
          <div
            className="bg-white rounded shadow-xl w-full max-w-4xl max-h-[85vh] overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b p-3">
              <div className="font-medium">Cambios de nombres</div>
              <div className="flex items-center gap-3">
                <button
                  className="text-xs text-gray-500 underline"
                  type="button"
                  onClick={exportCambiosCSV}
                  disabled={!cambios.length}
                >
                  Exportar CSV
                </button>
                <button
                  className="text-xs text-gray-500 underline"
                  onClick={() => {
                    setCambiosOpen(false);
                    setCambios([]);
                  }}
                  type="button"
                >
                  Cerrar
                </button>
              </div>
            </div>
            <div className="p-3 overflow-auto max-h-[75vh]">
              {cambiosErr && (
                <div className="bg-red-100 text-red-700 p-2 rounded mb-2">{cambiosErr}</div>
              )}
              <table className="min-w-full text-xs">
                <thead>
                  <tr className="text-left">
                    <th className="p-2">Fecha</th>
                    <th className="p-2">Usuario</th>
                    <th className="p-2">Acción</th>
                    <th className="p-2">Código</th>
                    <th className="p-2">Nombre anterior</th>
                    <th className="p-2">Nombre nuevo</th>
                  </tr>
                </thead>
                <tbody>
                  {cambios.map((c) => (
                    <tr key={c.id} className="border-t">
                      <td className="p-2">{formatTs(c.created_at)}</td>
                      <td className="p-2">{c.created_by_nombre || "-"}</td>
                      <td className="p-2">{c.accion}</td>
                      <td className="p-2">{c.codigo || "-"}</td>
                      <td className="p-2">{c.nombre_prev || "-"}</td>
                      <td className="p-2">{c.nombre_new || "-"}</td>
                    </tr>
                  ))}
                  {cambiosLoading && (
                    <tr>
                      <td className="p-3 text-gray-500" colSpan={6}>
                        Cargando...
                      </td>
                    </tr>
                  )}
                  {!cambiosLoading && !cambios.length && (
                    <tr>
                      <td className="p-3 text-gray-500" colSpan={6}>
                        Sin cambios
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


