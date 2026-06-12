import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getMetricasFinanzas, getMetricasFinanzasLiberados, getTecnicos, getMarcas, getTiposEquipo } from "../lib/api";
import { METRICAS_DESDE_MIN, clampDesdeMin } from "../lib/constants";
import { downloadExcel } from "../lib/excel";
import MetricasNav from "../components/metricas/MetricasNav.jsx";
import SimpleBars from "../components/metricas/charts/SimpleBars.jsx";
import SimpleLine from "../components/metricas/charts/SimpleLine.jsx";

const PROJECTION_OPTIONS = [3, 6, 12];

function formatMoney(n) {
  if (n == null || isNaN(n)) return "-";
  const value = Number(n);
  if (Number.isNaN(value)) return "-";
  return `$${value.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPct(n) {
  if (n == null || isNaN(n)) return "-";
  const value = Number(n);
  if (Number.isNaN(value)) return "-";
  return `${value.toFixed(0)}%`;
}

function StatCard({ label, value, help }) {
  return (
    <div className="p-4 border rounded bg-white">
      <div className="text-sm text-gray-500">{label}</div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
      {help ? <div className="text-xs text-gray-400 mt-1">{help}</div> : null}
    </div>
  );
}

export default function MetricasFinanzas() {
  const [search, setSearch] = useSearchParams();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [data, setData] = useState(null);
  const [tecnicos, setTecnicos] = useState([]);
  const [marcas, setMarcas] = useState([]);
  const [tipos, setTipos] = useState([]);
  const [tecnicoId, setTecnicoId] = useState(search.get("tecnico_id") || "");
  const [marcaId, setMarcaId] = useState(search.get("marca_id") || "");
  const [tipoEquipo, setTipoEquipo] = useState(search.get("tipo_equipo") || "");
  const [showCharts, setShowCharts] = useState(() => (search.get("view") || "") === "charts");
  const [viewMode, setViewMode] = useState(() => {
    const v = (search.get("scope") || "full").toLowerCase();
    return v === "costos" ? "costos" : "full";
  });
  const [projectionMonths, setProjectionMonths] = useState(() => {
    const v = Number(search.get("proj") || "3");
    return PROJECTION_OPTIONS.includes(v) ? v : 3;
  });
  const [exportMonth, setExportMonth] = useState(() => search.get("lib_month") || new Date().toISOString().slice(0, 7));
  const [desde, setDesde] = useState(() => {
    const s = search.get("from");
    if (s) return clampDesdeMin(s);
    const d = new Date(); d.setMonth(d.getMonth() - 6);
    return clampDesdeMin(d.toISOString().slice(0, 10));
  });
  const [hasta, setHasta] = useState(() => search.get("to") || new Date().toISOString().slice(0, 10));

  useEffect(() => {
    getTecnicos().then(setTecnicos).catch(() => {});
    getMarcas().then(setMarcas).catch(() => {});
    getTiposEquipo().then(setTipos).catch(() => {});
  }, []);

  const desdeClamped = useMemo(() => clampDesdeMin(desde), [desde]);

  useEffect(() => {
    const next = new URLSearchParams(search.toString());
    next.set("from", desdeClamped);
    next.set("to", hasta);
    if (tecnicoId) next.set("tecnico_id", tecnicoId); else next.delete("tecnico_id");
    if (marcaId) next.set("marca_id", marcaId); else next.delete("marca_id");
    if (tipoEquipo) next.set("tipo_equipo", tipoEquipo); else next.delete("tipo_equipo");
    if (showCharts) next.set("view", "charts"); else next.delete("view");
    if (viewMode && viewMode !== "full") next.set("scope", viewMode); else next.delete("scope");
    if (projectionMonths) next.set("proj", String(projectionMonths)); else next.delete("proj");
    if (exportMonth) next.set("lib_month", exportMonth); else next.delete("lib_month");
    setSearch(next, { replace: true });
  }, [desdeClamped, hasta, tecnicoId, marcaId, tipoEquipo, showCharts, viewMode, projectionMonths, exportMonth]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    const params = { from: desdeClamped, to: hasta };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    getMetricasFinanzas(params)
      .then((res) => { if (alive) setData(res); })
      .catch((err) => { if (alive) setError(err?.message || String(err)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [desdeClamped, hasta, tecnicoId, marcaId, tipoEquipo]);

  function exportSeriesExcel(kind) {
    const src = kind === "yearly" ? (data?.yearly || []) : (data?.monthly || []);
    let rows = [];
    if (viewMode === "costos") {
      rows = [["period", "costos_repuestos", "costo_promedio_por_liberado"]];
      src.forEach((it) => {
        const liberados = Number(it.liberados || 0);
        const costos = Number(it.costos_repuestos || 0);
        const costoProm = liberados ? (costos / liberados) : "";
        rows.push([
          it.period,
          costos,
          costoProm,
        ]);
      });
    } else {
      rows = [["period", "ingresos_sin_iva", "liberados", "mano_obra", "repuestos", "costos_repuestos", "margen", "ticket_promedio"]];
      src.forEach((it) => {
        const ingresos = Number(it.cobro || 0);
        const liberados = Number(it.liberados || 0);
        const ticket = liberados ? (ingresos / liberados) : "";
        rows.push([
          it.period,
          ingresos,
          liberados,
          Number(it.mo || 0),
          Number(it.repuestos || 0),
          Number(it.costos_repuestos || 0),
          Number(it.margen || (ingresos - Number(it.costos_repuestos || 0))),
          ticket,
        ]);
      });
    }
    const scope = viewMode === "costos" ? "costos" : "full";
    downloadExcel(`metricas_finanzas_${scope}_${kind}_${desdeClamped}_${hasta}.xls`, rows, viewMode === "costos" ? "Costos" : "Finanzas");
  }

  async function exportLiberadosExcel() {
    if (!exportMonth) return;
    const params = { month: exportMonth };
    if (tecnicoId) params.tecnico_id = tecnicoId;
    if (marcaId) params.marca_id = marcaId;
    if (tipoEquipo) params.tipo_equipo = tipoEquipo;
    try {
      const rows = await getMetricasFinanzasLiberados(params);
      const header = [
        "ingreso_id",
        "fecha_liberado",
        "fecha_ingreso",
        "cliente",
        "marca",
        "modelo",
        "tipo_equipo",
        "numero_serie",
        "numero_interno",
        "técnico",
        "presupuesto_estado",
        "quote_id",
        "quote_estado",
        "ingresos_sin_iva",
        "mano_obra",
        "repuestos",
        "costo_repuestos",
        "margen",
      ];
      const body = (rows || []).map((r) => ([
        r.ingreso_id,
        r.fecha_liberado,
        r.fecha_ingreso,
        r.cliente,
        r.marca,
        r.modelo,
        r.tipo_equipo,
        r.numero_serie,
        r.numero_interno,
        r.tecnico_nombre,
        r.presupuesto_estado,
        r.quote_id,
        r.quote_estado,
        r.ingresos_sin_iva,
        r.mano_obra,
        r.repuestos,
        r.costo_repuestos,
        r.margen,
      ]));
      downloadExcel(`liberados_${exportMonth}_finanzas.xls`, [header, ...body], "Liberados");
    } catch (err) {
      alert(`Error al exportar liberados: ${err?.message || String(err)}`);
    }
  }

  const monthly = useMemo(() => data?.monthly || [], [data]);
  const yearly = useMemo(() => data?.yearly || [], [data]);
  const summary = data?.summary || {};

  const totalIngresos = Number(summary.cobro_total || 0);
  const totalLiberados = Number(summary.liberados_total || 0);
  const totalMo = Number(summary.mo_total || 0);
  const totalRep = Number(summary.repuestos_total || 0);
  const totalCostosRep = Number(summary.costos_repuestos_total || 0);
  const totalMargen = summary.margen_total != null ? Number(summary.margen_total) : (totalIngresos - totalCostosRep);
  const ticketProm = summary.ticket_promedio != null ? Number(summary.ticket_promedio) : (totalLiberados ? totalIngresos / totalLiberados : null);
  const mixMoPct = totalIngresos ? (totalMo / totalIngresos) * 100 : null;
  const mixRepPct = totalIngresos ? (totalRep / totalIngresos) * 100 : null;
  const costoPromTicket = totalLiberados ? (totalCostosRep / totalLiberados) : null;
  const costoPromMensual = monthly.length ? (totalCostosRep / monthly.length) : null;

  const projection = useMemo(() => {
    if (!monthly.length) return { avg: 0, total: 0, count: 0 };
    const slice = monthly.slice(-projectionMonths);
    const sum = slice.reduce((acc, it) => {
      const base = viewMode === "costos" ? it.costos_repuestos : it.cobro;
      return acc + (Number(base) || 0);
    }, 0);
    const avg = slice.length ? (sum / slice.length) : 0;
    return { avg, total: avg * projectionMonths, count: slice.length };
  }, [monthly, projectionMonths, viewMode]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Métricas - Finanzas</h1>
      </div>

      <MetricasNav />

      <div className="grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
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
            {tecnicos.map((t) => (
              <option key={t.id} value={t.id}>{t.nombre}</option>
            ))}
          </select>
        </div>
        <div>
          <div className="text-sm text-gray-600">Marca</div>
          <select className="mt-1 border rounded px-2 py-1 w-full" value={marcaId} onChange={(e) => setMarcaId(e.target.value)}>
            <option value="">Todas</option>
            {marcas.map((m) => (
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
        <div className="md:col-span-5 flex flex-wrap gap-2 items-center">
          <div className="mr-2 inline-flex border rounded overflow-hidden">
            <button
              onClick={() => setShowCharts(false)}
              className={`px-3 py-1.5 ${!showCharts ? "bg-gray-100 font-semibold" : "bg-white hover:bg-gray-50"}`}
            >Tablas</button>
            <button
              onClick={() => setShowCharts(true)}
              className={`px-3 py-1.5 border-l ${showCharts ? "bg-gray-100 font-semibold" : "bg-white hover:bg-gray-50"}`}
            >Graficos</button>
          </div>
          <div className="mr-2 inline-flex border rounded overflow-hidden">
            <button
              onClick={() => setViewMode("full")}
              className={`px-3 py-1.5 ${viewMode === "full" ? "bg-gray-100 font-semibold" : "bg-white hover:bg-gray-50"}`}
            >Completa</button>
            <button
              onClick={() => setViewMode("costos")}
              className={`px-3 py-1.5 border-l ${viewMode === "costos" ? "bg-gray-100 font-semibold" : "bg-white hover:bg-gray-50"}`}
            >Costos</button>
          </div>
          <button onClick={() => exportSeriesExcel("monthly")} className="px-3 py-1.5 border rounded bg-white hover:bg-gray-50">Exportar mensual (Excel)</button>
          <button onClick={() => exportSeriesExcel("yearly")} className="px-3 py-1.5 border rounded bg-white hover:bg-gray-50">Exportar anual (Excel)</button>
          {viewMode !== "costos" && (
            <div className="inline-flex items-center gap-2">
              <label className="text-sm text-gray-600">Mes liberados</label>
              <input
                type="month"
                value={exportMonth}
                onChange={(e) => setExportMonth(e.target.value)}
                className="border rounded px-2 py-1"
              />
              <button onClick={exportLiberadosExcel} className="px-3 py-1.5 border rounded bg-white hover:bg-gray-50">Exportar liberados (Excel)</button>
            </div>
          )}
          <div className="ml-auto flex items-center gap-2">
            <span className="text-sm text-gray-600">Proyeccion</span>
            <select
              className="border rounded px-2 py-1"
              value={projectionMonths}
              onChange={(e) => setProjectionMonths(Number(e.target.value))}
            >
              {PROJECTION_OPTIONS.map((opt) => (
                <option key={opt} value={opt}>{opt} meses</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {loading && <div className="text-gray-500">Cargando métricas</div>}
      {error && (
        <div className="text-red-600">Error al cargar métricas: {error}</div>
      )}

      {data && (
        <>
          {viewMode === "full" ? (
            <>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <StatCard
                  label="Ingresos (ARS, sin IVA)"
                  value={formatMoney(totalIngresos)}
                  help="Presupuestos de equipos liberados (sin IVA)"
                />
                <StatCard
                  label="Costos repuestos (ARS, sin IVA)"
                  value={formatMoney(totalCostosRep)}
                  help="Costos congelados al cargar repuestos"
                />
                <StatCard
                  label="Margen bruto (sin IVA)"
                  value={formatMoney(totalMargen)}
                  help="Ingresos - costos de repuestos"
                />
                <StatCard
                  label="Liberados"
                  value={totalLiberados ? totalLiberados.toLocaleString() : "0"}
                  help="Cantidad de equipos liberados en el rango"
                />
                <StatCard
                  label="Ticket promedio"
                  value={ticketProm != null ? formatMoney(ticketProm) : "-"}
                  help="Ingresos sin IVA / liberados"
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <StatCard
                  label={`Promedio mensual (${projection.count || 0} meses)`}
                  value={formatMoney(projection.avg)}
                  help="Base para proyeccion"
                />
                <StatCard
                  label={`Proyeccion ${projectionMonths} meses`}
                  value={formatMoney(projection.total)}
                  help="Promedio mensual x horizonte"
                />
                <StatCard
                  label="Mix MO / Repuestos"
                  value={(mixMoPct != null && mixRepPct != null) ? `${formatPct(mixMoPct)} / ${formatPct(mixRepPct)}` : "-"}
                  help={`MO ${formatMoney(totalMo)} - Rep ${formatMoney(totalRep)}`}
                />
              </div>
            </>
          ) : (
            <>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <StatCard
                  label="Costos repuestos (ARS, sin IVA)"
                  value={formatMoney(totalCostosRep)}
                  help="Costos congelados al cargar repuestos"
                />
                <StatCard
                  label="Costo promedio mensual (rango)"
                  value={costoPromMensual != null ? formatMoney(costoPromMensual) : "-"}
                  help="Promedio mensual del rango seleccionado"
                />
                <StatCard
                  label="Costo promedio por liberado"
                  value={costoPromTicket != null ? formatMoney(costoPromTicket) : "-"}
                  help="Promedio de costos repuestos por equipo"
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <StatCard
                  label={`Promedio mensual (${projection.count || 0} meses)`}
                  value={formatMoney(projection.avg)}
                  help="Base para proyeccion de costos"
                />
                <StatCard
                  label={`Proyeccion costos ${projectionMonths} meses`}
                  value={formatMoney(projection.total)}
                  help="Promedio mensual x horizonte"
                />
              </div>
            </>
          )}

          <div className="text-sm text-gray-500">
            Costos de repuestos incluidos (sin IVA). Si no hay costo registrado, se estima con el multiplicador general. Otros costos pendientes de carga. Moneda: ARS.
          </div>

          {showCharts && (
            <div className="space-y-4">
              {viewMode === "costos" ? (
                <>
                  <SimpleBars
                    title="Costos repuestos (ARS, sin IVA) por mes"
                    categories={monthly.map((m) => m.period)}
                    values={monthly.map((m) => m.costos_repuestos || 0)}
                    fmt={(v) => formatMoney(v)}
                    color="#ef4444"
                  />
                  <SimpleBars
                    title="Costos repuestos (ARS, sin IVA) por Ano"
                    categories={yearly.map((m) => m.period)}
                    values={yearly.map((m) => m.costos_repuestos || 0)}
                    fmt={(v) => formatMoney(v)}
                    color="#f97316"
                  />
                  <SimpleLine
                    title="Costo promedio por liberado"
                    categories={monthly.map((m) => m.period)}
                    series={[
                      {
                        name: "Costo promedio",
                        values: monthly.map((m) => {
                          const liberados = Number(m.liberados || 0);
                          const costos = Number(m.costos_repuestos || 0);
                          return liberados ? (costos / liberados) : 0;
                        }),
                        color: "#ef4444",
                      },
                    ]}
                    fmt={(v) => formatMoney(v)}
                  />
                </>
              ) : (
                <>
                  <SimpleBars
                    title="Ingresos (ARS, sin IVA) por mes (liberados)"
                    categories={monthly.map((m) => m.period)}
                    values={monthly.map((m) => m.cobro || 0)}
                    fmt={(v) => formatMoney(v)}
                  />
                  <SimpleBars
                    title="Ingresos (ARS, sin IVA) por Ano"
                    categories={yearly.map((m) => m.period)}
                    values={yearly.map((m) => m.cobro || 0)}
                    fmt={(v) => formatMoney(v)}
                    color="#10b981"
                  />
                  <SimpleLine
                    title="Ingresos vs Costos repuestos"
                    categories={monthly.map((m) => m.period)}
                    series={[
                      { name: "Ingresos", values: monthly.map((m) => m.cobro || 0), color: "#10b981" },
                      { name: "Costos repuestos", values: monthly.map((m) => m.costos_repuestos || 0), color: "#ef4444" },
                    ]}
                    fmt={(v) => formatMoney(v)}
                  />
                  <SimpleLine
                    title="Mix mensual: Mano de obra vs Repuestos"
                    categories={monthly.map((m) => m.period)}
                    series={[
                      { name: "Mano de obra", values: monthly.map((m) => m.mo || 0) },
                      { name: "Repuestos", values: monthly.map((m) => m.repuestos || 0), color: "#f59e0b" },
                    ]}
                    fmt={(v) => formatMoney(v)}
                  />
                </>
              )}
            </div>
          )}

          {!showCharts && viewMode !== "costos" && (
            <>
              <div className="mt-6">
                <h2 className="font-semibold mb-2">Series mensuales</h2>
                <div className="overflow-x-auto border rounded bg-white">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-gray-600">
                        <th className="p-2 text-left">Periodo</th>
                        <th className="p-2 text-right">Ingresos (ARS, sin IVA)</th>
                        <th className="p-2 text-right">Liberados</th>
                        <th className="p-2 text-right">MO</th>
                        <th className="p-2 text-right">Repuestos</th>
                        <th className="p-2 text-right">Costos repuestos</th>
                        <th className="p-2 text-right">Margen</th>
                        <th className="p-2 text-right">Ticket</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(monthly || []).map((m) => (
                        <tr className="border-t" key={m.period}>
                          <td className="p-2">{m.period}</td>
                          <td className="p-2 text-right">{formatMoney(m.cobro || 0)}</td>
                          <td className="p-2 text-right">{Number(m.liberados || 0).toLocaleString()}</td>
                          <td className="p-2 text-right">{formatMoney(m.mo || 0)}</td>
                          <td className="p-2 text-right">{formatMoney(m.repuestos || 0)}</td>
                          <td className="p-2 text-right">{formatMoney(m.costos_repuestos || 0)}</td>
                          <td className="p-2 text-right">{formatMoney(m.margen ?? ((Number(m.cobro || 0)) - Number(m.costos_repuestos || 0)))}</td>
                          <td className="p-2 text-right">{m.liberados ? formatMoney((Number(m.cobro || 0) / Number(m.liberados || 0))) : "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="mt-8">
                <h2 className="font-semibold mb-2">Series anuales</h2>
                <div className="overflow-x-auto border rounded bg-white">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-gray-600">
                        <th className="p-2 text-left">Ano</th>
                        <th className="p-2 text-right">Ingresos (ARS, sin IVA)</th>
                        <th className="p-2 text-right">Liberados</th>
                        <th className="p-2 text-right">MO</th>
                        <th className="p-2 text-right">Repuestos</th>
                        <th className="p-2 text-right">Costos repuestos</th>
                        <th className="p-2 text-right">Margen</th>
                        <th className="p-2 text-right">Ticket</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(yearly || []).map((m) => (
                        <tr className="border-t" key={m.period}>
                          <td className="p-2">{m.period}</td>
                          <td className="p-2 text-right">{formatMoney(m.cobro || 0)}</td>
                          <td className="p-2 text-right">{Number(m.liberados || 0).toLocaleString()}</td>
                          <td className="p-2 text-right">{formatMoney(m.mo || 0)}</td>
                          <td className="p-2 text-right">{formatMoney(m.repuestos || 0)}</td>
                          <td className="p-2 text-right">{formatMoney(m.costos_repuestos || 0)}</td>
                          <td className="p-2 text-right">{formatMoney(m.margen ?? ((Number(m.cobro || 0)) - Number(m.costos_repuestos || 0)))}</td>
                          <td className="p-2 text-right">{m.liberados ? formatMoney((Number(m.cobro || 0) / Number(m.liberados || 0))) : "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
          {!showCharts && viewMode === "costos" && (
            <>
              <div className="mt-6">
                <h2 className="font-semibold mb-2">Series mensuales - Costos</h2>
                <div className="overflow-x-auto border rounded bg-white">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-gray-600">
                        <th className="p-2 text-left">Periodo</th>
                        <th className="p-2 text-right">Costos repuestos</th>
                        <th className="p-2 text-right">Costo por liberado</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(monthly || []).map((m) => {
                        const liberados = Number(m.liberados || 0);
                        const costos = Number(m.costos_repuestos || 0);
                        const costoProm = liberados ? (costos / liberados) : null;
                        return (
                          <tr className="border-t" key={m.period}>
                            <td className="p-2">{m.period}</td>
                            <td className="p-2 text-right">{formatMoney(costos)}</td>
                            <td className="p-2 text-right">{costoProm != null ? formatMoney(costoProm) : "-"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="mt-8">
                <h2 className="font-semibold mb-2">Series anuales - Costos</h2>
                <div className="overflow-x-auto border rounded bg-white">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-gray-600">
                        <th className="p-2 text-left">Ano</th>
                        <th className="p-2 text-right">Costos repuestos</th>
                        <th className="p-2 text-right">Costo por liberado</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(yearly || []).map((m) => {
                        const liberados = Number(m.liberados || 0);
                        const costos = Number(m.costos_repuestos || 0);
                        const costoProm = liberados ? (costos / liberados) : null;
                        return (
                          <tr className="border-t" key={m.period}>
                            <td className="p-2">{m.period}</td>
                            <td className="p-2 text-right">{formatMoney(costos)}</td>
                            <td className="p-2 text-right">{costoProm != null ? formatMoney(costoProm) : "-"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
