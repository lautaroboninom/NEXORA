import { useEffect, useState } from "react";
import { LaborHoursInput } from "./TechnicalInterventionEditor";
import { reviewTechnicalLabor } from "../lib/api";
import { hoursTextFromMinutes, minutesFromHoursText } from "../lib/labor-hours";

export default function TechnicalQuoteReview({ ingresoId, quote, editable, onUpdated, onError, onVersion, canCreateVersion }) {
  const info = quote?.technical;
  const [hours, setHours] = useState("0");
  const [bonified, setBonified] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setHours(hoursTextFromMinutes(info?.labor_snapshot?.minutos_presupuestados ?? 0)); setBonified(info?.labor_snapshot?.bonificado ?? false); }, [info]);
  if (!info) return null;
  const labor = info.labor_snapshot;
  const snapshot = info.technical_snapshot;
  async function updateLabor(event) {
    event.preventDefault();
    const minutes = minutesFromHoursText(hours);
    if (!labor.sin_falla && minutes === null) {
      onError("Ingresá la mano de obra en horas, por ejemplo 1,5.");
      return;
    }
    setBusy(true);
    try {
      onUpdated(await reviewTechnicalLabor(ingresoId, {
        minutos_presupuestados: minutes ?? labor.minutos_presupuestados,
        bonificado: bonified,
      }));
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }
  return <section className="rounded-lg border border-blue-200 bg-blue-50 p-4 mb-4 space-y-3">
    <h3 className="font-semibold">Revisión de la carga técnica</h3>
    {info.requiere_actualizacion && <p role="alert" className="text-amber-900">Hay cambios técnicos pendientes de incorporar a esta versión.</p>}
    {info.submitted_revision !== info.scope_revision && <p className="text-amber-900">El técnico todavía debe confirmar el envío de la carga actualizada.</p>}
    {labor?.tarifa_pendiente && <p role="alert" className="text-red-800">Falta configurar la tarifa. Completala en «Trabajos, tarifas y solicitudes de alta» y actualizá el cálculo.</p>}
    {snapshot?.items && <div className="text-sm"><strong>Alcance técnico de esta versión</strong><ul className="list-disc ml-5 space-y-2 mt-2">{snapshot.items.map((row) => <li key={row.id}>
      <span className="font-medium">{row.tipo === "repuesto" ? `Repuesto ${row.codigo}: ` : "Trabajo: "}{row.descripcion}</span>
      <span className="block text-slate-600">Cantidad: {Math.max(Number(row.cantidad_prevista), Number(row.cantidad_realizada))} · {Number(row.cantidad_realizada) >= Number(row.cantidad_prevista) ? (row.tipo === "repuesto" ? "Instalado" : "Realizado") : Number(row.cantidad_realizada) > 0 ? "En curso" : "Pendiente"}</span>
      {row.observaciones && <p className="whitespace-pre-wrap">{row.observaciones}</p>}
    </li>)}</ul>{snapshot.observaciones && <p className="whitespace-pre-wrap mt-3"><strong>Observaciones generales: </strong>{snapshot.observaciones}</p>}</div>}
    {labor && <form className="flex flex-wrap gap-4 items-end" onSubmit={updateLabor}>
      {labor.sin_falla ? <div><p className="font-medium">Diagnóstico y limpieza sin falla</p><p className="text-sm">Tarifa general: $ {labor.tarifa_diagnostico ?? "pendiente"}</p><label className="flex items-center gap-2 mt-2 text-sm"><input type="checkbox" disabled={!editable || busy} checked={bonified} onChange={(e) => setBonified(e.target.checked)} />Bonificar diagnóstico y limpieza</label></div>
        : <div><LaborHoursInput value={hours} onChange={setHours} disabled={!editable || busy} /><p className="text-xs text-slate-600 mt-2">Primera hora: $ {labor.primera_hora ?? "pendiente"} · De la 2.ª a la 6.ª: $ {labor.hora_adicional ?? "pendiente"} · Desde la 7.ª: $ {labor.hora_desde_septima ?? labor.hora_adicional ?? "pendiente"}{labor.marca_tarifa ? ` · Tarifa por marca: ${labor.marca_tarifa}` : ""}</p></div>}
      {editable && <button disabled={busy} className="rounded border bg-white px-3 py-2">Actualizar cálculo</button>}
    </form>}
    {canCreateVersion && !editable && ["presupuestado", "emitido", "aprobado", "rechazado"].includes(quote.estado) && <button type="button" className="rounded bg-slate-700 text-white px-3 py-2" onClick={onVersion}>Crear nueva versión para revisión</button>}
  </section>;
}
