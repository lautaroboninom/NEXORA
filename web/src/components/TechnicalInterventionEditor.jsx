import { useEffect, useState } from "react";
import { getTechnicalIntervention, saveTechnicalIntervention, submitTechnicalIntervention, getTechnicalParts, getTechnicalWorks, requestTechnicalCatalogItem } from "../lib/api";
import { hoursTextFromMinutes, minutesFromHoursText } from "../lib/labor-hours";

export const TECHNICAL_STATUS_LABELS = {
  en_preparacion: "En preparación", listo_revision: "Listo para revisar",
  alta_pendiente: "Alta de catálogo pendiente", ampliacion_pendiente: "Ampliación pendiente", historico: "Carga anterior",
};

function prepareForm(value) {
  return { ...value, horas_mano_obra: hoursTextFromMinutes(value.minutos_estimados), items: value.items.map((item) => {
    const quantity = Math.max(Number(item.cantidad_prevista), Number(item.cantidad_realizada));
    return { ...item, cantidad_prevista: quantity, realizado: quantity > 0 && Number(item.cantidad_realizada) >= quantity };
  }) };
}

export function LaborHoursInput({ value, onChange, disabled, label = "Mano de obra (horas)" }) {
  return <label className="block text-sm font-medium">{label}
    <input aria-label={label} type="text" inputMode="decimal" className="block border rounded p-2 w-40 mt-1 font-normal" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} placeholder="Ej.: 1,5" />
  </label>;
}

