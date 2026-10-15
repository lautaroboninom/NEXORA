import { useEffect, useState } from "react";
import { DurationInput } from "./TechnicalInterventionEditor";
import { reviewTechnicalLabor } from "../lib/api";

export default function TechnicalQuoteReview({ ingresoId, quote, editable, onUpdated, onError, onVersion, canCreateVersion }) {
  const info = quote?.technical;
  const [minutes, setMinutes] = useState(0);
  const [bonified, setBonified] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setMinutes(info?.labor_snapshot?.minutos_presupuestados ?? 0); setBonified(info?.labor_snapshot?.bonificado ?? false); }, [info]);
  if (!info) return null;
  const labor = info.labor_snapshot;
  const snapshot = info.technical_snapshot;
  return <section className="rounded-lg border border-blue-200 bg-blue-50 p-4 mb-4 space-y-3">
    <h3 className="font-semibold">Revisión de la carga técnica</h3>
    {info.requiere_actualizacion && <p role="alert" className="text-amber-900">Hay cambios técnicos pendientes de incorporar a esta versión.</p>}
    {info.submitted_revision !== info.scope_revision && <p className="text-amber-900">El técnico todavía debe confirmar el envío de la carga actualizada.</p>}
    {labor?.tarifa_pendiente && <p role="alert" className="text-red-800">Falta configurar la tarifa. Completala en «Trabajos, tarifas y solicitudes de alta» y actualizá el cálculo.</p>}
    {snapshot?.items && <div className="text-sm"><strong>Alcance técnico de esta versión</strong><ul className="list-disc ml-5 space-y-2 mt-2">{snapshot.items.map((row) => <li key={row.id}>
      <span className="font-medium">{row.tipo === "repuesto" ? `Repuesto ${row.codigo}: ` : "Trabajo: "}{row.descripcion}</span>
      <span className="block text-slate-600">Cantidad prevista: {Number(row.cantidad_prevista)} · Realizada al enviar: {Number(row.cantidad_realizada)}</span>
      {row.observaciones && <p className="whitespace-pre-wrap">{row.observaciones}</p>}
    </li>)}</ul>{snapshot.observaciones && <p className="whitespace-pre-wrap mt-3"><strong>Observaciones generales: </strong>{snapshot.observaciones}</p>}</div>}
    <p className="text-sm">Tiempo real registrado: {Math.floor(info.minutos_reales / 60)} h {info.minutos_reales % 60} min.</p>
    {labor && <form className="flex flex-wrap gap-4 items-end" onSubmit={async (e) => { e.preventDefault(); setBusy(true); try { onUpdated(await reviewTechnicalLabor(ingresoId, { minutos_presupuestados: minutes, bonificado: bonified })); } catch (err) { onError(err.message); } finally { setBusy(false); } }}>
      {labor.sin_falla ? <div><p className="font-medium">Diagnóstico y limpieza sin falla</p><p className="text-sm">Tarifa general: $ {labor.tarifa_diagnostico ?? "pendiente"}</p><label className="flex items-center gap-2 mt-2 text-sm"><input type="checkbox" disabled={!editable || busy} checked={bonified} onChange={(e) => setBonified(e.target.checked)} />Bonificar diagnóstico y limpieza</label></div>
        : <div><DurationInput label="Tiempo a presupuestar" value={minutes} onChange={setMinutes} disabled={!editable || busy} /><p className="text-xs text-slate-600 mt-2">Primera hora: $ {labor.primera_hora ?? "pendiente"} · Hora adicional: $ {labor.hora_adicional ?? "pendiente"}</p></div>}
      {editable && <button disabled={busy} className="rounded border bg-white px-3 py-2">Actualizar cálculo</button>}
    </form>}
    {canCreateVersion && !editable && ["presupuestado", "emitido", "aprobado", "rechazado"].includes(quote.estado) && <button type="button" className="rounded bg-slate-700 text-white px-3 py-2" onClick={onVersion}>Crear nueva versión para revisión</button>}
  </section>;
}
