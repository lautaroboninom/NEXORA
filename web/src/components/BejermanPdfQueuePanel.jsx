import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, HelpCircle, RefreshCw } from "lucide-react";

import { getBejermanPdfQueueStatus, postBejermanPdfQueueRetry } from "@/lib/api";

const REFRESH_MS = 15000;

function WorkerBadge({ worker }) {
  if (worker?.alive === true) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-800">
        <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
        Servicio de PDF activo
        {Number.isFinite(worker?.ageSeconds) && <span className="text-emerald-600">· hace {worker.ageSeconds}s</span>}
      </span>
    );
  }
  if (worker?.alive === false) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-red-50 px-2.5 py-1 text-xs font-medium text-red-800">
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
        Servicio de PDF caído o trabado
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-700">
      <HelpCircle className="h-3.5 w-3.5" aria-hidden="true" />
      Estado del servicio desconocido
    </span>
  );
}

/**
 * Estado de la cola de PDF de Bejerman: responde "¿está corriendo el servicio?" y
 * permite destrabar un comprobante cuyo PDF Bejerman no publicó.
 */
export default function BejermanPdfQueuePanel({ canManage = false }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [retryingKey, setRetryingKey] = useState("");

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await getBejermanPdfQueueStatus({ limit: 25 }));
      setError("");
    } catch (e) {
      setError(e?.message || "No se pudo leer el estado de la cola de PDF.");
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      load({ silent: true });
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const retry = async (row) => {
    if (!canManage || retryingKey) return;
    setRetryingKey(row.documentKey);
    setMessage("");
    setError("");
    try {
      const result = await postBejermanPdfQueueRetry(row.documentKey);
      setMessage(result?.detail || "Se pidió el PDF de nuevo.");
      await load({ silent: true });
    } catch (e) {
      setError(e?.data?.detail || e?.message || "No se pudo reintentar el PDF.");
    } finally {
      setRetryingKey("");
    }
  };

  const worker = status?.worker;
  const queue = status?.queue;
  const rows = status?.rows || [];
  const stuckRows = rows.filter((row) => row.stuck);

  return (
    <section className="mt-4 rounded border bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Cola de PDF de Bejerman</h2>
          <p className="text-sm text-gray-600">
            Los remitos se emiten en Bejerman al instante; el PDF lo prepara un servicio aparte.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <WorkerBadge worker={worker} />
          <button
            type="button"
            onClick={() => load()}
            disabled={loading}
            className="inline-flex h-9 items-center gap-2 rounded border border-gray-300 px-3 text-sm hover:bg-gray-50 disabled:opacity-60"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
            Actualizar
          </button>
        </div>
      </div>

      {worker?.alive === false && (
        <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          Nadie está procesando la cola de PDF. Los remitos igual se emiten bien en Bejerman, pero los PDF
          no se van a preparar hasta que el servicio vuelva. Revisar el contenedor <code>nexora-bejerman-pdf</code>.
        </p>
      )}

      {error && <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
      {message && <p className="mt-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{message}</p>}

      {queue && (
        <p className="mt-3 text-sm text-gray-700">
          {Object.entries(queue.counts || {}).length
            ? Object.entries(queue.counts).map(([state, total]) => `${state}: ${total}`).join(" · ")
            : "Sin solicitudes registradas."}
          {queue.stuckCount > 0 && (
            <span className="ml-2 font-medium text-amber-700">· {queue.stuckCount} trabada(s)</span>
          )}
        </p>
      )}

      {stuckRows.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b text-left text-xs uppercase text-gray-500">
                <th className="py-2 pr-3">Comprobante</th>
                <th className="py-2 pr-3">Empresa</th>
                <th className="py-2 pr-3">Estado</th>
                <th className="py-2 pr-3">Motivo</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {stuckRows.map((row) => (
                <tr key={row.documentKey} className="border-b last:border-0 align-top">
                  <td className="py-2 pr-3 font-medium text-gray-900">{row.comprobante}</td>
                  <td className="py-2 pr-3 text-gray-700">{row.companyKey}</td>
                  <td className="py-2 pr-3 text-gray-700">
                    {row.stageLabel || row.state}
                    <div className="text-xs text-gray-500">
                      ciclos: {row.regenerationCount} · hace {Math.round(row.ageSeconds / 60)} min
                    </div>
                  </td>
                  <td className="py-2 pr-3 text-gray-600">{row.lastErrorDetail || "-"}</td>
                  <td className="py-2 text-right">
                    {canManage && (
                      <button
                        type="button"
                        onClick={() => retry(row)}
                        disabled={Boolean(retryingKey)}
                        className="rounded bg-sky-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-800 disabled:opacity-60"
                      >
                        {retryingKey === row.documentKey ? "Reintentando..." : "Reintentar PDF"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