export default function TechnicalInterventionEditor({ ingresoId, disabled = false, equipmentHint, onSaved, onDirtyChange, refreshKey }) {
  const [form, setForm] = useState(null);
  const [works, setWorks] = useState([]);
  const [parts, setParts] = useState([]);
  const [search, setSearch] = useState("");
  const [selectedWork, setSelectedWork] = useState("");
  const [selectedPart, setSelectedPart] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [request, setRequest] = useState({ tipo: "repuesto", nombre: "", cantidad: 1, observaciones: "" });
  const [showRequest, setShowRequest] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([getTechnicalIntervention(ingresoId), getTechnicalWorks()])
      .then(([value, catalog]) => { if (alive) { setForm(prepareForm(value)); setWorks(catalog); setDirty(false); } })
      .catch((e) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [ingresoId, refreshKey]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!dirty) return undefined;
    const listener = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", listener);
    return () => window.removeEventListener("beforeunload", listener);
  }, [dirty]);
  useEffect(() => {
    let alive = true;
    const timer = setTimeout(() => getTechnicalParts(ingresoId, search, equipmentHint)
      .then((rows) => { if (alive) setParts(rows); })
      .catch((e) => { if (alive) setError(e.message); }), 250);
    return () => { alive = false; clearTimeout(timer); };
  }, [ingresoId, search, equipmentHint]);

  function change(patch) { setForm((old) => ({ ...old, ...patch })); setDirty(true); setNotice(""); }
  function changeItem(index, patch) { change({ items: form.items.map((item, i) => i === index ? { ...item, ...patch } : item) }); }
  function changeQuantity(index, value) {
    const item = form.items[index];
    changeItem(index, { cantidad_prevista: value,
      realizado: item.realizado || (Number(value) > 0 && Number(item.cantidad_realizada) >= Number(value)) });
  }
  function add(kind, row) {
    if (!row) return;
    const existing = form.items.findIndex((item) => item.tipo === kind && Number(item[kind === "trabajo" ? "trabajo_id" : "repuesto_id"]) === Number(row.id));
    if (existing >= 0) { changeItem(existing, { cantidad_prevista: Number(form.items[existing].cantidad_prevista) + 1 }); return; }
    change({ items: [...form.items, { tipo: kind, trabajo_id: kind === "trabajo" ? row.id : null, repuesto_id: kind === "repuesto" ? row.id : null,
      descripcion: row.nombre, codigo: row.codigo, cantidad_prevista: 1, cantidad_realizada: 0, realizado: false, observaciones: "" }] });
  }
  async function persist() {
    const minutes = minutesFromHoursText(form.horas_mano_obra);
    if (minutes === null) throw new Error("Ingresá la mano de obra en horas, por ejemplo 1,5.");
    const payload = { revision: form.revision, diagnostico: form.diagnostico, minutos_estimados: minutes,
      sin_falla: form.sin_falla, observaciones: form.observaciones,
      items: form.items.map(({ id, tipo, trabajo_id, repuesto_id, cantidad_prevista, cantidad_realizada, realizado, observaciones }) =>
        ({ id, tipo, trabajo_id, repuesto_id, cantidad_prevista,
          cantidad_realizada: realizado ? cantidad_prevista : Math.min(Number(cantidad_realizada), Number(cantidad_prevista)), observaciones })) };
    const value = await saveTechnicalIntervention(ingresoId, payload);
    setForm(prepareForm(value)); setDirty(false); await onSaved?.(); return value;
  }
  async function run(action) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (e) { setError(`${e.message}${e.status === 409 ? " Guardá una copia de tus cambios y recargá la ficha para comparar." : ""}`); }
    finally { setBusy(false); }
  }

  if (!form) return <div className="border rounded p-4 mt-4" role="status">{error || "Cargando intervención técnica..."}</div>;
  const locked = disabled || busy;
  const alreadySent = !dirty && form.submitted_revision != null && form.submitted_revision === form.scope_revision;
  const selectedPartInfo = parts.find((row) => String(row.id) === String(selectedPart));
  return <section className="min-w-0 max-w-full border rounded-lg p-4 mt-4 space-y-4" aria-label="Intervención técnica">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="font-semibold text-lg">Diagnóstico, trabajos y repuestos</h2>
      <span className="text-sm rounded bg-slate-100 px-3 py-1">{dirty ? "Cambios sin guardar" : TECHNICAL_STATUS_LABELS[form.estado_revision]}</span>
    </div>
    {error && <div role="alert" className="rounded bg-red-50 p-3 text-red-800">{error}</div>}
    {notice && <div role="status" className="rounded bg-green-50 p-3 text-green-800">{notice}</div>}
    <label className="block font-medium">Diagnóstico
      <textarea className="block w-full border rounded p-2 min-h-[140px] mt-1 font-normal" value={form.diagnostico} disabled={locked} onChange={(e) => change({ diagnostico: e.target.value })} placeholder="Describí la falla encontrada y el diagnóstico técnico." />
    </label>
    {form.trabajos_historicos && <details className="rounded bg-slate-50 p-3"><summary className="cursor-pointer text-sm font-medium">Trabajos registrados anteriormente</summary><p className="whitespace-pre-wrap text-sm mt-2">{form.trabajos_historicos}</p></details>}
    <div className="grid gap-4 md:grid-cols-2">
      <div><label htmlFor={`work-${ingresoId}`} className="block text-sm font-medium mb-1">Trabajo del catálogo</label>
        <div className="flex gap-2"><select id={`work-${ingresoId}`} className="border rounded p-2 w-full min-w-0" disabled={locked} value={selectedWork} onChange={(e) => setSelectedWork(e.target.value)}>
          <option value="">Seleccionar trabajo</option>{works.map((row) => <option key={row.id} value={row.id}>{row.nombre}</option>)}
        </select><button type="button" className="border rounded px-3" disabled={locked || !selectedWork} onClick={() => { add("trabajo", works.find((row) => Number(row.id) === Number(selectedWork))); setSelectedWork(""); }}>Agregar</button></div>
      </div>
      <div><label htmlFor={`part-search-${ingresoId}`} className="block text-sm font-medium mb-1">Repuesto del catálogo</label>
        <input id={`part-search-${ingresoId}`} className="border rounded p-2 w-full mb-2" value={search} disabled={locked} onChange={(e) => { setSearch(e.target.value); setSelectedPart(""); setParts([]); }} placeholder="Buscar por código o nombre" />
        <div className="flex gap-2"><select aria-label="Repuesto encontrado" className="border rounded p-2 w-full min-w-0" disabled={locked} value={selectedPart} onChange={(e) => setSelectedPart(e.target.value)}>
          <option value="">Seleccionar repuesto</option>{parts.map((row) => <option key={row.id} value={row.id}>{row.codigo} · {row.nombre}{row.equipos_asociados?.length ? ` — Equipos: ${row.equipos_asociados.join("; ")}` : ""}</option>)}
        </select><button type="button" className="border rounded px-3" disabled={locked || !selectedPart} onClick={() => { add("repuesto", parts.find((row) => Number(row.id) === Number(selectedPart))); setSelectedPart(""); }}>Agregar</button></div>
        {selectedPartInfo && <div className="mt-2 text-sm text-slate-600" role="status" aria-label="Equipos asociados al repuesto seleccionado">
          {selectedPartInfo.equipos_asociados?.length ? <><p className="font-medium">Equipos asociados</p><ul className="list-disc ml-5 break-words">{selectedPartInfo.equipos_asociados.map((equipment, index) => <li key={`${equipment}-${index}`}>{equipment}</li>)}</ul></> : <p>Sin equipos asociados configurados.</p>}
        </div>}
      </div>
    </div>
    <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="bg-slate-50 text-left"><th className="p-2">Trabajo o repuesto</th><th className="p-2">Cantidad</th><th className="p-2">Observaciones</th><th className="p-2"><span className="sr-only">Acciones</span></th></tr></thead>
      <tbody>{form.items.map((item, index) => <tr className="border-b" key={item.id || `new-${index}`}>
        <td className="p-2 min-w-44"><span className="text-xs text-slate-500 block">{item.tipo === "repuesto" ? item.codigo : "Trabajo"}</span>{item.descripcion}
          <label className="flex items-center gap-2 mt-2 text-sm text-slate-600"><input aria-label={`${item.tipo === "repuesto" ? "Instalado" : "Realizado"}: ${item.descripcion}`} type="checkbox" disabled={locked} checked={item.realizado} onChange={(e) => changeItem(index, { realizado: e.target.checked, cantidad_realizada: 0 })} />{item.tipo === "repuesto" ? "Instalado" : "Realizado"}</label>
          {!item.realizado && Number(item.cantidad_realizada) > 0 && <span className="block mt-1 text-xs text-slate-500">En curso</span>}
        </td>
        <td className="p-2"><input aria-label={`Cantidad: ${item.descripcion}`} className="border rounded p-2 w-24" disabled={locked} type="number" min="0.01" step="0.01" value={item.cantidad_prevista} onChange={(e) => changeQuantity(index, e.target.value)} /></td>
        <td className="p-2"><input aria-label={`Observaciones: ${item.descripcion}`} className="border rounded p-2 min-w-40 w-full" disabled={locked} value={item.observaciones} onChange={(e) => changeItem(index, { observaciones: e.target.value })} /></td>
        <td className="p-2"><button type="button" className="text-red-700 underline" disabled={locked} onClick={() => change({ items: form.items.filter((_, i) => i !== index) })}>Quitar</button></td>
      </tr>)}</tbody></table>{!form.items.length && <p className="text-sm text-slate-500 py-3">Agregá los trabajos previstos y las piezas que requiere la reparación.</p>}</div>
    <LaborHoursInput value={form.horas_mano_obra} disabled={locked} onChange={(value) => change({ horas_mano_obra: value })} />
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={locked} checked={form.sin_falla} onChange={(e) => change({ sin_falla: e.target.checked })} />No se encontró falla</label>
    <label className="block text-sm font-medium">Observaciones generales<textarea className="block mt-1 w-full border rounded p-2 font-normal" disabled={locked} value={form.observaciones} onChange={(e) => change({ observaciones: e.target.value })} /></label>
    <div className="flex flex-wrap gap-2 items-center">
      <button type="button" disabled={locked || !dirty} className="border rounded px-4 py-2 disabled:opacity-50" onClick={() => run(async () => { await persist(); setNotice("Cambios guardados."); })}>Guardar cambios</button>
      <button type="button" disabled={locked || alreadySent} className="rounded bg-blue-700 text-white px-4 py-2 disabled:bg-slate-200 disabled:text-slate-500 disabled:cursor-not-allowed" onClick={() => run(async () => { const saved = dirty || !form.revision ? await persist() : form; const sent = await submitTechnicalIntervention(ingresoId, saved.revision); setForm(prepareForm(sent)); await onSaved?.(); setNotice("Carga enviada para revisión del presupuesto."); })}>{busy ? "Guardando..." : alreadySent ? "Enviado a presupuestar" : "Enviar a presupuestar"}</button>
      <button type="button" disabled={locked} className="text-blue-700 underline p-2" onClick={() => setShowRequest((value) => !value)}>Solicitar alta de trabajo o repuesto</button>
    </div>
    {showRequest && <form className="rounded bg-slate-50 border p-3 grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); run(async () => { if (dirty || !form.revision) await persist(); await requestTechnicalCatalogItem({ ...request, ingreso_id: ingresoId }); setForm(prepareForm(await getTechnicalIntervention(ingresoId))); setRequest({ tipo: "repuesto", nombre: "", cantidad: 1, observaciones: "" }); setShowRequest(false); setNotice("Solicitud de alta enviada a administración."); }); }}>
      <label className="text-sm">Tipo<select className="block border rounded p-2 w-full" disabled={locked} value={request.tipo} onChange={(e) => setRequest({ ...request, tipo: e.target.value })}><option value="repuesto">Repuesto</option><option value="trabajo">Trabajo</option></select></label>
      <label className="text-sm">Cantidad<input className="block border rounded p-2 w-full" disabled={locked} type="number" min="0.01" step="0.01" value={request.cantidad} onChange={(e) => setRequest({ ...request, cantidad: e.target.value })} /></label>
      <label className="text-sm">Nombre solicitado<input required className="block border rounded p-2 w-full" disabled={locked} value={request.nombre} onChange={(e) => setRequest({ ...request, nombre: e.target.value })} /></label>
      <label className="text-sm">Descripción técnica<input className="block border rounded p-2 w-full" disabled={locked} value={request.observaciones} onChange={(e) => setRequest({ ...request, observaciones: e.target.value })} /></label>
      <button type="submit" className="rounded bg-slate-700 text-white px-4 py-2" disabled={locked}>Enviar solicitud</button>
    </form>}
    {form.solicitudes.length > 0 && <div className="text-sm space-y-2"><h3 className="font-medium">Solicitudes de catálogo</h3>{form.solicitudes.map((row) => <p key={row.id}>{row.nombre} · {row.estado}{row.respuesta ? ` · ${row.respuesta}` : ""}</p>)}</div>}
  </section>;
}
