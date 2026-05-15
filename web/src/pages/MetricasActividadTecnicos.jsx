import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getMetricasActividadTecnicos, getTecnicos } from "../lib/api";
import { METRICAS_DESDE_MIN, clampDesdeMin } from "../lib/constants";
import { formatOS } from "../lib/ui-helpers";
import MetricasNav from "../components/metricas/MetricasNav.jsx";

function isoDateLocal(date) {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function computePresetRange(preset) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (preset === "today") {
    return { from: isoDateLocal(today), to: isoDateLocal(today) };
  }
  if (preset === "yesterday") {
    const target = new Date(today);
    target.setDate(target.getDate() - 1);
    return { from: isoDateLocal(target), to: isoDateLocal(target) };
  }
  const monday = new Date(today);
  monday.setDate(monday.getDate() - monday.getDay() + (monday.getDay() === 0 ? -6 : 1));
  return { from: isoDateLocal(monday), to: isoDateLocal(today) };
}

function formatNumber(value) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("es-AR");
}

function formatDateTimeFull(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("es-AR", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function sourceLabel(source) {
  if (source === "change_log") return "Cambios";
  if (source === "ingreso_event") return "Eventos";
  if (source === "repuestos_movimientos") return "Repuestos";
  if (source === "audit_log") return "Aperturas";
  return source || "-";
}

function StatCard({ label, value, help }) {
  return (
    <div className="rounded border bg-white p-4">
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-900">{value}</div>
      {help ? <div className="mt-2 text-xs text-gray-500">{help}</div> : null}
    </div>
  );
}

function QuickRangeButton({ label, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded border px-2.5 py-1 text-xs ${active ? "border-gray-300 bg-gray-100 font-semibold" : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50 hover:text-gray-900"}`}
    >
      {label}
    </button>
  );
}

export default function MetricasActividadTecnicos() {
  const [search, setSearch] = useSearchParams();
  const initialPreset = ["today", "yesterday", "week"].includes(search.get("preset"))
    ? search.get("preset")
    : "week";
  const initialRange = computePresetRange(initialPreset);
  const [preset, setPreset] = useState(initialPreset);
  const [desde, setDesde] = useState(() => clampDesdeMin(search.get("from") || initialRange.from));
  const [hasta, setHasta] = useState(() => search.get("to") || initialRange.to);
  const [tecnicoId, setTecnicoId] = useState(search.get("tecnico_id") || "");
  const [tipo, setTipo] = useState(search.get("tipo") || "");
  const [tecnicos, setTecnicos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);

  useEffect(() => {
    getTecnicos().then(setTecnicos).catch(() => {});
  }, []);

  const desdeClamped = useMemo(() => clampDesdeMin(desde), [desde]);
  const periodLabel = useMemo(() => `${desdeClamped} a ${hasta}`, [desdeClamped, hasta]);

  useEffect(() => {
    const next = new URLSearchParams(search.toString());
    next.set("from", desdeClamped);
    next.set("to", hasta);
    if (preset) next.set("preset", preset); else next.delete("preset");
    if (tecnicoId) next.set("tecnico_id", tecnicoId); else next.delete("tecnico_id");
    if (tipo) next.set("tipo", tipo); else next.delete("tipo");
    setSearch(next, { replace: true });
  }, [desdeClamped, hasta, preset, tecnicoId, tipo]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    const params = { from: desdeClamped, to: hasta };
    if (preset) params.preset = preset;
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (tipo) params.tipo = tipo;
    getMetricasActividadTecnicos(params)
      .then((res) => {
        if (!alive) return;
        setData(res);
      })
      .catch((err) => {
        if (!alive) return;
        setError(err?.message || String(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [desdeClamped, hasta, preset, tecnicoId, tipo]);

  function applyPreset(nextPreset) {
    const range = computePresetRange(nextPreset);
    setPreset(nextPreset);
    setDesde(clampDesdeMin(range.from));
    setHasta(range.to);
  }

  const summary = data?.summary || {};
  const timeline = data?.timeline || [];
  const availableTypes = data?.available_activity_types || summary.activity_types || [];
  const summaryTypes = summary.activity_types || summary.by_activity_type || [];
  const aperturaCount = useMemo(
    () => summaryTypes
      .filter((item) => String(item.activity_type || "").startsWith("apertura_"))
      .reduce((acc, item) => acc + Number(item.count || 0), 0),
    [summaryTypes],
  );
  const repuestoCount = useMemo(() => {
    const row = summaryTypes.find((item) => item.activity_type === "movimiento_repuesto");
    return Number(row?.count || 0);
  }, [summaryTypes]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-xl font-semibold">Actividad de técnicos</h1>
          <div className="text-sm text-gray-500">
            Bitácora cronológica de aperturas, cambios y movimientos realizados por cada técnico.
          </div>
        </div>
      </div>

      <MetricasNav />

      <div className="space-y-4 rounded border bg-white p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
          <div>
            <div className="text-sm text-gray-600">Desde</div>
            <input
              type="date"
              value={desdeClamped}
              min={METRICAS_DESDE_MIN}
              onChange={(e) => {
                setPreset("");
                setDesde(clampDesdeMin(e.target.value));
              }}
              className="mt-1 w-full rounded border px-2 py-1"
            />
          </div>
          <div>
            <div className="text-sm text-gray-600">Hasta</div>
            <input
              type="date"
              value={hasta}
              onChange={(e) => {
                setPreset("");
                setHasta(e.target.value);
              }}
              className="mt-1 w-full rounded border px-2 py-1"
            />
          </div>
          <div>
            <div className="text-sm text-gray-600">Técnico</div>
            <select
              className="mt-1 w-full rounded border px-2 py-1"
              value={tecnicoId}
              onChange={(e) => setTecnicoId(e.target.value)}
            >
              <option value="">Todos</option>
              {tecnicos.map((tecnico) => (
                <option key={tecnico.id} value={tecnico.id}>
                  {tecnico.nombre}
                </option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-sm text-gray-600">Tipo</div>
            <select
              className="mt-1 w-full rounded border px-2 py-1"
              value={tipo}
              onChange={(e) => setTipo(e.target.value)}
            >
              <option value="">Todos</option>
              {availableTypes.map((item) => (
                <option key={item.activity_type} value={item.activity_type}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="mr-2 text-xs text-gray-500">Rangos rápidos</div>
          <QuickRangeButton label="Hoy" active={preset === "today"} onClick={() => applyPreset("today")} />
          <QuickRangeButton label="Ayer" active={preset === "yesterday"} onClick={() => applyPreset("yesterday")} />
          <QuickRangeButton label="Semana" active={preset === "week"} onClick={() => applyPreset("week")} />
          {(tecnicoId || tipo || preset || desdeClamped !== METRICAS_DESDE_MIN) ? (
            <button
              type="button"
              onClick={() => {
                applyPreset("week");
                setTecnicoId("");
                setTipo("");
              }}
              className="rounded border bg-white px-2.5 py-1 text-xs hover:bg-gray-50"
            >
              Restablecer
            </button>
          ) : null}
          <div className="ml-auto text-xs text-gray-500">Período {periodLabel}</div>
        </div>
      </div>

      {loading && <div className="text-gray-500">Cargando actividad</div>}
      {error && <div className="text-red-600">Error al cargar actividad: {error}</div>}

      {data && (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
            <StatCard label="Acciones" value={formatNumber(summary.total || 0)} />
            <StatCard label="Técnicos activos" value={formatNumber(summary.unique_tecnicos || 0)} />
            <StatCard label="Aperturas" value={formatNumber(aperturaCount)} />
            <StatCard label="Movimientos de repuesto" value={formatNumber(repuestoCount)} />
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_minmax(0,1fr)]">
            <div className="rounded border bg-white">
              <div className="border-b px-4 py-3">
                <h2 className="font-semibold">Resumen por técnico</h2>
              </div>
              <div className="max-h-[28rem] overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="p-2 text-left">Técnico</th>
                      <th className="p-2 text-right">Acciones</th>
                      <th className="p-2 text-right">Aperturas</th>
                      <th className="p-2 text-right">Rep.</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(summary.by_tecnico || []).length === 0 && (
                      <tr>
                        <td colSpan={4} className="p-3 text-gray-500">Sin datos</td>
                      </tr>
                    )}
                    {(summary.by_tecnico || []).map((row) => (
                      <tr key={row.tecnico_id ?? row.tecnico_nombre} className="border-t">
                        <td className="p-2">{row.tecnico_nombre || "(sin nombre)"}</td>
                        <td className="p-2 text-right">{formatNumber(row.total)}</td>
                        <td className="p-2 text-right">{formatNumber(row.aperturas)}</td>
                        <td className="p-2 text-right">{formatNumber(row.movimientos_repuestos)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="rounded border bg-white">
              <div className="border-b px-4 py-3">
                <h2 className="font-semibold">Timeline</h2>
              </div>
              <div className="overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-gray-600">
                      <th className="p-2 text-left">Fecha y hora</th>
                      <th className="p-2 text-left">Técnico</th>
                      <th className="p-2 text-left">Tipo</th>
                      <th className="p-2 text-left">Detalle</th>
                      <th className="p-2 text-left">Referencia</th>
                      <th className="p-2 text-left">Fuente</th>
                    </tr>
                  </thead>
                  <tbody>
                    {timeline.length === 0 && (
                      <tr>
                        <td colSpan={6} className="p-3 text-gray-500">Sin movimientos en el rango seleccionado.</td>
                      </tr>
                    )}
                    {timeline.map((row) => {
                      const referenceText = row.reference || row.ingreso_ref || row.path || "-";
                      const osText = row.os || formatOS(row.ingreso_id);
                      return (
                        <tr key={`${row.source}-${row._id}`} className="border-t align-top">
                          <td className="whitespace-nowrap p-2">{formatDateTimeFull(row.ts)}</td>
                          <td className="p-2">{row.tecnico_nombre || "-"}</td>
                          <td className="p-2">
                            <div className="font-medium text-gray-900">{row.activity_type_label || "-"}</div>
                            <div className="text-xs text-gray-500">{row.activity_type || "-"}</div>
                          </td>
                          <td className="p-2">
                            <div className="font-medium text-gray-900">{row.title || "-"}</div>
                            <div className="text-gray-600">{row.detail || "-"}</div>
                          </td>
                          <td className="p-2">
                            {row.ingreso_id ? (
                              <Link to={`/ingresos/${row.ingreso_id}`} className="text-blue-700 underline">
                                {referenceText || osText}
                              </Link>
                            ) : (
                              <span>{referenceText}</span>
                            )}
                          </td>
                          <td className="p-2 text-gray-600">{sourceLabel(row.source)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
