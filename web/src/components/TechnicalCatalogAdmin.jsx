import { useEffect, useState } from "react";
import { useAuth } from "../context/AuthContext";
import { can, PERMISSION_CODES } from "../lib/permissions";
import { getTechnicalRates, saveTechnicalRates, getTechnicalWorks, createTechnicalWork, patchTechnicalWork, getTechnicalCatalogRequests, resolveTechnicalCatalogRequest, getTechnicalParts } from "../lib/api";

export default function TechnicalCatalogAdmin() {
  const { user } = useAuth();
  const allowed = can(user, PERMISSION_CODES.ACTION_PRESUPUESTO_MANAGE);
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [works, setWorks] = useState([]);
  const [requests, setRequests] = useState([]);
  const [rate, setRate] = useState({ tipo_equipo: "", primera_hora: "", hora_adicional: "" });
  const [diagnostic, setDiagnostic] = useState("");
  const [newWork, setNewWork] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [resolution, setResolution] = useState(null);
  const [search, setSearch] = useState("");
  const [options, setOptions] = useState([]);
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");

  async function load() {
    const [rates, catalog, pending] = await Promise.all([getTechnicalRates(), getTechnicalWorks(true), getTechnicalCatalogRequests()]);
    setData(rates); setDiagnostic(rates.configuracion?.diagnostico_limpieza ?? ""); setWorks(catalog); setRequests(pending);
  }
  useEffect(() => { if (allowed && open) load().catch((e) => setError(e.message)); }, [allowed, open]);
  useEffect(() => {
    if (!resolution) return undefined;
    let alive = true;
    const timer = setTimeout(() => {
      const task = resolution.tipo === "trabajo" ? getTechnicalWorks() : getTechnicalParts(resolution.ingreso_id, search);
      task.then((rows) => { if (alive) setOptions(rows); }).catch((e) => { if (alive) setError(e.message); });
    }, 200);
    return () => { alive = false; clearTimeout(timer); };
  }, [resolution, search]);
  async function run(task) { setBusy(true); setError(""); try { await task(); await load(); } catch (e) { setError(e.message); } finally { setBusy(false); } }
  if (!allowed) return null;
  return <details className="border rounded-lg bg-white my-4" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
    <summary className="p-3 cursor-pointer font-medium">Trabajos, tarifas y solicitudes de alta</summary>
    <div className="px-4 pb-4 space-y-5">
      {error && <p role="alert" className="bg-red-50 text-red-800 rounded p-3">{error}</p>}
      {!data ? <p>Cargando configuración...</p> : <>
        <section><h3 className="font-semibold mb-2">Tarifas por tipo de equipo</h3><p className="text-sm text-slate-600 mb-3">Importes netos en pesos. Se cobra como mínimo una hora y luego los minutos adicionales proporcionales.</p>
          <form className="flex flex-wrap gap-3 items-end" onSubmit={(e) => { e.preventDefault(); run(() => saveTechnicalRates(rate)); }}>
            <label className="text-sm">Tipo de equipo<input required list="technical-equipment-types" className="block border rounded p-2" value={rate.tipo_equipo} onChange={(e) => { const match = data.tarifas.find((row) => row.tipo_equipo === e.target.value.toLowerCase()); setRate(match || { ...rate, tipo_equipo: e.target.value }); }} />
              <datalist id="technical-equipment-types">{data.tipos_equipo.map((row) => <option key={row.nombre} value={row.nombre} />)}</datalist></label>
            <label className="text-sm">Primera hora<input required type="number" step="0.01" min="0" className="block border rounded p-2 w-36" value={rate.primera_hora} onChange={(e) => setRate({ ...rate, primera_hora: e.target.value })} /></label>
            <label className="text-sm">Hora adicional<input required type="number" step="0.01" min="0" className="block border rounded p-2 w-36" value={rate.hora_adicional} onChange={(e) => setRate({ ...rate, hora_adicional: e.target.value })} /></label>
            <button disabled={busy} className="border rounded px-3 py-2">Guardar tarifa</button>
          </form>
          <ul className="text-sm space-y-1 mt-3">{data.tarifas.map((row) => <li key={row.tipo_equipo}><button type="button" className="text-blue-700 underline text-left" onClick={() => setRate(row)}>{row.tipo_equipo}</button>: primera hora $ {row.primera_hora}; adicional $ {row.hora_adicional}</li>)}</ul>
        </section>
        <form className="flex flex-wrap gap-3 items-end" onSubmit={(e) => { e.preventDefault(); run(() => saveTechnicalRates({ diagnostico_limpieza: diagnostic })); }}>
          <label className="text-sm">Diagnóstico y limpieza sin falla · tarifa general<input required type="number" min="0" step="0.01" className="block border rounded p-2 w-40" value={diagnostic} onChange={(e) => setDiagnostic(e.target.value)} /></label>
          <button disabled={busy} className="border rounded px-3 py-2">Guardar tarifa general</button>
        </form>
        <section><h3 className="font-semibold mb-2">Catálogo de trabajos</h3>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); run(async () => { await createTechnicalWork(newWork); setNewWork(""); }); }}><input required className="border rounded p-2 grow min-w-0" aria-label="Nombre del nuevo trabajo" value={newWork} onChange={(e) => setNewWork(e.target.value)} placeholder="Nombre del trabajo" /><button disabled={busy} className="border rounded px-3">Crear trabajo</button></form>
          <ul className="text-sm mt-3 space-y-2">{works.map((row) => <li key={row.id} className="flex items-center justify-between gap-3"><span>{row.nombre}{!row.activo && " (inactivo)"}</span><button type="button" disabled={busy} className="text-blue-700 underline" onClick={() => run(() => patchTechnicalWork(row.id, { activo: !row.activo }))}>{row.activo ? "Desactivar" : "Activar"}</button></li>)}</ul>
        </section>
        <section><h3 className="font-semibold mb-2">Solicitudes pendientes ({requests.filter((row) => row.estado === "pendiente").length})</h3>
          <p className="text-sm mb-2">Para un repuesto nuevo, <a className="text-blue-700 underline" href="/catalogo/repuestos" target="_blank" rel="noreferrer">abrí el catálogo de repuestos</a>, crealo y luego vinculalo a la solicitud.</p>
          <ul className="text-sm space-y-2">{requests.filter((row) => row.estado === "pendiente").map((row) => <li key={row.id} className="border rounded p-3 flex flex-wrap justify-between gap-2"><span>OS {row.ingreso_id} · {row.tipo}: {row.nombre} · cantidad {row.cantidad}{row.observaciones && <span className="block text-slate-600">{row.observaciones}</span>}</span><button type="button" className="text-blue-700 underline" onClick={() => { setResolution(row); setTarget(""); setSearch(row.nombre); setReason(""); }}>Resolver solicitud</button></li>)}</ul>
          {resolution && <form className="mt-3 rounded border border-blue-200 bg-blue-50 p-3 space-y-3" onSubmit={(e) => { e.preventDefault(); run(async () => { await resolveTechnicalCatalogRequest(resolution.id, { estado: "aprobada", catalog_id: Number(target), respuesta: reason }); setResolution(null); }); }}>
            <p className="font-medium">Vincular «{resolution.nombre}» con un artículo del catálogo</p>
            {resolution.tipo === "repuesto" && <input aria-label="Buscar artículo para vincular" className="border rounded p-2 w-full" value={search} onChange={(e) => setSearch(e.target.value)} />}
            <select required aria-label="Artículo aprobado" className="border rounded p-2 w-full" value={target} onChange={(e) => setTarget(e.target.value)}><option value="">Seleccionar artículo existente</option>{options.map((row) => <option key={row.id} value={row.id}>{row.codigo ? `${row.codigo} · ` : ""}{row.nombre}</option>)}</select>
            <input aria-label="Respuesta a la solicitud" className="border rounded p-2 w-full" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Respuesta o motivo del rechazo" />
            <div className="flex flex-wrap gap-3"><button disabled={busy || !target} className="bg-blue-700 text-white rounded px-3 py-2">Aprobar y agregar a la orden</button><button type="button" disabled={busy || !reason.trim()} className="text-red-700 underline" onClick={() => run(async () => { await resolveTechnicalCatalogRequest(resolution.id, { estado: "rechazada", respuesta: reason }); setResolution(null); })}>Rechazar con motivo</button><button type="button" className="underline" onClick={() => setResolution(null)}>Cancelar</button></div>
          </form>}
        </section>
      </>}
    </div>
  </details>;
}
