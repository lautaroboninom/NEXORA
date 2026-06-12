import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { getMetricasResumen, getMetricasSeries, getTecnicos, getMarcas, getTiposEquipo, getMetricasCalibracion } from "../lib/api";
import { METRICAS_DESDE_MIN, clampDesdeMin } from "../lib/constants";
import { downloadExcel } from "../lib/excel";
import ConfigPanel from "../components/metricas/ConfigPanel.jsx";
import MetricasNav from "../components/metricas/MetricasNav.jsx";
import SimpleBars from "../components/metricas/charts/SimpleBars.jsx";
import SimpleLine from "../components/metricas/charts/SimpleLine.jsx";
import BoxPlot from "../components/metricas/charts/BoxPlot.jsx";

function formatPct(n) {
  if (n == null || isNaN(n)) return "-";
  return `${(n * 100).toFixed(0)}%`;
}

function formatNumber(value, decimals = 0) {
  if (value == null || isNaN(value)) return "-";
  const v = Number(value);
  if (Number.isNaN(v)) return "-";
  return v.toLocaleString("es-AR", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function formatDays(value, decimals = 1) {
  if (value == null || isNaN(value)) return "-";
  return `${formatNumber(value, decimals)} d`;
}

function formatHours(value, decimals = 1) {
  if (value == null || isNaN(value)) return "-";
  return `${formatNumber(value, decimals)} h`;
}

const STATUS_STYLES = {
  good: { dot: "bg-emerald-500", border: "border-emerald-200", bg: "bg-emerald-50/60", text: "text-emerald-700" },
  warn: { dot: "bg-amber-500", border: "border-amber-200", bg: "bg-amber-50/60", text: "text-amber-700" },
  bad: { dot: "bg-red-500", border: "border-red-200", bg: "bg-red-50/60", text: "text-red-700" },
  neutral: { dot: "bg-gray-300", border: "border-gray-200", bg: "bg-white", text: "text-gray-600" },
};

function StatCard({ label, value, help, status, meta }) {
  const style = STATUS_STYLES[status || "neutral"] || STATUS_STYLES.neutral;
  return (
    <div className={`p-4 border rounded ${style.border} ${style.bg}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-gray-500 flex items-center gap-2">
            <span className={`inline-block w-2 h-2 rounded-full ${style.dot}`}></span>
            <span>{label}</span>
          </div>
          <div className="text-2xl font-semibold mt-1 text-gray-900">{value}</div>
        </div>
        {meta ? <div className={`text-xs ${style.text} text-right`}>{meta}</div> : null}
      </div>
      {help ? <div className="text-xs text-gray-500 mt-2">{help}</div> : null}
    </div>
  );
}

function FilterChip({ label, onClear }) {
  return (
    <button
      type="button"
      onClick={onClear}
      className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-white px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50"
    >
      <span>{label}</span>
      {onClear ? <span aria-hidden="true">x</span> : null}
    </button>
  );
}

function QuickRangeButton({ label, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2.5 py-1 rounded border text-xs ${active ? "bg-gray-100 border-gray-300 font-semibold" : "bg-white border-gray-200 text-gray-600 hover:text-gray-900 hover:bg-gray-50"}`}
    >
      {label}
    </button>
  );
}

export default function Metricas() {
  const { user } = useAuth();
  const [search, setSearch] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);
  const [series, setSeries] = useState(null);
  const [calib, setCalib] = useState(null);
  const [tecnicos, setTecnicos] = useState([]);
  const [marcas, setMarcas] = useState([]);
  const [tipos, setTipos] = useState([]);
  const [tecnicoId, setTecnicoId] = useState(search.get('tecnico_id') || "");
  const [marcaId, setMarcaId] = useState(search.get('marca_id') || "");
  const [tipoEquipo, setTipoEquipo] = useState(search.get('tipo_equipo') || "");
  const [desde, setDesde] = useState(() => {
    const s = search.get('from');
    if (s) return clampDesdeMin(s);
    const d = new Date(); d.setDate(d.getDate() - 30);
    return clampDesdeMin(d.toISOString().slice(0, 10));
  });
  const [hasta, setHasta] = useState(() => search.get('to') || new Date().toISOString().slice(0, 10));
  const [slaExclDer, setSlaExclDer] = useState(() => {
    const v = search.get('sla_excluir_derivados');
    return v ? v === '1' : true; // por defecto: excluir derivados
  });
  const [configOpen, setConfigOpen] = useState(() => search.get('config') === '1' || (location?.pathname || '').endsWith('/metricas/config'));
  const [targets, setTargets] = useState(()=>{ try { return JSON.parse(localStorage.getItem('metricas_targets')||'{}')||{}; } catch { return {}; } });
  const [showCharts, setShowCharts] = useState(() => (search.get('view') || '') === 'charts');
  const [presets, setPresets] = useState(()=>{ try { return JSON.parse(localStorage.getItem('metricas_presets')||'[]')||[]; } catch { return []; } });
  const [presetSel, setPresetSel] = useState('');
  const [presetName, setPresetName] = useState('');

  useEffect(() => {
    // cargar filtros
    getTecnicos().then(setTecnicos).catch(() => {});
    getMarcas().then(setMarcas).catch(() => {});
    getTiposEquipo().then(setTipos).catch(() => {});
  }, []);

  const desdeClamped = useMemo(() => clampDesdeMin(desde), [desde]);
  const tecnicoMap = useMemo(() => new Map(tecnicos.map((t) => [String(t.id), t.nombre])), [tecnicos]);
  const marcaMap = useMemo(() => new Map(marcas.map((m) => [String(m.id), m.nombre])), [marcas]);
  const tipoMap = useMemo(() => new Map(tipos.map((t) => [String(t.nombre), t.nombre])), [tipos]);

  const rangeDays = useMemo(() => {
    if (!desdeClamped || !hasta) return null;
    const from = new Date(desdeClamped);
    const to = new Date(hasta);
    if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return null;
    const ms = to.setHours(0, 0, 0, 0) - from.setHours(0, 0, 0, 0);
    if (ms < 0) return null;
    return Math.round(ms / 86400000) + 1;
  }, [desdeClamped, hasta]);
  const periodLabel = useMemo(() => {
    if (!desdeClamped || !hasta) return "";
    if (!rangeDays) return `${desdeClamped} a ${hasta}`;
    return `${desdeClamped} a ${hasta} (${rangeDays} dias)`;
  }, [desdeClamped, hasta, rangeDays]);

  function applyQuickRange(days) {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - (days - 1));
    setDesde(clampDesdeMin(from.toISOString().slice(0, 10)));
    setHasta(to.toISOString().slice(0, 10));
  }

  function applyYearToDate() {
    const now = new Date();
    const from = new Date(now.getFullYear(), 0, 1);
    setDesde(clampDesdeMin(from.toISOString().slice(0, 10)));
    setHasta(now.toISOString().slice(0, 10));
  }

  const activeFilters = useMemo(() => {
    const items = [];
    if (tecnicoId) items.push({ key: "tecnico", label: `Técnico: ${tecnicoMap.get(String(tecnicoId)) || tecnicoId}` });
    if (marcaId) items.push({ key: "marca", label: `Marca: ${marcaMap.get(String(marcaId)) || marcaId}` });
    if (tipoEquipo) items.push({ key: "tipo", label: `Tipo: ${tipoMap.get(String(tipoEquipo)) || tipoEquipo}` });
    if (!slaExclDer) items.push({ key: "sla", label: "SLA: incluye derivados" });
    return items;
  }, [tecnicoId, marcaId, tipoEquipo, slaExclDer, tecnicoMap, marcaMap, tipoMap]);

  // Sincronizar filtros en la URL
  useEffect(() => {
    const next = new URLSearchParams(search.toString());
    next.set('from', desdeClamped);
    next.set('to', hasta);
    if (tecnicoId) next.set('tecnico_id', tecnicoId); else next.delete('tecnico_id');
    if (marcaId) next.set('marca_id', marcaId); else next.delete('marca_id');
    if (tipoEquipo) next.set('tipo_equipo', tipoEquipo); else next.delete('tipo_equipo');
    if (slaExclDer) next.set('sla_excluir_derivados', '1'); else next.delete('sla_excluir_derivados');
    if (showCharts) next.set('view', 'charts'); else next.delete('view');
    setSearch(next, { replace: true });
  }, [desdeClamped, hasta, tecnicoId, marcaId, tipoEquipo, slaExclDer, showCharts]);

  // Abrir/cerrar config por query o ruta
  useEffect(() => {
    const shouldOpen = search.get('config') === '1' || (location?.pathname || '').endsWith('/metricas/config');
    setConfigOpen(!!shouldOpen);
  }, [search, location?.pathname]);

  function openConfig() {
    const next = new URLSearchParams(search.toString());
    next.set('config', '1');
    setSearch(next, { replace: false });
    setConfigOpen(true);
  }
  function closeConfig() {
    const next = new URLSearchParams(search.toString());
    next.delete('config');
    setSearch(next, { replace: true });
    setConfigOpen(false);
  }
  useEffect(() => {
    // recargar objetivos al abrir/cerrar
    try { setTargets(JSON.parse(localStorage.getItem('metricas_targets')||'{}')||{}); } catch {}
  }, [configOpen]);
  function statusFor(value, target, higherIsBetter = false) {
    if (target == null || isNaN(target) || value == null || isNaN(value)) return null;
    const v = Number(value), t = Number(target);
    if (higherIsBetter) {
      if (v >= t) return 'good';
      if (v >= t*0.8) return 'warn';
      return 'bad';
    } else {
      if (v <= t) return 'good';
      if (v <= t*1.2) return 'warn';
      return 'bad';
    }
  }
  function monthRange(period) {
    const [y,m] = String(period||'').split('-').map(Number);
    if (!y || !m) return { from: '', to: '' };
    const from = new Date(Date.UTC(y, m-1, 1));
    const to = new Date(Date.UTC(y, m, 0));
    const iso = d => new Date(d).toISOString().slice(0,10);
    return { from: iso(from), to: iso(to) };
  }
  function drillToDelivered(period) {
    const {from,to} = monthRange(period);
    if (from && to) navigate(`/ingresos/historico?from=${from}&to=${to}&delivered=1`);
  }

  function reloadPresets(){ try { setPresets(JSON.parse(localStorage.getItem('metricas_presets')||'[]')||[]); } catch {} }
  function savePreset(){
    const name = (presetName || '').trim(); if(!name) return;
    const obj = { name, filters: { from: desdeClamped, to: hasta, tecnicoId, marcaId, tipoEquipo, slaExclDer } };
    const list = presets.filter(p=>p.name!==name).concat([obj]);
    localStorage.setItem('metricas_presets', JSON.stringify(list));
    setPresets(list); setPresetSel(name); setPresetName('');
  }
  function applyPreset(){
    const p = presets.find(p=>p.name===presetSel); if(!p) return;
    const f = p.filters||{};
    setDesde(f.from || desde);
    setHasta(f.to || hasta);
    setTecnicoId(f.tecnicoId || "");
    setMarcaId(f.marcaId || "");
    setTipoEquipo(f.tipoEquipo || "");
    setSlaExclDer(!!f.slaExclDer);
  }
  function deletePreset(){
    if(!presetSel) return; const list = presets.filter(p=>p.name!==presetSel);
    localStorage.setItem('metricas_presets', JSON.stringify(list));
    setPresets(list); setPresetSel('');
  }

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    const params = { from: desdeClamped, to: hasta };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    if (slaExclDer) params.sla_excluir_derivados = 1;
    getMetricasResumen(params)
      .then((res) => {
        if (!alive) return;
        setData(res);
      })
      .catch((err) => {
        if (!alive) return;
        setError(err?.message || String(err));
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [desde, hasta, tecnicoId, marcaId, tipoEquipo]);

  useEffect(() => {
    let alive = true;
    setSeries(null);
    const params = { from: desdeClamped, to: hasta };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    if (slaExclDer) params.sla_excluir_derivados = 1;
    getMetricasSeries(params)
      .then((res) => { if (alive) setSeries(res); })
      .catch(() => { /* silencio */ });
    return () => { alive = false; };
  }, [desde, hasta, tecnicoId, marcaId, tipoEquipo]);

  // Calibración (percentiles) para mostrar en UI
  useEffect(() => {
    let alive = true;
    setCalib(null);
    const params = { from: desdeClamped, to: hasta };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    if (slaExclDer) params.sla_excluir_derivados = 1;
    getMetricasCalibracion(params)
      .then((res)=>{ if (alive) setCalib(res); })
      .catch(()=>{});
    return () => { alive = false; };
  }, [desde, hasta, tecnicoId, marcaId, tipoEquipo, slaExclDer]);

  function exportExcel(filename, rows) {
    downloadExcel(filename, rows, "Metricas");
  }

  function exportTablasExcel() {
    const rows = [];
    rows.push(["Tabla","Tecnico","Valor"]);
    (data?.cerrados_por_tecnico_7d || []).forEach(r => rows.push(["cerrados_7d", r.tecnico_nombre, r.cerrados]));
    (data?.cerrados_por_tecnico_30d || []).forEach(r => rows.push(["cerrados_30d", r.tecnico_nombre, r.cerrados]));
    (data?.wip_por_tecnico || []).forEach(r => rows.push(["wip", r.tecnico_nombre, r.wip]));
    (data?.facturacion_por_tecnico || []).forEach(r => rows.push(["facturacion_aprobada", r.tecnico_nombre, r.facturacion]));
    (data?.utilidad_mo_por_tecnico || []).forEach(r => rows.push(["utilidad_mo", r.tecnico_nombre, r.utilidad_mo]));
    (data?.repuestos_por_tecnico || []).forEach(r => rows.push(["repuestos", r.tecnico_nombre, r.ingreso_repuestos]));
    exportExcel(`metricas_tablas_${desdeClamped}_${hasta}.xls`, rows);
  }

  function exportSeriesExcel(kind) {
    const src = kind === 'yearly' ? (series?.yearly || []) : (series?.monthly || []);
    const header = ["period","entregados","mttr_dias","sla_diag_24h","t_emitir_horas","t_aprobar_horas","ext_derivados","ext_devueltos","ext_t_deriv_a_dev_dias"];
    const rows = [header];
    src.forEach(m => {
      rows.push([
        m.period,
        m.entregados,
        m.mttr_dias != null ? m.mttr_dias.toFixed(2) : "",
        m.sla_diag_24h ? (m.sla_diag_24h.cumplimiento * 100).toFixed(0) + "%" : "",
        m.aprob_presupuestos?.t_emitir_horas != null ? m.aprob_presupuestos.t_emitir_horas.toFixed(2) : "",
        m.aprob_presupuestos?.t_aprobar_horas != null ? m.aprob_presupuestos.t_aprobar_horas.toFixed(2) : "",
        m.externo?.derivados ?? "",
        m.externo?.devueltos ?? "",
        m.externo?.t_deriv_a_devuelto_dias != null ? m.externo.t_deriv_a_devuelto_dias.toFixed(2) : "",
      ]);
    });
    exportExcel(`metricas_series_${kind}_${desdeClamped}_${hasta}.xls`, rows);
  }

  async function exportDetalleTecnicoMensual() {
    const params = { from: desdeClamped, to: hasta, group: 'tecnico' };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    if (slaExclDer) params.sla_excluir_derivados = 1;
    const res = await getMetricasSeries(params);
    const header = ["period","tecnico","entregados","facturacion","mano_obra","repuestos"];
    const key = (p, id) => `${p}:${id}`;
    const map = new Map();
    (res.by_tecnico_monthly || []).forEach(r => {
      const k = key(r.period, r.tecnico_id);
      map.set(k, { period: r.period, tecnico: r.tecnico_nombre, entregados: r.entregados, facturacion: 0, mo: 0, rep: 0 });
    });
    (res.facturacion_tecnico_monthly || []).forEach(r => {
      const k = key(r.period, r.tecnico_id);
      const o = map.get(k) || { period: r.period, tecnico: r.tecnico_nombre, entregados: 0, facturacion: 0, mo: 0, rep: 0 };
      o.facturacion = r.facturacion || 0;
      map.set(k, o);
    });
    (res.mo_tecnico_monthly || []).forEach(r => {
      const k = key(r.period, r.tecnico_id);
      const o = map.get(k) || { period: r.period, tecnico: r.tecnico_nombre, entregados: 0, facturacion: 0, mo: 0, rep: 0 };
      o.mo = r.monto_mo || 0;
      map.set(k, o);
    });
    (res.repuestos_tecnico_monthly || []).forEach(r => {
      const k = key(r.period, r.tecnico_id);
      const o = map.get(k) || { period: r.period, tecnico: r.tecnico_nombre, entregados: 0, facturacion: 0, mo: 0, rep: 0 };
      o.rep = r.monto_repuestos || 0;
      map.set(k, o);
    });
    const rows = [header, ...Array.from(map.values())
      .sort((a,b) => a.period.localeCompare(b.period) || a.tecnico.localeCompare(b.tecnico))
      .map(o => [o.period, o.tecnico, o.entregados, o.facturacion, o.mo, o.rep])];
    exportExcel(`metricas_detalle_tecnico_mensual_${desdeClamped}_${hasta}.xls`, rows);
  }

  async function exportDetalleMarcaMensual() {
    const params = { from: desdeClamped, to: hasta, group: 'marca' };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    getMetricasSeries(params).then((res) => {
      const header = ["period","marca","entregados","facturacion","mano_obra","repuestos"];
      const key = (p, id) => `${p}:${id}`;
      const map = new Map();
      (res.by_marca_monthly || []).forEach(r => {
        const k = key(r.period, r.marca_id);
        map.set(k, { period: r.period, marca: r.marca_nombre, entregados: r.entregados, facturacion: 0, mo: 0, rep: 0 });
      });
      (res.facturacion_marca_monthly || []).forEach(r => {
        const k = key(r.period, r.marca_id);
        const o = map.get(k) || { period: r.period, marca: r.marca_nombre, entregados: 0, facturacion: 0, mo: 0, rep: 0 };
        o.facturacion = r.facturacion || 0;
        map.set(k, o);
      });
      (res.mo_marca_monthly || []).forEach(r => {
        const k = key(r.period, r.marca_id);
        const o = map.get(k) || { period: r.period, marca: r.marca_nombre, entregados: 0, facturacion: 0, mo: 0, rep: 0 };
        o.mo = r.monto_mo || 0;
        map.set(k, o);
      });
      (res.repuestos_marca_monthly || []).forEach(r => {
        const k = key(r.period, r.marca_id);
        const o = map.get(k) || { period: r.period, marca: r.marca_nombre, entregados: 0, facturacion: 0, mo: 0, rep: 0 };
        o.rep = r.monto_repuestos || 0;
        map.set(k, o);
      });
      const rows = [header, ...Array.from(map.values())
        .sort((a,b) => a.period.localeCompare(b.period) || a.marca.localeCompare(b.marca))
        .map(o => [o.period, o.marca, o.entregados, o.facturacion, o.mo, o.rep])];
      exportExcel(`metricas_detalle_marca_mensual_${desdeClamped}_${hasta}.xls`, rows);
    });
  }

  async function exportDetalleTipoMensual() {
    const params = { from: desdeClamped, to: hasta, group: 'tipo' };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    getMetricasSeries(params).then((res) => {
      const header = ["period","tipo","entregados","facturacion","mano_obra","repuestos"];
      const key = (p, name) => `${p}:${name || ''}`;
      const map = new Map();
      (res.by_tipo_monthly || []).forEach(r => {
        const k = key(r.period, r.tipo_equipo);
        map.set(k, { period: r.period, tipo: r.tipo_equipo || '(sin tipo)', entregados: r.entregados, facturacion: 0, mo: 0, rep: 0 });
      });
      (res.facturacion_tipo_monthly || []).forEach(r => {
        const k = key(r.period, r.tipo_equipo);
        const o = map.get(k) || { period: r.period, tipo: r.tipo_equipo || '(sin tipo)', entregados: 0, facturacion: 0, mo: 0, rep: 0 };
        o.facturacion = r.facturacion || 0;
        map.set(k, o);
      });
      (res.mo_tipo_monthly || []).forEach(r => {
        const k = key(r.period, r.tipo_equipo);
        const o = map.get(k) || { period: r.period, tipo: r.tipo_equipo || '(sin tipo)', entregados: 0, facturacion: 0, mo: 0, rep: 0 };
        o.mo = r.monto_mo || 0;
        map.set(k, o);
      });
      (res.repuestos_tipo_monthly || []).forEach(r => {
        const k = key(r.period, r.tipo_equipo);
        const o = map.get(k) || { period: r.period, tipo: r.tipo_equipo || '(sin tipo)', entregados: 0, facturacion: 0, mo: 0, rep: 0 };
        o.rep = r.monto_repuestos || 0;
        map.set(k, o);
      });
      const rows = [header, ...Array.from(map.values())
        .sort((a,b) => a.period.localeCompare(b.period) || a.tipo.localeCompare(b.tipo))
        .map(o => [o.period, o.tipo, o.entregados, o.facturacion, o.mo, o.rep])];
      exportExcel(`metricas_detalle_tipo_mensual_${desdeClamped}_${hasta}.xls`, rows);
    });
  }

  async function exportCalibracionExcel() {
    const params = { from: desdeClamped, to: hasta };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    const res = await getMetricasCalibracion(params);
    const rows = [["metric","p50","p75","p90","p95","avg","count"]];
    const push = (name, obj) => rows.push([
      name,
      obj?.p50 != null ? obj.p50.toFixed(2) : "",
      obj?.p75 != null ? obj.p75.toFixed(2) : "",
      obj?.p90 != null ? obj.p90.toFixed(2) : "",
      obj?.p95 != null ? obj.p95.toFixed(2) : "",
      obj?.avg != null ? obj.avg.toFixed(2) : "",
      obj?.count ?? 0,
    ]);
    push('diag_business_minutes', res.diag_business_minutes);
    push('diag_to_emit_hours', res.diag_to_emit_hours);
    push('emit_to_approve_hours', res.emit_to_approve_hours);
    push('approve_to_repair_days', res.approve_to_repair_days);
    push('repair_to_deliver_days', res.repair_to_deliver_days);
    push('ingreso_to_deliver_days', res.ingreso_to_deliver_days);
    exportExcel(`metricas_calibracion_${desdeClamped}_${hasta}.xls`, rows);
  }

  const monthlySeries = useMemo(() => (series?.monthly || []).slice().sort((a, b) => (a.period || "").localeCompare(b.period || "")), [series]);
  const yearlySeries = useMemo(() => (series?.yearly || []).slice().sort((a, b) => (a.period || "").localeCompare(b.period || "")), [series]);

  const cerrados7 = useMemo(() => (data?.cerrados_por_tecnico_7d || []).slice().sort((a, b) => (b.cerrados || 0) - (a.cerrados || 0)), [data]);
  const cerrados30 = useMemo(() => (data?.cerrados_por_tecnico_30d || []).slice().sort((a, b) => (b.cerrados || 0) - (a.cerrados || 0)), [data]);
  const wip = useMemo(() => (data?.wip_por_tecnico || []).slice().sort((a, b) => (b.wip || 0) - (a.wip || 0)), [data]);
  const facturacionPorTecnico = useMemo(() => (data?.facturacion_por_tecnico || []).slice().sort((a, b) => (b.facturacion || 0) - (a.facturacion || 0)), [data]);
  const utilidadPorTecnico = useMemo(() => (data?.utilidad_mo_por_tecnico || []).slice().sort((a, b) => (b.utilidad_mo || 0) - (a.utilidad_mo || 0)), [data]);
  const repuestosPorTecnico = useMemo(() => (data?.repuestos_por_tecnico || []).slice().sort((a, b) => (b.ingreso_repuestos || 0) - (a.ingreso_repuestos || 0)), [data]);

  const totalEntregados = useMemo(() => monthlySeries.reduce((acc, it) => acc + (Number(it.entregados) || 0), 0), [monthlySeries]);
  const avgEntregados = useMemo(() => (monthlySeries.length ? (totalEntregados / monthlySeries.length) : null), [monthlySeries, totalEntregados]);
  const totalWip = useMemo(() => wip.reduce((acc, it) => acc + (Number(it.wip) || 0), 0), [wip]);
  const wipBuckets = data?.wip_aging_buckets || null;
  const emitidosCount = data?.aprob_presupuestos?.emitidos || 0;
  const aprobadosCount = data?.aprob_presupuestos?.aprobados || 0;
  const wipBucketTotal = useMemo(() => {
    if (!wipBuckets) return 0;
    return Object.values(wipBuckets).reduce((acc, v) => acc + (Number(v) || 0), 0);
  }, [wipBuckets]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-xl font-semibold">Métricas</h1>
          <div className="text-sm text-gray-500">Indicadores operativos para técnicos, con foco en tiempos, SLA y volumen.</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={openConfig} className="px-3 py-1.5 border rounded bg-white hover:bg-gray-50">Configurar</button>
          <div className="inline-flex border rounded overflow-hidden">
            <button
              onClick={() => setShowCharts(false)}
              className={`px-3 py-1.5 ${!showCharts ? "bg-gray-100 font-semibold" : "bg-white hover:bg-gray-50"}`}
            >Tablas</button>
            <button
              onClick={() => setShowCharts(true)}
              className={`px-3 py-1.5 border-l ${showCharts ? "bg-gray-100 font-semibold" : "bg-white hover:bg-gray-50"}`}
            >Gráficos</button>
          </div>
        </div>
      </div>

      <MetricasNav />

      <div className="border rounded bg-white p-4 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-6 gap-3 items-end">
          <div>
            <div className="text-sm text-gray-600">Desde</div>
            <input
              type="date"
              value={desdeClamped}
              min={METRICAS_DESDE_MIN}
              onChange={(e) => setDesde(clampDesdeMin(e.target.value))}
              className="mt-1 border rounded px-2 py-1"
            />
          </div>
          <div>
            <div className="text-sm text-gray-600">Hasta</div>
            <input
              type="date"
              value={hasta}
              onChange={(e) => setHasta(e.target.value)}
              className="mt-1 border rounded px-2 py-1"
            />
          </div>
          <div>
            <div className="text-sm text-gray-600">Técnico</div>
            <select className="mt-1 border rounded px-2 py-1 w-full" value={tecnicoId} onChange={(e) => setTecnicoId(e.target.value)}>
              <option value="">Todos</option>
              {tecnicos.map(t => (
                <option key={t.id} value={t.id}>{t.nombre}</option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-sm text-gray-600">Marca</div>
            <select className="mt-1 border rounded px-2 py-1 w-full" value={marcaId} onChange={(e) => setMarcaId(e.target.value)}>
              <option value="">Todas</option>
              {marcas.map(m => (
                <option key={m.id} value={m.id}>{m.nombre}</option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-sm text-gray-600">Tipo equipo</div>
            <select className="mt-1 border rounded px-2 py-1 w-full" value={tipoEquipo} onChange={(e) => setTipoEquipo(e.target.value)}>
              <option value="">Todos</option>
              {tipos.map((t, idx) => (
                <option key={t.id || idx} value={t.nombre}>{t.nombre}</option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-sm text-gray-600">&nbsp;</div>
            <label className="inline-flex items-center gap-2 mt-1">
              <input type="checkbox" className="h-4 w-4" checked={slaExclDer} onChange={(e)=>setSlaExclDer(e.target.checked)} />
              <span className="text-sm">Excluir derivados del SLA</span>
            </label>
          </div>
        </div>

        <div className="flex flex-wrap gap-2 items-center">
          <div className="text-xs text-gray-500 mr-2">Rangos rápidos</div>
          <QuickRangeButton label="7 días" active={rangeDays === 7} onClick={() => applyQuickRange(7)} />
          <QuickRangeButton label="30 días" active={rangeDays === 30} onClick={() => applyQuickRange(30)} />
          <QuickRangeButton label="90 días" active={rangeDays === 90} onClick={() => applyQuickRange(90)} />
          <QuickRangeButton
            label="YTD"
            active={desdeClamped === `${new Date().getFullYear()}-01-01` && hasta === new Date().toISOString().slice(0, 10)}
            onClick={applyYearToDate}
          />
        </div>

        <div className="flex flex-wrap gap-2 items-center">
          <div className="text-xs text-gray-500 mr-2">Presets</div>
          <select
            className="border rounded px-2 py-1 text-sm"
            value={presetSel}
            onChange={(e) => setPresetSel(e.target.value)}
          >
            <option value="">Seleccionar preset</option>
            {presets.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
          <button onClick={applyPreset} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Aplicar</button>
          <button onClick={deletePreset} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Eliminar</button>
          <input
            className="border rounded px-2 py-1 text-sm"
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            placeholder="Nuevo preset"
          />
          <button onClick={savePreset} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Guardar</button>
        </div>

        <div className="flex flex-wrap gap-2 items-center">
          <div className="text-xs text-gray-500 mr-2">Filtros activos</div>
          {activeFilters.length === 0 ? (
            <span className="text-xs text-gray-400">Sin filtros adicionales</span>
          ) : (
            activeFilters.map((f) => (
              <FilterChip
                key={f.key}
                label={f.label}
                onClear={() => {
                  if (f.key === "tecnico") setTecnicoId("");
                  if (f.key === "marca") setMarcaId("");
                  if (f.key === "tipo") setTipoEquipo("");
                  if (f.key === "sla") setSlaExclDer(true);
                }}
              />
            ))
          )}
          {activeFilters.length > 0 ? (
            <button
              type="button"
              onClick={() => { setTecnicoId(""); setMarcaId(""); setTipoEquipo(""); setSlaExclDer(true); }}
              className="px-2.5 py-1 text-xs border rounded bg-white hover:bg-gray-50"
            >
              Limpiar
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2 items-center">
          <div className="text-xs text-gray-500 mr-2">Exportar</div>
          <button onClick={exportTablasExcel} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Tablas (Excel)</button>
          <button onClick={() => exportSeriesExcel("monthly")} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Series mensuales</button>
          <button onClick={() => exportSeriesExcel("yearly")} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Series anuales</button>
          <button onClick={exportDetalleTecnicoMensual} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Detalle técnico</button>
          <button onClick={exportDetalleMarcaMensual} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Detalle marca</button>
          <button onClick={exportDetalleTipoMensual} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Detalle tipo</button>
          <button onClick={exportCalibracionExcel} className="px-2.5 py-1 border rounded text-xs bg-white hover:bg-gray-50">Calibración</button>
        </div>
      </div>

      {loading && <div className="text-gray-500">Cargando métricas</div>}
      {error && (
        <div className="text-red-600">Error al cargar métricas</div>
      )}

      {data && (
        <>
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">Resumen</h2>
            {periodLabel ? <div className="text-xs text-gray-500">Periodo {periodLabel}</div> : null}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <StatCard
              label="MTTR promedio"
              value={formatDays(data.mttr_dias)}
              status={statusFor(data.mttr_dias != null ? Number(data.mttr_dias) : null, targets?.mttr_days, false)}
              meta={targets?.mttr_days != null ? `Objetivo ${formatDays(targets.mttr_days)}` : null}
              help="Desde inicio de arreglo hasta reparado"
            />
            <StatCard
              label="SLA diagnóstico < 24h"
              value={formatPct(data.sla_diag_24h?.cumplimiento || 0)}
              status={statusFor(((data.sla_diag_24h?.cumplimiento || 0) * 100), targets?.sla_diag_pct, true)}
              meta={targets?.sla_diag_pct != null ? `Objetivo ${formatNumber(targets.sla_diag_pct, 0)}%` : null}
              help={`${data.sla_diag_24h?.dentro || 0} de ${data.sla_diag_24h?.total || 0}`}
            />
            <StatCard
              label="Aprobación presupuestos"
              value={formatPct(data.aprob_presupuestos?.tasa || 0)}
              status={statusFor(((data.aprob_presupuestos?.tasa || 0) * 100), targets?.aprob_pres_pct, true)}
              meta={targets?.aprob_pres_pct != null ? `Objetivo ${formatNumber(targets.aprob_pres_pct, 0)}%` : null}
              help={`${data.aprob_presupuestos?.aprobados || 0} de ${data.aprob_presupuestos?.emitidos || 0}`}
            />
            <StatCard
              label="Entregados (periodo)"
              value={formatNumber(totalEntregados)}
              help={avgEntregados != null ? `Promedio mensual ${formatNumber(avgEntregados, 1)}` : null}
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <StatCard
              label="Tiempo emitir presupuesto"
              value={formatHours(data.aprob_presupuestos?.t_emitir_horas)}
              status={statusFor(data.aprob_presupuestos?.t_emitir_horas, targets?.t_emitir_h, false)}
              meta={targets?.t_emitir_h != null ? `Objetivo ${formatHours(targets.t_emitir_h)}` : null}
              help={emitidosCount ? `Promedio sobre ${formatNumber(emitidosCount)} equipos con emision` : "Sin datos de emision"}
            />
            <StatCard
              label="Tiempo aprobar presupuesto"
              value={formatHours(data.aprob_presupuestos?.t_aprobar_horas)}
              status={statusFor(data.aprob_presupuestos?.t_aprobar_horas, targets?.t_aprobar_h, false)}
              meta={targets?.t_aprobar_h != null ? `Objetivo ${formatHours(targets.t_aprobar_h)}` : null}
              help={aprobadosCount ? `Promedio sobre ${formatNumber(aprobadosCount)} equipos con aprobación` : "Sin datos de aprobación"}
            />
            <StatCard
              label="WIP total"
              value={formatNumber(totalWip)}
              help="Suma por técnico"
            />
            <StatCard
              label="Derivados externos (WIP)"
              value={formatNumber(data?.derivaciones?.wip_externo ?? 0)}
              help="Estado derivado/en_servicio"
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <StatCard
              label="Derivados (periodo)"
              value={formatNumber(data?.derivaciones?.derivados_periodo ?? 0)}
            />
            <StatCard
              label="Devueltos (periodo)"
              value={formatNumber(data?.derivaciones?.devueltos_periodo ?? 0)}
            />
            <StatCard
              label="Derivacion a devuelto"
              value={formatDays(data?.derivaciones?.t_deriv_a_devuelto_dias)}
            />
            <StatCard
              label="Devuelto a entregado"
              value={formatDays(data?.derivaciones?.t_devuelto_a_entregado_dias)}
            />
          </div>

          {wipBuckets && (
            <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
              <StatCard
                label="WIP 0-2 dias"
                value={formatNumber(wipBuckets["0-2"] ?? 0)}
                meta={wipBucketTotal ? `${Math.round(((wipBuckets["0-2"] || 0) / wipBucketTotal) * 100)}%` : null}
              />
              <StatCard
                label="WIP 3-5 dias"
                value={formatNumber(wipBuckets["3-5"] ?? 0)}
                meta={wipBucketTotal ? `${Math.round(((wipBuckets["3-5"] || 0) / wipBucketTotal) * 100)}%` : null}
              />
              <StatCard
                label="WIP 6-10 dias"
                value={formatNumber(wipBuckets["6-10"] ?? 0)}
                meta={wipBucketTotal ? `${Math.round(((wipBuckets["6-10"] || 0) / wipBucketTotal) * 100)}%` : null}
              />
              <StatCard
                label="WIP 11-15 dias"
                value={formatNumber(wipBuckets["11-15"] ?? 0)}
                meta={wipBucketTotal ? `${Math.round(((wipBuckets["11-15"] || 0) / wipBucketTotal) * 100)}%` : null}
              />
              <StatCard
                label="WIP 16+ dias"
                value={formatNumber(wipBuckets["16+"] ?? 0)}
                meta={wipBucketTotal ? `${Math.round(((wipBuckets["16+"] || 0) / wipBucketTotal) * 100)}%` : null}
              />
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div>
              <h2 className="font-semibold mb-2">Cerrados por técnico (7 días)</h2>
              <div className="border rounded bg-white max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="text-left p-2">Técnico</th>
                      <th className="text-right p-2">Cerrados</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cerrados7.length === 0 && (
                      <tr><td colSpan={2} className="p-3 text-gray-500">Sin datos</td></tr>
                    )}
                    {cerrados7.map((r) => (
                      <tr key={`c7-${r.tecnico_id}`} className="border-t">
                        <td className="p-2">{r.tecnico_nombre || '(sin asignar)'}</td>
                        <td className="p-2 text-right">{formatNumber(r.cerrados)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div>
              <h2 className="font-semibold mb-2">WIP por técnico</h2>
              <div className="border rounded bg-white max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="text-left p-2">Técnico</th>
                      <th className="text-right p-2">En curso</th>
                    </tr>
                  </thead>
                  <tbody>
                    {wip.length === 0 && (
                      <tr><td colSpan={2} className="p-3 text-gray-500">Sin datos</td></tr>
                    )}
                    {wip.map((r) => (
                      <tr key={`wip-${r.tecnico_id}`} className="border-t">
                        <td className="p-2">{r.tecnico_nombre || '(sin asignar)'}</td>
                        <td className="p-2 text-right">{formatNumber(r.wip)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div>
            <h2 className="font-semibold mb-2">Cerrados por técnico (30 días)</h2>
            <div className="border rounded bg-white max-h-72 overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 text-gray-600">
                    <th className="text-left p-2">Técnico</th>
                    <th className="text-right p-2">Cerrados</th>
                  </tr>
                </thead>
                <tbody>
                  {cerrados30.length === 0 && (
                    <tr><td colSpan={2} className="p-3 text-gray-500">Sin datos</td></tr>
                  )}
                  {cerrados30.map((r) => (
                    <tr key={`c30-${r.tecnico_id}`} className="border-t">
                      <td className="p-2">{r.tecnico_nombre || '(sin asignar)'}</td>
                      <td className="p-2 text-right">{formatNumber(r.cerrados)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
            <div>
              <h2 className="font-semibold mb-2">Facturación aprobada por técnico</h2>
              <div className="border rounded bg-white max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="text-left p-2">Técnico</th>
                      <th className="text-right p-2">$ Aprobado</th>
                    </tr>
                  </thead>
                  <tbody>
                    {facturacionPorTecnico.length === 0 && (
                      <tr><td colSpan={2} className="p-3 text-gray-500">Sin datos</td></tr>
                    )}
                    {facturacionPorTecnico.map((r) => (
                      <tr key={`fac-${r.tecnico_id}`} className="border-t">
                        <td className="p-2">{r.tecnico_nombre || '(sin asignar)'}</td>
                        <td className="p-2 text-right">{formatNumber(r.facturacion || 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div>
              <h2 className="font-semibold mb-2">Utilidad estimada (mano de obra)</h2>
              <div className="border rounded bg-white max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="text-left p-2">Técnico</th>
                      <th className="text-right p-2">$ MO</th>
                    </tr>
                  </thead>
                  <tbody>
                    {utilidadPorTecnico.length === 0 && (
                      <tr><td colSpan={2} className="p-3 text-gray-500">Sin datos</td></tr>
                    )}
                    {utilidadPorTecnico.map((r) => (
                      <tr key={`umo-${r.tecnico_id}`} className="border-t">
                        <td className="p-2">{r.tecnico_nombre || '(sin asignar)'}</td>
                        <td className="p-2 text-right">{formatNumber(r.utilidad_mo || 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-6">
            <div>
              <h2 className="font-semibold mb-2">Repuestos facturados por técnico</h2>
              <div className="border rounded bg-white max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="text-left p-2">Técnico</th>
                      <th className="text-right p-2">$ Repuestos</th>
                    </tr>

          {calib && (
            <div className="mt-6">
              <h2 className="font-semibold mb-2">Calibración (percentiles)</h2>
              <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                <StatCard
                  label="Diag en horas hábiles (P50)"
                  value={calib.diag_business_minutes?.p50 != null ? (calib.diag_business_minutes.p50/60).toFixed(1) + ' h' : '-'}
                  help={`P75 ${(calib.diag_business_minutes?.p75/60)?.toFixed(1)||'-'} h • P90 ${(calib.diag_business_minutes?.p90/60)?.toFixed(1)||'-'} h`}
                />
                <StatCard
                  label="Ingreso→Emitir presupuesto (P50)"
                  value={calib.diag_to_emit_hours?.p50 != null ? calib.diag_to_emit_hours.p50.toFixed(1) + ' h' : '-'}
                  help={`P75 ${(calib.diag_to_emit_hours?.p75)?.toFixed(1)||'-'} h • P90 ${(calib.diag_to_emit_hours?.p90)?.toFixed(1)||'-'} h`}
                />
                <StatCard
                  label="Emitir→Aprobar (P50)"
                  value={calib.emit_to_approve_hours?.p50 != null ? calib.emit_to_approve_hours.p50.toFixed(1) + ' h' : '-'}
                  help={`P75 ${(calib.emit_to_approve_hours?.p75)?.toFixed(1)||'-'} h • P90 ${(calib.emit_to_approve_hours?.p90)?.toFixed(1)||'-'} h`}
                />
                <StatCard
                  label="Ingreso→Entregado (P50)"
                  value={calib.ingreso_to_deliver_days?.p50 != null ? calib.ingreso_to_deliver_days.p50.toFixed(1) + ' d' : '-'}
                  help={`P75 ${(calib.ingreso_to_deliver_days?.p75)?.toFixed(1)||'-'} d • P90 ${(calib.ingreso_to_deliver_days?.p90)?.toFixed(1)||'-'} d`}
                />
              </div>
            </div>
          )}
                  </thead>
                  <tbody>
                    {repuestosPorTecnico.length === 0 && (
                      <tr><td colSpan={2} className="p-3 text-gray-500">Sin datos</td></tr>
                    )}
                    {repuestosPorTecnico.map((r) => (
                      <tr key={`rep-${r.tecnico_id}`} className="border-t">
                        <td className="p-2">{r.tecnico_nombre || '(sin asignar)'}</td>
                        <td className="p-2 text-right">{formatNumber(r.ingreso_repuestos || 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {series && (
            <>
              {showCharts && (
                <>
                  <div className="space-y-4">
                    <SimpleBars
                      title="Entregados por mes"
                      subtitle="Click en una barra para ver entregados"
                      categories={monthlySeries.map(m=>m.period)}
                      values={monthlySeries.map(m=>m.entregados||0)}
                      fmt={(v)=>formatNumber(v)}
                      onClickBar={(i)=>drillToDelivered(monthlySeries[i]?.period)}
                    />
                    <SimpleLine
                      title="MTTR (días) y TAT (días)"
                      categories={monthlySeries.map(m=>m.period)}
                      series={[
                        { name: 'MTTR (días)', values: monthlySeries.map(m=>m.mttr_dias||0) },
                        { name: 'TAT ingreso→entrega (días)', values: monthlySeries.map(m=>m.tat_dias||0), color: '#ef4444' },
                      ]}
                      fmt={(v)=>formatNumber(v, 1)}
                    />
                    <BoxPlot
                      title="MTTR percentiles (mensual)"
                      subtitle="Click en un mes para ver entregados"
                      categories={monthlySeries.map(m=>m.period)}
                      items={monthlySeries.map(m=>({p25:m.mttr_percentiles?.p25, p50:m.mttr_percentiles?.p50, p75:m.mttr_percentiles?.p75, p90:m.mttr_percentiles?.p90, p95:m.mttr_percentiles?.p95}))}
                      onClickBox={(i)=>drillToDelivered(monthlySeries[i]?.period)}
                    />
                    <SimpleLine
                      title="Tiempos de presupuesto (h)"
                      subtitle="Promedio por equipos con emisión/aprobación"
                      categories={monthlySeries.map(m=>m.period)}
                      series={[
                        { name: 'Emitir (h)', values: monthlySeries.map(m=>m.aprob_presupuestos?.t_emitir_horas||0) },
                        { name: 'Aprobar (h)', values: monthlySeries.map(m=>m.aprob_presupuestos?.t_aprobar_horas||0), color: '#f59e0b' },
                      ]}
                      fmt={(v)=>formatNumber(v, 1)}
                    />
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <MiniSpark title="Entregados" values={monthlySeries.map(m=>m.entregados||0)} fmt={(v)=>formatNumber(v)} />
                    <MiniSpark title="MTTR (días)" values={monthlySeries.map(m=>m.mttr_dias||0)} fmt={(v)=>formatNumber(v, 1)} />
                    <MiniSpark title="SLA diag 24h (%)" values={monthlySeries.map(m=>Math.round((m.sla_diag_24h?.cumplimiento||0)*100))} fmt={(v)=>`${v}%`} />
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mt-4">
                    <MiniSpark title="T emitir presupuesto (h)" values={monthlySeries.map(m=>m.aprob_presupuestos?.t_emitir_horas||0)} fmt={(v)=>formatNumber(v, 1)} />
                    <MiniSpark title="T aprobar presupuesto (h)" values={monthlySeries.map(m=>m.aprob_presupuestos?.t_aprobar_horas||0)} fmt={(v)=>formatNumber(v, 1)} />
                    <MiniSpark title="Derivados externos (mensual)" values={monthlySeries.map(m=>m.externo?.derivados||0)} fmt={(v)=>formatNumber(v)} />
                  </div>
                </>
              )}

              {!showCharts && (
              <div className="mt-8">
                <h2 className="font-semibold mb-2">Tendencias mensuales</h2>
                <div className="overflow-x-auto border rounded bg-white">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-gray-600">
                        <th className="p-2 text-left">Periodo</th>
                        <th className="p-2 text-right">Entregados</th>
                        <th className="p-2 text-right">MTTR (días)</th>
                        <th className="p-2 text-right">SLA diag 24h</th>
                        <th className="p-2 text-right">T. emitir (h)</th>
                        <th className="p-2 text-right">T. aprobar (h)</th>
                        <th className="p-2 text-right">Derivados ext</th>
                        <th className="p-2 text-right">Devueltos ext</th>
                        <th className="p-2 text-right">T derivdev (d)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {monthlySeries.map((m) => (
                        <tr className="border-t" key={m.period}>
                          <td className="p-2">{m.period}</td>
                          <td className="p-2 text-right">{formatNumber(m.entregados)}</td>
                          <td className="p-2 text-right">{m.mttr_dias != null ? m.mttr_dias.toFixed(1) : '-'}</td>
                          <td className="p-2 text-right">{formatPct(m.sla_diag_24h?.cumplimiento || 0)}</td>
                          <td className="p-2 text-right">{m.aprob_presupuestos?.t_emitir_horas != null ? m.aprob_presupuestos.t_emitir_horas.toFixed(1) : '-'}</td>
                          <td className="p-2 text-right">{m.aprob_presupuestos?.t_aprobar_horas != null ? m.aprob_presupuestos.t_aprobar_horas.toFixed(1) : '-'}</td>
                          <td className="p-2 text-right">{formatNumber(m.externo?.derivados ?? 0)}</td>
                          <td className="p-2 text-right">{formatNumber(m.externo?.devueltos ?? 0)}</td>
                          <td className="p-2 text-right">{m.externo?.t_deriv_a_devuelto_dias != null ? m.externo.t_deriv_a_devuelto_dias.toFixed(1) : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              )}

              {!showCharts && (
              <div className="mt-8">
                <h2 className="font-semibold mb-2">Tendencias anuales</h2>
                <div className="overflow-x-auto border rounded bg-white">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-gray-600">
                        <th className="p-2 text-left">Año</th>
                        <th className="p-2 text-right">Entregados</th>
                        <th className="p-2 text-right">MTTR (días)</th>
                        <th className="p-2 text-right">SLA diag 24h</th>
                        <th className="p-2 text-right">T. emitir (h)</th>
                        <th className="p-2 text-right">T. aprobar (h)</th>
                        <th className="p-2 text-right">Derivados ext</th>
                        <th className="p-2 text-right">Devueltos ext</th>
                        <th className="p-2 text-right">T derivdev (d)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {yearlySeries.map((m) => (
                        <tr className="border-t" key={m.period}>
                          <td className="p-2">{m.period}</td>
                          <td className="p-2 text-right">{formatNumber(m.entregados)}</td>
                          <td className="p-2 text-right">{m.mttr_dias != null ? m.mttr_dias.toFixed(1) : '-'}</td>
                          <td className="p-2 text-right">{formatPct(m.sla_diag_24h?.cumplimiento || 0)}</td>
                          <td className="p-2 text-right">{m.aprob_presupuestos?.t_emitir_horas != null ? m.aprob_presupuestos.t_emitir_horas.toFixed(1) : '-'}</td>
                          <td className="p-2 text-right">{m.aprob_presupuestos?.t_aprobar_horas != null ? m.aprob_presupuestos.t_aprobar_horas.toFixed(1) : '-'}</td>
                          <td className="p-2 text-right">{formatNumber(m.externo?.derivados ?? 0)}</td>
                          <td className="p-2 text-right">{formatNumber(m.externo?.devueltos ?? 0)}</td>
                          <td className="p-2 text-right">{m.externo?.t_deriv_a_devuelto_dias != null ? m.externo.t_deriv_a_devuelto_dias.toFixed(1) : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              )}
            </>
          )}
        </>
      )}
      <ConfigPanel open={configOpen} onClose={closeConfig} />
    </div>
  );
}

function MiniSpark({ title, values = [], fmt = (v)=>v }) {
  const w = 300, h = 60, p = 6;
  const n = values.length;
  const max = values.reduce((m,v)=>Math.max(m, v||0), 0) || 1;
  const pts = values.map((v,i)=>{
    const x = p + (i*(w-2*p))/Math.max(1, n-1);
    const y = h - p - ((v||0)/max)*(h-2*p);
    return `${x},${y}`;
  }).join(" ");
  const last = values[n-1] || 0;
  return (
    <div className="p-3 border rounded bg-white">
      <div className="text-sm text-gray-600 flex justify-between">
        <span>{title}</span>
        <span className="font-semibold">{fmt(last)}</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-16 mt-1" role="img" aria-label={title}>
        <title>{title}</title>
        <polyline fill="none" stroke="#3b82f6" strokeWidth="2" points={pts} />
      </svg>
    </div>
  );
}




