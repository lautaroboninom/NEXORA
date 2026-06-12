import { useEffect, useState } from "react";
import { getMetricasConfig, getFeriados, postFeriado, deleteFeriado } from "../../lib/api";

export default function ConfigPanel({ open, onClose }) {
  const [cfg, setCfg] = useState(null);
  const [feriados, setFeriados] = useState([]);
  const [fecha, setFecha] = useState("");
  const [nombre, setNombre] = useState("");
  const [err, setErr] = useState("");
  const [targets, setTargets] = useState(()=>{
    try { return JSON.parse(localStorage.getItem('metricas_targets')||'{}')||{}; } catch { return {}; }
  });

  useEffect(() => {
    if (!open) return;
    setErr("");
    getMetricasConfig().then(setCfg).catch(e => setErr(e.message||String(e)));
    getFeriados().then(setFeriados).catch(()=>setFeriados([]));
  }, [open]);

  async function addFeriado(e) {
    e.preventDefault();
    setErr("");
    try {
      if (!fecha) return;
      await postFeriado(fecha, nombre || "Feriado");
      setFecha(""); setNombre("");
      const rows = await getFeriados();
      setFeriados(rows);
    } catch (ex) {
      setErr(ex.message||String(ex));
    }
  }

  async function removeFeriado(f) {
    if (!confirm(`Eliminar feriado ${f.fecha}?`)) return;
    await deleteFeriado(f.fecha);
    const rows = await getFeriados();
    setFeriados(rows);
  }

  return (
    <div className={`${open ? '' : 'pointer-events-none'} fixed inset-0 z-40`}>
      {/* Backdrop */}
      <div
        className={`absolute inset-0 bg-black/30 transition-opacity ${open ? 'opacity-100' : 'opacity-0'}`}
        onClick={onClose}
      />
      {/* Drawer */}
      <div
        className={`absolute right-0 top-0 h-full w-full max-w-xl bg-white shadow-xl border-l transform transition-transform ${open ? 'translate-x-0' : 'translate-x-full'}`}
      >
        <div className="p-4 border-b flex items-center justify-between">
          <h2 className="text-lg font-semibold">Configurar Métricas</h2>
          <button onClick={onClose} className="px-3 py-1.5 border rounded hover:bg-gray-50">Cerrar</button>
        </div>

        <div className="p-4 space-y-6 overflow-y-auto h-[calc(100%-56px)]">
          {err && <div className="text-red-600 text-sm">{err}</div>}

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="p-4 border rounded bg-white">
              <div className="text-sm text-gray-500">País feriados</div>
              <div className="text-lg font-semibold">{cfg?.holidays_country || 'AR'}</div>
              <div className="text-xs text-gray-400 mt-1">Fuente automática: Nager.Date</div>
            </div>
            <div className="p-4 border rounded bg-white">
              <div className="text-sm text-gray-500">Horario laboral</div>
              <div className="text-lg font-semibold">{cfg ? `${cfg.workday_start_hour}:00–${cfg.workday_end_hour}:00` : '-'}</div>
              <div className="text-xs text-gray-400 mt-1">Días hábiles: {(cfg?.workdays||[]).join(', ')}</div>
            </div>
            <div className="p-4 border rounded bg-white">
              <div className="text-sm text-gray-500">SLA diagnóstico</div>
              <div className="text-lg font-semibold">Excluir derivados por defecto</div>
              <div className="text-xs text-gray-400 mt-1">Se puede sobreescribir en la vista Métricas</div>
            </div>
          </div>

          <div>
            <h2 className="font-semibold mb-2">Objetivos / Umbrales</h2>
            <p className="text-sm text-gray-500 mb-2">Se guardan en este navegador (localStorage). Para centralizar, luego podemos llevarlo al backend.</p>
            <form onSubmit={(e)=>{e.preventDefault(); try { localStorage.setItem('metricas_targets', JSON.stringify({ mttr_days: Number((targets&&targets.mttr_days)||0)||null, sla_diag_pct: Number((targets&&targets.sla_diag_pct)||0)||null, t_emitir_h: Number((targets&&targets.t_emitir_h)||0)||null, t_aprobar_h: Number((targets&&targets.t_aprobar_h)||0)||null, aprob_pres_pct: Number((targets&&targets.aprob_pres_pct)||0)||null })); alert('Objetivos guardados localmente.'); } catch {} }} className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-4">
              <label className="text-sm">
                <span className="text-gray-600">MTTR objetivo (días)</span>
                <input type="number" step="0.1" className="mt-1 border rounded px-2 py-1 w-full" value={(targets&&targets.mttr_days) ?? ''} onChange={(e)=>setTargets(t=>({...(t||{}), mttr_days: e.target.value}))} />
              </label>
              <label className="text-sm">
                <span className="text-gray-600">SLA diag 24h objetivo (%)</span>
                <input type="number" step="1" className="mt-1 border rounded px-2 py-1 w-full" value={(targets&&targets.sla_diag_pct) ?? ''} onChange={(e)=>setTargets(t=>({...(t||{}), sla_diag_pct: e.target.value}))} />
              </label>
              <label className="text-sm">
                <span className="text-gray-600">Aprobación presupuestos objetivo (%)</span>
                <input type="number" step="1" className="mt-1 border rounded px-2 py-1 w-full" value={(targets&&targets.aprob_pres_pct) ?? ''} onChange={(e)=>setTargets(t=>({...(t||{}), aprob_pres_pct: e.target.value}))} />
              </label>
              <label className="text-sm">
                <span className="text-gray-600">Tiempo emitir presupuesto (h)</span>
                <input type="number" step="0.1" className="mt-1 border rounded px-2 py-1 w-full" value={(targets&&targets.t_emitir_h) ?? ''} onChange={(e)=>setTargets(t=>({...(t||{}), t_emitir_h: e.target.value}))} />
              </label>
              <label className="text-sm">
                <span className="text-gray-600">Tiempo aprobar presupuesto (h)</span>
                <input type="number" step="0.1" className="mt-1 border rounded px-2 py-1 w-full" value={(targets&&targets.t_aprobar_h) ?? ''} onChange={(e)=>setTargets(t=>({...(t||{}), t_aprobar_h: e.target.value}))} />
              </label>
              <div className="flex items-end">
                <button type="submit" className="px-3 py-1.5 border rounded bg-white hover:bg-gray-50">Guardar objetivos</button>
              </div>
            </form>
          </div>

          <div>
            <h3 className="font-semibold mb-2">Feriados locales (extras)</h3>
            <p className="text-sm text-gray-500 mb-2">Los feriados oficiales se toman automáticamente. Agregue excepciones locales que aplican a horas hábiles.</p>
            <form onSubmit={addFeriado} className="flex gap-2 items-end">
              <div>
                <div className="text-sm text-gray-600">Fecha</div>
                <input type="date" value={fecha} onChange={e=>setFecha(e.target.value)} className="mt-1 border rounded px-2 py-1" />
              </div>
              <div>
                <div className="text-sm text-gray-600">Nombre</div>
                <input type="text" value={nombre} onChange={e=>setNombre(e.target.value)} placeholder="Feriado local" className="mt-1 border rounded px-2 py-1" />
              </div>
              <button type="submit" className="px-3 py-1.5 border rounded bg-white hover:bg-gray-50">Agregar</button>
            </form>

            <div className="mt-3 border rounded bg-white overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 text-gray-600">
                    <th className="p-2 text-left">Fecha</th>
                    <th className="p-2 text-left">Nombre</th>
                    <th className="p-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {(feriados||[]).length === 0 && (
                    <tr><td colSpan={3} className="p-3 text-gray-500">Sin feriados locales</td></tr>
                  )}
                  {(feriados||[]).map((f) => (
                    <tr key={f.fecha} className="border-t">
                      <td className="p-2">{f.fecha}</td>
                      <td className="p-2">{f.nombre}</td>
                      <td className="p-2 text-right">
                        <button onClick={()=>removeFeriado(f)} className="px-2 py-1 border rounded hover:bg-gray-50">Eliminar</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
